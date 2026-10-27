"""Plugin contracts.

Every integration (LLM, Kubernetes cluster, VM fleet, monitoring backend) is a Plugin subclass with:
  * a pydantic ``Config`` model  -> drives validation, the UI form (JSON schema) and secret handling
  * ``health()``                 -> connection test shown in the UI
  * ``tools()``                  -> read-only investigation tools exposed to the agent

Adding a new backend = one class decorated with @register_plugin. Nothing else in the codebase changes.
"""
from __future__ import annotations

import string
import typing
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, SecretStr


class PluginKind(StrEnum):
    LLM = "llm"
    KUBERNETES = "kubernetes"
    VM = "vm"
    MONITORING = "monitoring"


@dataclass
class HealthStatus:
    ok: bool
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "…"


@dataclass
class ToolSpec:
    """A tool the investigation agent may call. Tools MUST be read-only; writes go through remediation actions."""

    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[..., Awaitable[Any]]
    # Plain-language label shown to non-expert users, formatted with the call args, e.g. "Read logs of pod {name}"
    label: str = ""
    category: str = "general"

    def render_label(self, args: dict[str, Any]) -> str:
        template = self.label or self.name.replace("_", " ").capitalize()
        try:
            return string.Formatter().vformat(template, (), _SafeDict({k: v for k, v in args.items() if v is not None}))
        except Exception:  # noqa: BLE001
            return template


class Plugin(ABC):
    kind: ClassVar[PluginKind]
    type: ClassVar[str]
    display_name: ClassVar[str]
    description: ClassVar[str] = ""
    capabilities: ClassVar[frozenset[str]] = frozenset()
    Config: ClassVar[type[BaseModel]]

    def __init__(self, name: str, config: BaseModel):
        self.name = name
        self.config = config

    @abstractmethod
    async def health(self) -> HealthStatus: ...

    def tools(self) -> list[ToolSpec]:
        return []

    def context_hint(self) -> str:
        """Short description injected in the agent prompt so the LLM knows what this integration covers."""
        return self.description

    async def close(self) -> None:  # release clients / connections
        return None


def _is_secret(annotation: Any) -> bool:
    if annotation is SecretStr:
        return True
    return any(_is_secret(a) for a in typing.get_args(annotation))


def secret_fields(model: type[BaseModel]) -> list[str]:
    return [name for name, f in model.model_fields.items() if _is_secret(f.annotation)]


class EmptyArgs(BaseModel):
    pass
