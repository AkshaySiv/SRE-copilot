"""A self-contained simulated cluster with a realistic incident, for demos, UI development and tests.

Scenario: a bad deploy of payments-api (revision 7) introduced an invalid DB_POOL_SIZE value, so the pods
crash-loop, the payments Service has no endpoints and the shop frontend's checkout returns 503s. A second,
unrelated issue: ledger-worker is being OOMKilled. A tiny reconciler simulates controllers so that approved
remediations (rollback, restart, scale, delete pod) visibly change the cluster.
"""
from __future__ import annotations

import copy
import hashlib
import json
import random
import string
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import Field

from app.plugins import register_plugin

from . import model as m
from .base import KubernetesConfigBase, KubernetesConnector


class DemoConfig(KubernetesConfigBase):
    environment: str = "demo"
    allow_remediation: bool = Field(True, description="Demo clusters allow simulated remediation")


def _ts(minutes_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rand(n: int = 5) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def _meta(name, ns=None, labels=None, minutes_ago=60 * 24, owner=None, annotations=None) -> dict:
    md: dict[str, Any] = {"name": name, "creationTimestamp": _ts(minutes_ago), "uid": _rand(12)}
    if ns:
        md["namespace"] = ns
    if labels:
        md["labels"] = dict(labels)
    if owner:
        md["ownerReferences"] = [{"kind": owner[0], "name": owner[1], "controller": True}]
    if annotations:
        md["annotations"] = dict(annotations)
    return md


def _container(name, image, env=None, mem_limit="512Mi", port=8080) -> dict:
    return {"name": name, "image": image, "ports": [{"containerPort": port}],
            "env": [{"name": k, "value": v} for k, v in (env or {}).items()],
            "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"memory": mem_limit}},
            "readinessProbe": {"httpGet": {"path": "/healthz", "port": port}, "periodSeconds": 10}}


class DemoState:
    def __init__(self) -> None:
        self.objs: dict[str, list[dict]] = {k.kind: [] for k in m.BUILTIN_KINDS}
        self.logs: dict[tuple[str, str], dict[str, str]] = {}
        self._build()

    # ---------------------------------------------------------------------------------- builders
    def add(self, kind: str, obj: dict) -> dict:
        self.objs[kind].append(obj)
        return obj

    def event(self, ns, kind, name, type_, reason, message, count=1, minutes_ago=1.0):
        self.add("Event", {"metadata": _meta(f"{name}.{_rand(8)}", ns, minutes_ago=minutes_ago),
                           "involvedObject": {"kind": kind, "name": name, "namespace": ns},
                           "type": type_, "reason": reason, "message": message, "count": count,
                           "lastTimestamp": _ts(minutes_ago)})

    def deployment(self, ns, name, containers, replicas=2, revision=1, minutes_ago=60 * 24 * 10, labels=None,
                   history: list[tuple[int, list[dict], float]] | None = None):
        labels = labels or {"app": name}
        dep = self.add("Deployment", {
            "metadata": _meta(name, ns, labels, minutes_ago, annotations={"deployment.kubernetes.io/revision": str(revision)}),
            "spec": {"replicas": replicas, "selector": {"matchLabels": dict(labels)},
                     "template": {"metadata": {"labels": dict(labels)}, "spec": {"containers": containers}}},
            "status": {},
        })
        for rev, ctrs, age_min in history or []:
            tmpl = {"metadata": {"labels": dict(labels)}, "spec": {"containers": ctrs}}
            self._replicaset(dep, tmpl, rev, replicas=0, minutes_ago=age_min)
        self.reconcile(dep, minutes_ago=min(minutes_ago, 60 * 24))
        return dep

    def _replicaset(self, dep, template, revision, replicas, minutes_ago=1.0) -> dict:
        ns, dname = m.ns_of(dep), m.name_of(dep)
        h = hashlib.sha1(json.dumps(template, sort_keys=True).encode()).hexdigest()[:9]
        rs_name = f"{dname}-{h}"
        for rs in self.objs["ReplicaSet"]:
            if m.name_of(rs) == rs_name and m.ns_of(rs) == ns:
                rs["spec"]["replicas"] = replicas
                return rs
        tmpl = copy.deepcopy(template)
        tmpl["metadata"].setdefault("labels", {})["pod-template-hash"] = h
        return self.add("ReplicaSet", {
            "metadata": _meta(rs_name, ns, tmpl["metadata"]["labels"], minutes_ago, owner=("Deployment", dname),
                              annotations={"deployment.kubernetes.io/revision": str(revision)}),
            "spec": {"replicas": replicas, "template": tmpl}, "status": {},
        })

    @staticmethod
    def _is_broken(template: dict) -> str | None:
        for c in template["spec"]["containers"]:
            for e in c.get("env") or []:
                if e["name"].endswith("_SIZE") and not str(e.get("value", "")).isdigit():
                    return f'invalid value for {e["name"]}: strconv.Atoi: parsing "{e["value"]}": invalid syntax'
        return None

    def reconcile(self, dep: dict, minutes_ago: float = 0.5) -> None:
        """Simulated deployment controller: one active ReplicaSet, pods reflecting the template's health."""
        ns, dname = m.ns_of(dep), m.name_of(dep)
        template = dep["spec"]["template"]
        revisions = [int((m.meta(rs).get("annotations") or {}).get("deployment.kubernetes.io/revision", 0))
                     for rs in self.objs["ReplicaSet"] if m.ns_of(rs) == ns
                     and any(o["name"] == dname for o in m.meta(rs).get("ownerReferences") or [])]
        current_rev = int(dep["metadata"]["annotations"]["deployment.kubernetes.io/revision"])
        next_rev = max(revisions) + 1 if revisions else current_rev
        count_before = len(self.objs["ReplicaSet"])
        rs = self._replicaset(dep, template, next_rev, dep["spec"]["replicas"], minutes_ago)
        ann = rs["metadata"]["annotations"]
        reused = len(self.objs["ReplicaSet"]) == count_before
        if reused and int(ann["deployment.kubernetes.io/revision"]) != current_rev:
            ann["deployment.kubernetes.io/revision"] = str(next_rev)  # rollback re-activates an old RS as a new revision
            rs["metadata"]["creationTimestamp"] = _ts(0)
        rs_rev = int(ann["deployment.kubernetes.io/revision"])
        dep["metadata"]["annotations"]["deployment.kubernetes.io/revision"] = str(rs_rev)
        for other in self.objs["ReplicaSet"]:
            if other is not rs and m.ns_of(other) == ns and any(
                    o["name"] == dname for o in m.meta(other).get("ownerReferences") or []):
                other["spec"]["replicas"] = 0
        # replace pods
        self.objs["Pod"] = [p for p in self.objs["Pod"] if not (
            m.ns_of(p) == ns and any(o["name"].startswith(dname + "-") for o in m.meta(p).get("ownerReferences") or []))]
        broken = self._is_broken(template)
        oom = any(c["name"] == "ledger-worker" and c["resources"]["limits"]["memory"] in ("256Mi",)
                  for c in template["spec"]["containers"])
        replicas = dep["spec"]["replicas"]
        for _ in range(replicas):
            self._pod_from_template(ns, m.name_of(rs), rs["spec"]["template"], broken, oom, minutes_ago)
        ready = 0 if broken else replicas
        dep["status"] = {"replicas": replicas, "readyReplicas": ready, "availableReplicas": ready,
                         "updatedReplicas": replicas,
                         "conditions": [{"type": "Available", "status": "True" if ready else "False",
                                         "reason": "MinimumReplicasAvailable" if ready else "MinimumReplicasUnavailable"}]}
        rs["status"] = {"replicas": replicas, "readyReplicas": ready}

    def _pod_from_template(self, ns, rs_name, template, broken, oom, minutes_ago, node=None):
        pname = f"{rs_name}-{_rand()}"
        node = node or random.choice(["worker-1", "worker-2", "worker-3"])
        containers = copy.deepcopy(template["spec"]["containers"])
        statuses = []
        app = containers[0]["name"]
        image = containers[0]["image"]
        version = image.rsplit(":", 1)[-1]
        if broken:
            restarts = random.randint(8, 12)
            statuses.append({"name": app, "ready": False, "restartCount": restarts, "image": image,
                             "state": {"waiting": {"reason": "CrashLoopBackOff",
                                                   "message": f"back-off 5m0s restarting failed container={app}"}},
                             "lastState": {"terminated": {"reason": "Error", "exitCode": 1, "finishedAt": _ts(1)}}})
            prev = (f"{_ts(2)} INFO  {app} {version} starting (pid 1)\n{_ts(2)} INFO  loading configuration from environment\n"
                    f"{_ts(2)} ERROR config: {broken}\n{_ts(2)} FATAL startup aborted: configuration error, exiting with code 1\n")
            self.logs[(ns, pname)] = {"current": "", "previous": prev}
            phase = "Running"
        else:
            restarts = random.randint(5, 7) if oom else 0
            st: dict[str, Any] = {"name": app, "ready": True, "restartCount": restarts, "image": image,
                                  "state": {"running": {"startedAt": _ts(minutes_ago)}}}
            if oom:
                st["lastState"] = {"terminated": {"reason": "OOMKilled", "exitCode": 137, "finishedAt": _ts(14)}}
            statuses.append(st)
            self.logs[(ns, pname)] = {"current": self._healthy_logs(app, version, ns, oom),
                                      "previous": self._healthy_logs(app, version, ns, oom, previous=True) if oom else ""}
            phase = "Running"
        labels = template["metadata"].get("labels") or {}
        self.add("Pod", {
            "metadata": _meta(pname, ns, labels, minutes_ago, owner=("ReplicaSet", rs_name)),
            "spec": {"nodeName": node, "containers": containers, "volumes": copy.deepcopy(template["spec"].get("volumes", []))},
            "status": {"phase": phase, "podIP": f"10.42.{random.randint(0, 3)}.{random.randint(2, 250)}",
                       "qosClass": "Burstable", "containerStatuses": statuses,
                       "conditions": [{"type": "Ready", "status": "False" if broken else "True"}]},
        })
        return pname

    @staticmethod
    def _healthy_logs(app, version, ns, oom=False, previous=False) -> str:
        if app == "frontend":
            lines = [f"{_ts(9)} INFO  GET /products 200 18ms", f"{_ts(8)} INFO  GET /cart 200 22ms",
                     f"{_ts(7)} ERROR POST /checkout 503 upstream payments-api.payments.svc:8080: connection refused",
                     f"{_ts(5)} ERROR POST /checkout 503 upstream payments-api.payments.svc:8080: no healthy upstream",
                     f"{_ts(3)} WARN  checkout error rate 41% over last 5m",
                     f"{_ts(1)} ERROR POST /checkout 503 upstream payments-api.payments.svc:8080: no healthy upstream"]
        elif app == "ledger-worker":
            lines = [f"{_ts(30)} INFO  ledger-worker {version} started",
                     f"{_ts(20)} INFO  loading ledger cache: 1,284,113 entries",
                     f"{_ts(16)} WARN  heap usage 241Mi / limit 256Mi",
                     f"{_ts(15)} INFO  processing settlement batch #8812 (52,004 records)"]
            if previous:
                lines.append(f"{_ts(14)} WARN  heap usage 255Mi / limit 256Mi - GC thrashing")
        elif app == "payments-api":
            lines = [f"{_ts(1)} INFO  payments-api {version} starting", f"{_ts(1)} INFO  connected to postgres-payments:5432 pool=20",
                     f"{_ts(0.5)} INFO  listening on :8080", f"{_ts(0.2)} INFO  POST /v1/charge 200 41ms"]
        else:
            lines = [f"{_ts(10)} INFO  {app} {version} running normally", f"{_ts(2)} INFO  health check ok"]
        return "\n".join(lines) + "\n"

    def _build(self) -> None:
        for n in ("default", "kube-system", "shop", "payments", "monitoring"):
            self.add("Namespace", {"metadata": _meta(n, labels={"kubernetes.io/metadata.name": n}, minutes_ago=60 * 24 * 90),
                                   "status": {"phase": "Active"}})
        for i, (node, extra) in enumerate([("control-plane-1", {"node-role.kubernetes.io/control-plane": "true"}),
                                           ("worker-1", {}), ("worker-2", {}), ("worker-3", {})]):
            conds = [{"type": "Ready", "status": "True", "reason": "KubeletReady", "message": "kubelet is posting ready status"},
                     {"type": "MemoryPressure", "status": "False"}, {"type": "PIDPressure", "status": "False"},
                     {"type": "DiskPressure", "status": "True" if node == "worker-3" else "False",
                      "message": "ephemeral-storage usage 88% exceeds eviction threshold" if node == "worker-3" else ""}]
            self.add("Node", {"metadata": _meta(node, labels={"kubernetes.io/hostname": node, **extra}, minutes_ago=60 * 24 * 120),
                              "spec": {}, "status": {"conditions": conds,
                                                     "addresses": [{"type": "InternalIP", "address": f"10.0.0.{11 + i}"}],
                                                     "capacity": {"cpu": "8", "memory": "32Gi", "ephemeral-storage": "100Gi"},
                                                     "allocatable": {"cpu": "7800m", "memory": "30Gi"},
                                                     "nodeInfo": {"kubeletVersion": "v1.30.6+rke2r1", "osImage": "Ubuntu 22.04.4 LTS",
                                                                  "containerRuntimeVersion": "containerd://1.7.22-k3s1"}}})
        reg = "harbor.internal/platform"
        # kube-system / monitoring
        self.deployment("kube-system", "coredns", [_container("coredns", "rancher/mirrored-coredns-coredns:1.11.1", port=53)],
                        replicas=2, labels={"k8s-app": "kube-dns"})
        self.deployment("monitoring", "prometheus", [_container("prometheus", "prom/prometheus:v2.54.1", port=9090)], replicas=1)
        self.deployment("monitoring", "grafana", [_container("grafana", "grafana/grafana:11.2.0", port=3000)], replicas=1)
        # shop
        self.deployment("shop", "frontend", [_container("frontend", f"{reg}/shop/frontend:5.12.0",
                                                        env={"PAYMENTS_URL": "http://payments-api.payments.svc:8080"})], replicas=3)
        self.deployment("shop", "cart", [_container("cart", f"{reg}/shop/cart:3.4.1", env={"REDIS_ADDR": "redis:6379"})], replicas=2)
        self._statefulset("shop", "redis", "redis:7.2-alpine", 6379, "2Gi")
        for svc, port in (("frontend", 8080), ("cart", 8080), ("redis", 6379)):
            self._service("shop", svc, {"app": svc}, port)
        self._ingress("shop", "shop", "shop.example.internal", "frontend")
        # payments
        good_env = {"DB_HOST": "postgres-payments", "DB_POOL_SIZE": "20", "LOG_LEVEL": "info"}
        bad_env = {**good_env, "DB_POOL_SIZE": "twenty", "FEATURE_FAST_REFUNDS": "true"}
        history = [(5, [_container("payments-api", f"{reg}/payments/payments-api:2.3.1", env=good_env)], 60 * 24 * 12),
                   (6, [_container("payments-api", f"{reg}/payments/payments-api:2.3.4", env=good_env)], 60 * 24 * 3)]
        dep = self.deployment("payments", "payments-api",
                              [_container("payments-api", f"{reg}/payments/payments-api:2.4.0", env=bad_env)],
                              replicas=2, revision=7, minutes_ago=60 * 24 * 40, history=history)
        for rs in self.objs["ReplicaSet"]:
            if m.name_of(rs).startswith("payments-api") and rs["spec"]["replicas"]:
                rs["metadata"]["creationTimestamp"] = _ts(26)
                rs["metadata"]["annotations"]["kubernetes.io/change-cause"] = "helm upgrade payments 2.4.0 (CI pipeline #4512)"
        for p in self.objs["Pod"]:
            if m.name_of(p).startswith("payments-api"):
                p["metadata"]["creationTimestamp"] = _ts(26)
        self.deployment("payments", "ledger-worker",
                        [_container("ledger-worker", f"{reg}/payments/ledger-worker:1.9.2", mem_limit="256Mi")], replicas=1)
        self._statefulset("payments", "postgres-payments", "postgres:16.4", 5432, "20Gi")
        self._service("payments", "payments-api", {"app": "payments-api"}, 8080)
        self._service("payments", "postgres-payments", {"app": "postgres-payments"}, 5432)
        self._ingress("payments", "payments-public", "pay.example.internal", "payments-api")
        self.add("HorizontalPodAutoscaler", {"metadata": _meta("payments-api", "payments"),
                                             "spec": {"minReplicas": 2, "maxReplicas": 6,
                                                      "scaleTargetRef": {"kind": "Deployment", "name": "payments-api"}},
                                             "status": {"currentReplicas": 2}})
        _ = dep
        for p in self.objs["Pod"]:
            if m.name_of(p).startswith("payments-api"):
                self.event("payments", "Pod", m.name_of(p), "Warning", "BackOff",
                           f"Back-off restarting failed container payments-api in pod {m.name_of(p)}", 41, 0.5)
                self.event("payments", "Pod", m.name_of(p), "Warning", "Unhealthy",
                           "Readiness probe failed: dial tcp :8080: connect: connection refused", 63, 1)
            if m.name_of(p).startswith("ledger-worker"):
                self.event("payments", "Pod", m.name_of(p), "Warning", "OOMKilling",
                           "Memory cgroup out of memory: Killed process (ledger-worker) total-vm:812044kB", 6, 14)
        self.event("payments", "Deployment", "payments-api", "Normal", "ScalingReplicaSet",
                   "Scaled up replica set payments-api to 2 (revision 7)", 1, 26)
        self.event("shop", "Ingress", "shop", "Normal", "Sync", "Scheduled for sync", 1, 300)
        self.event(None, "Node", "worker-3", "Warning", "EvictionThresholdMet",
                   "Attempting to reclaim ephemeral-storage", 3, 40)

    def _statefulset(self, ns, name, image, port, size):
        labels = {"app": name}
        self.add("StatefulSet", {"metadata": _meta(name, ns, labels, 60 * 24 * 60),
                                 "spec": {"replicas": 1, "selector": {"matchLabels": labels},
                                          "template": {"metadata": {"labels": labels},
                                                       "spec": {"containers": [_container(name, image, port=port)]}}},
                                 "status": {"replicas": 1, "readyReplicas": 1}})
        pvc_name = f"data-{name}-0"
        self.add("PersistentVolumeClaim", {"metadata": _meta(pvc_name, ns, labels, 60 * 24 * 60),
                                           "spec": {"storageClassName": "longhorn", "resources": {"requests": {"storage": size}}},
                                           "status": {"phase": "Bound", "capacity": {"storage": size}}})
        pname = f"{name}-0"
        self.add("Pod", {"metadata": _meta(pname, ns, labels, 60 * 24 * 5, owner=("StatefulSet", name)),
                         "spec": {"nodeName": "worker-2", "containers": [_container(name, image, port=port)],
                                  "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": pvc_name}}]},
                         "status": {"phase": "Running", "podIP": "10.42.1.40", "containerStatuses": [
                             {"name": name, "ready": True, "restartCount": 0, "image": image,
                              "state": {"running": {"startedAt": _ts(60 * 24 * 5)}}}]}})
        self.logs[(ns, pname)] = {"current": self._healthy_logs(name, image.split(":")[-1], ns), "previous": ""}

    def _service(self, ns, name, selector, port):
        self.add("Service", {"metadata": _meta(name, ns, {"app": name}),
                             "spec": {"type": "ClusterIP", "clusterIP": f"10.43.{random.randint(0, 200)}.{random.randint(2, 250)}",
                                      "selector": selector, "ports": [{"port": port, "targetPort": port, "protocol": "TCP"}]}})

    def _ingress(self, ns, name, host, svc):
        self.add("Ingress", {"metadata": _meta(name, ns),
                             "spec": {"ingressClassName": "nginx", "rules": [{"host": host, "http": {"paths": [
                                 {"path": "/", "pathType": "Prefix", "backend": {"service": {"name": svc, "port": {"number": 8080}}}}]}}]}})


@register_plugin
class DemoKubernetes(KubernetesConnector):
    type = "demo"
    display_name = "Demo cluster (simulated)"
    description = "A simulated cluster with a live incident - try the copilot without any infrastructure."
    Config = DemoConfig

    def __init__(self, name: str, config: DemoConfig):
        super().__init__(name, config)
        self.state = DemoState()

    async def server_version(self) -> str:
        return "v1.30.6+rke2r1 (simulated)"

    def _find(self, info: m.KindInfo, name: str, namespace: str | None) -> dict | None:
        for o in self.state.objs.get(info.kind, []):
            if m.name_of(o) == name and (not info.namespaced or m.ns_of(o) == namespace):
                return o
        return None

    async def _list_raw(self, info, namespace, label_selector):
        items = self.state.objs.get(info.kind, [])
        if namespace and info.namespaced:
            items = [o for o in items if m.ns_of(o) == namespace]
        if label_selector:
            want = dict(p.split("=", 1) for p in label_selector.split(",") if "=" in p)
            items = [o for o in items if all((m.meta(o).get("labels") or {}).get(k) == v for k, v in want.items())]
        return copy.deepcopy(items)

    async def _get_raw(self, info, name, namespace):
        obj = self._find(info, name, namespace)
        return copy.deepcopy(obj) if obj else None

    async def _pod_logs(self, name, namespace, container, tail_lines, previous, since_seconds):
        if not self._find(m.KindInfo("Pod", "v1"), name, namespace):
            raise LookupError(f"pod {namespace}/{name} not found")
        logs = self.state.logs.get((namespace, name), {})
        text = logs.get("previous" if previous else "current", "")
        if not text and not previous and logs.get("previous"):
            return "<container is waiting to start (CrashLoopBackOff) - request previous=true for the crashed instance>"
        return "\n".join(text.splitlines()[-tail_lines:]) or "<no log output>"

    async def _patch(self, info, name, namespace, body, dry_run, patch_type="merge"):
        obj = self._find(info, name, namespace)
        if obj is None:
            raise LookupError(f"{info.kind} {namespace}/{name} not found")
        if dry_run:
            return {"dry_run": True, "kind": info.kind, "name": name, "namespace": namespace, "validated": True}
        if patch_type == "json":
            for op in body:
                if op["op"] == "replace" and op["path"] == "/spec/template":
                    obj["spec"]["template"] = copy.deepcopy(op["value"])
                    obj["spec"]["template"]["metadata"]["labels"].pop("pod-template-hash", None)
        else:
            _deep_merge(obj, body)
        if info.kind == "Deployment":
            self.state.reconcile(obj)
            self.state.event(namespace, "Deployment", name, "Normal", "ScalingReplicaSet",
                             f"Deployment {name} updated by SRE Copilot remediation", 1, 0)
            if not self.state._is_broken(obj["spec"]["template"]):
                self.state.objs["Event"] = [e for e in self.state.objs["Event"]
                                            if not (e["involvedObject"]["name"].startswith(name + "-")
                                                    and e["reason"] in ("BackOff", "Unhealthy"))]
        return {"dry_run": False, "kind": info.kind, "name": name, "namespace": namespace}

    async def _delete(self, info, name, namespace, dry_run):
        obj = self._find(info, name, namespace)
        if obj is None:
            raise LookupError(f"{info.kind} {namespace}/{name} not found")
        if dry_run:
            return {"dry_run": True, "deleted": f"{info.kind}/{namespace}/{name}"}
        self.state.objs[info.kind].remove(obj)
        if info.kind == "Pod":
            owner = (m.meta(obj).get("ownerReferences") or [{}])[0]
            rs = self._find(m.KindInfo("ReplicaSet", "apps/v1"), owner.get("name", ""), namespace)
            if rs:
                broken = self.state._is_broken(rs["spec"]["template"])
                self.state._pod_from_template(namespace, m.name_of(rs), rs["spec"]["template"], broken, False, 0.1)
        return {"dry_run": False, "deleted": f"{info.kind}/{namespace}/{name}"}


def _deep_merge(dst: dict, src: dict) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = copy.deepcopy(v)
