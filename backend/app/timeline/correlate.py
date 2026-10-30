"""Deterministic correlation: merge duplicates, find what is connected to the problem, pick the origin and
build the causal chain. No LLM involved - the same evidence always yields the same story; the model reviews it.

Heuristics (kept simple and explainable):
  * connected  = resources reachable from the focus through dependency relations (pod -> node is one-way,
                 so a shared node does not connect every workload in the cluster)
  * symptom    = first critical event on the focus itself (else anywhere connected)
  * origin     = the latest change shortly before the symptom; otherwise the earliest critical event on a
                 connected resource in the 15 minutes before the symptom
  * chain      = origin + the first critical event of every other layer after it, in time order
                 (infra joins the chain only as its origin: an app failure does not fill a disk)
                 (simultaneous events ordered by causal rank: change > infra > dependency > app > k8s > network > impact)
  * unrelated  = events on resources not connected to the focus
"""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta

from .model import CAUSAL_RANK, LAYER_INFO, Relation, Timeline, TimelineEvent

SEV = {"info": 0, "warning": 1, "critical": 2}
CHANGE_LOOKBACK = timedelta(minutes=45)
ORIGIN_LOOKBACK = timedelta(minutes=15)
SIMULTANEOUS = timedelta(seconds=2)


def _key(e: TimelineEvent) -> str:
    owner = e.attrs.get("group") or e.entity
    return f"{e.layer}|{owner}|{e.kind}|{e.attrs.get('norm') or e.title[:80]}"


def merge(events: list[TimelineEvent]) -> list[TimelineEvent]:
    """Collapse the same thing reported repeatedly or by several integrations (e.g. pod logs and Loki)."""
    out: dict[str, TimelineEvent] = {}
    for e in events:
        k = _key(e)
        cur = out.get(k)
        if cur is None:
            out[k] = e.model_copy(deep=True)
            continue
        if e.ts < cur.ts:
            cur.ts, cur.estimated = e.ts, e.estimated
        ends = [t for t in (cur.last_ts, e.last_ts, e.ts) if t]
        cur.last_ts = max(ends) if ends else cur.last_ts
        cur.count = max(cur.count, e.count)
        if SEV[e.severity] > SEV[cur.severity]:
            cur.severity = e.severity
        if len(e.evidence) > len(cur.evidence):
            cur.evidence = e.evidence
        if e.integration and e.integration not in cur.source:
            cur.source = f"{cur.source} + {e.source}"
        if e.entity != cur.entity and e.attrs.get("group"):  # same failure on several pods of one workload
            pods = list(dict.fromkeys((cur.attrs.get("pods") or []) + (e.attrs.get("pods") or [])))
            cur.attrs["pods"] = pods
            cur.entity = e.attrs["group"]
            cur.evidence = f"affects {len(pods)} pods: {', '.join(pods)}\n" + cur.evidence.split("\n", 1)[-1] \
                if cur.evidence.startswith("affects ") else f"affects {len(pods)} pods: {', '.join(pods)}\n{cur.evidence}"
    return list(out.values())


def _sort_key(e: TimelineEvent):
    return (e.ts, CAUSAL_RANK.get(e.layer, 9))


def connected_entities(relations: list[Relation], focus: list[str], max_depth: int = 6) -> set[str]:
    adj: dict[str, set[str]] = defaultdict(set)
    for r in relations:
        adj[r.source].add(r.target)
        if not r.directed:
            adj[r.target].add(r.source)
    seen = set(focus)
    queue = deque((f, 0) for f in focus)
    while queue:
        node, depth = queue.popleft()
        if depth >= max_depth:
            continue
        for nxt in adj.get(node, ()):
            if nxt not in seen:
                seen.add(nxt)
                queue.append((nxt, depth + 1))
    return seen


def _namespace(entity: str) -> str | None:
    parts = entity.split("/", 2)
    return parts[1] if len(parts) == 3 and parts[1] != "_" else None


def _is_connected(entity: str, connected: set[str], namespaces: set[str]) -> bool:
    if entity in connected:
        return True
    kind, _, rest = entity.partition("/")
    if kind == "Namespace":  # alerts that only carry a namespace label
        return rest.split("/")[-1] in namespaces
    return False


def correlate(events: list[TimelineEvent], relations: list[Relation], focus: list[str],
              window_start: datetime, window_end: datetime, errors: list[str] | None = None) -> Timeline:
    raw_count = sum(max(e.count, 1) for e in events)
    merged = [e for e in merge(events)
              if e.ts <= window_end and (e.ts >= window_start or (e.last_ts and e.last_ts >= window_start)
                                         or e.attrs.get("ongoing"))]
    for e in merged:
        e.ensure_id()
    merged.sort(key=_sort_key)

    if not focus:  # fall back to whatever is failing
        focus = list(dict.fromkeys(e.entity for e in merged if e.severity == "critical"))[:5]
    connected = connected_entities(relations, focus)
    connected_ns = {ns for ns in (_namespace(x) for x in connected) if ns}
    is_conn = {e.id: _is_connected(e.entity, connected, connected_ns)
               or bool(e.attrs.get("group") and e.attrs["group"] in connected) for e in merged}

    anomalies = [e for e in merged if e.severity != "info"]
    conn_crit = [e for e in merged if is_conn[e.id] and e.severity == "critical"]
    focus_set = set(focus)
    near_focus = connected_entities(relations, focus, max_depth=2)
    # The symptom is a workload-level failure; a node problem is a candidate cause, never the symptom itself.
    workload_crit = [e for e in conn_crit if e.layer not in ("infra", "change")]
    owner = lambda e: e.attrs.get("group") or e.entity  # noqa: E731
    symptom = next((e for e in workload_crit if e.entity in focus_set or owner(e) in focus_set), None) \
        or next((e for e in workload_crit if e.entity in near_focus or owner(e) in near_focus), None) \
        or (workload_crit[0] if workload_crit else None) \
        or (conn_crit[0] if conn_crit else None) \
        or next((e for e in anomalies if is_conn[e.id]), None)

    origin: TimelineEvent | None = None
    if symptom is not None:
        changes = [e for e in merged if e.layer == "change" and is_conn[e.id]
                   and symptom.ts - CHANGE_LOOKBACK <= e.ts <= symptom.ts + timedelta(seconds=60)]
        if changes:
            origin = changes[-1]
        else:
            candidates = [e for e in conn_crit if symptom.ts - ORIGIN_LOOKBACK <= e.ts <= symptom.ts]
            origin = min(candidates, key=_sort_key) if candidates else symptom

    chain: list[TimelineEvent] = []
    if origin is not None:
        chain.append(origin)
        seen_layers = {origin.layer}
        for e in conn_crit:
            if e.layer in seen_layers or e.id == origin.id or e.ts < origin.ts - SIMULTANEOUS:
                continue
            if e.layer == "infra" and origin.layer != "infra":
                continue  # node/OS problems cause application failures, not the other way round
            chain.append(e)
            seen_layers.add(e.layer)
        chain = [chain[0]] + sorted(chain[1:], key=_sort_key)
        chain = chain[:7]

    unrelated = [e.id for e in merged if not is_conn[e.id]]
    by_layer: dict[str, int] = defaultdict(int)
    for e in merged:
        by_layer[e.layer] += 1
    stats = {
        "events": len(merged), "raw_signals": raw_count, "anomalies": len(anomalies),
        "layers_with_events": len(by_layer), "by_layer": dict(by_layer),
        "integrations": sorted({e.integration for e in merged if e.integration}),
        "first_symptom": symptom.id if symptom else None,
    }
    if origin is not None and symptom is not None:
        stats["origin_to_symptom_seconds"] = int((symptom.ts - origin.ts).total_seconds())
        impact = next((e for e in chain if e.layer == "impact"), None)
        if impact is not None:
            stats["origin_to_impact_seconds"] = int((impact.ts - origin.ts).total_seconds())

    return Timeline(window_start=window_start, window_end=window_end, focus=focus, events=merged,
                    origin=origin.id if origin else None, chain=[e.id for e in chain], unrelated=unrelated,
                    connected_entities=sorted(connected), stats=stats, errors=errors or [])


def summarize(tl: Timeline) -> dict[str, str | None]:
    """Deterministic one-paragraph verdict, used when no LLM is available to review the timeline."""
    by = tl.by_id()
    chain = [by[i] for i in tl.chain if i in by]
    origin = by.get(tl.origin) if tl.origin else None
    if not origin:
        crit = [e for e in tl.events if e.severity == "critical" and e.id not in set(tl.unrelated)]
        if not crit:
            return {"summary": "No failures were found in the collected window.", "root_cause": None,
                    "severity": "info"}
        first = min(crit, key=lambda e: e.ts)
        return {"summary": f"No clear origin could be determined. The earliest failure is {first.title} "
                           f"({first.entity}, {first.ts:%H:%M:%S} UTC).", "root_cause": None, "severity": "high"}
    name = LAYER_INFO[origin.layer]["name"]
    parts = [f"Started on the {name} layer at {origin.ts:%H:%M:%S} UTC: {origin.title}."]
    steps = [e for e in chain if e.id != origin.id]
    if steps:
        parts.append("Then: " + " → ".join(e.title for e in steps[:6]) + ".")
    severity = "critical" if any(e.severity == "critical" for e in chain) else "high"
    return {"summary": " ".join(parts), "root_cause": origin.title, "severity": severity}


def to_text(tl: Timeline, max_lines: int = 70) -> str:
    """Compact text form for the model (and the 'Copy as text' button)."""
    chain_pos = {eid: i + 1 for i, eid in enumerate(tl.chain)}
    unrelated = set(tl.unrelated)
    rows = [e for e in tl.events if e.severity != "info" or e.layer == "change" or e.id in chain_pos]
    if len(rows) > max_lines:
        rows = [e for e in rows if e.id in chain_pos or e.severity == "critical"][:max_lines]
    lines = [f"Window {tl.window_start:%Y-%m-%d %H:%M:%S} -> {tl.window_end:%H:%M:%S} UTC · "
             f"{tl.stats.get('events', 0)} events from {tl.stats.get('raw_signals', 0)} raw signals"]
    for e in rows:
        tag = " ORIGIN" if e.id == tl.origin else (f" CHAIN#{chain_pos[e.id]}" if e.id in chain_pos else "")
        tag += " UNRELATED" if e.id in unrelated else ""
        rep = f" (x{e.count}{', until ' + e.last_ts.strftime('%H:%M:%S') if e.last_ts and e.count > 1 else ''})" \
            if e.count > 1 else ""
        est = " ~estimated" if e.estimated else ""
        lines.append(f"[{e.id}] {e.ts:%H:%M:%S}{est} {e.layer.upper():<10} {e.severity:<8} {e.entity}: "
                     f"{e.title}{rep}{tag}")
    by = tl.by_id()
    if tl.chain:
        lines.append("PROPOSED CAUSAL CHAIN: " + " -> ".join(f"{by[i].title} [{i}]" for i in tl.chain if i in by))
    return "\n".join(lines)
