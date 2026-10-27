"""Backend-agnostic Kubernetes connector.

Subclasses implement five raw primitives (_list_raw, _get_raw, _pod_logs, _patch, _delete). Everything
user-facing - overview, topology, investigation tools, remediation primitives, namespace guardrails -
lives here so it behaves identically for every cluster type.
"""
from __future__ import annotations

import asyncio
import json
from abc import abstractmethod
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from app.plugins import EmptyArgs, HealthStatus, Plugin, PluginKind, ToolSpec

from . import model as m


class ExtraKind(BaseModel):
    kind: str = Field(description="e.g. VirtualService")
    api_version: str = Field(description="e.g. networking.istio.io/v1beta1")
    namespaced: bool = True
    aliases: list[str] = Field(default_factory=list)


class KubernetesConfigBase(BaseModel):
    environment: str = Field("", description="Label shown in the UI, e.g. prod, staging, kyber-dev")
    namespaces_allowlist: list[str] = Field(default_factory=list,
                                            description="If set, the copilot only sees these namespaces")
    namespaces_denylist: list[str] = Field(default_factory=list, description="Namespaces hidden from the copilot")
    allow_remediation: bool = Field(False, description="Allow approved remediation actions to modify this cluster")
    extra_kinds: list[ExtraKind] = Field(default_factory=list,
                                         description="CRDs the copilot may read (Istio, Argo, cert-manager…)")


# ---- tool argument models -----------------------------------------------------------------------
class NamespaceArg(BaseModel):
    namespace: str | None = Field(None, description="Namespace; omit for all allowed namespaces")


class ListArgs(BaseModel):
    kind: str = Field(description="Resource kind, e.g. Pod, Deployment, Service, Node, Ingress, PVC")
    namespace: str | None = Field(None, description="Namespace; omit for all namespaces")
    label_selector: str | None = Field(None, description="e.g. app=payments-api")


class GetArgs(BaseModel):
    kind: str
    name: str
    namespace: str | None = None


class EventsArgs(BaseModel):
    namespace: str | None = None
    object_name: str | None = Field(None, description="Only events about this object (substring match)")
    warnings_only: bool = True
    limit: int = Field(40, ge=1, le=200)


class LogsArgs(BaseModel):
    name: str = Field(description="Pod name")
    namespace: str
    container: str | None = None
    tail_lines: int = Field(150, ge=1, le=2000)
    previous: bool = Field(False, description="Logs of the previous (crashed) container instance")
    since_minutes: int | None = Field(None, ge=1, le=1440)


class RolloutArgs(BaseModel):
    name: str = Field(description="Deployment name")
    namespace: str


class WorkloadLogsArgs(BaseModel):
    kind: Literal["Deployment", "StatefulSet", "DaemonSet", "Job"] = "Deployment"
    name: str
    namespace: str
    tail_lines: int = Field(80, ge=1, le=500)
    previous: bool = False


class KubernetesConnector(Plugin):
    kind = PluginKind.KUBERNETES
    capabilities = frozenset({"topology", "events", "logs", "remediation"})
    Config = KubernetesConfigBase

    def __init__(self, name: str, config: KubernetesConfigBase):
        super().__init__(name, config)
        self.kinds = m.KindRegistry([
            m.KindInfo(e.kind, e.api_version, e.namespaced, tuple(e.aliases)) for e in config.extra_kinds
        ])

    # ---------------------------------------------------------------- raw primitives (subclass)
    @abstractmethod
    async def _list_raw(self, info: m.KindInfo, namespace: str | None, label_selector: str | None) -> list[dict]: ...

    @abstractmethod
    async def _get_raw(self, info: m.KindInfo, name: str, namespace: str | None) -> dict | None: ...

    @abstractmethod
    async def _pod_logs(self, name: str, namespace: str, container: str | None, tail_lines: int,
                        previous: bool, since_seconds: int | None) -> str: ...

    @abstractmethod
    async def _patch(self, info: m.KindInfo, name: str, namespace: str | None, body: Any, dry_run: bool,
                     patch_type: Literal["merge", "json", "strategic"] = "merge") -> dict: ...

    @abstractmethod
    async def _delete(self, info: m.KindInfo, name: str, namespace: str | None, dry_run: bool) -> dict: ...

    async def server_version(self) -> str:
        return "unknown"

    # ---------------------------------------------------------------- guardrails
    def namespace_allowed(self, namespace: str | None) -> bool:
        if namespace is None:
            return True
        c: KubernetesConfigBase = self.config
        if c.namespaces_allowlist and namespace not in c.namespaces_allowlist:
            return False
        return namespace not in c.namespaces_denylist

    def _check_ns(self, namespace: str | None) -> None:
        if not self.namespace_allowed(namespace):
            raise PermissionError(f"Namespace '{namespace}' is not allowed for integration '{self.name}'")

    # ---------------------------------------------------------------- generic read API
    async def list_objects(self, kind: str, namespace: str | None = None, label_selector: str | None = None) -> list[dict]:
        info = self.kinds.resolve(kind)
        if not info.namespaced:
            namespace = None
        self._check_ns(namespace)
        allow = self.config.namespaces_allowlist
        if info.namespaced and namespace is None and allow:
            results = await asyncio.gather(*(self._list_raw(info, ns, label_selector) for ns in allow),
                                           return_exceptions=True)
            items = [o for r in results if isinstance(r, list) for o in r]
        else:
            items = await self._list_raw(info, namespace, label_selector)
        if info.kind == "Namespace":
            return [o for o in items if self.namespace_allowed(m.name_of(o))]
        return [o for o in items if self.namespace_allowed(m.ns_of(o))]

    async def get_object(self, kind: str, name: str, namespace: str | None) -> dict | None:
        info = self.kinds.resolve(kind)
        ns = namespace if info.namespaced else None
        self._check_ns(ns)
        obj = await self._get_raw(info, name, ns)
        return m.sanitize(info.kind, obj) if obj else None

    async def list_events(self, namespace: str | None = None, object_name: str | None = None,
                          warnings_only: bool = False, limit: int = 100) -> list[dict]:
        events = [m.summarize_event(e) for e in await self.list_objects("Event", namespace)]
        if object_name:
            events = [e for e in events if object_name in e["object"]]
        if warnings_only:
            events = [e for e in events if e["type"] == "Warning"]
        events.sort(key=lambda e: e["last_seen"] or "", reverse=True)
        return events[:limit]

    async def pod_logs(self, name: str, namespace: str, container: str | None = None, tail_lines: int = 200,
                       previous: bool = False, since_minutes: int | None = None) -> str:
        self._check_ns(namespace)
        return await self._pod_logs(name, namespace, container, tail_lines, previous,
                                    since_minutes * 60 if since_minutes else None)

    async def overview(self) -> dict:
        kinds = ("Namespace", "Node", "Pod", "Deployment", "StatefulSet", "DaemonSet", "Service",
                 "PersistentVolumeClaim", "Job", "Ingress")
        results = await asyncio.gather(*(self.list_objects(k) for k in kinds), return_exceptions=True)
        data = {k: (r if isinstance(r, list) else []) for k, r in zip(kinds, results)}
        errors = {k: str(r) for k, r in zip(kinds, results) if isinstance(r, Exception)}

        per_ns: dict[str, dict] = defaultdict(lambda: {"counts": Counter(), "health": "healthy", "issues": []})
        for kind in kinds[2:]:
            for obj in data[kind]:
                s = m.summarize(kind, obj)
                bucket = per_ns[s["namespace"] or "_"]
                bucket["counts"][kind] += 1
                if s["health"] in ("warning", "critical"):
                    bucket["health"] = m.worst(bucket["health"], s["health"])
                    bucket["issues"].append({"kind": kind, "name": s["name"], "health": s["health"],
                                             "reasons": s["reasons"][:2]})
        namespaces = []
        for ns_obj in data["Namespace"]:
            name = m.name_of(ns_obj)
            b = per_ns.get(name, {"counts": Counter(), "health": "healthy", "issues": []})
            namespaces.append({"name": name, "health": b["health"], "counts": dict(b["counts"]),
                               "issues": sorted(b["issues"], key=lambda i: -m.HEALTH_ORDER[i["health"]])[:20],
                               "labels": m.meta(ns_obj).get("labels") or {}, "age": m.age(ns_obj)})
        namespaces.sort(key=lambda n: (-m.HEALTH_ORDER[n["health"]], n["name"]))
        nodes = [m.summarize("Node", n) for n in data["Node"]]
        pods = [m.summarize("Pod", p) for p in data["Pod"]]
        return {
            "cluster": self.name, "environment": self.config.environment,
            "totals": {"namespaces": len(namespaces), "nodes": len(nodes), "pods": len(pods),
                       "unhealthy_pods": sum(1 for p in pods if p["health"] in ("warning", "critical")),
                       "workloads": sum(len(data[k]) for k in ("Deployment", "StatefulSet", "DaemonSet"))},
            "health": m.worst(*(n["health"] for n in namespaces), *(n["health"] for n in nodes)),
            "namespaces": namespaces, "nodes": nodes, "errors": errors,
        }

    async def topology(self, namespace: str | None = None, include_config: bool = False) -> dict:
        kinds = list(m.TOPOLOGY_KINDS) + (["ConfigMap"] if include_config else [])
        results = await asyncio.gather(*(self.list_objects(k, namespace) for k in kinds), return_exceptions=True)
        objs = {k: r for k, r in zip(kinds, results) if isinstance(r, list)}
        topo = m.build_topology(objs, include_config=include_config)
        topo["errors"] = {k: str(r) for k, r in zip(kinds, results) if isinstance(r, Exception)}
        return topo

    async def resource_detail(self, kind: str, name: str, namespace: str | None) -> dict | None:
        info = self.kinds.resolve(kind)
        obj = await self.get_object(info.kind, name, namespace)
        if obj is None:
            return None
        events = await self.list_events(namespace, object_name=name, limit=30) if info.kind != "Namespace" else []
        return {"summary": m.summarize(info.kind, obj), "events": events,
                "yaml": yaml.safe_dump(obj, sort_keys=False, default_flow_style=False)}

    async def find_unhealthy(self, namespace: str | None = None) -> list[dict]:
        kinds = ("Pod", "Deployment", "StatefulSet", "DaemonSet", "Job", "PersistentVolumeClaim", "Node")
        results = await asyncio.gather(*(self.list_objects(k, namespace) for k in kinds), return_exceptions=True)
        out = []
        for kind, items in zip(kinds, results):
            if isinstance(items, Exception):
                out.append({"kind": kind, "error": str(items)})
                continue
            for obj in items:
                s = m.summarize(kind, obj)
                if s["health"] in ("warning", "critical"):
                    out.append({k: s[k] for k in ("kind", "name", "namespace", "health", "status", "reasons")}
                               | {"details": {k: v for k, v in s["details"].items()
                                              if k in ("restarts", "ready", "images", "node", "revision")}})
        out.sort(key=lambda s: -m.HEALTH_ORDER.get(s.get("health", "unknown"), 1))
        return out

    async def rollout_history(self, name: str, namespace: str) -> list[dict]:
        dep = await self.get_object("Deployment", name, namespace)
        if not dep:
            raise LookupError(f"Deployment {namespace}/{name} not found")
        current = (m.meta(dep).get("annotations") or {}).get("deployment.kubernetes.io/revision")
        rows = []
        for rs in await self.list_objects("ReplicaSet", namespace):
            if not any(r.get("kind") == "Deployment" and r.get("name") == name
                       for r in m.meta(rs).get("ownerReferences") or []):
                continue
            ann = m.meta(rs).get("annotations") or {}
            tmpl = ((rs.get("spec") or {}).get("template") or {})
            rows.append({
                "revision": int(ann.get("deployment.kubernetes.io/revision", 0)),
                "replicaset": m.name_of(rs), "created": m.meta(rs).get("creationTimestamp"), "age": m.age(rs),
                "images": [c.get("image") for c in (tmpl.get("spec") or {}).get("containers") or []],
                "replicas": (rs.get("spec") or {}).get("replicas"),
                "change_cause": ann.get("kubernetes.io/change-cause"),
                "current": str(ann.get("deployment.kubernetes.io/revision")) == str(current),
            })
        return sorted(rows, key=lambda r: r["revision"], reverse=True)

    # ---------------------------------------------------------------- write primitives (used ONLY by remediation actions)
    def _check_write(self, namespace: str | None) -> None:
        if not self.config.allow_remediation:
            raise PermissionError(f"Remediation is disabled for cluster '{self.name}' (allow_remediation=false)")
        self._check_ns(namespace)

    async def restart_workload(self, kind: str, name: str, namespace: str, dry_run: bool) -> dict:
        self._check_write(namespace)
        stamp = datetime.now(timezone.utc).isoformat()
        body = {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": stamp}}}}}
        return await self._patch(self.kinds.resolve(kind), name, namespace, body, dry_run)

    async def scale_workload(self, kind: str, name: str, namespace: str, replicas: int, dry_run: bool) -> dict:
        self._check_write(namespace)
        return await self._patch(self.kinds.resolve(kind), name, namespace, {"spec": {"replicas": replicas}}, dry_run)

    async def delete_pod(self, name: str, namespace: str, dry_run: bool) -> dict:
        self._check_write(namespace)
        return await self._delete(self.kinds.resolve("Pod"), name, namespace, dry_run)

    async def set_node_schedulable(self, name: str, schedulable: bool, dry_run: bool) -> dict:
        self._check_write(None)
        return await self._patch(self.kinds.resolve("Node"), name, None, {"spec": {"unschedulable": not schedulable}},
                                 dry_run)

    async def rollback_deployment(self, name: str, namespace: str, to_revision: int | None, dry_run: bool) -> dict:
        self._check_write(namespace)
        history = await self.rollout_history(name, namespace)
        current = next((h for h in history if h["current"]), None)
        candidates = [h for h in history if not h["current"] and (to_revision is None or h["revision"] == to_revision)]
        if current:
            candidates = [h for h in candidates if h["revision"] < current["revision"]] or candidates
        if not candidates:
            raise LookupError("No previous revision available to roll back to")
        target = candidates[0]
        rs = await self._get_raw(self.kinds.resolve("ReplicaSet"), target["replicaset"], namespace)
        template = (rs or {}).get("spec", {}).get("template")
        if not template:
            raise LookupError(f"ReplicaSet {target['replicaset']} has no pod template")
        template = json.loads(json.dumps(template))
        (template.get("metadata", {}).get("labels") or {}).pop("pod-template-hash", None)
        body = [{"op": "replace", "path": "/spec/template", "value": template}]
        result = await self._patch(self.kinds.resolve("Deployment"), name, namespace, body, dry_run, patch_type="json")
        return {"rolled_back_to_revision": target["revision"], "images": target["images"], "result": result}

    # ---------------------------------------------------------------- plugin API
    async def health(self) -> HealthStatus:
        try:
            version = await self.server_version()
            namespaces = await self.list_objects("Namespace")
            return HealthStatus(True, f"Connected - Kubernetes {version}, {len(namespaces)} visible namespaces",
                                {"version": version})
        except Exception as exc:  # noqa: BLE001
            return HealthStatus(False, f"{type(exc).__name__}: {exc}")

    def context_hint(self) -> str:
        env = f" (environment: {self.config.environment})" if self.config.environment else ""
        scope = (f" Restricted to namespaces: {', '.join(self.config.namespaces_allowlist)}."
                 if self.config.namespaces_allowlist else "")
        return f"Kubernetes cluster{env}.{scope}"

    def tools(self) -> list[ToolSpec]:
        async def find_unhealthy(namespace: str | None = None):
            return await self.find_unhealthy(namespace)

        async def list_resources(kind: str, namespace: str | None = None, label_selector: str | None = None):
            items = await self.list_objects(kind, namespace, label_selector)
            info = self.kinds.resolve(kind)
            return [{k: s[k] for k in ("name", "namespace", "health", "status", "reasons", "age", "details")}
                    for s in (m.summarize(info.kind, o) for o in items)][:200]

        async def get_resource(kind: str, name: str, namespace: str | None = None):
            obj = await self.get_object(kind, name, namespace)
            if obj is None:
                return f"{kind} {namespace}/{name} not found"
            return yaml.safe_dump(obj, sort_keys=False)

        async def get_events(namespace: str | None = None, object_name: str | None = None,
                             warnings_only: bool = True, limit: int = 40):
            return await self.list_events(namespace, object_name, warnings_only, limit)

        async def get_pod_logs(name: str, namespace: str, container: str | None = None, tail_lines: int = 150,
                               previous: bool = False, since_minutes: int | None = None):
            return await self.pod_logs(name, namespace, container, tail_lines, previous, since_minutes)

        async def get_workload_logs(name: str, namespace: str, kind: str = "Deployment", tail_lines: int = 80,
                                    previous: bool = False):
            wl = await self.get_object(kind, name, namespace)
            if not wl:
                return f"{kind} {namespace}/{name} not found"
            selector = ((wl.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
            sel = ",".join(f"{k}={v}" for k, v in selector.items())
            pods = (await self.list_objects("Pod", namespace, sel))[:3]
            chunks = []
            for p in pods:
                try:
                    logs = await self.pod_logs(m.name_of(p), namespace, None, tail_lines, previous)
                except Exception as exc:  # noqa: BLE001
                    logs = f"<error: {exc}>"
                chunks.append(f"===== pod {m.name_of(p)} =====\n{logs}")
            return "\n".join(chunks) or "no pods found for workload"

        async def rollout_history(name: str, namespace: str):
            return await self.rollout_history(name, namespace)

        async def cluster_overview():
            ov = await self.overview()
            return {"totals": ov["totals"], "health": ov["health"],
                    "namespaces": [{k: n[k] for k in ("name", "health", "issues")} for n in ov["namespaces"]
                                   if n["health"] != "healthy"] or "all namespaces healthy",
                    "nodes": [{k: n[k] for k in ("name", "health", "status", "reasons")} for n in ov["nodes"]]}

        return [
            ToolSpec("cluster_overview", "Cluster-wide health: node status and every namespace with problems. "
                     "Good first step when the scope is unclear.", EmptyArgs, cluster_overview,
                     "Checked overall cluster health", "kubernetes"),
            ToolSpec("find_unhealthy", "List unhealthy pods, workloads, PVCs and nodes with the reasons "
                     "(CrashLoopBackOff, OOMKilled, unschedulable, replicas not ready…).", NamespaceArg,
                     find_unhealthy, "Looked for unhealthy resources in {namespace}", "kubernetes"),
            ToolSpec("list_resources", "List resources of a kind with health and status summary.", ListArgs,
                     list_resources, "Listed {kind} resources in {namespace}", "kubernetes"),
            ToolSpec("get_resource", "Full spec of one resource as YAML (env, probes, limits, volumes, selectors). "
                     "Secrets cannot be read.", GetArgs, get_resource, "Inspected {kind} {name}", "kubernetes"),
            ToolSpec("get_events", "Kubernetes events (scheduling failures, probe failures, OOM kills, image pulls, "
                     "back-offs).", EventsArgs, get_events, "Read cluster events for {object_name}", "kubernetes"),
            ToolSpec("get_pod_logs", "Container logs of a pod. Use previous=true for the crashed instance of a "
                     "restarting container.", LogsArgs, get_pod_logs, "Read logs of pod {name}", "kubernetes"),
            ToolSpec("get_workload_logs", "Logs from up to 3 pods of a workload in one call.", WorkloadLogsArgs,
                     get_workload_logs, "Read logs of {kind} {name}", "kubernetes"),
            ToolSpec("rollout_history", "Deployment revision history with images and timestamps - use to "
                     "correlate failures with recent deploys.", RolloutArgs, rollout_history,
                     "Checked deployment history of {name}", "kubernetes"),
        ]
