from __future__ import annotations

from pydantic import BaseModel, Field

from app.connectors.vm.ssh import SSHVirtualMachines
from app.plugins import PluginKind

from .base import RemediationAction, register_action


class RunbookParams(BaseModel):
    host: str = Field(description="Host name")
    runbook: str = Field(description="Runbook id configured on the VM integration, e.g. restart_service")
    params: dict[str, str] = Field(default_factory=dict)


@register_action
class RunVMRunbook(RemediationAction):
    name = "run_vm_runbook"
    title = "Run VM runbook"
    description = "Run an admin-defined runbook on a VM (e.g. restart_service unit=nginx, vacuum_journal). " \
                  "Only runbooks configured on the integration can be used."
    target_kind = PluginKind.VM
    risk = "high"
    Params = RunbookParams

    def plan(self, p: RunbookParams):
        return {"summary": f"Run runbook '{p.runbook}' on {p.host}",
                "changes": [f"runbook {p.runbook} with {p.params or 'no params'}"],
                "impact": "Executes a privileged command on the host; the exact command is shown after dry-run.",
                "rollback": "Depends on the runbook - review the command before approving."}

    async def dry_run(self, vm: SSHVirtualMachines, p: RunbookParams):
        return await vm.run_runbook(p.host, p.runbook, p.params, dry_run=True)

    async def execute(self, vm: SSHVirtualMachines, p: RunbookParams):
        return await vm.run_runbook(p.host, p.runbook, p.params, dry_run=False)
