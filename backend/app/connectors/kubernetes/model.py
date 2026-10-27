"""Pure functions over raw Kubernetes objects (camelCase dicts as returned by the API).

Health analysis, summaries and topology are computed here so every Kubernetes backend (real cluster,
demo, a future Rancher/OpenShift adapter) gets identical behaviour by only implementing raw list/get.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

HEALTH_ORDER = {"healthy": 0, "unknown": 1, "warning": 2, "critical": 3}


@dataclass(frozen=True)
class KindInfo:
    kind: str
    api_version: str
    namespaced: bool = True
    aliases: tuple[str, ...] = ()


BUILTIN_KINDS: list[KindInfo] = [
    KindInfo("Namespace", "v1", False, ("ns", "namespaces")),
    KindInfo("Node", "v1", False, ("nodes", "no")),
    KindInfo("Pod", "v1", True, ("pods", "po")),
    KindInfo("Service", "v1", True, ("services", "svc")),
    KindInfo("ConfigMap", "v1", True, ("configmaps", "cm")),
    KindInfo("PersistentVolumeClaim", "v1", True, ("pvc", "pvcs", "persistentvolumeclaims")),
    KindInfo("PersistentVolume", "v1", False, ("pv", "pvs", "persistentvolumes")),
    KindInfo("Event", "v1", True, ("events", "ev")),
    KindInfo("ServiceAccount", "v1", True, ("serviceaccounts", "sa")),
    KindInfo("Deployment", "apps/v1", True, ("deployments", "deploy")),
    KindInfo("StatefulSet", "apps/v1", True, ("statefulsets", "sts")),
    KindInfo("DaemonSet", "apps/v1", True, ("daemonsets", "ds")),
    KindInfo("ReplicaSet", "apps/v1", True, ("replicasets", "rs")),
    KindInfo("Job", "batch/v1", True, ("jobs",)),
    KindInfo("CronJob", "batch/v1", True, ("cronjobs", "cj")),
    KindInfo("Ingress", "networking.k8s.io/v1", True, ("ingresses", "ing")),
    KindInfo("NetworkPolicy", "networking.k8s.io/v1", True, ("networkpolicies", "netpol")),
    KindInfo("HorizontalPodAutoscaler", "autoscaling/v2", True, ("hpa", "horizontalpodautoscalers")),
    KindInfo("PodDisruptionBudget", "policy/v1", True, ("pdb", "poddisruptionbudgets")),
    KindInfo("StorageClass", "storage.k8s.io/v1", False, ("storageclasses", "sc")),
]

WORKLOAD_KINDS = ("Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob")
TOPOLOGY_KINDS = ("Pod", "Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job", "CronJob", "Service",
                  "Ingress", "PersistentVolumeClaim", "HorizontalPodAutoscaler")
# Secrets are never read by the copilot: their names are taken from pod specs only.
FORBIDDEN_KINDS = {"secret", "secrets"}

BAD_WAITING = {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError",
               "CreateContainerError", "InvalidImageName", "RunContainerError"}


class KindRegistry:
    def __init__(self, extra: list[KindInfo] | None = None):
        self._by_key: dict[str, KindInfo] = {}
        for info in BUILTIN_KINDS + list(extra or []):
            for key in (info.kind, *info.aliases):
                self._by_key[key.lower()] = info

    def resolve(self, kind: str) -> KindInfo:
        key = kind.strip().lower()
        if key in FORBIDDEN_KINDS:
            raise PermissionError("Reading Secrets is not permitted by the copilot")
        if key in self._by_key:
            return self._by_key[key]
        if key.endswith("s") and key[:-1] in self._by_key:
            return self._by_key[key[:-1]]
        raise ValueError(f"Unsupported kind '{kind}'. Supported: {', '.join(sorted(self.kinds()))}")

    def kinds(self) -> set[str]:
        return {i.kind for i in self._by_key.values()}


# --- small helpers ---------------------------------------------------------------------------------
def meta(obj: dict) -> dict:
    return obj.get("metadata") or {}


def name_of(obj: dict) -> str:
    return meta(obj).get("name", "")


def ns_of(obj: dict) -> str | None:
    return meta(obj).get("namespace")


def parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def age(obj_or_ts: Any) -> str:
    ts = parse_ts(meta(obj_or_ts).get("creationTimestamp") if isinstance(obj_or_ts, dict) else obj_or_ts)
    if not ts:
        return "?"
    s = int((datetime.now(timezone.utc) - ts).total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            return f"{s // size}{unit}"
    return f"{max(s, 0)}s"


def worst(*levels: str) -> str:
    return max(levels or ("healthy",), key=lambda lvl: HEALTH_ORDER.get(lvl, 1))


def sanitize(kind: str, obj: dict) -> dict:
    """Remove noise and anything secret before returning an object to the UI or the LLM."""
    obj = copy.deepcopy(obj)
    m = obj.get("metadata") or {}
    m.pop("managedFields", None)
    (m.get("annotations") or {}).pop("kubectl.kubernetes.io/last-applied-configuration", None)
    if kind == "Secret":
        obj.pop("data", None)
        obj.pop("stringData", None)
    return obj


@dataclass
class Health:
    level: str = "healthy"
    status: str = ""
    reasons: list[str] = field(default_factory=list)

    def bump(self, level: str, reason: str) -> None:
        self.level = worst(self.level, level)
        self.reasons.append(reason)


# --- health analysis -------------------------------------------------------------------------------
def analyze_pod(pod: dict) -> tuple[Health, dict]:
    status = pod.get("status") or {}
    spec = pod.get("spec") or {}
    phase = status.get("phase", "Unknown")
    cstatuses = (status.get("initContainerStatuses") or []) + (status.get("containerStatuses") or [])
    main = status.get("containerStatuses") or []
    ready = sum(1 for c in main if c.get("ready"))
    total = len(spec.get("containers") or main)
    restarts = sum(int(c.get("restartCount") or 0) for c in cstatuses)
    h = Health(status=phase)

    for c in cstatuses:
        state, last = c.get("state") or {}, c.get("lastState") or {}
        waiting = (state.get("waiting") or {}).get("reason")
        if waiting:
            h.status = waiting
            if waiting in BAD_WAITING:
                msg = (state.get("waiting") or {}).get("message") or ""
                h.bump("critical", f"container {c.get('name')}: {waiting}{(' - ' + msg[:160]) if msg else ''}")
        term = (state.get("terminated") or {})
        if term and term.get("reason") not in (None, "Completed"):
            h.bump("critical", f"container {c.get('name')} terminated: {term.get('reason')} (exit {term.get('exitCode')})")
        last_term = last.get("terminated") or {}
        if last_term.get("reason") == "OOMKilled":
            h.bump("warning", f"container {c.get('name')} was OOMKilled previously")
        elif last_term and int(c.get("restartCount") or 0) > 0 and last_term.get("reason") not in ("Completed",):
            h.reasons.append(f"container {c.get('name')} last exit: {last_term.get('reason')} (code {last_term.get('exitCode')})")

    if phase == "Succeeded":
        h.status, h.level, h.reasons = "Completed", "healthy", []
    elif phase == "Failed":
        h.bump("critical", f"pod failed: {status.get('reason') or status.get('message') or 'unknown'}")
    elif phase == "Pending":
        for cond in status.get("conditions") or []:
            if cond.get("type") == "PodScheduled" and cond.get("status") == "False":
                h.bump("critical", f"unschedulable: {cond.get('message', '')[:200]}")
        created = parse_ts(meta(pod).get("creationTimestamp"))
        if created and (datetime.now(timezone.utc) - created).total_seconds() > 300:
            h.bump("warning", "pending for more than 5 minutes")
    elif phase == "Running" and total and ready < total and h.level == "healthy":
        h.bump("warning", f"{ready}/{total} containers ready")
    if restarts >= 5:
        h.bump("warning", f"{restarts} restarts")
    if meta(pod).get("deletionTimestamp"):
        h.status = "Terminating"

    details = {
        "ready": f"{ready}/{total}",
        "restarts": restarts,
        "node": spec.get("nodeName"),
        "podIP": status.get("podIP"),
        "images": [c.get("image") for c in spec.get("containers") or []],
        "containers": [c.get("name") for c in spec.get("containers") or []],
        "qosClass": status.get("qosClass"),
    }
    return h, details


def analyze_workload(kind: str, obj: dict) -> tuple[Health, dict]:
    spec, status = obj.get("spec") or {}, obj.get("status") or {}
    h = Health()
    if kind == "DaemonSet":
        desired, ready = int(status.get("desiredNumberScheduled") or 0), int(status.get("numberReady") or 0)
    elif kind in ("Deployment", "StatefulSet", "ReplicaSet"):
        desired, ready = int(spec.get("replicas", 1) or 0), int(status.get("readyReplicas") or 0)
    elif kind == "Job":
        failed, succeeded = int(status.get("failed") or 0), int(status.get("succeeded") or 0)
        h.status = "Complete" if succeeded else ("Failed" if failed else "Running")
        for cond in status.get("conditions") or []:
            if cond.get("type") == "Failed" and cond.get("status") == "True":
                h.bump("critical", f"job failed: {cond.get('reason')}: {cond.get('message', '')[:160]}")
        return h, {"succeeded": succeeded, "failed": failed, "active": int(status.get("active") or 0)}
    elif kind == "CronJob":
        h.status = "Suspended" if spec.get("suspend") else "Scheduled"
        return h, {"schedule": spec.get("schedule"), "lastSchedule": status.get("lastScheduleTime")}
    else:
        return h, {}

    h.status = f"{ready}/{desired} ready"
    if desired == 0:
        h.status = "Scaled to 0"
    elif ready == 0:
        h.bump("critical", f"0 of {desired} replicas ready")
    elif ready < desired:
        h.bump("warning", f"{ready} of {desired} replicas ready")
    for cond in status.get("conditions") or []:
        if cond.get("type") == "Progressing" and cond.get("reason") == "ProgressDeadlineExceeded":
            h.bump("critical", "rollout stuck: progress deadline exceeded")
        if cond.get("type") == "ReplicaFailure" and cond.get("status") == "True":
            h.bump("critical", f"replica failure: {cond.get('message', '')[:160]}")
    containers = ((spec.get("template") or {}).get("spec") or {}).get("containers") or []
    details = {
        "desired": desired, "ready": ready,
        "updated": status.get("updatedReplicas"), "available": status.get("availableReplicas"),
        "images": [c.get("image") for c in containers],
        "revision": (meta(obj).get("annotations") or {}).get("deployment.kubernetes.io/revision"),
        "selector": (spec.get("selector") or {}).get("matchLabels"),
    }
    return h, details


def analyze_node(node: dict) -> tuple[Health, dict]:
    status, spec = node.get("status") or {}, node.get("spec") or {}
    h = Health(status="Ready")
    for cond in status.get("conditions") or []:
        t, s = cond.get("type"), cond.get("status")
        if t == "Ready" and s != "True":
            h.status = "NotReady"
            h.bump("critical", f"node not ready: {cond.get('reason')}: {cond.get('message', '')[:160]}")
        elif t in ("MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable") and s == "True":
            h.bump("warning", f"{t}: {cond.get('message', '')[:160]}")
    if spec.get("unschedulable"):
        h.status += ",SchedulingDisabled"
        h.bump("warning", "cordoned (unschedulable)")
    info = status.get("nodeInfo") or {}
    labels = meta(node).get("labels") or {}
    roles = [k.split("/", 1)[1] for k in labels if k.startswith("node-role.kubernetes.io/")]
    return h, {
        "roles": roles or ["worker"], "kubelet": info.get("kubeletVersion"), "os": info.get("osImage"),
        "runtime": info.get("containerRuntimeVersion"), "capacity": status.get("capacity"),
        "allocatable": status.get("allocatable"),
        "internalIP": next((a.get("address") for a in status.get("addresses") or [] if a.get("type") == "InternalIP"), None),
    }


def analyze(kind: str, obj: dict) -> tuple[Health, dict]:
    if kind == "Pod":
        return analyze_pod(obj)
    if kind in ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job", "CronJob"):
        return analyze_workload(kind, obj)
    if kind == "Node":
        return analyze_node(obj)
    if kind == "PersistentVolumeClaim":
        phase = (obj.get("status") or {}).get("phase", "Unknown")
        h = Health(status=phase)
        if phase == "Pending":
            h.bump("warning", "claim pending (not bound)")
        elif phase == "Lost":
            h.bump("critical", "claim lost its volume")
        spec = obj.get("spec") or {}
        return h, {"storageClass": spec.get("storageClassName"),
                   "request": ((spec.get("resources") or {}).get("requests") or {}).get("storage"),
                   "capacity": ((obj.get("status") or {}).get("capacity") or {}).get("storage")}
    if kind == "Service":
        spec = obj.get("spec") or {}
        return Health(status=spec.get("type", "ClusterIP")), {
            "type": spec.get("type"), "clusterIP": spec.get("clusterIP"), "selector": spec.get("selector"),
            "ports": [f"{p.get('port')}->{p.get('targetPort')}/{p.get('protocol', 'TCP')}" for p in spec.get("ports") or []],
        }
    if kind == "Ingress":
        spec = obj.get("spec") or {}
        return Health(status="Active"), {"class": spec.get("ingressClassName"),
                                         "hosts": [r.get("host") for r in spec.get("rules") or [] if r.get("host")]}
    if kind == "HorizontalPodAutoscaler":
        spec, status = obj.get("spec") or {}, obj.get("status") or {}
        h = Health(status=f"{status.get('currentReplicas', '?')} replicas")
        if status.get("currentReplicas") and status.get("currentReplicas") >= spec.get("maxReplicas", 10 ** 9):
            h.bump("warning", "at max replicas")
        return h, {"min": spec.get("minReplicas"), "max": spec.get("maxReplicas"),
                   "target": (spec.get("scaleTargetRef") or {}).get("name")}
    if kind == "Namespace":
        return Health(status=(obj.get("status") or {}).get("phase", "Active")), {}
    return Health(status="-"), {}


def summarize(kind: str, obj: dict) -> dict:
    h, details = analyze(kind, obj)
    return {
        "kind": kind, "name": name_of(obj), "namespace": ns_of(obj), "health": h.level, "status": h.status,
        "reasons": h.reasons, "age": age(obj), "labels": meta(obj).get("labels") or {}, "details": details,
    }


def summarize_event(ev: dict) -> dict:
    inv = ev.get("involvedObject") or ev.get("regarding") or {}
    ts = ev.get("lastTimestamp") or ev.get("eventTime") or meta(ev).get("creationTimestamp")
    return {
        "type": ev.get("type"), "reason": ev.get("reason"), "object": f"{inv.get('kind')}/{inv.get('name')}",
        "namespace": inv.get("namespace") or ns_of(ev), "message": (ev.get("message") or ev.get("note") or "")[:400],
        "count": ev.get("count") or (ev.get("series") or {}).get("count") or 1,
        "last_seen": str(ts) if ts else None, "age": age(ts),
    }


# --- topology --------------------------------------------------------------------------------------
def node_id(kind: str, namespace: str | None, name: str) -> str:
    return f"{kind}/{namespace or '_'}/{name}"


def _selector_matches(selector: dict | None, labels: dict | None) -> bool:
    if not selector:
        return False
    labels = labels or {}
    return all(labels.get(k) == v for k, v in selector.items())


def build_topology(objs: dict[str, list[dict]], include_config: bool = False) -> dict:
    """objs maps Kind -> raw objects (one namespace or many). Returns {"nodes": [...], "edges": [...]}."""
    nodes: dict[str, dict] = {}
    edges: dict[str, dict] = {}

    def add_node(kind: str, obj: dict | None, ns: str | None, name: str, **extra) -> str:
        nid = node_id(kind, ns, name)
        if nid not in nodes:
            if obj is not None:
                s = summarize(kind, obj)
            else:
                s = {"kind": kind, "name": name, "namespace": ns, "health": "unknown", "status": "referenced",
                     "reasons": [], "age": "?", "labels": {}, "details": {}}
            s.update(extra)
            s["id"] = nid
            nodes[nid] = s
        return nid

    def add_edge(src: str, dst: str, rel: str) -> None:
        eid = f"{src}->{dst}"
        edges.setdefault(eid, {"id": eid, "source": src, "target": dst, "relation": rel})

    by_kind = {k: objs.get(k) or [] for k in set(TOPOLOGY_KINDS) | {"ConfigMap"}}

    # ReplicaSet -> Deployment and Job -> CronJob maps let us collapse intermediate controllers.
    rs_owner: dict[tuple[str | None, str], str] = {}
    for rs in by_kind["ReplicaSet"]:
        for ref in meta(rs).get("ownerReferences") or []:
            if ref.get("kind") == "Deployment":
                rs_owner[(ns_of(rs), name_of(rs))] = ref.get("name")
    job_owner: dict[tuple[str | None, str], str] = {}
    for job in by_kind["Job"]:
        for ref in meta(job).get("ownerReferences") or []:
            if ref.get("kind") == "CronJob":
                job_owner[(ns_of(job), name_of(job))] = ref.get("name")

    for kind in WORKLOAD_KINDS:
        for obj in by_kind[kind]:
            if kind == "Job" and (ns_of(obj), name_of(obj)) in job_owner:
                cj = job_owner[(ns_of(obj), name_of(obj))]
                add_edge(node_id("CronJob", ns_of(obj), cj), add_node("Job", obj, ns_of(obj), name_of(obj)), "owns")
            else:
                add_node(kind, obj, ns_of(obj), name_of(obj))

    configmaps = {(ns_of(c), name_of(c)) for c in by_kind["ConfigMap"]}
    pvcs = {(ns_of(p), name_of(p)): p for p in by_kind["PersistentVolumeClaim"]}

    for pod in by_kind["Pod"]:
        ns, pname = ns_of(pod), name_of(pod)
        pid = add_node("Pod", pod, ns, pname)
        for ref in meta(pod).get("ownerReferences") or []:
            okind, oname = ref.get("kind"), ref.get("name")
            if okind == "ReplicaSet" and (ns, oname) in rs_owner:
                okind, oname = "Deployment", rs_owner[(ns, oname)]
            if okind in WORKLOAD_KINDS:
                owner = node_id(okind, ns, oname)
                if owner in nodes:
                    add_edge(owner, pid, "owns")
        spec = pod.get("spec") or {}
        for vol in spec.get("volumes") or []:
            claim = (vol.get("persistentVolumeClaim") or {}).get("claimName")
            if claim:
                add_edge(pid, add_node("PersistentVolumeClaim", pvcs.get((ns, claim)), ns, claim), "mounts")
            if include_config:
                cm = (vol.get("configMap") or {}).get("name")
                if cm:
                    cid = add_node("ConfigMap", {"metadata": {"name": cm, "namespace": ns}} if (ns, cm) in configmaps else None, ns, cm)
                    if (ns, cm) not in configmaps:
                        nodes[cid].update(health="critical", status="missing", reasons=["referenced but not found"])
                    add_edge(pid, cid, "uses")
                sec = (vol.get("secret") or {}).get("secretName")
                if sec:
                    add_edge(pid, add_node("Secret", None, ns, sec), "uses")
        if include_config:
            for c in spec.get("containers") or []:
                for src in c.get("envFrom") or []:
                    if (src.get("configMapRef") or {}).get("name"):
                        cm = src["configMapRef"]["name"]
                        cid = add_node("ConfigMap", {"metadata": {"name": cm, "namespace": ns}} if (ns, cm) in configmaps else None, ns, cm)
                        add_edge(pid, cid, "uses")
                    if (src.get("secretRef") or {}).get("name"):
                        add_edge(pid, add_node("Secret", None, ns, src["secretRef"]["name"]), "uses")

    for pvc in by_kind["PersistentVolumeClaim"]:
        add_node("PersistentVolumeClaim", pvc, ns_of(pvc), name_of(pvc))

    for svc in by_kind["Service"]:
        ns = ns_of(svc)
        sid = add_node("Service", svc, ns, name_of(svc))
        selector = (svc.get("spec") or {}).get("selector")
        matched = [p for p in by_kind["Pod"] if ns_of(p) == ns and _selector_matches(selector, meta(p).get("labels"))]
        for p in matched:
            add_edge(sid, node_id("Pod", ns, name_of(p)), "routes")
        if selector:
            ready = [p for p in matched if nodes[node_id("Pod", ns, name_of(p))]["health"] != "critical"
                     and (p.get("status") or {}).get("phase") == "Running"]
            n = nodes[sid]
            if not matched:
                n["health"], n["reasons"] = "warning", ["selector matches no pods"]
            elif not ready:
                n["health"], n["reasons"] = "critical", ["no ready endpoints - traffic will fail"]
            elif len(ready) < len(matched):
                n["health"], n["reasons"] = "warning", [f"{len(ready)}/{len(matched)} endpoints ready"]
            n["details"]["endpoints"] = f"{len(ready)}/{len(matched)}"

    for ing in by_kind["Ingress"]:
        ns = ns_of(ing)
        iid = add_node("Ingress", ing, ns, name_of(ing))
        spec = ing.get("spec") or {}
        backends = []
        if ((spec.get("defaultBackend") or {}).get("service") or {}).get("name"):
            backends.append(spec["defaultBackend"]["service"]["name"])
        for rule in spec.get("rules") or []:
            for path in (rule.get("http") or {}).get("paths") or []:
                svc = ((path.get("backend") or {}).get("service") or {}).get("name")
                if svc:
                    backends.append(svc)
        for svc in dict.fromkeys(backends):
            target = node_id("Service", ns, svc)
            if target not in nodes:
                add_node("Service", None, ns, svc)
                nodes[target].update(health="critical", status="missing", reasons=["ingress backend service not found"])
            add_edge(iid, target, "routes")
            if nodes[target]["health"] in ("critical", "warning"):
                nodes[iid]["health"] = worst(nodes[iid]["health"], nodes[target]["health"])
                nodes[iid]["reasons"].append(f"backend {svc}: {', '.join(nodes[target]['reasons'][:1])}")

    for hpa in by_kind["HorizontalPodAutoscaler"]:
        ns = ns_of(hpa)
        hid = add_node("HorizontalPodAutoscaler", hpa, ns, name_of(hpa))
        ref = (hpa.get("spec") or {}).get("scaleTargetRef") or {}
        target = node_id(ref.get("kind", ""), ns, ref.get("name", ""))
        if target in nodes:
            add_edge(hid, target, "scales")

    return {"nodes": list(nodes.values()), "edges": list(edges.values())}
