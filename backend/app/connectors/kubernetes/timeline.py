"""Kubernetes timeline collector, shared by every cluster type (real, demo, future adapters).

Turns raw objects into timestamped events across layers:
  change      rollouts with the exact pod-template diff between revisions
  infra       node conditions (DiskPressure, MemoryPressure, NotReady) and node events
  k8s         events, container exits / OOM kills (first failure estimated from restarts + back-off),
              scheduling failures
  app         error lines and stack traces from (previous) container logs
  network     Services losing ready endpoints (time derived from pod readiness transitions)
  dependency  the same, for datastores (postgres, redis, kafka …)
and relations: Deployment->Pod (owns), Service->Pod (routes), Ingress->Service, Pod->Node (runs_on, one way),
Pod->PVC (mounts), Pod->Service (calls, from env vars / DSNs that name a Service).
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from app.timeline.logparse import find_errors, normalise
from app.timeline.model import Relation, TimelineBatch, TimelineEvent, TimelineQuery, entity_id

from . import model as m

if TYPE_CHECKING:
    from .base import KubernetesConnector

DATASTORE_PORTS = {5432, 3306, 6379, 27017, 9042, 5672, 9092, 4222, 11211, 1433, 1521, 26257, 2181, 9200, 2379}
DATASTORE_NAME = re.compile(r"postgres|pgsql|mysql|maria|redis|valkey|mongo|kafka|rabbit|nats|elastic|opensearch|"
                            r"cassandra|memcache|etcd|zookeeper|minio|\bdb\b|-db$|database", re.I)
HOST_TOKEN = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*")

CRITICAL_REASONS = {"Evicted", "FailedScheduling", "BackOff", "OOMKilling", "OOMKilled", "Failed", "FailedMount",
                    "FailedAttachVolume", "FailedCreatePodSandBox", "ErrImagePull", "ImagePullBackOff", "FailedCreate",
                    "ProgressDeadlineExceeded", "CrashLoopBackOff", "FailedKillPod", "NetworkNotReady"}
NODE_CRITICAL = {"NodeNotReady", "EvictionThresholdMet", "NodeHasDiskPressure", "NodeHasInsufficientMemory",
                 "SystemOOM", "KernelOops", "Rebooted", "FreeDiskSpaceFailed", "ImageGCFailed", "NodeHasInsufficientPID"}
CONDITION_SEVERITY = {"DiskPressure": "critical", "MemoryPressure": "critical", "PIDPressure": "warning",
                      "NetworkUnavailable": "critical"}


def _ts(value: Any) -> datetime | None:
    return m.parse_ts(value)


def _first_sentence(text: str, limit: int = 140) -> str:
    text = " ".join((text or "").split())
    cut = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    return cut[:limit] + ("…" if len(cut) > limit else "")


def _backoff_seconds(restarts: int) -> int:
    """Approximate time CrashLoopBackOff spends between the first and the latest crash (10s doubling, 5m cap)."""
    return sum(min(10 * 2 ** i, 300) for i in range(max(restarts - 1, 0)))


def template_diff(old: dict, new: dict) -> list[str]:
    """Human-readable differences between two pod templates (images, env, resources, probes, commands)."""
    def containers(t):
        return {c.get("name"): c for c in ((t or {}).get("spec") or {}).get("containers") or []}

    def env(c):
        out = {}
        for e in c.get("env") or []:
            if "value" in e:
                out[e["name"]] = e.get("value")
            elif e.get("valueFrom"):
                src = e["valueFrom"]
                ref = src.get("secretKeyRef") or src.get("configMapKeyRef") or src.get("fieldRef") or {}
                out[e["name"]] = f"<from {next(iter(src))} {ref.get('name', ref.get('fieldPath', ''))}>"
        return out

    lines: list[str] = []
    a, b = containers(old), containers(new)
    for name in sorted(set(a) | set(b)):
        if name not in a:
            lines.append(f"+ container {name} ({b[name].get('image')})")
            continue
        if name not in b:
            lines.append(f"- container {name}")
            continue
        ca, cb = a[name], b[name]
        prefix = f"[{name}] " if len(b) > 1 else ""
        if ca.get("image") != cb.get("image"):
            lines.append(f"{prefix}image: {ca.get('image')} → {cb.get('image')}")
        ea, eb = env(ca), env(cb)
        for k in sorted(set(ea) | set(eb)):
            if k not in ea:
                lines.append(f'{prefix}+ env {k}="{eb[k]}"')
            elif k not in eb:
                lines.append(f"{prefix}- env {k}")
            elif ea[k] != eb[k]:
                lines.append(f'{prefix}env {k}: "{ea[k]}" → "{eb[k]}"')
        for field in ("resources", "command", "args", "readinessProbe", "livenessProbe", "ports"):
            if ca.get(field) != cb.get(field):
                lines.append(f"{prefix}{field}: {ca.get(field)} → {cb.get(field)}")
    va = {v.get("name") for v in ((old or {}).get("spec") or {}).get("volumes") or []}
    vb = {v.get("name") for v in ((new or {}).get("spec") or {}).get("volumes") or []}
    lines += [f"+ volume {v}" for v in sorted(vb - va)] + [f"- volume {v}" for v in sorted(va - vb)]
    return lines


def _service_refs(pod: dict, services: dict[tuple[str, str], dict]) -> set[tuple[str, str]]:
    """Services a pod talks to, inferred from env values such as DB_HOST=postgres or http://api.payments.svc:8080."""
    ns = m.ns_of(pod)
    refs: set[tuple[str, str]] = set()
    for c in (pod.get("spec") or {}).get("containers") or []:
        for e in c.get("env") or []:
            val = str(e.get("value") or "").lower()
            if not val:
                continue
            for token in HOST_TOKEN.findall(val):
                parts = token.split(".")
                name = parts[0]
                target_ns = parts[1] if len(parts) > 1 and parts[1] not in ("svc", "cluster") else ns
                if (target_ns, name) in services:
                    refs.add((target_ns, name))
    return refs


def _is_datastore(svc: dict) -> bool:
    ports = {int(p.get("port") or 0) for p in (svc.get("spec") or {}).get("ports") or []}
    return bool(ports & DATASTORE_PORTS) or bool(DATASTORE_NAME.search(m.name_of(svc)))


def _pod_ready_info(pod: dict) -> tuple[bool, datetime | None]:
    for c in (pod.get("status") or {}).get("conditions") or []:
        if c.get("type") == "Ready":
            return c.get("status") == "True", _ts(c.get("lastTransitionTime"))
    return False, None


async def collect(k: KubernetesConnector, q: TimelineQuery) -> TimelineBatch:
    kinds = ["Pod", "Service", "ReplicaSet", "Deployment", "StatefulSet", "Event", "Node", "Ingress"]
    namespaces = q.namespaces or [None]
    tasks, labels = [], []
    for kind in kinds:
        for ns in ([None] if kind == "Node" else namespaces):
            tasks.append(k.list_objects(kind, ns))
            labels.append(kind)
    results = await asyncio.gather(*tasks, return_exceptions=True)
    objs: dict[str, list[dict]] = {kind: [] for kind in kinds}
    batch = TimelineBatch()
    for kind, res in zip(labels, results):
        if isinstance(res, Exception):
            batch.errors.append(f"{k.name}: listing {kind} failed: {res}")
        else:
            objs[kind].extend(res)

    src = f"Kubernetes · {k.name}"
    now = datetime.now(timezone.utc)
    ev_out = batch.events
    rel = batch.relations

    def add(**kw) -> TimelineEvent:
        e = TimelineEvent(integration=k.name, **kw)
        ev_out.append(e)
        return e

    pods = objs["Pod"]
    services = {(m.ns_of(s), m.name_of(s)): s for s in objs["Service"]}

    # ---------------------------------------------------------------- relations
    rs_owner: dict[tuple[str | None, str], str] = {}
    for rs in objs["ReplicaSet"]:
        for ref in m.meta(rs).get("ownerReferences") or []:
            if ref.get("kind") == "Deployment":
                rs_owner[(m.ns_of(rs), m.name_of(rs))] = ref.get("name")
    pod_workload: dict[str, str] = {}
    for p in pods:
        ns, name = m.ns_of(p), m.name_of(p)
        pid = entity_id("Pod", ns, name)
        for ref in m.meta(p).get("ownerReferences") or []:
            okind, oname = ref.get("kind"), ref.get("name")
            if okind == "ReplicaSet" and (ns, oname) in rs_owner:
                okind, oname = "Deployment", rs_owner[(ns, oname)]
            wid = entity_id(okind, ns, oname)
            pod_workload[pid] = wid
            rel.append(Relation(source=wid, target=pid, type="owns"))
        node = (p.get("spec") or {}).get("nodeName")
        if node:
            rel.append(Relation(source=pid, target=entity_id("Node", None, node), type="runs_on", directed=True))
        for vol in (p.get("spec") or {}).get("volumes") or []:
            claim = (vol.get("persistentVolumeClaim") or {}).get("claimName")
            if claim:
                rel.append(Relation(source=pid, target=entity_id("PersistentVolumeClaim", ns, claim), type="mounts"))
        for tns, tname in _service_refs(p, services):
            rel.append(Relation(source=pid, target=entity_id("Service", tns, tname), type="calls"))
    def group(pid: str) -> dict:
        """Pods of one workload fail together: report them once, against the workload."""
        wid = pod_workload.get(pid)
        return {"group": wid} if wid else {}

    def who(pid: str, pname: str) -> str:
        wid = pod_workload.get(pid)
        return wid.split("/", 2)[2] if wid else pname

    selector_pods: dict[tuple[str | None, str], list[dict]] = {}
    for (ns, name), svc in services.items():
        sel = (svc.get("spec") or {}).get("selector")
        matched = [p for p in pods if m.ns_of(p) == ns and sel and m._selector_matches(sel, m.meta(p).get("labels"))]
        selector_pods[(ns, name)] = matched
        for p in matched:
            rel.append(Relation(source=entity_id("Service", ns, name), target=entity_id("Pod", ns, m.name_of(p)),
                                type="routes"))
    for ing in objs["Ingress"]:
        ns = m.ns_of(ing)
        spec = ing.get("spec") or {}
        backends = {((spec.get("defaultBackend") or {}).get("service") or {}).get("name")}
        for rule in spec.get("rules") or []:
            for path in (rule.get("http") or {}).get("paths") or []:
                backends.add(((path.get("backend") or {}).get("service") or {}).get("name"))
        for svc in filter(None, backends):
            rel.append(Relation(source=entity_id("Ingress", ns, m.name_of(ing)), target=entity_id("Service", ns, svc),
                                type="routes"))

    # ---------------------------------------------------------------- change: rollouts with diffs
    by_deploy: dict[tuple[str | None, str], list[dict]] = {}
    for rs in objs["ReplicaSet"]:
        key = (m.ns_of(rs), rs_owner.get((m.ns_of(rs), m.name_of(rs)), ""))
        if key[1]:
            by_deploy.setdefault(key, []).append(rs)
    for (ns, dname), rss in by_deploy.items():
        def rev(r):
            return int((m.meta(r).get("annotations") or {}).get("deployment.kubernetes.io/revision", 0) or 0)
        rss.sort(key=rev)
        for prev, cur in zip(rss, rss[1:]):
            created = _ts(m.meta(cur).get("creationTimestamp"))
            if not created or created < q.start - timedelta(minutes=45) or created > q.end:
                continue
            diff = template_diff((prev.get("spec") or {}).get("template") or {},
                                 (cur.get("spec") or {}).get("template") or {})
            cause = (m.meta(cur).get("annotations") or {}).get("kubernetes.io/change-cause")
            title = f"Deployment {dname} rolled out revision {rev(cur)}" + (f" — {cause}" if cause else "")
            add(ts=created, layer="change", severity="info", title=title, entity=entity_id("Deployment", ns, dname),
                source=f"{src} · rollout history", kind="rollout",
                evidence=(f"revision {rev(prev)} → {rev(cur)}  (ReplicaSet {m.name_of(cur)})\n"
                          + ("\n".join(diff) if diff else "pod template unchanged (re-rollout)")),
                attrs={"diff": diff, "revision": rev(cur), "norm": f"rev{rev(cur)}"})

    # ---------------------------------------------------------------- events
    for ev in objs["Event"]:
        inv = ev.get("involvedObject") or ev.get("regarding") or {}
        kind, reason, etype = inv.get("kind") or "", ev.get("reason") or "", ev.get("type") or "Normal"
        msg = ev.get("message") or ev.get("note") or ""
        first = _ts(ev.get("firstTimestamp")) or _ts(ev.get("eventTime")) or _ts(ev.get("lastTimestamp")) \
            or _ts(m.meta(ev).get("creationTimestamp"))
        last = _ts(ev.get("lastTimestamp")) or first
        if not first:
            continue
        ns = inv.get("namespace") or m.ns_of(ev)
        host = (ev.get("source") or {}).get("host") or ev.get("reportingInstance")
        if kind == "Pod" and host and inv.get("name"):
            # the kubelet that reported it: links an evicted/killed pod to its former node even after it is gone
            rel.append(Relation(source=entity_id("Pod", ns, inv.get("name")), target=entity_id("Node", None, host),
                                type="ran_on", directed=True))
        if kind == "Node":
            if etype != "Warning" and reason not in NODE_CRITICAL:
                continue
            layer, sev = "infra", "critical" if reason in NODE_CRITICAL else "warning"
            ent = entity_id("Node", None, inv.get("name"))
        elif reason in CRITICAL_REASONS:
            layer, sev, ent = "k8s", "critical", entity_id(kind, ns, inv.get("name"))
        elif etype == "Warning":
            layer, sev, ent = "k8s", "warning", entity_id(kind, ns, inv.get("name"))
        else:
            continue
        oname = inv.get("name") or ""
        display = who(ent, oname) if kind == "Pod" else oname
        add(ts=first, last_ts=last, layer=layer, severity=sev,
            title=f"{display} · {reason}: {_first_sentence(msg.replace(oname, display))}", entity=ent,
            source=f"{src} · events", kind=f"event:{reason}",
            count=int(ev.get("count") or (ev.get("series") or {}).get("count") or 1),
            evidence=f"{etype}  {reason}  {kind.lower()}/{oname}\n{msg}",
            attrs={"norm": f"{reason}|{normalise(msg.replace(oname, '<obj>'))[:80]}",
                   **(group(ent) if kind == "Pod" else {})})

    # ---------------------------------------------------------------- infra: node conditions
    for node in objs["Node"]:
        nname = m.name_of(node)
        for cond in (node.get("status") or {}).get("conditions") or []:
            ctype, status = cond.get("type"), cond.get("status")
            since = _ts(cond.get("lastTransitionTime"))
            bad = (ctype == "Ready" and status != "True") or (ctype in CONDITION_SEVERITY and status == "True")
            if not bad:
                continue
            sev = "critical" if ctype == "Ready" else CONDITION_SEVERITY[ctype]
            label = "NotReady" if ctype == "Ready" else f"{ctype}=True"
            add(ts=since or now, estimated=since is None, layer="infra", severity=sev,
                title=f"{nname} {label}" + (f": {_first_sentence(cond.get('message', ''))}" if cond.get("message") else ""),
                entity=entity_id("Node", None, nname), source=f"{src} · node conditions", kind=f"node:{ctype}",
                evidence=f"node {nname}\ncondition {ctype}={status}  reason={cond.get('reason')}\n"
                         f"since {cond.get('lastTransitionTime')}\n{cond.get('message', '')}",
                attrs={"ongoing": True, "norm": ctype})

    # ---------------------------------------------------------------- k8s: pod and container failures
    log_targets: list[tuple[int, dict, str, int, datetime | None]] = []
    focus = set(q.focus)
    for p in pods:
        ns, pname = m.ns_of(p), m.name_of(p)
        pid = entity_id("Pod", ns, pname)
        status = p.get("status") or {}
        start = _ts(status.get("startTime")) or _ts(m.meta(p).get("creationTimestamp"))
        if status.get("reason") == "Evicted":
            add(ts=_ts(m.meta(p).get("deletionTimestamp")) or start or now, estimated=True, layer="k8s",
                severity="critical", title=f"{pname} · Evicted: {_first_sentence(status.get('message', ''))}",
                entity=pid, source=f"{src} · pod status", kind="event:Evicted", evidence=status.get("message", ""),
                attrs={"norm": f"Evicted|{normalise(status.get('message', ''))[:80]}"})
        for cond in status.get("conditions") or []:
            if cond.get("type") == "PodScheduled" and cond.get("status") == "False":
                add(ts=_ts(cond.get("lastTransitionTime")) or start or now, layer="k8s", severity="critical",
                    title=f"{pname} unschedulable: {_first_sentence(cond.get('message', ''))}", entity=pid,
                    source=f"{src} · pod conditions", kind="unschedulable", evidence=cond.get("message", ""),
                    attrs={"ongoing": True, "norm": "unschedulable"})
        worst_restart = 0
        first_fail: datetime | None = None
        for cs in status.get("containerStatuses") or []:
            cname, rc = cs.get("name"), int(cs.get("restartCount") or 0)
            state, last = cs.get("state") or {}, cs.get("lastState") or {}
            waiting = (state.get("waiting") or {}).get("reason")
            term = last.get("terminated") or {}
            cur_term = state.get("terminated") or {}
            if cur_term and cur_term.get("reason") not in (None, "Completed"):
                term, rc = cur_term, max(rc, 1)
            if term and (rc > 0 or cur_term):
                finished = _ts(term.get("finishedAt")) or now
                est = max(finished - timedelta(seconds=_backoff_seconds(rc)), start) if start else finished
                ready, ready_since = _pod_ready_info(p)
                if start and not ready and ready_since:
                    # never became ready -> failing since it started; otherwise since it lost readiness
                    est = start if (ready_since - start) < timedelta(minutes=2) else min(ready_since, finished)
                code, reason = term.get("exitCode"), term.get("reason") or "Error"
                sev = "critical" if reason == "OOMKilled" or (code not in (0, None)) else "warning"
                tail = " → CrashLoopBackOff" if waiting == "CrashLoopBackOff" else ""
                title = (f"{who(pid, pname)}: container {cname} OOMKilled (memory limit reached){tail}"
                         if reason == "OOMKilled" else
                         f"{who(pid, pname)}: container {cname} exited ({reason}, code {code}){tail}")
                add(ts=est, last_ts=finished, estimated=rc > 1, layer="k8s", severity=sev, title=title, entity=pid,
                    source=f"{src} · container status", kind="container_exit", count=max(rc, 1),
                    evidence=(f"container {cname}: lastState.terminated reason={reason} exitCode={code}\n"
                              f"finishedAt={term.get('finishedAt')}  restartCount={rc}"
                              + (f"\nstate.waiting={waiting}" if waiting else "")
                              + (f"\nfirst failure estimated from {rc} restarts and back-off timing" if rc > 1 else "")),
                    attrs={"norm": f"exit|{cname}|{reason}", "pods": [pname], **group(pid)})
                worst_restart = max(worst_restart, rc)
                first_fail = est if first_fail is None else min(first_fail, est)
            elif waiting in m.BAD_WAITING:
                ready, since = _pod_ready_info(p)
                msg = (state.get("waiting") or {}).get("message") or ""
                add(ts=since or start or now, estimated=since is None, layer="k8s", severity="critical",
                    title=f"{who(pid, pname)}: container {cname} {waiting}"
                          + (f" — {_first_sentence(msg.replace(pname, who(pid, pname)), 120)}" if msg else ""),
                    entity=pid, source=f"{src} · container status", kind=f"waiting:{waiting}",
                    evidence=f"{pname} · container {cname}: state.waiting reason={waiting}\n{msg}",
                    attrs={"ongoing": True, "norm": waiting, "pods": [pname], **group(pid)})
        if worst_restart > 0 or any(((cs.get("state") or {}).get("waiting") or {}).get("reason") in m.BAD_WAITING
                                    for cs in status.get("containerStatuses") or []):
            prio = 0 if (pid in focus or pod_workload.get(pid) in focus) else 1
            log_targets.append((prio, p, pid, worst_restart, first_fail))

    # ---------------------------------------------------------------- app: logs + stack traces
    log_targets.sort(key=lambda t: (t[0], -t[3]))
    async def read_logs(p: dict, pid: str, restarts: int, first_fail: datetime | None) -> None:
        ns, pname = m.ns_of(p), m.name_of(p)
        containers = [c.get("name") for c in (p.get("spec") or {}).get("containers") or []]
        for cname in containers[:2]:
            text, previous = "", restarts > 0
            try:
                text = await k.pod_logs(pname, ns, cname if len(containers) > 1 else None, 300, previous)
            except Exception:  # noqa: BLE001 - previous logs may be gone; fall back to current
                previous = False
                try:
                    text = await k.pod_logs(pname, ns, cname if len(containers) > 1 else None, 300, False)
                except Exception as exc:  # noqa: BLE001
                    batch.errors.append(f"{k.name}: logs of {pname} unavailable: {exc}")
                    continue
            for f in find_errors(text, default_ts=None):
                seen_ts = f.first_ts or now
                repeating = previous and restarts > 1 and first_fail is not None
                add(ts=first_fail if repeating else seen_ts, last_ts=f.last_ts or seen_ts, estimated=repeating,
                    layer="app", severity="critical" if f.level == "fatal" else "warning",
                    title=f"{who(pid, pname)}: {_first_sentence(f.message, 150)}", entity=pid,
                    source=f"{src} · {'previous' if previous else 'current'} container logs ({cname})",
                    kind="log_error", count=max(f.count, restarts if repeating else 1),
                    evidence=(f"{pname} · container {cname} · {'previous instance' if previous else 'current'}"
                              + (f" · repeats on every restart (latest {seen_ts:%H:%M:%S})" if repeating else "")
                              + "\n" + f.evidence),
                    attrs={"norm": normalise(f.message), "pods": [pname], **group(pid)})

    await asyncio.gather(*(read_logs(p, pid, rc, ff) for _, p, pid, rc, ff in log_targets[: q.max_log_pods]))

    # ---------------------------------------------------------------- network / dependency: endpoints
    for (ns, sname), matched in selector_pods.items():
        svc = services[(ns, sname)]
        if not (svc.get("spec") or {}).get("selector"):
            continue
        layer = "dependency" if _is_datastore(svc) else "network"
        sid = entity_id("Service", ns, sname)
        if not matched:
            add(ts=now, estimated=True, layer=layer, severity="warning",
                title=f"Service {sname} selector matches no pods", entity=sid, source=f"{src} · service selector",
                kind="selector_empty", evidence=f"selector {(svc.get('spec') or {}).get('selector')} matches 0 pods "
                                                f"in namespace {ns}", attrs={"ongoing": True, "norm": "selector_empty"})
            continue
        infos = [(m.name_of(p), *_pod_ready_info(p)) for p in matched]
        ready = [i for i in infos if i[1]]
        if ready and len(ready) == len(infos):
            continue
        times = [t for _, ok, t in infos if not ok and t]
        when = max(times) if (not ready and times) else (min(times) if times else None)
        add(ts=when or now, estimated=when is None, layer=layer, severity="critical" if not ready else "warning",
            title=(f"Service {sname} has no ready endpoints (0/{len(infos)})" if not ready
                   else f"Service {sname}: {len(ready)}/{len(infos)} endpoints ready"),
            entity=sid, source=f"{src} · endpoints (from pod readiness)", kind="endpoints",
            evidence="\n".join(f"{n}  ready={ok}  since {t:%H:%M:%S}" if t else f"{n}  ready={ok}" for n, ok, t in infos),
            attrs={"ongoing": True, "norm": "endpoints_empty" if not ready else "endpoints_partial"})
    return batch
