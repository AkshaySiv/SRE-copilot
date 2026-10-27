from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal
from app.core.config import get_settings
from app.db.models import Remediation
from app.db.session import audit
from app.remediation.base import get_action

from .events import EventRecorder
from .integrations import get_plugin

log = logging.getLogger(__name__)


class RemediationError(Exception):
    pass


def serialize(r: Remediation) -> dict:
    return {"id": r.id, "investigation_id": r.investigation_id, "action": r.action, "integration_id": r.integration_id,
            "params": r.params, "title": r.title, "rationale": r.rationale, "risk": r.risk, "plan": r.plan,
            "status": r.status, "dry_run": r.dry_run, "result": r.result, "decided_by": r.decided_by,
            "created_at": r.created_at.isoformat(), "updated_at": r.updated_at.isoformat() if r.updated_at else None}


async def dry_run(session: AsyncSession, rem: Remediation, principal: Principal) -> Remediation:
    if get_settings().remediation_mode == "disabled":
        raise RemediationError("Remediation is disabled on this server")
    if rem.status in ("executed", "rejected"):
        raise RemediationError(f"Remediation already {rem.status}")
    action = get_action(rem.action)
    _, plugin = await get_plugin(session, rem.integration_id)
    params = action.Params.model_validate(rem.params)
    try:
        out = await action.dry_run(plugin, params)
        rem.dry_run, rem.status = {"ok": True, "output": out}, "dry_run_ok"
    except Exception as exc:  # noqa: BLE001
        rem.dry_run, rem.status = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, "dry_run_failed"
    await session.commit()
    await audit(principal.name, "remediation.dry_run", rem.id, action=rem.action, params=rem.params, result=rem.dry_run)
    await EventRecorder(rem.investigation_id).emit("remediation_update", serialize(rem))
    return rem


async def approve_and_execute(session: AsyncSession, rem: Remediation, principal: Principal) -> Remediation:
    settings = get_settings()
    if settings.remediation_mode == "disabled":
        raise RemediationError("Remediation is disabled on this server")
    if rem.status == "executed":
        raise RemediationError("Already executed")
    if rem.status == "rejected":
        raise RemediationError("Remediation was rejected")
    if settings.require_dry_run_before_execute and rem.status != "dry_run_ok":
        raise RemediationError("A successful dry-run is required before execution")
    action = get_action(rem.action)
    _, plugin = await get_plugin(session, rem.integration_id)
    params = action.Params.model_validate(rem.params)
    rem.decided_by = principal.name
    await audit(principal.name, "remediation.approved", rem.id, action=rem.action, params=rem.params)
    try:
        out = await action.execute(plugin, params)
        rem.result, rem.status = {"ok": True, "output": out}, "executed"
    except Exception as exc:  # noqa: BLE001
        log.exception("Remediation %s failed", rem.id)
        rem.result, rem.status = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, "failed"
    await session.commit()
    await audit(principal.name, f"remediation.{rem.status}", rem.id, action=rem.action, result=rem.result)
    await EventRecorder(rem.investigation_id).emit("remediation_update", serialize(rem))
    return rem


async def reject(session: AsyncSession, rem: Remediation, principal: Principal, reason: str = "") -> Remediation:
    if rem.status == "executed":
        raise RemediationError("Already executed")
    rem.status, rem.decided_by = "rejected", principal.name
    rem.result = {"reason": reason}
    await session.commit()
    await audit(principal.name, "remediation.rejected", rem.id, reason=reason)
    await EventRecorder(rem.investigation_id).emit("remediation_update", serialize(rem))
    return rem
