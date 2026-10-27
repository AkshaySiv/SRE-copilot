from __future__ import annotations

import asyncio
import fnmatch
from typing import Any

from pydantic import BaseModel, Field, SecretStr

from app.plugins import EmptyArgs, HealthStatus, Plugin, PluginKind, ToolSpec, register_plugin

from .catalog import BUILTIN_DIAGNOSTICS, BUILTIN_RUNBOOKS, CommandSpec


class HostConfig(BaseModel):
    name: str = Field(description="Friendly name, e.g. db-01")
    address: str = Field(description="IP or DNS name")
    port: int = 22
    labels: dict[str, str] = Field(default_factory=dict, description="e.g. role=db, env=prod")
    username: str | None = Field(None, description="Overrides the integration-wide username")


class SSHConfig(BaseModel):
    hosts: list[HostConfig] = Field(default_factory=list, description="Machines this integration can reach")
    username: str = "sre-copilot"
    private_key: SecretStr | None = Field(None, description="PEM/OpenSSH private key",
                                          json_schema_extra={"ui:widget": "textarea"})
    private_key_passphrase: SecretStr | None = None
    password: SecretStr | None = Field(None, description="Used only if no private key is configured")
    known_hosts: str | None = Field(None, description="known_hosts lines for host key verification",
                                    json_schema_extra={"ui:widget": "textarea"})
    verify_host_keys: bool = Field(True, description="Disable only for labs - enables MITM attacks")
    jump_host: str | None = Field(None, description="Optional bastion user@host:port")
    connect_timeout_seconds: int = 10
    extra_diagnostics: list[CommandSpec] = Field(default_factory=list, description="Your own read-only commands")
    runbooks: list[CommandSpec] = Field(default_factory=list,
                                        description="Write commands usable only as approved remediations")
    disable_builtin_runbooks: bool = False
    allow_remediation: bool = False


class HostsArgs(BaseModel):
    selector: str | None = Field(None, description="Host name glob (db-*) or label filter key=value")


class RunDiagnosticArgs(BaseModel):
    host: str = Field(description="Host name from list_hosts")
    diagnostic: str = Field(description="Diagnostic id from list_diagnostics")
    params: dict[str, str] = Field(default_factory=dict, description="Parameters required by the diagnostic")


class FleetArgs(BaseModel):
    diagnostic: str
    selector: str | None = Field(None, description="Host glob or key=value label; omit for all hosts")
    params: dict[str, str] = Field(default_factory=dict)


@register_plugin
class SSHVirtualMachines(Plugin):
    kind = PluginKind.VM
    type = "ssh"
    display_name = "Linux VMs over SSH"
    description = "Linux servers reached over SSH. Only catalogued, parameter-validated commands are executed."
    capabilities = frozenset({"diagnostics", "remediation"})
    Config = SSHConfig

    def __init__(self, name: str, config: SSHConfig):
        super().__init__(name, config)
        self._diagnostics = {d.id: d for d in BUILTIN_DIAGNOSTICS}
        self._diagnostics.update({d.id: d.model_copy(update={"mode": "read"}) for d in config.extra_diagnostics})
        self._runbooks = {} if config.disable_builtin_runbooks else {r.id: r for r in BUILTIN_RUNBOOKS}
        self._runbooks.update({r.id: r.model_copy(update={"mode": "write"}) for r in config.runbooks})

    # ----------------------------------------------------------------------- host selection
    def host(self, name: str) -> HostConfig:
        for h in self.config.hosts:
            if h.name == name or h.address == name:
                return h
        raise LookupError(f"Unknown host '{name}'. Known: {', '.join(h.name for h in self.config.hosts)}")

    def select_hosts(self, selector: str | None) -> list[HostConfig]:
        if not selector:
            return list(self.config.hosts)
        if "=" in selector:
            k, v = selector.split("=", 1)
            return [h for h in self.config.hosts if h.labels.get(k.strip()) == v.strip()]
        return [h for h in self.config.hosts if fnmatch.fnmatch(h.name, selector)]

    @property
    def runbooks(self) -> dict[str, CommandSpec]:
        return self._runbooks

    # ----------------------------------------------------------------------- ssh execution
    def _client_keys(self):
        import asyncssh

        if not self.config.private_key:
            return None
        passphrase = self.config.private_key_passphrase.get_secret_value() if self.config.private_key_passphrase else None
        return [asyncssh.import_private_key(self.config.private_key.get_secret_value(), passphrase)]

    async def _connect(self, host: HostConfig):
        import asyncssh

        c: SSHConfig = self.config
        known_hosts: Any = None
        if c.verify_host_keys:
            known_hosts = asyncssh.import_known_hosts(c.known_hosts) if c.known_hosts else ()
        opts: dict[str, Any] = {
            "username": host.username or c.username, "port": host.port, "known_hosts": known_hosts,
            "connect_timeout": c.connect_timeout_seconds,
        }
        keys = self._client_keys()
        if keys:
            opts["client_keys"] = keys
        elif c.password:
            opts["password"] = c.password.get_secret_value()
            opts["client_keys"] = None
        if c.jump_host:
            jump_user, _, jump = c.jump_host.rpartition("@")
            jhost, _, jport = jump.partition(":")
            tunnel = await asyncssh.connect(jhost, port=int(jport or 22), username=jump_user or opts["username"],
                                            known_hosts=known_hosts, client_keys=opts.get("client_keys"),
                                            password=opts.get("password"), connect_timeout=c.connect_timeout_seconds)
            opts["tunnel"] = tunnel
        return await asyncssh.connect(host.address, **opts)

    async def execute(self, host: HostConfig, command: str, timeout: int) -> dict:
        try:
            async with await self._connect(host) as conn:
                result = await asyncio.wait_for(conn.run(command, check=False), timeout=timeout)
                return {"host": host.name, "exit_code": result.exit_status,
                        "stdout": (result.stdout or "")[-20000:], "stderr": (result.stderr or "")[-4000:]}
        except asyncio.TimeoutError:
            return {"host": host.name, "error": f"timed out after {timeout}s"}
        except Exception as exc:  # noqa: BLE001
            return {"host": host.name, "error": f"{type(exc).__name__}: {exc}"}

    async def run_diagnostic(self, host_name: str, diagnostic: str, params: dict[str, str] | None = None) -> dict:
        spec = self._diagnostics.get(diagnostic)
        if not spec:
            raise LookupError(f"Unknown diagnostic '{diagnostic}'. Use list_diagnostics.")
        return await self.execute(self.host(host_name), spec.render(params), spec.timeout_seconds)

    async def run_runbook(self, host_name: str, runbook: str, params: dict[str, str] | None, dry_run: bool) -> dict:
        if not self.config.allow_remediation:
            raise PermissionError(f"Remediation disabled for VM integration '{self.name}'")
        spec = self._runbooks.get(runbook)
        if not spec:
            raise LookupError(f"Unknown runbook '{runbook}'")
        command = spec.render(params)
        host = self.host(host_name)
        if dry_run:
            probe = await self.execute(host, "true", 15)  # proves connectivity & auth; command is not run
            return {"dry_run": True, "host": host.name, "command": command, "connectivity": "error" not in probe,
                    **({"error": probe["error"]} if "error" in probe else {})}
        return await self.execute(host, command, spec.timeout_seconds)

    # ----------------------------------------------------------------------- plugin API
    async def health(self) -> HealthStatus:
        if not self.config.hosts:
            return HealthStatus(False, "No hosts configured")
        results = await asyncio.gather(*(self.execute(h, "uname -sr", 15) for h in self.config.hosts[:10]))
        ok = [r for r in results if "error" not in r]
        bad = {r["host"]: r["error"] for r in results if "error" in r}
        return HealthStatus(not bad, f"{len(ok)}/{len(results)} hosts reachable", {"errors": bad})

    def context_hint(self) -> str:
        hosts = ", ".join(f"{h.name}({','.join(f'{k}={v}' for k, v in h.labels.items())})" for h in self.config.hosts[:30])
        return f"Linux VMs over SSH: {hosts or 'none configured'}"

    def tools(self) -> list[ToolSpec]:
        async def list_hosts(selector: str | None = None):
            return [{"name": h.name, "address": h.address, "labels": h.labels} for h in self.select_hosts(selector)]

        async def list_diagnostics():
            return [{"id": d.id, "description": d.description,
                     "params": [{"name": p.name, "description": p.description, "default": p.default} for p in d.params]}
                    for d in self._diagnostics.values()]

        async def run_diagnostic(host: str, diagnostic: str, params: dict[str, str] | None = None):
            return await self.run_diagnostic(host, diagnostic, params)

        async def run_on_fleet(diagnostic: str, selector: str | None = None, params: dict[str, str] | None = None):
            hosts = self.select_hosts(selector)[:25]
            return await asyncio.gather(*(self.run_diagnostic(h.name, diagnostic, params) for h in hosts))

        return [
            ToolSpec("list_hosts", "List configured VMs with labels.", HostsArgs, list_hosts, "Listed VMs", "vm"),
            ToolSpec("list_diagnostics", "List the read-only diagnostics that can be run on VMs.", EmptyArgs,
                     list_diagnostics, "Looked up available VM checks", "vm"),
            ToolSpec("run_diagnostic", "Run one catalogued read-only diagnostic on a VM (no free-form commands).",
                     RunDiagnosticArgs, run_diagnostic, "Ran '{diagnostic}' on {host}", "vm"),
            ToolSpec("run_on_fleet", "Run one diagnostic on many VMs at once (max 25).", FleetArgs, run_on_fleet,
                     "Ran '{diagnostic}' across VMs {selector}", "vm"),
        ]
