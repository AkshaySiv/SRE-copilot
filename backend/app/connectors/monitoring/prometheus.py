"""Prometheus-compatible metrics (Prometheus, Thanos, Mimir, VictoriaMetrics, Cortex) + optional Alertmanager."""
from __future__ import annotations

import asyncio
import statistics
from typing import Any

from pydantic import BaseModel, Field

from app.plugins import EmptyArgs, HealthStatus, ToolSpec, register_plugin

from app.timeline.logparse import parse_ts
from app.timeline.model import TimelineBatch, TimelineQuery

from .base import HttpAuthConfig, MonitoringPlugin, auto_step, window
from .timeline_support import DEFAULT_SIGNALS, TimelineSignal, alert_event, onset_events

# Query templates are configuration, not code: override or extend them per integration to match your
# exporters / recording rules. Placeholders: {namespace}, {pod}, {workload}, {service}, {instance}.
DEFAULT_TEMPLATES: dict[str, str] = {
    "pod_restarts": 'sum by (pod) (increase(kube_pod_container_status_restarts_total{{namespace="{namespace}", pod=~"{pod}"}}[1h]))',
    "pod_cpu_cores": 'sum by (pod) (rate(container_cpu_usage_seconds_total{{namespace="{namespace}", pod=~"{pod}", container!=""}}[5m]))',
    "pod_memory_bytes": 'sum by (pod) (container_memory_working_set_bytes{{namespace="{namespace}", pod=~"{pod}", container!=""}})',
    "pod_memory_limit_ratio": 'max by (pod) (container_memory_working_set_bytes{{namespace="{namespace}", pod=~"{pod}", container!=""}} '
                              '/ on (namespace, pod, container) kube_pod_container_resource_limits{{resource="memory"}})',
    "cpu_throttling_ratio": 'sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{{namespace="{namespace}", pod=~"{pod}"}}[5m])) '
                            '/ sum by (pod) (rate(container_cpu_cfs_periods_total{{namespace="{namespace}", pod=~"{pod}"}}[5m]))',
    "oom_kills": 'sum by (pod) (increase(kube_pod_container_status_last_terminated_reason{{namespace="{namespace}", reason="OOMKilled"}}[1h]))',
    "http_error_rate_istio": 'sum by (destination_workload) (rate(istio_requests_total{{destination_workload_namespace="{namespace}", response_code=~"5.."}}[5m])) '
                             '/ sum by (destination_workload) (rate(istio_requests_total{{destination_workload_namespace="{namespace}"}}[5m]))',
    "http_p99_latency_istio": 'histogram_quantile(0.99, sum by (le, destination_workload) (rate(istio_request_duration_milliseconds_bucket{{destination_workload_namespace="{namespace}"}}[5m])))',
    "node_cpu_utilisation": '1 - avg by (instance) (rate(node_cpu_seconds_total{{mode="idle"}}[5m]))',
    "node_memory_available_ratio": 'node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes',
    "node_disk_free_ratio": 'node_filesystem_avail_bytes{{fstype!~"tmpfs|overlay"}} / node_filesystem_size_bytes{{fstype!~"tmpfs|overlay"}}',
    "pvc_usage_ratio": 'kubelet_volume_stats_used_bytes{{namespace="{namespace}"}} / kubelet_volume_stats_capacity_bytes{{namespace="{namespace}"}}',
    "targets_down": 'up == 0',
}


class PrometheusConfig(HttpAuthConfig):
    url: str = "http://prometheus-operated.monitoring.svc:9090"
    alertmanager_url: str | None = Field(None, description="If set, alerts are read from Alertmanager (silences aware)")
    query_templates: dict[str, str] = Field(default_factory=dict, description="Extra/overriding PromQL templates")
    max_series: int = Field(30, description="Cap on series returned to the LLM per query")
    timeline_alerts: bool = Field(True, description="Put firing alerts on the investigation timeline")
    timeline_signals: list[TimelineSignal] = Field(
        default_factory=lambda: [s.model_copy() for s in DEFAULT_SIGNALS],
        description="Metrics that mark a timeline event when they cross a threshold (edit to match your exporters)")


class InstantArgs(BaseModel):
    query: str = Field(description="PromQL expression")


class RangeArgs(BaseModel):
    query: str
    minutes: int = Field(60, ge=5, le=60 * 24 * 7, description="Look-back window")


class TemplateArgs(BaseModel):
    template: str = Field(description="Template id from list_query_templates")
    namespace: str = ".*"
    pod: str = Field(".*", description="Pod regex, e.g. payments-api-.*")
    workload: str = ".*"
    service: str = ".*"
    instance: str = ".*"
    minutes: int | None = Field(None, description="If set, returns a range summary instead of an instant value")


class MetricSearchArgs(BaseModel):
    contains: str = Field(description="Substring of the metric name, e.g. http_request")


def _summarize_series(values: list[list[Any]]) -> dict:
    nums = [float(v[1]) for v in values if v[1] not in ("NaN", "+Inf", "-Inf")]
    if not nums:
        return {"points": 0}
    third = max(1, len(nums) // 3)
    return {"points": len(nums), "first": round(nums[0], 4), "last": round(nums[-1], 4), "min": round(min(nums), 4),
            "max": round(max(nums), 4), "avg": round(statistics.fmean(nums), 4),
            "trend": "rising" if statistics.fmean(nums[-third:]) > statistics.fmean(nums[:third]) * 1.2 else
            "falling" if statistics.fmean(nums[-third:]) < statistics.fmean(nums[:third]) * 0.8 else "flat"}


@register_plugin
class Prometheus(MonitoringPlugin):
    type = "prometheus"
    display_name = "Prometheus / Thanos / Mimir"
    description = "PromQL metrics and firing alerts."
    capabilities = frozenset({"metrics", "alerts"})
    Config = PrometheusConfig

    @property
    def templates(self) -> dict[str, str]:
        return {**DEFAULT_TEMPLATES, **self.config.query_templates}

    async def instant(self, query: str) -> Any:
        data = await self._get_json("/api/v1/query", {"query": query})
        result = data["data"]["result"]
        rows = [{"labels": r.get("metric", {}), "value": r.get("value", [None, None])[1]} for r in result]
        return {"series": len(rows), "result": rows[: self.config.max_series]} if rows else "no data (empty result)"

    async def range(self, query: str, minutes: int = 60) -> Any:
        start, end = window(minutes)
        data = await self._get_json("/api/v1/query_range", {"query": query, "start": start.timestamp(),
                                                             "end": end.timestamp(), "step": auto_step(minutes)})
        result = data["data"]["result"]
        rows = [{"labels": r.get("metric", {}), **_summarize_series(r.get("values", []))} for r in result]
        return {"series": len(rows), "window_minutes": minutes, "result": rows[: self.config.max_series]} if rows \
            else "no data (empty result)"

    async def alerts(self) -> list[dict]:
        if self.config.alertmanager_url:
            resp = await self.client().get(self.config.alertmanager_url.rstrip("/") + "/api/v2/alerts",
                                           params={"active": "true", "silenced": "false", "inhibited": "false"})
            resp.raise_for_status()
            return [{"name": a["labels"].get("alertname"), "severity": a["labels"].get("severity"),
                     "labels": a["labels"], "summary": a.get("annotations", {}).get("summary") or
                     a.get("annotations", {}).get("description"), "since": a.get("startsAt")} for a in resp.json()]
        data = await self._get_json("/api/v1/alerts")
        return [{"name": a["labels"].get("alertname"), "severity": a["labels"].get("severity"), "state": a.get("state"),
                 "labels": a["labels"], "summary": a.get("annotations", {}).get("summary") or
                 a.get("annotations", {}).get("description"), "since": a.get("activeAt")}
                for a in data["data"]["alerts"] if a.get("state") == "firing"]

    async def health(self) -> HealthStatus:
        try:
            data = await self._get_json("/api/v1/status/buildinfo")
            return HealthStatus(True, f"Prometheus API {data['data'].get('version', '?')}")
        except Exception:  # noqa: BLE001 - some compatible backends lack buildinfo
            try:
                await self._get_json("/api/v1/query", {"query": "1"})
                return HealthStatus(True, "Prometheus-compatible query API reachable")
            except Exception as exc:  # noqa: BLE001
                return HealthStatus(False, f"{type(exc).__name__}: {exc}")

    async def timeline(self, query: TimelineQuery) -> TimelineBatch:
        batch = TimelineBatch()
        start, end = query.start, query.end
        minutes = max(int((end - start).total_seconds() // 60), 5)
        step = auto_step(minutes, points=120)

        async def signal(sig: TimelineSignal):
            data = await self._get_json("/api/v1/query_range", {"query": sig.query, "start": start.timestamp(),
                                                                 "end": end.timestamp(), "step": step})
            return onset_events(sig, data["data"]["result"], self.name, f"Prometheus · {self.name} · {sig.name}")

        tasks = [signal(s) for s in self.config.timeline_signals]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for sig, res in zip(self.config.timeline_signals, results):
            if isinstance(res, Exception):
                batch.errors.append(f"{self.name}: signal {sig.name} failed: {res}")
            else:
                batch.events.extend(res)
        if self.config.timeline_alerts:
            try:
                for a in await self.alerts():
                    since = parse_ts(a.get("since"))
                    if since and since <= end:
                        batch.events.append(alert_event(a.get("name") or "alert", a.get("labels") or {}, since,
                                                        a.get("summary") or "", self.name, f"Alerts · {self.name}"))
            except Exception as exc:  # noqa: BLE001
                batch.errors.append(f"{self.name}: alerts unavailable: {exc}")
        return batch

    def tools(self) -> list[ToolSpec]:
        async def get_alerts():
            alerts = await self.alerts()
            return alerts or "no firing alerts"

        async def query_metrics(query: str):
            return await self.instant(query)

        async def query_metrics_range(query: str, minutes: int = 60):
            return await self.range(query, minutes)

        async def list_query_templates():
            return self.templates

        async def run_query_template(template: str, namespace: str = ".*", pod: str = ".*", workload: str = ".*",
                                     service: str = ".*", instance: str = ".*", minutes: int | None = None):
            if template not in self.templates:
                return f"unknown template; available: {', '.join(self.templates)}"
            q = self.templates[template].format(namespace=namespace, pod=pod, workload=workload, service=service,
                                                instance=instance)
            result = await (self.range(q, minutes) if minutes else self.instant(q))
            return {"promql": q, "result": result}

        async def find_metrics(contains: str):
            data = await self._get_json("/api/v1/label/__name__/values")
            names = [n for n in data["data"] if contains.lower() in n.lower()]
            return names[:100] or "no matching metric names"

        return [
            ToolSpec("get_alerts", "Currently firing alerts.", EmptyArgs, get_alerts, "Checked firing alerts", "metrics"),
            ToolSpec("list_query_templates", "Ready-made PromQL for restarts, CPU, memory vs limit, throttling, OOM, "
                     "HTTP errors/latency, node and PVC capacity.", EmptyArgs, list_query_templates,
                     "Looked up metric queries", "metrics"),
            ToolSpec("run_query_template", "Run a ready-made PromQL template with namespace/pod filters. Pass "
                     "minutes for a trend over time.", TemplateArgs, run_query_template,
                     "Checked metric '{template}' for {pod}", "metrics"),
            ToolSpec("query_metrics", "Instant PromQL query.", InstantArgs, query_metrics, "Queried metrics", "metrics"),
            ToolSpec("query_metrics_range", "PromQL over a time window; returns min/max/avg/trend per series.",
                     RangeArgs, query_metrics_range, "Analysed metric trend over {minutes} min", "metrics"),
            ToolSpec("find_metrics", "Search metric names available in Prometheus.", MetricSearchArgs, find_metrics,
                     "Searched metric names for '{contains}'", "metrics"),
        ]
