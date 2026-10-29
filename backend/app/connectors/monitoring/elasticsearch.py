from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from app.plugins import HealthStatus, ToolSpec, register_plugin

from app.timeline.logparse import parse_ts
from app.timeline.model import TimelineBatch, TimelineQuery

from .base import HttpAuthConfig, MonitoringPlugin, window
from .loki import _NUM
from .timeline_support import log_events


class ElasticConfig(HttpAuthConfig):
    url: str = "https://elasticsearch.logging.svc:9200"
    index_pattern: str = "logs-*"
    timestamp_field: str = "@timestamp"
    message_field: str = "message"
    namespace_field: str = "kubernetes.namespace_name"
    pod_field: str = "kubernetes.pod_name"
    host_field: str = "host.name"
    level_field: str | None = "level"


class ESSearchArgs(BaseModel):
    namespace: str | None = None
    pod_prefix: str | None = Field(None, description="Pod name prefix, e.g. payments-api")
    host: str | None = None
    query: str | None = Field(None, description="Lucene query_string, e.g. error OR exception")
    minutes: int = Field(60, ge=1, le=60 * 24 * 14)
    limit: int = Field(100, ge=1, le=500)


@register_plugin
class Elasticsearch(MonitoringPlugin):
    type = "elasticsearch"
    display_name = "Elasticsearch / OpenSearch"
    description = "Centralised logs stored in Elasticsearch or OpenSearch (EFK/ELK)."
    capabilities = frozenset({"logs"})
    Config = ElasticConfig

    async def search(self, namespace=None, pod_prefix=None, host=None, query=None, minutes=60, limit=100) -> dict:
        c: ElasticConfig = self.config
        start, end = window(minutes)
        must: list[dict] = [{"range": {c.timestamp_field: {"gte": start.isoformat(), "lte": end.isoformat()}}}]
        if namespace:
            must.append({"term": {c.namespace_field: namespace}})
        if pod_prefix:
            must.append({"prefix": {c.pod_field: pod_prefix}})
        if host:
            must.append({"term": {c.host_field: host}})
        if query:
            must.append({"query_string": {"query": query, "default_field": c.message_field}})
        body = {"size": limit, "sort": [{c.timestamp_field: "desc"}], "query": {"bool": {"must": must}},
                "_source": [c.timestamp_field, c.message_field, c.pod_field, c.host_field] + ([c.level_field] if c.level_field else [])}
        data = await self._post_json(f"/{c.index_pattern}/_search", body)

        def pick(src: dict, dotted: str):
            if dotted in src:  # flattened field names
                return src[dotted]
            cur: object = src
            for part in dotted.split("."):  # nested objects
                if not isinstance(cur, dict):
                    return None
                cur = cur.get(part)
            return cur

        lines = []
        for hit in data.get("hits", {}).get("hits", []):
            s = hit.get("_source", {})
            msg = pick(s, c.message_field) or ""
            src = pick(s, c.pod_field) or pick(s, c.host_field) or ""
            lines.append(f"{s.get(c.timestamp_field, '')} [{src}] {str(msg)[:600]}")
        patterns = Counter(_NUM.sub("#", ln.split("] ", 1)[-1])[:160] for ln in lines)
        total = data.get("hits", {}).get("total", {})
        return {"total_hits": total.get("value") if isinstance(total, dict) else total,
                "top_patterns": [{"count": n, "pattern": p} for p, n in patterns.most_common(15)], "lines": lines}

    async def health(self) -> HealthStatus:
        try:
            data = await self._get_json("/_cluster/health")
            return HealthStatus(data.get("status") in ("green", "yellow"), f"cluster status {data.get('status')}")
        except Exception as exc:  # noqa: BLE001
            return HealthStatus(False, f"{type(exc).__name__}: {exc}")

    async def timeline(self, query: TimelineQuery) -> TimelineBatch:
        batch = TimelineBatch()
        minutes = max(int((datetime.now(timezone.utc) - query.start).total_seconds() // 60), 1)
        groups: dict[tuple[str | None, str | None], list] = {}
        namespaces = query.namespaces or [None]
        for ns in namespaces[:10]:
            try:
                res = await self.search(namespace=ns, query="error OR exception OR fatal OR panic OR traceback",
                                        minutes=minutes, limit=500)
            except Exception as exc:  # noqa: BLE001
                batch.errors.append(f"{self.name}: log search failed: {exc}")
                return batch
            for line in res["lines"]:
                ts_raw, _, rest = line.partition(" [")
                src, _, msg = rest.partition("] ")
                ts = parse_ts(ts_raw)
                if ts and ts <= query.end:
                    groups.setdefault((ns, src or None), []).append((ts, msg))
        for (ns, pod), lines in list(groups.items())[:100]:
            batch.events.extend(log_events(lines, ns, pod, self.name, f"Elasticsearch · {self.name}"))
        return batch

    def tools(self) -> list[ToolSpec]:
        async def search_logs(namespace=None, pod_prefix=None, host=None, query=None, minutes=60, limit=100):
            return await self.search(namespace, pod_prefix, host, query, minutes, limit)

        return [ToolSpec("search_logs", "Search centralised logs (Elasticsearch/OpenSearch) by namespace, pod "
                         "prefix, host and Lucene query. Returns grouped patterns and lines.", ESSearchArgs,
                         search_logs, "Searched logs for '{query}' in {namespace}", "logs")]
