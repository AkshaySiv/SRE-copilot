from __future__ import annotations

import pytest

from app.connectors.kubernetes import model as m
from app.connectors.kubernetes.demo import DemoConfig, DemoKubernetes
from app.connectors.vm.catalog import BUILTIN_DIAGNOSTICS, CommandParam, CommandSpec
from app.core.security import redact
from app.plugins import registry, secret_fields


def test_registry_has_builtin_plugins_and_secret_detection():
    registry.load()
    types = {(p["kind"], p["type"]) for p in registry.describe()}
    assert {("llm", "openai_compatible"), ("kubernetes", "cluster"), ("vm", "ssh"), ("monitoring", "prometheus"),
            ("monitoring", "loki"), ("monitoring", "elasticsearch")} <= types
    assert secret_fields(registry.get("kubernetes", "cluster").Config) == ["kubeconfig", "token"]


def test_vm_command_params_are_validated_and_quoted():
    status = next(d for d in BUILTIN_DIAGNOSTICS if d.id == "service_status")
    assert status.render({"unit": "nginx"}).startswith("systemctl status nginx")
    with pytest.raises(ValueError):
        status.render({"unit": "nginx; rm -rf /"})
    with pytest.raises(ValueError):
        status.render({"unit": "nginx", "extra": "x"})
    spec = CommandSpec(id="echo_it", description="d", command="echo {v}",
                       params=[CommandParam(name="v", pattern=r"^.{1,50}$")])
    assert spec.render({"v": "a b'c"}) == "echo 'a b'\"'\"'c'"


def test_redaction():
    text = 'password=hunter22 Authorization: Bearer abc.def.ghi postgres://u:secretpw@db:5432 token: "xyz12345"'
    out = redact(text)
    for leaked in ("hunter22", "abc.def.ghi", "secretpw", "xyz12345"):
        assert leaked not in out


def test_secrets_kind_is_forbidden():
    with pytest.raises(PermissionError):
        m.KindRegistry().resolve("secrets")


async def test_demo_health_topology_and_rollback():
    k = DemoKubernetes("demo", DemoConfig())
    unhealthy = await k.find_unhealthy("payments")
    assert any(u["name"] == "payments-api" and u["health"] == "critical" for u in unhealthy)
    topo = await k.topology("payments")
    svc = next(n for n in topo["nodes"] if n["id"] == "Service/payments/payments-api")
    assert svc["health"] == "critical"
    assert any(e["source"] == "Ingress/payments/payments-public" for e in topo["edges"])
    await k.rollback_deployment("payments-api", "payments", None, dry_run=False)
    topo = await k.topology("payments")
    dep = next(n for n in topo["nodes"] if n["id"] == "Deployment/payments/payments-api")
    assert dep["health"] == "healthy"


async def test_namespace_guardrails():
    k = DemoKubernetes("demo", DemoConfig(namespaces_allowlist=["shop"], allow_remediation=False))
    assert {m.name_of(n) for n in await k.list_objects("Namespace")} == {"shop"}
    with pytest.raises(PermissionError):
        await k.list_objects("Pod", "payments")
    with pytest.raises(PermissionError):
        await k.restart_workload("Deployment", "frontend", "shop", dry_run=True)
