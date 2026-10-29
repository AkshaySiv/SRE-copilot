"""Timeline data model: one normalised event format for every technology layer.

Every integration contributes TimelineEvents (what happened, when, on which resource, with evidence) and
Relations (how resources depend on each other). The correlator merges them into one ordered story.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

Layer = Literal["change", "infra", "k8s", "dependency", "app", "network", "impact"]
Severity = Literal["info", "warning", "critical"]

# Display order (top → bottom) and default causal rank used to break ties between simultaneous events:
# a change causes an app error, which causes a container exit, which empties endpoints, which hurts users.
LAYERS: list[Layer] = ["change", "infra", "k8s", "dependency", "app", "network", "impact"]
CAUSAL_RANK: dict[str, int] = {"change": 0, "infra": 1, "dependency": 2, "app": 3, "k8s": 4, "network": 5,
                               "impact": 6}
LAYER_INFO: dict[str, dict[str, str]] = {
    "change": {"name": "Change", "stack": "Deploys · Config · GitOps"},
    "infra": {"name": "Infra", "stack": "Linux · Node · VM"},
    "k8s": {"name": "Kubernetes", "stack": "Pods · Events · Scheduler"},
    "dependency": {"name": "Dependency", "stack": "Databases · Queues"},
    "app": {"name": "Application", "stack": "Logs · Stack traces"},
    "network": {"name": "Network", "stack": "Services · Mesh"},
    "impact": {"name": "Impact", "stack": "Users · SLO · Alerts"},
}


def utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def entity_id(kind: str, namespace: str | None, name: str) -> str:
    """Same format as the cluster topology node ids: Kind/namespace/name ('_' for cluster-scoped)."""
    return f"{kind}/{namespace or '_'}/{name}"


class TimelineEvent(BaseModel):
    id: str = ""
    ts: datetime                       # when it (first) happened - the sort key
    last_ts: datetime | None = None    # last occurrence for repeated events
    layer: Layer
    severity: Severity = "info"
    title: str
    entity: str                        # Kind/namespace/name
    source: str                        # human label of where the evidence came from
    integration: str = ""
    kind: str = ""                     # machine category, e.g. "Evicted", "rollout", "log_error"
    evidence: str = ""                 # raw excerpt shown in the UI and given to the model
    count: int = 1
    estimated: bool = False            # ts is inferred (e.g. first crash from pod start), not observed
    attrs: dict[str, Any] = Field(default_factory=dict)

    def key(self) -> str:
        return f"{self.layer}|{self.entity}|{self.kind}|{self.title[:80]}"

    def ensure_id(self) -> None:
        if not self.id:
            self.id = "ev-" + hashlib.sha1(f"{self.key()}|{self.ts.isoformat()}".encode()).hexdigest()[:10]


class Relation(BaseModel):
    source: str
    target: str
    type: str          # owns | routes | runs_on | calls | mounts | scales
    # Traversal hint for "what is connected to the problem": runs_on (pod -> node) is followed one way only so
    # one sick node does not connect every workload in the cluster.
    directed: bool = False


@dataclass
class TimelineQuery:
    start: datetime
    end: datetime
    focus: list[str] = field(default_factory=list)       # entity ids the user asked about
    namespaces: list[str] = field(default_factory=list)  # empty = all allowed
    max_log_pods: int = 6


@dataclass
class TimelineBatch:
    events: list[TimelineEvent] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class Timeline(BaseModel):
    """What the UI renders and the agent reasons over."""

    window_start: datetime
    window_end: datetime
    focus: list[str] = Field(default_factory=list)
    events: list[TimelineEvent] = Field(default_factory=list)
    origin: str | None = None              # event id
    chain: list[str] = Field(default_factory=list)          # event ids, causal order (origin first)
    unrelated: list[str] = Field(default_factory=list)      # event ids
    connected_entities: list[str] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    layers: list[dict[str, str]] = Field(default_factory=lambda: [
        {"id": lid, **LAYER_INFO[lid]} for lid in LAYERS])

    def by_id(self) -> dict[str, TimelineEvent]:
        return {e.id: e for e in self.events}
