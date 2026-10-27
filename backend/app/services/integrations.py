"""Integration lifecycle: validate + encrypt config, build plugin instances (cached), seed from YAML."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import MASK, get_secret_box
from app.db.models import Integration
from app.db.session import sessionmaker
from app.plugins import Plugin, registry, secret_fields

log = logging.getLogger(__name__)


def plugin_class(kind: str, type_: str) -> type[Plugin]:
    return registry.get(kind, type_)


def decrypt_raw(cls: type[Plugin], stored: dict[str, Any]) -> dict[str, Any]:
    box = get_secret_box()
    data = dict(stored or {})
    for f in secret_fields(cls.Config):
        if isinstance(data.get(f), str):
            data[f] = box.decrypt(data[f])
    return data


def prepare_config(cls: type[Plugin], incoming: dict[str, Any], existing: dict[str, Any] | None = None) -> dict:
    """Validate a config from the API/bootstrap and return the storable (secret-encrypted) form.
    A secret submitted as the mask (or omitted) keeps its existing value."""
    secrets = secret_fields(cls.Config)
    merged = dict(incoming or {})
    old = decrypt_raw(cls, existing or {})
    for f in secrets:
        if merged.get(f) in (MASK, None) and old.get(f):
            merged[f] = old[f]
        elif merged.get(f) == "":
            merged[f] = None
    model = cls.Config.model_validate(merged)
    data = model.model_dump(mode="json", exclude=set(secrets))
    box = get_secret_box()
    for f in secrets:
        val = getattr(model, f)
        data[f] = box.encrypt(val.get_secret_value()) if val is not None else None
    return data


def masked_config(cls: type[Plugin] | None, stored: dict[str, Any]) -> dict[str, Any]:
    data = dict(stored or {})
    if cls is None:
        return data
    for f in secret_fields(cls.Config):
        data[f] = MASK if data.get(f) else None
    return data


def build_plugin(integ_name: str, kind: str, type_: str, stored: dict[str, Any]) -> Plugin:
    cls = plugin_class(kind, type_)
    config: BaseModel = cls.Config.model_validate(decrypt_raw(cls, stored))
    return cls(integ_name, config)


class PluginPool:
    """Caches live plugin instances (clients, connections, demo state) keyed by integration id + version."""

    def __init__(self) -> None:
        self._items: dict[str, tuple[str, Plugin]] = {}
        self._lock = asyncio.Lock()

    async def get(self, integ: Integration) -> Plugin:
        version = f"{integ.updated_at.isoformat() if integ.updated_at else ''}:{integ.name}"
        async with self._lock:
            cached = self._items.get(integ.id)
            if cached and cached[0] == version:
                return cached[1]
            if cached:
                await self._close(cached[1])
            plugin = build_plugin(integ.name, integ.kind, integ.type, integ.config)
            self._items[integ.id] = (version, plugin)
            return plugin

    async def evict(self, integration_id: str) -> None:
        async with self._lock:
            cached = self._items.pop(integration_id, None)
        if cached:
            await self._close(cached[1])

    async def close_all(self) -> None:
        for _, plugin in list(self._items.values()):
            await self._close(plugin)
        self._items.clear()

    @staticmethod
    async def _close(plugin: Plugin) -> None:
        try:
            await plugin.close()
        except Exception:  # noqa: BLE001
            log.exception("Error closing plugin %s", plugin.name)


pool = PluginPool()


async def get_plugin(session: AsyncSession, integration_id: str) -> tuple[Integration, Plugin]:
    integ = await session.get(Integration, integration_id)
    if integ is None:
        raise LookupError(f"Integration {integration_id} not found")
    if not integ.enabled:
        raise PermissionError(f"Integration '{integ.name}' is disabled")
    return integ, await pool.get(integ)


async def resolve_scope(session: AsyncSession, scope: dict[str, Any]) -> dict[str, list[Integration]]:
    """Scope -> integrations per kind. Explicit ids win; otherwise all enabled (LLM: the default one)."""
    rows = (await session.scalars(select(Integration).where(Integration.enabled.is_(True)))).all()
    out: dict[str, list[Integration]] = {}
    for kind, key in (("llm", "llm"), ("kubernetes", "clusters"), ("vm", "vms"), ("monitoring", "monitoring")):
        of_kind = [r for r in rows if r.kind == kind]
        wanted = scope.get(key)
        if isinstance(wanted, str):
            wanted = [wanted]
        if wanted:
            out[kind] = [r for r in of_kind if r.id in wanted or r.name in wanted]
        elif kind == "llm":
            out[kind] = sorted(of_kind, key=lambda r: (not r.is_default, r.created_at))[:1]
        else:
            out[kind] = of_kind
    return out


async def bootstrap_from_file(path: str) -> None:
    """Upsert integrations from YAML:

    integrations:
      - name: prod-cluster
        kind: kubernetes
        type: cluster
        default: true
        config: {auth_mode: in_cluster, environment: prod}
    Values like ${ENV_VAR} are expanded so secrets can come from Kubernetes Secrets.
    """
    import os
    import re

    text = Path(path).read_text()
    text = re.sub(r"\$\{([A-Z0-9_]+)\}", lambda mt: os.environ.get(mt.group(1), ""), text)
    spec = yaml.safe_load(text) or {}
    async with sessionmaker()() as s:
        for item in spec.get("integrations", []):
            cls = plugin_class(item["kind"], item["type"])
            existing = await s.scalar(select(Integration).where(Integration.name == item["name"]))
            cfg = prepare_config(cls, item.get("config", {}), existing.config if existing else None)
            if existing is None:
                existing = Integration(name=item["name"], kind=item["kind"], type=item["type"])
                s.add(existing)
            existing.config = cfg
            existing.type = item["type"]
            existing.description = item.get("description", "")
            existing.enabled = item.get("enabled", True)
            existing.is_default = item.get("default", False)
            existing.managed_by = "bootstrap"
            log.info("Bootstrapped integration %s (%s/%s)", item["name"], item["kind"], item["type"])
        await s.commit()
