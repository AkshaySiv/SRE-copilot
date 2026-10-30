"""Rule-based "why is this failing" for the cluster map: one exact sentence per problem, with the evidence.

No LLM involved - every insight comes from the pod spec/status, its events and (for crashes) the last log
lines, so it is instant and exact. When no rule explains a failure (e.g. an app-specific exit code), the UI
offers an on-demand AI explanation built on the same evidence (see `explain_context`).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from app.timeline.logparse import find_errors

from . import model as m

if TYPE_CHECKING:
    from .base import KubernetesConnector


@dataclass
class Insight:
    title: str                       # the one-liner shown under "Why it is critical"
    category: str                    # config | image | resources | scheduling | probe | crash | storage | node | network
    evidence: list[str] = field(default_factory=list)
    fix: str | None = None
    confidence: str = "high"         # high = read directly from Kubernetes; medium = inferred
    subject: str | None = None       # e.g. "Pod backend-7f9c-x2" when diagnosing a workload


# --------------------------------------------------------------------------------------------- helpers
_NOT_FOUND = re.compile(r'(secret|configmap)s? "([^"]+)" not found', re.I)
_MISSING_KEY = re.compile(r"couldn'?t find key (\S+) in (Secret|ConfigMap) ([\w.-]+)/([\w.-]+)", re.I)
_PVC_NOT_FOUND = re.compile(r'persistentvolumeclaim "([^"]+)" not found', re.I)
_PROBE = re.compile(r"(Readiness|Liveness|Startup) probe failed: (.*)", re.I)
_SCHED_REASONS = [
    (re.compile(r"Insufficient (cpu|memory|ephemeral-storage|nvidia\.com/gpu|[\w./-]+)", re.I), "not enough {0}"),
    (re.compile(r"untolerated taint \{([^:}]+)", re.I), "nodes have the taint {0}"),
    (re.compile(r"didn'?t match Pod'?s node affinity/selector", re.I), "no node matches its nodeSelector/affinity"),
    (re.compile(r"volume node affinity conflict", re.I), "its volume is bound to a node it cannot run on"),
    (re.compile(r"unbound immediate PersistentVolumeClaims", re.I), "its PersistentVolumeClaim is not bound"),
    (re.compile(r"didn'?t have free ports", re.I), "the host port is already used"),
    (re.compile(r"Too many pods", re.I), "nodes are at their pod limit"),
]
_CONN = re.compile(r"connection refused|could not connect|can'?t connect|no such host|name or service not known|"
                   r"timed? ?out|unreachable|ECONNREFUSED|no route to host|connection reset", re.I)
_LEVEL = re.compile(r"^\s*(\[?(ERROR|FATAL|CRITICAL|CRIT|SEVERE|PANIC|E)\]?[:\s]+|level=\w+\s+)", re.I)
_HOST = re.compile(r"(?:\bserver at|\bhost[=:]|//(?:[^@/\s]+@)?|\bdial tcp|\bconnect(?:ing)? to|\bupstream)\s*\"?"
                   r"([a-z][a-z0-9-]*(?:\.[a-z0-9-]+)*)|\b([a-z][a-z0-9-]*(?:\.[a-z0-9-]+)*):\d{2,5}\b", re.I)
_EXIT_CODES = {
    126: "the command is not executable (permission problem)",
    127: "the command/binary was not found in the image",
    128: "invalid exit argument",
    134: "the process aborted (SIGABRT)",
    137: "the process was killed (SIGKILL - out of memory or a failed liveness probe)",
    139: "the process crashed with a segmentation fault (SIGSEGV)",
    143: "the process was terminated (SIGTERM)",
}


def _refs(pod: dict, kind: str, name: str) -> list[str]:
    """Where a Secret/ConfigMap is referenced in the pod spec, e.g. ['env DB_PASSWORD (container api)']."""
    spec = pod.get("spec") or {}
    key_ref = "secretKeyRef" if kind == "secret" else "configMapKeyRef"
    from_ref = "secretRef" if kind == "secret" else "configMapRef"
    out = []
    for c in (spec.get("initContainers") or []) + (spec.get("containers") or []):
        for e in c.get("env") or []:
            ref = (e.get("valueFrom") or {}).get(key_ref) or {}
            if ref.get("name") == name:
                out.append(f"env {e.get('name')} (container {c.get('name')})")
        for ef in c.get("envFrom") or []:
            if (ef.get(from_ref) or {}).get("name") == name:
                out.append(f"envFrom (container {c.get('name')})")
    for v in spec.get("volumes") or []:
        if kind == "secret" and (v.get("secret") or {}).get("secretName") == name:
            out.append(f"volume {v.get('name')}")
        if kind == "configmap" and (v.get("configMap") or {}).get("name") == name:
            out.append(f"volume {v.get('name')}")
        for src in (v.get("projected") or {}).get("sources") or []:
            if (src.get("secret" if kind == "secret" else "configMap") or {}).get("name") == name:
                out.append(f"volume {v.get('name')}")
    for ps in spec.get("imagePullSecrets") or []:
        if kind == "secret" and ps.get("name") == name:
            out.append("imagePullSecrets")
    return out


def _container_spec(pod: dict, cname: str) -> dict:
    spec = pod.get("spec") or {}
    return next((c for c in (spec.get("initContainers") or []) + (spec.get("containers") or [])
                 if c.get("name") == cname), {})


def _missing_ref(pod: dict, message: str) -> Insight | None:
    mk = _MISSING_KEY.search(message)
    if mk:
        key, kind, _ns, name = mk.groups()
        where = _refs(pod, kind.lower(), name)
        return Insight(f"{kind} `{name}` exists but has no key `{key}`" + (f" (used by {where[0]})" if where else ""),
                       "config", [message], f"Add the key `{key}` to {kind} `{name}`, or fix the key name in the pod spec.")
    nf = _NOT_FOUND.search(message)
    if nf:
        kind, name = nf.group(1).lower(), nf.group(2)
        label = "Secret" if kind == "secret" else "ConfigMap"
        where = _refs(pod, kind, name)
        return Insight(f"Missing {label} `{name}`" + (f" - referenced by {', '.join(where[:2])}" if where else ""),
                       "config", [message], f"Create {label} `{name}` in this namespace (or fix the name in the "
                                            f"workload spec), then the pod starts on its next retry.")
    pvc = _PVC_NOT_FOUND.search(message)
    if pvc:
        return Insight(f"Missing PersistentVolumeClaim `{pvc.group(1)}`", "storage", [message],
                       f"Create PVC `{pvc.group(1)}` or fix the claimName in the volume definition.")
    return None


def _image_insight(cs: dict, pod: dict, message: str) -> Insight:
    image = cs.get("image") or _container_spec(pod, cs.get("name", "")).get("image") or "?"
    low = message.lower()
    if any(s in low for s in ("manifest unknown", "not found", "manifest for", "no such manifest")):
        return Insight(f"Image `{image}` does not exist in the registry (wrong tag or name)", "image", [message[:300]],
                       "Fix the image tag, or push the image to the registry.")
    if any(s in low for s in ("unauthorized", "authentication required", "denied", "forbidden", "401", "403")):
        secrets = [p.get("name") for p in (pod.get("spec") or {}).get("imagePullSecrets") or []]
        extra = f" (imagePullSecrets: {', '.join(secrets)})" if secrets else " and the pod has no imagePullSecrets"
        return Insight(f"Registry refused to serve `{image}` - authentication failed{extra}", "image", [message[:300]],
                       "Add or fix the imagePullSecret for this registry.")
    if any(s in low for s in ("no such host", "i/o timeout", "dial tcp", "connection refused", "tls", "x509")):
        return Insight(f"Can't reach the registry for `{image}` (network/DNS/TLS)", "image", [message[:300]],
                       "Check that the nodes can reach the registry (DNS, proxy, firewall, CA certificates).")
    return Insight(f"Can't pull image `{image}`", "image", [message[:300]] if message else [],
                   "Check the image name, tag and registry credentials.", "medium")


def _mem_limit(pod: dict, cname: str) -> str | None:
    return ((_container_spec(pod, cname).get("resources") or {}).get("limits") or {}).get("memory")


async def _log_error(k: KubernetesConnector, pod: dict, cname: str, previous: bool) -> str | None:
    try:
        text = await k.pod_logs(m.name_of(pod), m.ns_of(pod), cname, 120, previous=previous)
    except Exception:  # noqa: BLE001 - logs are best effort
        return None
    findings = find_errors(text or "", max_findings=6)
    if not findings:
        return None
    # a stack trace names the real exception; otherwise the first error of the run is usually the cause and
    # later lines ("FATAL startup aborted") are consequences
    traced = [f for f in findings if f.trace]
    return _LEVEL.sub("", (traced[-1] if traced else findings[0]).message)[:240]


async def _upstream(k: KubernetesConnector, ns: str, err: str) -> Insight | None:
    """For 'connection refused to postgres:5432' - find that Service and say why it has no healthy pods."""
    hosts = list(dict.fromkeys(h for pair in _HOST.findall(err) for h in pair if h and h.lower() != "server"))
    for host in hosts[:3]:
        parts = host.split(".")
        svc_name, svc_ns = parts[0], parts[1] if len(parts) > 1 and parts[1] not in ("svc", "cluster") else ns
        try:
            svc = await k.get_object("Service", svc_name, svc_ns)
        except Exception:  # noqa: BLE001
            svc = None
        if not svc:
            continue
        sel = (svc.get("spec") or {}).get("selector") or {}
        pods = await k.list_objects("Pod", svc_ns, ",".join(f"{a}={b}" for a, b in sel.items())) if sel else []
        bad = [p for p in pods if m.summarize("Pod", p)["health"] != "healthy"]
        if pods and len(bad) < len(pods):
            return None  # the dependency looks healthy - nothing more to say
        if not pods:
            return Insight(f"Its dependency, Service `{svc_name}`, has no pods behind it", "network",
                           [f"selector {sel}"], f"Start the `{svc_name}` workload or fix its selector.", "medium")
        why = await diagnose_pod(k, bad[0], depth=1)
        reason = why[0].title if why else m.summarize("Pod", bad[0])["status"]
        return Insight(f"Likely root cause upstream: `{svc_name}` is down - {m.name_of(bad[0])}: {reason}",
                       why[0].category if why else "network", [f"{len(bad)}/{len(pods)} pods of {svc_name} unhealthy"],
                       why[0].fix if why else f"Fix `{svc_name}` first; this workload recovers on its own.",
                       "medium")
    return None


# --------------------------------------------------------------------------------------------- pods
async def diagnose_pod(k: KubernetesConnector, pod: dict, events: list[dict] | None = None,
                       depth: int = 0) -> list[Insight]:
    name, ns = m.name_of(pod), m.ns_of(pod)
    status = pod.get("status") or {}
    if events is None:
        events = await k.list_events(ns, object_name=name, limit=40)
    ev_msgs = [(e.get("reason") or "", e.get("message") or "") for e in events if e.get("type") == "Warning"]
    out: list[Insight] = []
    seen: set[str] = set()

    def add(i: Insight | None) -> None:
        if i and i.title not in seen:
            seen.add(i.title)
            out.append(i)

    if status.get("reason") == "Evicted":
        add(Insight(f"Evicted: {status.get('message') or 'the node ran out of resources'}", "node",
                    [status.get("message") or ""], "Free resources on the node (or move the workload); the controller "
                                                   "recreates the pod elsewhere."))

    for cs in (status.get("initContainerStatuses") or []) + (status.get("containerStatuses") or []):
        cname = cs.get("name", "?")
        state, last = cs.get("state") or {}, cs.get("lastState") or {}
        waiting = state.get("waiting") or {}
        wreason, wmsg = waiting.get("reason") or "", waiting.get("message") or ""
        if wreason in ("CreateContainerConfigError", "CreateContainerError", "RunContainerError"):
            add(_missing_ref(pod, wmsg) or Insight(f"Container `{cname}` can't be created: {wmsg[:200] or wreason}",
                                                   "config", [wmsg] if wmsg else [], None, "medium"))
        elif wreason in ("ImagePullBackOff", "ErrImagePull", "InvalidImageName", "ErrImageNeverPull"):
            msg = wmsg or next((mm for r, mm in ev_msgs if r in ("Failed", "ErrImagePull")), "")
            add(_image_insight(cs, pod, msg))
        terminated = state.get("terminated") or {}
        crashing = wreason == "CrashLoopBackOff" or (terminated and terminated.get("reason") not in (None, "Completed"))
        if crashing:
            term = terminated or last.get("terminated") or {}
            code = term.get("exitCode")
            if term.get("reason") == "OOMKilled":
                limit = _mem_limit(pod, cname)
                add(Insight(f"Out of memory: `{cname}` exceeds its {limit or 'memory'} limit and is killed "
                            f"({cs.get('restartCount', 0)} restarts)", "resources",
                            [f"lastState: OOMKilled (exit {code})"],
                            f"Raise the memory limit{f' above {limit}' if limit else ''} or reduce the app's memory use."))
                continue
            err = await _log_error(k, pod, cname, previous=not terminated)
            if err:
                if _CONN.search(err):
                    add(Insight(f"`{cname}` can't reach a dependency: {err}", "network", [f"exit code {code}", err],
                                "Check the service it connects to - the root cause is most likely there.", "high"))
                    if depth == 0:
                        add(await _upstream(k, ns, err))
                else:
                    add(Insight(f"`{cname}` crashes on start: {err}", "crash", [f"exit code {code}", err],
                                "Fix the error above (often configuration from the last change), or roll back.",
                                "high"))
            else:
                meaning = _EXIT_CODES.get(int(code)) if isinstance(code, int) else None
                add(Insight(f"`{cname}` exits with code {code}" + (f" - {meaning}" if meaning else
                                                                   " (application-specific; no error in the logs)"),
                            "crash", [f"exit code {code}", f"reason {term.get('reason')}"], None,
                            "high" if meaning else "medium"))
        elif last.get("terminated", {}).get("reason") == "OOMKilled":
            limit = _mem_limit(pod, cname)
            add(Insight(f"Was OOMKilled: `{cname}` hit its {limit or 'memory'} limit", "resources",
                        ["lastState: OOMKilled"], f"Raise the memory limit{f' above {limit}' if limit else ''}.",
                        "high"))
        elif state.get("running") and not cs.get("ready"):
            probe = next((mm for r, mm in ev_msgs if r == "Unhealthy"), None)
            if probe:
                pm = _PROBE.search(probe)
                kind, why = (pm.group(1), pm.group(2)) if pm else ("Readiness", probe)
                c = _container_spec(pod, cname)
                http = ((c.get(f"{kind.lower()}Probe") or {}).get("httpGet") or {})
                target = f"GET {http.get('path')} :{http.get('port')}" if http else "the probe"
                add(Insight(f"{kind} check failing: {target} → {why[:160]}", "probe", [probe],
                            "The app is running but not healthy - check its logs and dependencies."))

    phase = status.get("phase")
    if phase == "Pending":
        sched = next((c for c in status.get("conditions") or [] if c.get("type") == "PodScheduled"
                      and c.get("status") == "False"), None)
        msg = (sched or {}).get("message") or next((mm for r, mm in ev_msgs if r == "FailedScheduling"), "")
        if msg:
            why = []
            for rx, tmpl in _SCHED_REASONS:
                mt = rx.search(msg)
                if mt:
                    why.append(tmpl.format(*mt.groups()) if mt.groups() else tmpl)
            add(Insight("Can't be scheduled: " + ("; ".join(dict.fromkeys(why)) if why else msg[:200]), "scheduling",
                        [msg], "Free capacity / fix the constraint shown above, or adjust requests and affinity."))
        for r, mm in ev_msgs:
            if r in ("FailedMount", "FailedAttachVolume"):
                add(_missing_ref(pod, mm) or Insight(f"Volume can't be mounted: {mm[:200]}", "storage", [mm]))
    return out


# --------------------------------------------------------------------------------------------- other kinds
async def _workload(k: KubernetesConnector, kind: str, obj: dict) -> list[Insight]:
    ns = m.ns_of(obj)
    sel = ((obj.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
    if not sel:
        return []
    pods = await k.list_objects("Pod", ns, ",".join(f"{a}={b}" for a, b in sel.items()))
    bad = [p for p in pods if m.summarize("Pod", p)["health"] in ("warning", "critical")]
    out: list[Insight] = []
    titles: set[str] = set()
    for p in bad[:5]:
        for i in await diagnose_pod(k, p):
            if i.title not in titles:  # the same cause on every replica is reported once
                titles.add(i.title)
                i.subject = f"{len(bad)} of {len(pods)} pods" if len(bad) > 1 else f"Pod {m.name_of(p)}"
                out.append(i)
    if not pods:
        out.append(Insight(f"{kind} has no pods", "scheduling", [], "Check the ReplicaSet events and quotas.", "medium"))
    return out


async def _service(k: KubernetesConnector, svc: dict) -> list[Insight]:
    ns = m.ns_of(svc)
    sel = (svc.get("spec") or {}).get("selector") or {}
    if not sel:
        return []
    pods = await k.list_objects("Pod", ns)
    matched = [p for p in pods if all((m.meta(p).get("labels") or {}).get(a) == b for a, b in sel.items())]
    if not matched:
        hints = []
        for a, b in sel.items():
            values = sorted({(m.meta(p).get("labels") or {}).get(a) for p in pods} - {None})
            if values:
                hints.append(f"pods use {a}={', '.join(values[:4])}")
        sel_txt = ",".join(f"{a}={b}" for a, b in sel.items())
        return [Insight(f"Selector `{sel_txt}` matches no pods" + (f" - {'; '.join(hints)}" if hints else ""),
                        "network", [f"selector {sel_txt}", f"{len(pods)} pods in namespace"],
                        "Fix the Service selector (or the pod labels) so they match.")]
    ready = [p for p in matched if m.summarize("Pod", p)["health"] == "healthy"]
    if not ready:
        names = ", ".join(sorted({m.name_of(p).rsplit("-", 2)[0] for p in matched}))
        return [Insight(f"All {len(matched)} backing pods ({names}) are unhealthy - see their cause", "network",
                        [f"0/{len(matched)} endpoints ready"], None, "high")]
    return []


async def _pvc(k: KubernetesConnector, pvc: dict) -> list[Insight]:
    if ((pvc.get("status") or {}).get("phase")) == "Bound":
        return []
    events = await k.list_events(m.ns_of(pvc), object_name=m.name_of(pvc), limit=20)
    msg = next((e["message"] for e in events if e.get("type") == "Warning"), "")
    sc = (pvc.get("spec") or {}).get("storageClassName")
    return [Insight(f"Volume not provisioned{f' (StorageClass `{sc}`)' if sc else ''}" + (f": {msg[:160]}" if msg else ""),
                    "storage", [msg] if msg else [], "Check the StorageClass and the provisioner.",
                    "high" if msg else "medium")]


def _node(node: dict) -> list[Insight]:
    out = []
    for c in (node.get("status") or {}).get("conditions") or []:
        t, s = c.get("type"), c.get("status")
        if t == "Ready" and s != "True":
            out.append(Insight(f"Node not ready: {c.get('message') or c.get('reason') or 'kubelet stopped reporting'}",
                               "node", [str(c.get("reason"))], "Check the kubelet and the machine itself."))
        elif t in ("DiskPressure", "MemoryPressure", "PIDPressure") and s == "True":
            out.append(Insight(f"{t}: {c.get('message') or c.get('reason')} - pods may be evicted", "node",
                               [str(c.get("reason"))], "Free disk/memory on the node (old images, logs, emptyDir).",
                               "high"))
    return out


async def diagnose(k: KubernetesConnector, kind: str, name: str, namespace: str | None) -> dict[str, Any]:
    info = k.kinds.resolve(kind)
    obj = await k.get_object(info.kind, name, namespace)
    if obj is None:
        return {"insights": [], "health": "unknown"}
    summary = m.summarize(info.kind, obj)
    # The rules return nothing for healthy objects, so they run for every kind (a Service's health is only
    # known from its endpoints, which `summarize` does not see).
    insights: list[Insight] = []
    if info.kind == "Pod":
        insights = await diagnose_pod(k, obj)
    elif info.kind in ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"):
        insights = await _workload(k, info.kind, obj)
    elif info.kind == "Service":
        insights = await _service(k, obj)
    elif info.kind == "PersistentVolumeClaim":
        insights = await _pvc(k, obj)
    elif info.kind == "Node":
        insights = _node(obj)
    return {"health": summary["health"], "insights": [asdict(i) for i in insights]}


async def explain_context(k: KubernetesConnector, kind: str, name: str, namespace: str | None) -> str:
    """Evidence for the on-demand AI explanation: status, warnings, the relevant spec and the last log lines."""
    info = k.kinds.resolve(kind)
    obj = await k.get_object(info.kind, name, namespace)
    if obj is None:
        return ""
    s = m.summarize(info.kind, obj)
    parts = [f"{info.kind} {namespace or ''}/{name}: health={s['health']} status={s['status']}",
             "Reasons: " + "; ".join(s["reasons"])]
    events = await k.list_events(namespace, object_name=name, limit=15)
    parts.append("Recent events:\n" + "\n".join(f"- {e['type']} {e['reason']} x{e['count']}: {e['message']}"
                                               for e in events[:12]))
    pod = obj
    if info.kind != "Pod":
        sel = ((obj.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
        pods = await k.list_objects("Pod", namespace, ",".join(f"{a}={b}" for a, b in sel.items())) if sel else []
        pod = next((p for p in pods if m.summarize("Pod", p)["health"] != "healthy"), None)
    if pod:
        spec = pod.get("spec") or {}
        for c in spec.get("containers") or []:
            env = [e.get("name") + ("=<from " + next(iter(e["valueFrom"])) + ">" if e.get("valueFrom")
                                    else "=" + str(e.get("value", ""))[:60]) for e in c.get("env") or []]
            parts.append(f"Container {c.get('name')} image={c.get('image')} command={c.get('command')} "
                         f"args={c.get('args')} env=[{', '.join(env)}] resources={c.get('resources')}")
        parts.append(f"Container statuses: {(pod.get('status') or {}).get('containerStatuses')}")
        for c in spec.get("containers") or []:
            for previous in (True, False):
                try:
                    text = await k.pod_logs(m.name_of(pod), m.ns_of(pod), c.get("name"), 60, previous=previous)
                except Exception:  # noqa: BLE001
                    continue
                if text and not text.startswith("<"):
                    parts.append(f"Last log lines of {c.get('name')} ({'previous' if previous else 'current'} run):\n"
                                 f"{text[-4000:]}")
                    break
    return "\n\n".join(parts)
