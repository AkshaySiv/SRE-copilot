from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError


class Evidence(BaseModel):
    source: str = Field(description="Where it came from: tool / integration, e.g. 'pod logs payments-api-…'")
    finding: str = Field(description="What it shows, in plain language")
    detail: str | None = Field(None, description="Short verbatim excerpt (log line, metric value, event)")


class TimelineEntry(BaseModel):
    time: str
    event: str


class RootCause(BaseModel):
    description: str
    category: str = Field("other", description="deployment|configuration|capacity|dependency|infrastructure|network|"
                                               "storage|security|code|other")
    confidence: float = Field(0.5, ge=0, le=1)
    affected_components: list[str] = Field(default_factory=list)


class ProposedAction(BaseModel):
    action: str = Field(description="Exact action id from the remediation catalog")
    integration: str = Field(description="Integration name the action targets")
    params: dict[str, Any] = Field(default_factory=dict)


class Recommendation(BaseModel):
    title: str
    description: str
    kind: Literal["immediate", "follow_up", "prevention"] = "immediate"
    automated_action: ProposedAction | None = None


class IncidentReport(BaseModel):
    title: str
    severity: Literal["critical", "high", "medium", "low", "info"] = "medium"
    status: Literal["root_cause_identified", "probable_cause", "inconclusive", "no_issue_found"] = "probable_cause"
    summary: str = Field(description="3-5 sentences a non-engineer can understand")
    impact: str = ""
    root_cause: RootCause | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    timeline: list[TimelineEntry] = Field(default_factory=list)
    contributing_factors: list[str] = Field(default_factory=list)
    other_findings: list[str] = Field(default_factory=list, description="Unrelated issues noticed along the way")
    recommendations: list[Recommendation] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


_FENCE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.S)


def extract_json(text: str) -> dict[str, Any]:
    m = _FENCE.search(text)
    candidate = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    if not candidate:
        raise ValueError("no JSON object found in model output")
    return json.loads(candidate)


def parse_report(text: str) -> IncidentReport:
    try:
        return IncidentReport.model_validate(extract_json(text))
    except (ValueError, ValidationError) as exc:
        raise ValueError(str(exc)[:1500]) from exc


def report_schema_hint() -> str:
    return json.dumps(IncidentReport.model_json_schema(), separators=(",", ":"))
