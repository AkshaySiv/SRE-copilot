from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.connectors.kubernetes.base import KubernetesConnector
from app.plugins import PluginKind

from .base import RemediationAction, register_action


class WorkloadRef(BaseModel):
    kind: Literal["Deployment", "StatefulSet", "DaemonSet"] = "Deployment"
    name: str
    namespace: str


class ScaleParams(WorkloadRef):
    kind: Literal["Deployment", "StatefulSet"] = "Deployment"
    replicas: int = Field(ge=0, le=500)


class PodRef(BaseModel):
    name: str
    namespace: str


class RollbackParams(BaseModel):
    name: str = Field(description="Deployment name")
    namespace: str
    to_revision: int | None = Field(None, description="Omit for the previous revision")


class NodeRef(BaseModel):
    name: str


class _K8sAction(RemediationAction):
    target_kind = PluginKind.KUBERNETES


@register_action
class RestartWorkload(_K8sAction):
    name = "restart_workload"
    title = "Rolling restart"
    description = "Rolling restart of a Deployment/StatefulSet/DaemonSet (like kubectl rollout restart). " \
                  "Helps for stuck connections, leaked memory, stale config/secrets. Does not fix bad config."
    risk = "medium"
    Params = WorkloadRef

    def plan(self, p: WorkloadRef):
        return {"summary": f"Restart all pods of {p.kind} {p.namespace}/{p.name} one by one",
                "changes": [f"Annotate pod template of {p.kind}/{p.name} with restartedAt=<now>"],
                "impact": "Pods are replaced gradually per the rollout strategy; brief capacity reduction.",
                "rollback": "Not needed - the pod spec is unchanged apart from an annotation."}

    async def dry_run(self, k: KubernetesConnector, p: WorkloadRef):
        return await k.restart_workload(p.kind, p.name, p.namespace, dry_run=True)

    async def execute(self, k: KubernetesConnector, p: WorkloadRef):
        return await k.restart_workload(p.kind, p.name, p.namespace, dry_run=False)


@register_action
class ScaleWorkload(_K8sAction):
    name = "scale_workload"
    title = "Scale workload"
    description = "Change replica count of a Deployment/StatefulSet (e.g. add capacity under load). " \
                  "If an HPA manages the workload, it may override this."
    Params = ScaleParams

    def risk_for(self, p: ScaleParams):
        return "high" if p.replicas == 0 else "medium"

    def plan(self, p: ScaleParams):
        return {"summary": f"Scale {p.kind} {p.namespace}/{p.name} to {p.replicas} replicas",
                "changes": [f"spec.replicas = {p.replicas}"],
                "impact": "Scaling to 0 makes the service unavailable." if p.replicas == 0 else
                "Adds/removes pods; check namespace quota and node capacity.",
                "rollback": "Scale back to the previous replica count."}

    async def dry_run(self, k: KubernetesConnector, p: ScaleParams):
        return await k.scale_workload(p.kind, p.name, p.namespace, p.replicas, dry_run=True)

    async def execute(self, k: KubernetesConnector, p: ScaleParams):
        return await k.scale_workload(p.kind, p.name, p.namespace, p.replicas, dry_run=False)


@register_action
class DeletePod(_K8sAction):
    name = "delete_pod"
    title = "Delete (recycle) pod"
    description = "Delete a single pod so its controller recreates it. For a stuck or wedged pod."
    risk = "low"
    Params = PodRef

    def plan(self, p: PodRef):
        return {"summary": f"Delete pod {p.namespace}/{p.name}; its controller will recreate it",
                "changes": [f"DELETE Pod/{p.name}"],
                "impact": "One replica briefly unavailable. Standalone pods (no controller) are NOT recreated.",
                "rollback": "Not applicable."}

    async def dry_run(self, k: KubernetesConnector, p: PodRef):
        pod = await k.get_object("Pod", p.name, p.namespace)
        result = await k.delete_pod(p.name, p.namespace, dry_run=True)
        result["has_controller"] = bool((pod or {}).get("metadata", {}).get("ownerReferences"))
        if not result["has_controller"]:
            result["warning"] = "Pod has no controller and will not be recreated"
        return result

    async def execute(self, k: KubernetesConnector, p: PodRef):
        return await k.delete_pod(p.name, p.namespace, dry_run=False)


@register_action
class RollbackDeployment(_K8sAction):
    name = "rollback_deployment"
    title = "Roll back deployment"
    description = "Revert a Deployment to a previous revision's pod template (like kubectl rollout undo). " \
                  "The right fix when a recent rollout introduced the failure."
    risk = "medium"
    Params = RollbackParams

    def plan(self, p: RollbackParams):
        target = f"revision {p.to_revision}" if p.to_revision else "the previous revision"
        return {"summary": f"Roll back Deployment {p.namespace}/{p.name} to {target}",
                "changes": ["Replace spec.template with the pod template of the target ReplicaSet"],
                "impact": "Pods are replaced with the older version. If you deploy via Helm/Argo CD/Fleet, also "
                          "revert there or the next sync will re-apply the broken version.",
                "rollback": "Roll back again to the current revision, or redeploy from your pipeline."}

    async def dry_run(self, k: KubernetesConnector, p: RollbackParams):
        return await k.rollback_deployment(p.name, p.namespace, p.to_revision, dry_run=True)

    async def execute(self, k: KubernetesConnector, p: RollbackParams):
        return await k.rollback_deployment(p.name, p.namespace, p.to_revision, dry_run=False)


@register_action
class CordonNode(_K8sAction):
    name = "cordon_node"
    title = "Cordon node"
    description = "Mark a node unschedulable so no new pods land on it (existing pods keep running)."
    Params = NodeRef

    def plan(self, p: NodeRef):
        return {"summary": f"Cordon node {p.name}", "changes": ["spec.unschedulable = true"],
                "impact": "Reduces schedulable cluster capacity.", "rollback": f"Uncordon node {p.name}."}

    async def dry_run(self, k: KubernetesConnector, p: NodeRef):
        return await k.set_node_schedulable(p.name, False, dry_run=True)

    async def execute(self, k: KubernetesConnector, p: NodeRef):
        return await k.set_node_schedulable(p.name, False, dry_run=False)


@register_action
class UncordonNode(_K8sAction):
    name = "uncordon_node"
    title = "Uncordon node"
    description = "Make a cordoned node schedulable again."
    risk = "low"
    Params = NodeRef

    def plan(self, p: NodeRef):
        return {"summary": f"Uncordon node {p.name}", "changes": ["spec.unschedulable = false"],
                "impact": "New pods may be scheduled on the node.", "rollback": f"Cordon node {p.name}."}

    async def dry_run(self, k: KubernetesConnector, p: NodeRef):
        return await k.set_node_schedulable(p.name, True, dry_run=True)

    async def execute(self, k: KubernetesConnector, p: NodeRef):
        return await k.set_node_schedulable(p.name, True, dry_run=False)
