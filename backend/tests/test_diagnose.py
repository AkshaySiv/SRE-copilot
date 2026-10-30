"""Rule-based failure insights for the cluster map, against the demo cluster's scenarios."""
from app.connectors.kubernetes.demo import DemoConfig, DemoKubernetes
from app.connectors.kubernetes.diagnose import diagnose


async def _titles(kind, name, ns):
    k = DemoKubernetes("demo", DemoConfig())
    return [i["title"] for i in (await diagnose(k, kind, name, ns))["insights"]]


async def test_missing_secret_names_secret_and_reference():
    t = await _titles("Deployment", "report-generator", "platform")
    assert t and "Missing Secret `report-db-credentials`" in t[0] and "env DB_PASSWORD" in t[0]


async def test_crash_uses_first_log_error():
    t = await _titles("Deployment", "payments-api", "payments")
    assert "DB_POOL_SIZE" in t[0] and "twenty" in t[0]


async def test_dependency_failure_points_upstream():
    t = await _titles("Deployment", "backend", "sre-demo")
    assert "can't reach a dependency" in t[0]
    assert "upstream" in t[1] and "postgres-0" in t[1] and "disk-pressure" in t[1]


async def test_unknown_exit_code_is_marked_for_ai():
    k = DemoKubernetes("demo", DemoConfig())
    ins = (await diagnose(k, "Deployment", "pg-cron", "platform"))["insights"]
    assert "exit" in ins[0]["title"] and "code 7" in ins[0]["title"] and ins[0]["confidence"] == "medium"


async def test_healthy_resource_has_no_insights():
    assert await _titles("Deployment", "cart", "shop") == []
