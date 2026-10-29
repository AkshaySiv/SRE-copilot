"""Shared helpers for monitoring timeline collectors: label -> resource mapping, onset detection, log grouping."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.timeline.logparse import find_errors, normalise
from app.timeline.model import TimelineEvent, entity_id

_DEPLOY_POD = re.compile(r"^(?P<w>.+)-[a-z0-9]{6,10}-[a-z0-9]{5}$")
_STS_POD = re.compile(r"^(?P<w>.+)-\d+$")


class TimelineSignal(BaseModel):
    """A metric that marks an event on the timeline when it crosses a threshold."""

    name: str
    query: str = Field(description="PromQL returning one series per resource")
    op: Literal[">", "<"] = ">"
    threshold: float
    layer: Literal["infra", "k8s", "dependency", "app", "network", "impact"]
    severity: Literal["warning", "critical"] = "critical"
    title: str = Field(description="Placeholders: {name} (resource) and {value}")
    unit: Literal["ratio", "number"] = "ratio"


DEFAULT_SIGNALS: list[TimelineSignal] = [
    TimelineSignal(
        name="http_5xx_ratio", layer="impact", threshold=0.05, unit="ratio",
        title="{name}: 5xx error rate rose to {value}",
        query='sum by (destination_workload, destination_workload_namespace) (rate(istio_requests_total{response_code=~"5.."}[2m]))'
              ' / sum by (destination_workload, destination_workload_namespace) (rate(istio_requests_total[2m]))'),
    TimelineSignal(
        name="node_disk_used", layer="infra", threshold=0.85, unit="ratio", title="{name}: disk usage reached {value}",
        query='max by (instance, node, nodename) (1 - node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|squashfs|ramfs"}'
              ' / node_filesystem_size_bytes{fstype!~"tmpfs|overlay|squashfs|ramfs"})'),
    TimelineSignal(
        name="node_memory_used", layer="infra", threshold=0.92, severity="warning", unit="ratio",
        title="{name}: memory usage reached {value}",
        query="max by (instance, node, nodename) (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)"),
    TimelineSignal(
        name="container_memory_near_limit", layer="k8s", threshold=0.9, severity="warning", unit="ratio",
        title="{name}: memory at {value} of its limit",
        query='max by (namespace, pod) (container_memory_working_set_bytes{container!=""}'
              ' / on (namespace, pod, container) kube_pod_container_resource_limits{resource="memory"})'),
]


def workload_of_pod(namespace: str | None, pod: str) -> str | None:
    """Best-effort Deployment/StatefulSet id from a pod name (for sources that only know the pod)."""
    mt = _DEPLOY_POD.match(pod)
    if mt:
        return entity_id("Deployment", namespace, mt.group("w"))
    mt = _STS_POD.match(pod)
    if mt:
        return entity_id("StatefulSet", namespace, mt.group("w"))
    return None


def entity_from_labels(lbl: dict[str, Any]) -> str:
    ns = lbl.get("namespace") or lbl.get("destination_workload_namespace") or lbl.get("exported_namespace")
    for key, kind in (("destination_workload", "Deployment"), ("deployment", "Deployment"),
                      ("statefulset", "StatefulSet"), ("daemonset", "DaemonSet"), ("pod", "Pod"),
                      ("service", "Service"), ("persistentvolumeclaim", "PersistentVolumeClaim")):
        if lbl.get(key) and (ns or kind == "Node"):
            return entity_id(kind, ns, lbl[key])
    node = lbl.get("node") or lbl.get("nodename") or lbl.get("kubernetes_node")
    if not node and lbl.get("instance") and not ns:
        node = str(lbl["instance"]).rsplit(":", 1)[0]
    if node:
        return entity_id("Node", None, node)
    if ns:
        return entity_id("Namespace", None, ns)
    return entity_id("Cluster", None, lbl.get("cluster", "cluster"))


def short_name(entity: str) -> str:
    return entity.split("/", 2)[-1]


def fmt_value(v: float, unit: str) -> str:
    return f"{v * 100:.0f}%" if unit == "ratio" else f"{v:g}"


def onset_events(sig: TimelineSignal, series: list[dict], integration: str, source: str) -> list[TimelineEvent]:
    """First moment each series crossed the threshold (with the value just before, for context)."""
    out = []
    for s in series[:50]:
        values = [(float(ts), float(v)) for ts, v in s.get("values", []) if v not in ("NaN", "+Inf", "-Inf")]
        crossed = (lambda x: x > sig.threshold) if sig.op == ">" else (lambda x: x < sig.threshold)
        idx = next((i for i, (_, v) in enumerate(values) if crossed(v)), None)
        if idx is None:
            continue
        ts, val = values[idx]
        before = values[idx - 1][1] if idx > 0 else None
        peak = max(v for _, v in values[idx:]) if sig.op == ">" else min(v for _, v in values[idx:])
        ent = entity_from_labels(s.get("metric", {}))
        name = short_name(ent)
        title = sig.title.format(name=name, value=fmt_value(val, sig.unit))
        if before is not None:
            title += f" (from {fmt_value(before, sig.unit)})"
        else:
            title += " (already above at window start)"
        out.append(TimelineEvent(
            ts=datetime.fromtimestamp(ts, tz=timezone.utc), layer=sig.layer, severity=sig.severity, title=title,
            entity=ent, source=source, integration=integration, kind=f"signal:{sig.name}",
            evidence=(f"{sig.query}\n\nthreshold {sig.op} {fmt_value(sig.threshold, sig.unit)}\n"
                      f"before onset  {fmt_value(before, sig.unit) if before is not None else 'n/a'}\n"
                      f"at onset      {fmt_value(val, sig.unit)}  ({datetime.fromtimestamp(ts, tz=timezone.utc):%H:%M:%S})\n"
                      f"peak          {fmt_value(peak, sig.unit)}"),
            attrs={"norm": sig.name, "ongoing": before is None}))
    return out


def alert_event(name: str, labels: dict[str, Any], since: datetime, summary: str, integration: str,
                source: str) -> TimelineEvent:
    ent = entity_from_labels(labels)
    sev = "critical" if str(labels.get("severity", "")).lower() in ("critical", "page", "high") else "warning"
    layer = "infra" if ent.startswith("Node/") else "impact"
    return TimelineEvent(
        ts=since, layer=layer, severity=sev, title=f"Alert {name} fired ({labels.get('severity', 'n/a')})",
        entity=ent, source=source, integration=integration, kind=f"alert:{name}",
        evidence="\n".join([f"{name}  since {since:%H:%M:%S}", summary or ""] +
                           [f"{k}={v}" for k, v in sorted(labels.items()) if k not in ("alertname", "__name__")]),
        attrs={"norm": name, "group": workload_group(ent)} if workload_group(ent) else {"norm": name})


def workload_group(entity: str) -> str | None:
    kind, ns, name = (entity.split("/", 2) + ["", ""])[:3]
    if kind == "Pod":
        return workload_of_pod(None if ns == "_" else ns, name)
    return None


def log_events(lines: list[tuple[datetime, str]], namespace: str | None, pod: str | None, integration: str,
               source: str) -> list[TimelineEvent]:
    """Error findings from centralised logs, keyed so they merge with the Kubernetes collector's findings."""
    text = "\n".join(f"{ts.isoformat()} {line}" for ts, line in sorted(lines))
    out = []
    pod_ent = entity_id("Pod", namespace, pod) if pod and namespace else entity_id("Namespace", None, namespace or "unknown")
    wid = workload_of_pod(namespace, pod) if pod else None
    display = (wid.split("/", 2)[2] if wid else pod) or (namespace or "logs")
    for f in find_errors(text):
        attrs: dict[str, Any] = {"norm": normalise(f.message), "pods": [pod] if pod else []}
        if wid:
            attrs["group"] = wid
        out.append(TimelineEvent(
            ts=f.first_ts or datetime.now(timezone.utc), last_ts=f.last_ts, layer="app",
            severity="critical" if f.level == "fatal" else "warning", title=f"{display}: {f.message[:150]}",
            entity=pod_ent, source=source, integration=integration, kind="log_error", count=f.count,
            evidence=f"{pod or namespace} · centralised logs\n{f.evidence}", attrs=attrs))
    return out
