from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.core.auth import Principal, Role, get_principal, require
from app.core.config import get_settings
from app.db.models import AuditLog
from app.db.session import get_session
from app.plugins import registry
from app.remediation.base import catalog

router = APIRouter(tags=["meta"])


@router.get("/health")
async def health():
    return {"status": "ok", "version": __version__}


@router.get("/ready")
async def ready(session: AsyncSession = Depends(get_session)):
    await session.execute(text("SELECT 1"))
    return {"status": "ready"}


@router.get("/me")
async def me(principal: Principal = Depends(get_principal)):
    s = get_settings()
    return {**principal.to_dict(), "remediation_mode": s.remediation_mode, "auth_mode": s.auth_mode,
            "version": __version__}


@router.get("/plugins", dependencies=[Depends(require(Role.viewer))])
async def plugins():
    return registry.describe()


@router.get("/actions", dependencies=[Depends(require(Role.viewer))])
async def actions():
    return catalog()


@router.get("/audit", dependencies=[Depends(require(Role.operator))])
async def audit_log(limit: int = 200, session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 1000)))).all()
    return [{"id": r.id, "actor": r.actor, "action": r.action, "target": r.target, "details": r.details,
             "created_at": r.created_at.isoformat()} for r in rows]
