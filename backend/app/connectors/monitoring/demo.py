"""Simulated metrics + logs + alerts matching the demo cluster's incident."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pydantic import BaseModel

from app.plugins import EmptyArgs, HealthStatus, Plugin, PluginKind, ToolSpec, register_plugin

from .loki import SearchArgs
from .prometheus import DEFAULT_TEMPLATES, RangeArgs, TemplateArgs


class DemoMonitoringConfig(BaseModel):
    pass


def _ago(minutes: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


_CANNED = {
    "pod_restarts": [{"labels": {"pod": "payments-api-*"}, "value": "19"}, {"labels": {"pod": "ledger-worker-*"}, "value": "6"}],
    "pod_memory_limit_ratio": [{"labels": {"pod": "ledger-worker-*"}, "first": 0.71, "last": 0.99, "max": 1.0, "avg": 0.9,
                                "trend": "rising"}, {"labels": {"pod": "frontend-*"}, "last": 0.32, "trend": "flat"}],
    "oom_kills": [{"labels": {"pod": "ledger-worker-*"}, "value": "6"}],
    "http_error_rate_istio": [{"labels": {"destination_workload": "frontend"}, "first": 0.002, "last": 0.41, "max": 0.44,
                               "trend": "rising", "note": "jump started ~25 minutes ago"},
                              {"labels": {"destination_workload": "payments-api"}, "value": "no traffic - no ready pods"}],
    "pod_cpu_cores": [{"labels": {"pod": "payments-api-*"}, "last": 0.0}, {"labels": {"pod": "ledger-worker-*"}, "last": 0.61}],
    "node_disk_free_ratio": [{"labels": {"instance": "worker-3"}, "last": 0.12, "trend": "falling"},
                             {"labels": {"instance": "worker-1"}, "last": 0.58}],
}


@register_plugin
class DemoMonitoring(Plugin):
    kind = PluginKind.MONITORING
    type = "demo"
    display_name = "Demo monitoring (simulated)"
    description = "Simulated Prometheus metrics, alerts and Loki logs that match the demo cluster."
    capabilities = frozenset({"metrics", "logs", "alerts"})
    Config = DemoMonitoringConfig

    async def health(self) -> HealthStatus:
        return HealthStatus(True, "Simulated metrics and logs available")

    def tools(self) -> list[ToolSpec]:
        async def get_alerts():
            return [
                {"name": "KubePodCrashLooping", "severity": "critical", "since": _ago(22),
                 "labels": {"namespace": "payments", "pod": "payments-api-*"},
                 "summary": "Pod payments/payments-api is crash looping"},
                {"name": "KubeDeploymentReplicasMismatch", "severity": "warning", "since": _ago(15),
                 "labels": {"namespace": "payments", "deployment": "payments-api"},
                 "summary": "Deployment payments/payments-api has 0/2 replicas available"},
                {"name": "HighCheckoutErrorRate", "severity": "critical", "since": _ago(20),
                 "labels": {"namespace": "shop", "workload": "frontend"}, "summary": "Checkout 5xx ratio is 41%"},
                {"name": "KubeContainerOOMKilled", "severity": "warning", "since": _ago(14),
                 "labels": {"namespace": "payments", "pod": "ledger-worker-*"}, "summary": "ledger-worker OOMKilled 6 times in 1h"},
                {"name": "NodeFilesystemSpaceFillingUp", "severity": "warning", "since": _ago(40),
                 "labels": {"instance": "worker-3"}, "summary": "worker-3 root filesystem 88% full"},
            ]

        async def list_query_templates():
            return DEFAULT_TEMPLATES

        async def run_query_template(template: str, namespace: str = ".*", pod: str = ".*", minutes: int | None = None, **_):
            rows = _CANNED.get(template)
            if rows is None:
                return f"no data for template '{template}' (simulated backend)"
            return {"template": template, "namespace": namespace, "pod": pod, "result": rows}

        async def query_metrics_range(query: str, minutes: int = 60):
            for key, rows in _CANNED.items():
                if any(token in query for token in key.split("_")[:2]):
                    return {"window_minutes": minutes, "result": rows}
            return "no data (simulated backend recognises restarts, memory, oom, http errors, cpu, disk)"

        async def search_logs(namespace=None, pod_regex=None, host=None, contains=None, minutes=60, limit=100):
            lines = []
            if namespace in (None, "payments") and (not pod_regex or "payments" in pod_regex):
                lines += [f"{_ago(25 - i)} [payments-api] ERROR config: invalid value for DB_POOL_SIZE: "
                          'strconv.Atoi: parsing "twenty": invalid syntax' for i in range(0, 24, 2)]
                lines.insert(0, f"{_ago(27)} [payments-api] INFO payments-api 2.3.4 healthy, pool=20, p99=48ms")
            if namespace in (None, "shop"):
                lines += [f"{_ago(24 - i)} [frontend] ERROR POST /checkout 503 upstream payments-api.payments.svc:8080: "
                          "no healthy upstream" for i in range(0, 24, 3)]
            if namespace in (None, "payments") and (not pod_regex or "ledger" in pod_regex):
                lines += [f"{_ago(14)} [ledger-worker] WARN heap usage 255Mi / limit 256Mi - GC thrashing"]
            if contains:
                import re
                rx = re.compile(contains, re.I)
                lines = [ln for ln in lines if rx.search(ln)]
            return {"total_lines": len(lines), "lines": lines[:limit]}

        return [
            ToolSpec("get_alerts", "Currently firing alerts.", EmptyArgs, get_alerts, "Checked firing alerts", "metrics"),
            ToolSpec("list_query_templates", "Ready-made metric queries.", EmptyArgs, list_query_templates,
                     "Looked up metric queries", "metrics"),
            ToolSpec("run_query_template", "Run a ready-made metric query (pod_restarts, pod_memory_limit_ratio, "
                     "oom_kills, http_error_rate_istio, pod_cpu_cores, node_disk_free_ratio…).", TemplateArgs,
                     run_query_template, "Checked metric '{template}' for {pod}", "metrics"),
            ToolSpec("query_metrics_range", "Metric trend over a window.", RangeArgs, query_metrics_range,
                     "Analysed metric trend over {minutes} min", "metrics"),
            ToolSpec("search_logs", "Search centralised logs by namespace / pod regex / text.", SearchArgs,
                     search_logs, "Searched logs for '{contains}' in {namespace}", "logs"),
        ]
