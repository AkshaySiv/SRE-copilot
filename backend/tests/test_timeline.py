from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.connectors.kubernetes.demo import DemoConfig, DemoKubernetes
from app.connectors.kubernetes.timeline import template_diff
from app.connectors.monitoring.demo import DemoMonitoring, DemoMonitoringConfig
from app.timeline import Relation, TimelineEvent, correlate
from app.timeline.logparse import find_errors
from app.timeline.service import build_timeline

T0 = datetime(2026, 10, 29, 9, 0, 0, tzinfo=timezone.utc)


def at(sec: int) -> datetime:
    return T0 + timedelta(seconds=sec)


def test_python_traceback_is_one_grouped_finding():
    log = "\n".join([
        "2026-10-29T09:12:15Z INFO starting",
        "2026-10-29T09:12:15Z Traceback (most recent call last):",
        '  File "/app/db.py", line 18, in connect',
        "    return psycopg2.connect(DSN)",
        'psycopg2.OperationalError: connection to server at "postgres" (10.96.41.7), port 5432 failed',
        "2026-10-29T09:12:45Z Traceback (most recent call last):",
        '  File "/app/db.py", line 18, in connect',
        "    return psycopg2.connect(DSN)",
        'psycopg2.OperationalError: connection to server at "postgres" (10.96.41.7), port 5432 failed',
    ])
    findings = find_errors(log)
    assert len(findings) == 1
    f = findings[0]
    assert f.count == 2 and f.level == "fatal" and f.message.startswith("psycopg2.OperationalError")
    assert f.first_ts == datetime(2026, 10, 29, 9, 12, 15, tzinfo=timezone.utc)
    assert any("db.py" in line for line in f.trace)


def test_java_and_go_traces():
    java = ("2026-10-29 09:00:01,123 ERROR [main] c.e.App - request failed\n"
            "java.lang.IllegalStateException: pool exhausted\n\tat com.example.Pool.get(Pool.java:42)\n"
            "\tat com.example.App.run(App.java:10)\n")
    found = find_errors(java)
    assert found and found[0].trace and "Pool.java" in "\n".join(found[0].trace)
    go = "panic: runtime error: invalid memory address\n\ngoroutine 1 [running]:\nmain.main()\n\t/app/main.go:12 +0x1d\n"
    g = find_errors(go)[0]
    assert g.level == "fatal" and any("main.go" in t for t in g.trace)


def test_template_diff_names_the_change():
    old = {"spec": {"containers": [{"name": "api", "image": "api:1", "env": [{"name": "POOL", "value": "20"}]}]}}
    new = {"spec": {"containers": [{"name": "api", "image": "api:2", "env": [{"name": "POOL", "value": "twenty"},
                                                                                 {"name": "NEW", "value": "x"}]}]}}
    diff = template_diff(old, new)
    assert 'env POOL: "20" → "twenty"' in diff and "image: api:1 → api:2" in diff and '+ env NEW="x"' in diff


def _ev(sec, layer, sev, entity, title, **kw):
    return TimelineEvent(ts=at(sec), layer=layer, severity=sev, entity=entity, title=title, source="test", **kw)


def test_correlate_infra_origin_and_unrelated():
    rel = [Relation(source="Deployment/a/backend", target="Pod/a/backend-1", type="owns"),
           Relation(source="Pod/a/backend-1", target="Service/a/postgres", type="calls"),
           Relation(source="Service/a/postgres", target="Pod/a/postgres-0", type="routes"),
           Relation(source="Pod/a/postgres-0", target="Node/_/w2", type="runs_on", directed=True),
           Relation(source="Pod/b/other-1", target="Node/_/w2", type="runs_on", directed=True)]
    events = [
        _ev(0, "infra", "critical", "Node/_/w2", "w2 DiskPressure"),
        _ev(38, "k8s", "critical", "Pod/a/postgres-0", "postgres-0 evicted"),
        _ev(40, "dependency", "critical", "Service/a/postgres", "postgres no endpoints"),
        _ev(75, "app", "critical", "Pod/a/backend-1", "OperationalError"),
        _ev(76, "k8s", "critical", "Pod/a/backend-1", "backend exited"),
        _ev(100, "impact", "critical", "Deployment/a/backend", "5xx 63%"),
        _ev(50, "k8s", "critical", "Pod/b/other-1", "other OOMKilled"),  # shares the node, not the problem
    ]
    tl = correlate(events, rel, ["Pod/a/backend-1"], at(-600), at(600))
    by = tl.by_id()
    assert [by[i].title for i in tl.chain] == ["w2 DiskPressure", "postgres-0 evicted", "postgres no endpoints",
                                               "OperationalError", "5xx 63%"]
    assert by[tl.origin].layer == "infra"
    assert [by[i].title for i in tl.unrelated] == ["other OOMKilled"]


def test_correlate_prefers_a_recent_change_as_origin_and_merges_repeats():
    rel = [Relation(source="Deployment/p/api", target="Pod/p/api-1", type="owns")]
    events = [
        _ev(0, "change", "info", "Deployment/p/api", "api rolled out revision 7"),
        _ev(5, "app", "critical", "Pod/p/api-1", "FATAL bad config", attrs={"norm": "fatal"}),
        _ev(65, "app", "critical", "Pod/p/api-1", "FATAL bad config", attrs={"norm": "fatal"}),
        _ev(6, "k8s", "critical", "Pod/p/api-1", "api exited"),
        _ev(-3000, "infra", "warning", "Node/_/old", "ancient warning"),
    ]
    tl = correlate(events, rel, ["Deployment/p/api"], at(-1800), at(600))
    by = tl.by_id()
    assert by[tl.origin].title == "api rolled out revision 7"
    assert sum(1 for e in tl.events if e.title == "FATAL bad config") == 1      # merged
    assert all(e.title != "ancient warning" for e in tl.events)                  # outside window


async def test_demo_scenarios_produce_the_expected_chains():
    plugins = [DemoKubernetes("demo-cluster", DemoConfig()), DemoMonitoring("demo-monitoring", DemoMonitoringConfig())]
    disk = await build_timeline(plugins, "sre-demo app is down", {})
    by = disk.by_id()
    assert [by[i].layer for i in disk.chain] == ["infra", "k8s", "dependency", "app", "network", "impact"]
    assert "DiskPressure" in by[disk.origin].title and "worker-2" in by[disk.origin].entity

    bad_deploy = await build_timeline(plugins, "Customers say checkout is failing", {})
    by = bad_deploy.by_id()
    assert by[bad_deploy.origin].layer == "change"
    assert 'DB_POOL_SIZE: "20" → "twenty"' in by[bad_deploy.origin].evidence
    assert [by[i].layer for i in bad_deploy.chain][-1] == "impact"
    assert any("ledger-worker" in by[i].title for i in bad_deploy.unrelated)
