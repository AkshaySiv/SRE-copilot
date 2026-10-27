from __future__ import annotations

import re
from collections import Counter

from pydantic import BaseModel, Field

from app.plugins import EmptyArgs, HealthStatus, ToolSpec, register_plugin

from .base import HttpAuthConfig, MonitoringPlugin, window


class LokiConfig(HttpAuthConfig):
    url: str = "http://loki-gateway.monitoring.svc"
    tenant_id: str | None = Field(None, description="X-Scope-OrgID for multi-tenant Loki")
    namespace_label: str = Field("namespace", description="Label holding the k8s namespace")
    pod_label: str = Field("pod", description="Label holding the pod name")
    host_label: str = Field("host", description="Label holding the VM hostname (journald / promtail)")
    max_lines: int = 200


class SearchArgs(BaseModel):
    namespace: str | None = None
    pod_regex: str | None = Field(None, description="e.g. payments-api-.*")
    host: str | None = Field(None, description="VM hostname, for VM logs")
    contains: str | None = Field(None, description="Case-insensitive text/regex filter, e.g. error|exception|timeout")
    minutes: int = Field(60, ge=1, le=60 * 24 * 7)
    limit: int = Field(100, ge=1, le=1000)


class LogQLArgs(BaseModel):
    logql: str = Field(description='Raw LogQL, e.g. {namespace="payments"} |= "error"')
    minutes: int = Field(60, ge=1, le=60 * 24 * 7)
    limit: int = Field(100, ge=1, le=1000)


_NUM = re.compile(r"\b[0-9a-f]{6,}\b|\d+")


@register_plugin
class Loki(MonitoringPlugin):
    type = "loki"
    display_name = "Grafana Loki"
    description = "Application and system logs via LogQL."
    capabilities = frozenset({"logs"})
    Config = LokiConfig

    def client(self):
        if self._client is None and self.config.tenant_id:
            self.config.headers.setdefault("X-Scope-OrgID", self.config.tenant_id)
        return super().client()

    async def query(self, logql: str, minutes: int, limit: int) -> dict:
        start, end = window(minutes)
        data = await self._get_json("/loki/api/v1/query_range", {
            "query": logql, "start": int(start.timestamp() * 1e9), "end": int(end.timestamp() * 1e9),
            "limit": min(limit, self.config.max_lines), "direction": "backward"})
        lines = []
        for stream in data["data"]["result"]:
            lbl = stream.get("stream", {})
            src = lbl.get(self.config.pod_label) or lbl.get(self.config.host_label) or lbl.get("job", "")
            for _, line in stream.get("values", []):
                lines.append(f"[{src}] {line[:600]}")
        # Group repeated messages so the LLM sees patterns, not 500 copies of the same error.
        patterns = Counter(_NUM.sub("#", ln.split("] ", 1)[-1])[:160] for ln in lines)
        return {"query": logql, "total_lines": len(lines),
                "top_patterns": [{"count": c, "pattern": p} for p, c in patterns.most_common(15)],
                "lines": lines[:limit]}

    def build_query(self, namespace=None, pod_regex=None, host=None, contains=None) -> str:
        c: LokiConfig = self.config
        sel = []
        if namespace:
            sel.append(f'{c.namespace_label}="{namespace}"')
        if pod_regex:
            sel.append(f'{c.pod_label}=~"{pod_regex}"')
        if host:
            sel.append(f'{c.host_label}="{host}"')
        if not sel:
            sel.append(f'{c.namespace_label}=~".+"')
        q = "{" + ", ".join(sel) + "}"
        if contains:
            q += f' |~ "(?i){contains}"'
        return q

    async def health(self) -> HealthStatus:
        try:
            await self._get_json("/loki/api/v1/labels")
            return HealthStatus(True, "Loki API reachable")
        except Exception as exc:  # noqa: BLE001
            return HealthStatus(False, f"{type(exc).__name__}: {exc}")

    def tools(self) -> list[ToolSpec]:
        async def search_logs(namespace=None, pod_regex=None, host=None, contains=None, minutes=60, limit=100):
            return await self.query(self.build_query(namespace, pod_regex, host, contains), minutes, limit)

        async def query_logql(logql: str, minutes: int = 60, limit: int = 100):
            return await self.query(logql, minutes, limit)

        async def log_labels():
            return (await self._get_json("/loki/api/v1/labels"))["data"]

        return [
            ToolSpec("search_logs", "Search centralised logs by namespace / pod regex / VM host and text. Returns "
                     "grouped error patterns plus raw lines - covers history beyond the current container.",
                     SearchArgs, search_logs, "Searched logs for '{contains}' in {namespace}", "logs"),
            ToolSpec("query_logql", "Raw LogQL query.", LogQLArgs, query_logql, "Ran a log query", "logs"),
            ToolSpec("log_labels", "Available Loki label names.", EmptyArgs, log_labels, "Listed log labels", "logs"),
        ]
