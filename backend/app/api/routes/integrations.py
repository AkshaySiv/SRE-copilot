from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal, Role, require
from app.db.models import Integration
from app.db.session import audit, get_session
from app.services.integrations import build_plugin, masked_config, plugin_class, pool, prepare_config

router = APIRouter(prefix="/integrations", tags=["integrations"])


class IntegrationIn(BaseModel):
    name: str = Field(min_length=2, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")
    kind: str
    type: str
    description: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    is_default: bool = False


class TestIn(BaseModel):
    kind: str
    type: str
    config: dict[str, Any] = Field(default_factory=dict)
    integration_id: str | None = Field(None, description="Reuse stored secrets of this integration")


def serialize(row: Integration) -> dict[str, Any]:
    try:
        cls = plugin_class(row.kind, row.type)
    except KeyError:
        cls = None
    return {"id": row.id, "name": row.name, "kind": row.kind, "type": row.type, "description": row.description,
            "config": masked_config(cls, row.config), "enabled": row.enabled, "is_default": row.is_default,
            "managed_by": row.managed_by, "plugin_available": cls is not None,
            "created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat() if row.updated_at else None}


def _validate(kind: str, type_: str, config: dict, existing: dict | None = None) -> dict:
    try:
        cls = plugin_class(kind, type_)
    except KeyError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        return prepare_config(cls, config, existing)
    except ValidationError as exc:
        raise HTTPException(422, exc.errors(include_url=False, include_input=False)) from exc


async def _clear_default(session: AsyncSession, kind: str, keep_id: str) -> None:
    await session.execute(update(Integration).where(Integration.kind == kind, Integration.id != keep_id)
                          .values(is_default=False))


@router.get("", dependencies=[Depends(require(Role.viewer))])
async def list_integrations(kind: str | None = None, session: AsyncSession = Depends(get_session)):
    q = select(Integration).order_by(Integration.kind, Integration.name)
    if kind:
        q = q.where(Integration.kind == kind)
    return [serialize(r) for r in (await session.scalars(q)).all()]


@router.post("", status_code=201)
async def create_integration(body: IntegrationIn, session: AsyncSession = Depends(get_session),
                             principal: Principal = Depends(require(Role.admin))):
    if await session.scalar(select(Integration).where(Integration.name == body.name)):
        raise HTTPException(409, f"An integration named '{body.name}' already exists")
    row = Integration(name=body.name, kind=body.kind, type=body.type, description=body.description,
                      config=_validate(body.kind, body.type, body.config), enabled=body.enabled,
                      is_default=body.is_default)
    session.add(row)
    await session.flush()
    if body.is_default:
        await _clear_default(session, body.kind, row.id)
    await session.commit()
    await audit(principal.name, "integration.created", row.name, kind=row.kind, type=row.type)
    return serialize(row)


@router.get("/{integration_id}", dependencies=[Depends(require(Role.viewer))])
async def get_integration(integration_id: str, session: AsyncSession = Depends(get_session)):
    row = await session.get(Integration, integration_id)
    if not row:
        raise HTTPException(404, "Not found")
    return serialize(row)


@router.put("/{integration_id}")
async def update_integration(integration_id: str, body: IntegrationIn, session: AsyncSession = Depends(get_session),
                             principal: Principal = Depends(require(Role.admin))):
    row = await session.get(Integration, integration_id)
    if not row:
        raise HTTPException(404, "Not found")
    if body.kind != row.kind:
        raise HTTPException(400, "kind cannot be changed")
    clash = await session.scalar(select(Integration).where(Integration.name == body.name, Integration.id != row.id))
    if clash:
        raise HTTPException(409, f"An integration named '{body.name}' already exists")
    row.config = _validate(body.kind, body.type, body.config, row.config if body.type == row.type else None)
    row.name, row.type, row.description = body.name, body.type, body.description
    row.enabled, row.is_default = body.enabled, body.is_default
    if body.is_default:
        await _clear_default(session, row.kind, row.id)
    await session.commit()
    await pool.evict(row.id)
    await audit(principal.name, "integration.updated", row.name)
    return serialize(row)


@router.delete("/{integration_id}", status_code=204)
async def delete_integration(integration_id: str, session: AsyncSession = Depends(get_session),
                             principal: Principal = Depends(require(Role.admin))):
    row = await session.get(Integration, integration_id)
    if not row:
        raise HTTPException(404, "Not found")
    await session.delete(row)
    await session.commit()
    await pool.evict(integration_id)
    await audit(principal.name, "integration.deleted", row.name)


@router.post("/{integration_id}/test", dependencies=[Depends(require(Role.viewer))])
async def test_saved(integration_id: str, session: AsyncSession = Depends(get_session)):
    row = await session.get(Integration, integration_id)
    if not row:
        raise HTTPException(404, "Not found")
    plugin = await pool.get(row)
    try:
        status = await asyncio.wait_for(plugin.health(), timeout=90)
    except asyncio.TimeoutError:
        return {"ok": False, "message": "Health check timed out"}
    return {"ok": status.ok, "message": status.message, "details": status.details}


@router.post("/test", dependencies=[Depends(require(Role.admin))])
async def test_unsaved(body: TestIn, session: AsyncSession = Depends(get_session)):
    existing = None
    if body.integration_id:
        row = await session.get(Integration, body.integration_id)
        existing = row.config if row and row.type == body.type else None
    stored = _validate(body.kind, body.type, body.config, existing)
    plugin = build_plugin("connection-test", body.kind, body.type, stored)
    try:
        status = await asyncio.wait_for(plugin.health(), timeout=90)
        return {"ok": status.ok, "message": status.message, "details": status.details}
    except asyncio.TimeoutError:
        return {"ok": False, "message": "Health check timed out"}
    finally:
        await plugin.close()
