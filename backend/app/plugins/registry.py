from __future__ import annotations

import importlib
import logging
from importlib.metadata import entry_points
from typing import Any

from .base import Plugin, PluginKind, secret_fields

log = logging.getLogger(__name__)

BUILTIN_MODULES = [
    "app.connectors.llm.openai_compatible",
    "app.connectors.llm.anthropic",
    "app.connectors.llm.ollama",
    "app.connectors.llm.azure_openai",
    "app.connectors.kubernetes.cluster",
    "app.connectors.kubernetes.demo",
    "app.connectors.vm.ssh",
    "app.connectors.monitoring.prometheus",
    "app.connectors.monitoring.loki",
    "app.connectors.monitoring.elasticsearch",
    "app.connectors.monitoring.demo",
    "app.remediation.kubernetes_actions",
    "app.remediation.vm_actions",
]


class PluginRegistry:
    def __init__(self) -> None:
        self._plugins: dict[tuple[str, str], type[Plugin]] = {}
        self._loaded = False

    def register(self, cls: type[Plugin]) -> type[Plugin]:
        key = (str(cls.kind), cls.type)
        if key in self._plugins and self._plugins[key] is not cls:
            log.warning("Plugin %s/%s overridden by %s", *key, cls.__qualname__)
        self._plugins[key] = cls
        return cls

    def get(self, kind: str, type_: str) -> type[Plugin]:
        self.load()
        try:
            return self._plugins[(kind, type_)]
        except KeyError:
            raise KeyError(f"Unknown plugin {kind}/{type_}") from None

    def all(self, kind: str | None = None) -> list[type[Plugin]]:
        self.load()
        return [p for (k, _), p in sorted(self._plugins.items()) if kind is None or k == kind]

    def describe(self) -> list[dict[str, Any]]:
        out = []
        for cls in self.all():
            out.append({
                "kind": str(cls.kind),
                "type": cls.type,
                "display_name": cls.display_name,
                "description": cls.description,
                "capabilities": sorted(cls.capabilities),
                "schema": cls.Config.model_json_schema(),
                "secret_fields": secret_fields(cls.Config),
            })
        return out

    def load(self, extra_modules: list[str] | None = None) -> None:
        if self._loaded and not extra_modules:
            return
        self._loaded = True
        for mod in BUILTIN_MODULES + list(extra_modules or []):
            try:
                importlib.import_module(mod)
            except Exception:  # noqa: BLE001 - a broken optional plugin must not take the app down
                log.exception("Failed to import plugin module %s", mod)
        for ep in entry_points(group="sre_copilot.plugins"):
            try:
                obj = ep.load()
                if isinstance(obj, type) and issubclass(obj, Plugin):
                    self.register(obj)
            except Exception:  # noqa: BLE001
                log.exception("Failed to load plugin entry point %s", ep.name)


registry = PluginRegistry()


def register_plugin(cls: type[Plugin]) -> type[Plugin]:
    """Class decorator: makes a plugin available in the UI, API and agent."""
    if not isinstance(getattr(cls, "kind", None), PluginKind):
        raise TypeError(f"{cls.__qualname__}.kind must be a PluginKind")
    return registry.register(cls)
