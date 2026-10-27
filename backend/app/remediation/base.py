"""Remediation actions: the ONLY code path that changes infrastructure.

Safety model
  1. The LLM may only *propose* actions from this registry, with parameters validated by a pydantic model.
  2. Each proposal carries a human-readable plan (what changes, blast radius, how to undo).
  3. A server-side dry-run runs first (Kubernetes dryRun=All / SSH connectivity check).
  4. A human with the operator role approves; only then execute() runs. Everything is audit-logged.
  5. Integrations must opt in (allow_remediation=true) - the default is read-only.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Literal

from pydantic import BaseModel

from app.plugins import Plugin, PluginKind

Risk = Literal["low", "medium", "high"]


class RemediationAction(ABC):
    name: ClassVar[str]
    title: ClassVar[str]
    description: ClassVar[str]
    target_kind: ClassVar[PluginKind]
    risk: ClassVar[Risk] = "medium"
    Params: ClassVar[type[BaseModel]]

    def risk_for(self, params: BaseModel) -> Risk:
        return self.risk

    @abstractmethod
    def plan(self, params: BaseModel) -> dict[str, Any]:
        """{'summary': str, 'changes': [str], 'impact': str, 'rollback': str}"""

    @abstractmethod
    async def dry_run(self, plugin: Plugin, params: BaseModel) -> dict[str, Any]: ...

    @abstractmethod
    async def execute(self, plugin: Plugin, params: BaseModel) -> dict[str, Any]: ...


ACTIONS: dict[str, RemediationAction] = {}


def register_action(cls: type[RemediationAction]) -> type[RemediationAction]:
    ACTIONS[cls.name] = cls()
    return cls


def get_action(name: str) -> RemediationAction:
    if name not in ACTIONS:
        raise KeyError(f"Unknown remediation action '{name}'")
    return ACTIONS[name]


def catalog(kinds: set[str] | None = None) -> list[dict[str, Any]]:
    return [{"action": a.name, "title": a.title, "description": a.description, "target_kind": str(a.target_kind),
             "default_risk": a.risk, "params_schema": a.Params.model_json_schema()}
            for a in ACTIONS.values() if kinds is None or str(a.target_kind) in kinds]
