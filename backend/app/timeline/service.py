"""Time-based investigation pipeline: scope -> collect (all integrations, in parallel) -> correlate."""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from app.plugins import Plugin, PluginKind

from .correlate import correlate
from .model import Timeline, TimelineBatch, TimelineQuery, entity_id

log = logging.getLogger(__name__)

_KIND_ALIASES = {"pod": "Pod", "po": "Pod", "deployment": "Deployment", "deploy": "Deployment",
                 "statefulset": "StatefulSet", "sts": "StatefulSet", "daemonset": "DaemonSet", "ds": "DaemonSet",
                 "service": "Service", "svc": "Service", "node": "Node", "job": "Job", "ingress": "Ingress"}
_REF = re.compile(r"\b(pod|po|deployment|deploy|statefulset|sts|daemonset|ds|service|svc|node|job|ingress)?\s*"
                  r"([a-z0-9][a-z0-9-]{0,62})/([a-z0-9][a-z0-9.-]{0,252})\b", re.I)
_HEALTH = {"critical": 0, "warning": 1}
_KIND_PREF = {"Deployment": 0, "StatefulSet": 0, "DaemonSet": 0, "Job": 1, "Pod": 2, "PersistentVolumeClaim": 3, "Node": 4}


async def resolve_focus(k8s: list[Plugin], prompt: str, scope: dict[str, Any]) -> list[str]:
    """Which resources is the user asking about? Explicit scope > references in the prompt > what is failing."""
    res = scope.get("resource") if isinstance(scope.get("resource"), dict) else None
    if res and res.get("name"):
        kind = _KIND_ALIASES.get(str(res.get("kind", "")).lower(), res.get("kind") or "Pod")
        return [entity_id(kind, None if kind == "Node" else scope.get("namespace"), res["name"])]
    focus: list[str] = []
    for match in _REF.finditer(prompt or ""):
        kind_word, ns, name = match.groups()
        kind = _KIND_ALIASES.get((kind_word or "").lower())
        for plugin in k8s:
            for candidate in ([kind] if kind else ["Pod", "Deployment", "StatefulSet", "Service"]):
                try:
                    if await plugin.get_object(candidate, name, ns):
                        focus.append(entity_id(candidate, ns, name))
                        break
                except Exception:  # noqa: BLE001 - not found / not allowed
                    continue
            if focus:
                break
    if focus:
        return focus
    namespace = scope.get("namespace")
    words = set(re.findall(r"[a-z0-9][a-z0-9-]{3,}", (prompt or "").lower()))
    if not namespace:
        for plugin in k8s:
            try:
                names = {(n.get("metadata") or {}).get("name") for n in await plugin.list_objects("Namespace")}
            except Exception:  # noqa: BLE001
                continue
            hit = sorted(words & {n for n in names if n})
            if hit:
                namespace = hit[0]
                break
    for plugin in k8s:
        try:
            for kind in ("Deployment", "StatefulSet", "DaemonSet"):
                for obj in await plugin.list_objects(kind, namespace):
                    name = (obj.get("metadata") or {}).get("name", "")
                    if name.lower() in words or any(len(w) > 4 and w in name.lower().split("-") for w in words):
                        focus.append(entity_id(kind, (obj.get("metadata") or {}).get("namespace"), name))
        except Exception:  # noqa: BLE001
            continue
    unhealthy: list[dict] = []
    for plugin in k8s:
        try:
            unhealthy += await plugin.find_unhealthy(namespace)
        except Exception:  # noqa: BLE001
            continue
    unhealthy = [u for u in unhealthy if u.get("name")]
    if focus:
        failing = {entity_id(u["kind"], u.get("namespace"), u["name"]) for u in unhealthy}
        hits = [f for f in focus if f in failing]
        return hits or focus[:3]
    # Nothing named: focus on the single most severe failing workload (the user can narrow with a namespace).
    unhealthy = [u for u in unhealthy if u.get("kind") != "Node"] or unhealthy
    unhealthy.sort(key=lambda u: (_HEALTH.get(u.get("health"), 2), _KIND_PREF.get(u.get("kind"), 5)))
    return [entity_id(u["kind"], u.get("namespace"), u["name"]) for u in unhealthy[:1]]


async def build_timeline(plugins: list[Plugin], prompt: str, scope: dict[str, Any], *, lookback_minutes: int = 60,
                         timeout: int = 60, max_log_pods: int = 6,
                         progress: Callable[[str, dict], Awaitable[Any]] | None = None) -> Timeline:
    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=lookback_minutes)
    k8s = [p for p in plugins if p.kind == PluginKind.KUBERNETES]
    focus = await resolve_focus(k8s, prompt, scope)
    if progress:
        await progress("status", {"phase": "scoped", "focus": focus})
    query = TimelineQuery(start=start, end=now, focus=focus, max_log_pods=max_log_pods)

    async def one(p: Plugin) -> TimelineBatch:
        try:
            return await asyncio.wait_for(p.timeline(query), timeout=timeout)
        except asyncio.TimeoutError:
            return TimelineBatch(errors=[f"{p.name}: timed out after {timeout}s"])
        except Exception as exc:  # noqa: BLE001 - one broken integration must not stop the investigation
            log.exception("Timeline collection failed for %s", p.name)
            return TimelineBatch(errors=[f"{p.name}: {type(exc).__name__}: {exc}"])

    started = asyncio.get_running_loop().time()
    batches = await asyncio.gather(*(one(p) for p in plugins))
    events = [e for b in batches for e in b.events]
    relations = [r for b in batches for r in b.relations]
    errors = [e for b in batches for e in b.errors]
    tl = correlate(events, relations, focus, start, now, errors)
    tl.stats["collect_seconds"] = round(asyncio.get_running_loop().time() - started, 1)
    tl.stats["integrations_queried"] = [p.name for p in plugins]
    return tl
