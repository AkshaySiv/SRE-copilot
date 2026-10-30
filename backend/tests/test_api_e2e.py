"""End-to-end: API + agent + demo cluster + approval-gated remediation, driven by a scripted LLM."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from langchain_core.messages import AIMessage

from app.core.security import MASK
from app.main import app, lifespan
from tests import scripted_llm

REPORT = {
    "title": "payments-api crash-looping after release 2.4.0",
    "severity": "critical",
    "status": "root_cause_identified",
    "summary": "Payments stopped working because the new version cannot read one of its settings.",
    "impact": "Checkout fails for ~41% of customers.",
    "root_cause": {"description": "Revision 7 set DB_POOL_SIZE=twenty; the app exits on startup.",
                   "category": "configuration", "confidence": 0.95, "affected_components": ["payments-api"]},
    "evidence": [{"source": "pod logs", "finding": "startup aborts", "detail": "invalid value for DB_POOL_SIZE"}],
    "recommendations": [
        {"title": "Roll back payments-api to revision 6", "description": "Restores the last good version.",
         "kind": "immediate",
         "automated_action": {"action": "rollback_deployment", "integration": "demo-cluster",
                              "params": {"name": "payments-api", "namespace": "payments"}}},
        {"title": "Restart it", "description": "invalid on purpose",
         "automated_action": {"action": "launch_rockets", "integration": "demo-cluster", "params": {}}},
        {"title": "Validate config in CI", "description": "Add schema validation.", "kind": "prevention"},
    ],
}


@pytest.fixture
async def client():
    async with lifespan(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def _wait(client, inv_id, timeout=20):
    for _ in range(timeout * 10):
        data = (await client.get(f"/api/investigations/{inv_id}")).json()
        if data["status"] in ("completed", "failed", "cancelled") and not data["running"]:
            return data
        await asyncio.sleep(0.1)
    raise AssertionError("investigation did not finish")


async def _llm(client):
    r = await client.post("/api/integrations", json={"name": "test-llm", "kind": "llm", "type": "scripted",
                                                     "is_default": True})
    assert r.status_code in (201, 409)


async def test_incident_investigation_and_remediation(client):
    await _llm(client)
    scripted_llm.SCRIPT[:] = [
        AIMessage(content="Checking what is unhealthy and which alerts fire.", tool_calls=[
            {"name": "demo_cluster__find_unhealthy", "args": {"namespace": "payments"}, "id": "c1"},
            {"name": "demo_monitoring__get_alerts", "args": {}, "id": "c2"}]),
        AIMessage(content="", tool_calls=[
            {"name": "demo_cluster__get_workload_logs",
             "args": {"name": "payments-api", "namespace": "payments", "previous": True}, "id": "c3"},
            {"name": "demo_cluster__rollout_history", "args": {"name": "payments-api", "namespace": "payments"},
             "id": "c4"},
            {"name": "demo_cluster__get_resource", "args": {"kind": "Secret", "name": "x", "namespace": "payments"},
             "id": "c5"}]),
        AIMessage(content="The crash correlates with revision 7."),
        AIMessage(content="```json\n" + json.dumps(REPORT) + "\n```"),
    ]
    r = await client.post("/api/investigations", json={"kind": "incident",
                                                       "prompt": "Customers can't pay - checkout is failing"})
    assert r.status_code == 201
    data = await _wait(client, r.json()["id"])
    assert data["status"] == "completed", data.get("error")
    assert data["severity"] == "critical" and "DB_POOL_SIZE" in data["root_cause"]

    timeline = next(e["data"]["timeline"] for e in data["events"] if e["type"] == "timeline")
    tl_events = {e["id"]: e for e in timeline["events"]}
    assert tl_events[timeline["origin"]]["layer"] == "change"
    assert timeline["stats"]["events"] > 5

    results = [e["data"] for e in data["events"] if e["type"] == "tool_result"]
    logs = next(r for r in results if r["tool"] == "get_workload_logs")
    assert "DB_POOL_SIZE" in logs["output"]
    secret = next(r for r in results if r["tool"] == "get_resource")
    assert not secret["ok"] and "not permitted" in secret["output"]

    rems = data["remediations"]
    assert len(rems) == 1 and rems[0]["action"] == "rollback_deployment"
    assert "Not automatable" in data["report"]["recommendations"][1]["description"]

    rid = rems[0]["id"]
    r = await client.post(f"/api/remediations/{rid}/approve")
    assert r.status_code == 409  # dry-run required first
    r = await client.post(f"/api/remediations/{rid}/dry-run")
    assert r.status_code == 200 and r.json()["status"] == "dry_run_ok", r.text
    r = await client.post(f"/api/remediations/{rid}/approve")
    assert r.json()["status"] == "executed", r.json()

    cluster_id = next(c["id"] for c in (await client.get("/api/clusters")).json() if c["name"] == "demo-cluster")
    topo = (await client.get(f"/api/clusters/{cluster_id}/topology", params={"namespace": "payments"})).json()
    dep = next(n for n in topo["nodes"] if n["id"] == "Deployment/payments/payments-api")
    assert dep["health"] == "healthy"
    audit = (await client.get("/api/audit")).json()
    assert any(a["action"] == "remediation.executed" for a in audit)


async def test_question_with_follow_up(client):
    await _llm(client)
    scripted_llm.SCRIPT[:] = [
        AIMessage(content="", tool_calls=[{"name": "demo_cluster__cluster_overview", "args": {}, "id": "q1"}]),
        AIMessage(content="**Node worker-3** is under disk pressure."),
    ]
    r = await client.post("/api/investigations", json={"kind": "question", "prompt": "Are my nodes healthy?"})
    data = await _wait(client, r.json()["id"])
    answers = [e for e in data["events"] if e["type"] == "answer"]
    assert data["status"] == "completed" and "worker-3" in answers[0]["data"]["markdown"]

    scripted_llm.SCRIPT[:] = [AIMessage(content="It has 12% disk free.")]
    r = await client.post(f"/api/investigations/{data['id']}/messages", json={"prompt": "How bad is it?"})
    assert r.status_code == 200
    data = await _wait(client, data["id"])
    answers = [e for e in data["events"] if e["type"] == "answer"]
    assert len(answers) == 2 and "12%" in answers[1]["data"]["markdown"]


async def test_integration_secrets_are_masked_and_kept(client):
    body = {"name": "openai", "kind": "llm", "type": "openai_compatible",
            "config": {"model": "gpt-x", "api_key": "sk-supersecret"}}
    r = await client.post("/api/integrations", json=body)
    assert r.status_code == 201, r.text
    row = r.json()
    assert row["config"]["api_key"] == MASK and "sk-supersecret" not in r.text
    body["config"] = {**row["config"], "model": "gpt-y"}
    r = await client.put(f"/api/integrations/{row['id']}", json=body)
    assert r.status_code == 200 and r.json()["config"]["model"] == "gpt-y"

    from app.db.models import Integration
    from app.db.session import sessionmaker
    from app.services.integrations import decrypt_raw, plugin_class
    async with sessionmaker()() as s:
        stored = (await s.get(Integration, row["id"])).config
    assert stored["api_key"].startswith("enc:v1:")
    assert decrypt_raw(plugin_class("llm", "openai_compatible"), stored)["api_key"] == "sk-supersecret"

    r = await client.post("/api/integrations", json={"name": "bad", "kind": "monitoring", "type": "prometheus",
                                                     "config": {"timeout_seconds": 0}})
    assert r.status_code == 422


async def test_cause_question_runs_as_incident_and_survives_llm_failure(client):
    await _llm(client)
    scripted_llm.SCRIPT[:] = []  # the model "fails" on its first call
    r = await client.post("/api/investigations", json={"kind": "question",
                                                       "prompt": "What is causing the checkout errors?"})
    assert r.status_code == 201 and r.json()["kind"] == "incident"
    data = await _wait(client, r.json()["id"])
    first = next(e for e in data["events"] if e["type"] == "user_message")
    assert first["data"]["auto_kind"] == {"from": "question", "to": "incident"}
    # the AI review failed, but the deterministic timeline is kept and the run completes
    assert data["status"] == "completed" and data["error"].startswith("AI review failed")
    assert any(e["type"] == "timeline" for e in data["events"])
    assert any(e["type"] == "notice" for e in data["events"])
    assert data["summary"] and data["root_cause"]

    # re-run the same prompt as a plain question: no reclassification, no timeline
    scripted_llm.SCRIPT[:] = [AIMessage(content="Checkout errors come from payments-api.")]
    r = await client.post(f"/api/investigations/{data['id']}/rerun", json={"kind": "question"})
    assert r.status_code == 201 and r.json()["kind"] == "question"
    q = await _wait(client, r.json()["id"])
    assert q["status"] == "completed" and not any(e["type"] == "timeline" for e in q["events"])
