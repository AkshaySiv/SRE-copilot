from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.core.auth import Principal, Role, require
from app.db.models import Investigation, InvestigationEvent, Remediation
from app.db.session import audit, get_session
from app.services import remediations as rsvc
from app.services.events import EventRecorder, bus
from app.services.intent import classify
from app.services.investigations import runner

router = APIRouter(tags=["investigations"])


class InvestigationIn(BaseModel):
    kind: Literal["incident", "question"] = "incident"
    prompt: str = Field(min_length=3, max_length=8000)
    title: str | None = None
    scope: dict[str, Any] = Field(default_factory=dict,
                                  description="{llm, clusters[], vms[], monitoring[], namespace, resource{kind,name}, host}")
    strict: bool = Field(False, description="Keep `kind` as given. By default a question that describes a failure "
                                            "('what is causing the slowness') runs as an incident.")


class RerunIn(BaseModel):
    kind: Literal["incident", "question"]


class MessageIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)


class RejectIn(BaseModel):
    reason: str = ""


def _summary(inv: Investigation) -> dict[str, Any]:
    return {"id": inv.id, "kind": inv.kind, "title": inv.title, "prompt": inv.prompt, "scope": inv.scope,
            "status": inv.status, "severity": inv.severity, "summary": inv.summary, "root_cause": inv.root_cause,
            "error": inv.error, "created_by": inv.created_by, "created_at": inv.created_at.isoformat(),
            "completed_at": inv.completed_at.isoformat() if inv.completed_at else None,
            "running": runner.is_running(inv.id)}


def _title(prompt: str) -> str:
    first = prompt.strip().splitlines()[0]
    return first[:117] + "…" if len(first) > 120 else first


@router.post("/investigations", status_code=201)
async def create(body: InvestigationIn, session: AsyncSession = Depends(get_session),
                 principal: Principal = Depends(require(Role.viewer))):
    return await _start(session, principal, body.kind, body.prompt, body.scope, body.title, body.strict)


async def _start(session: AsyncSession, principal: Principal, kind: str, prompt: str, scope: dict[str, Any],
                 title: str | None = None, strict: bool = False, rerun_of: str | None = None):
    msg: dict[str, Any] = {"text": prompt, "by": principal.name}
    if not strict and kind == "question" and classify(prompt) == "incident":
        kind = "incident"
        msg["auto_kind"] = {"from": "question", "to": "incident"}
    if rerun_of:
        msg["rerun_of"] = rerun_of
    inv = Investigation(kind=kind, title=title or _title(prompt), prompt=prompt, scope=scope,
                        created_by=principal.name)
    session.add(inv)
    await session.commit()
    await EventRecorder(inv.id).emit("user_message", msg)
    runner.start(inv.id, prompt)
    await audit(principal.name, f"{kind}.started", inv.id, title=inv.title)
    return _summary(inv)


@router.post("/investigations/{inv_id}/rerun", status_code=201)
async def rerun(inv_id: str, body: RerunIn, session: AsyncSession = Depends(get_session),
                principal: Principal = Depends(require(Role.viewer))):
    """Start a new investigation from the same prompt and scope, as an incident timeline or a plain question."""
    inv = await session.get(Investigation, inv_id)
    if not inv:
        raise HTTPException(404, "Not found")
    return await _start(session, principal, body.kind, inv.prompt, dict(inv.scope or {}), strict=True,
                        rerun_of=inv.id)


@router.get("/investigations", dependencies=[Depends(require(Role.viewer))])
async def list_investigations(kind: str | None = None, status: str | None = None, q: str | None = None,
                              limit: int = 50, offset: int = 0, session: AsyncSession = Depends(get_session)):
    stmt = select(Investigation)
    if kind:
        stmt = stmt.where(Investigation.kind == kind)
    if status:
        stmt = stmt.where(Investigation.status == status)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(Investigation.title.ilike(like) | Investigation.summary.ilike(like)
                          | Investigation.prompt.ilike(like))
    total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (await session.scalars(stmt.order_by(Investigation.created_at.desc()).limit(min(limit, 200))
                                  .offset(offset))).all()
    return {"total": total, "items": [_summary(r) for r in rows]}


@router.get("/investigations/{inv_id}", dependencies=[Depends(require(Role.viewer))])
async def get_one(inv_id: str, session: AsyncSession = Depends(get_session)):
    inv = await session.get(Investigation, inv_id)
    if not inv:
        raise HTTPException(404, "Not found")
    events = (await session.scalars(select(InvestigationEvent).where(InvestigationEvent.investigation_id == inv_id)
                                    .order_by(InvestigationEvent.seq))).all()
    rems = (await session.scalars(select(Remediation).where(Remediation.investigation_id == inv_id)
                                  .order_by(Remediation.created_at))).all()
    return {**_summary(inv), "report": inv.report,
            "events": [{"seq": e.seq, "type": e.type, "data": e.data, "created_at": e.created_at.isoformat()}
                       for e in events],
            "remediations": [rsvc.serialize(r) for r in rems]}


@router.get("/investigations/{inv_id}/stream", dependencies=[Depends(require(Role.viewer))])
async def stream(inv_id: str, request: Request, after: int = 0, session: AsyncSession = Depends(get_session)):
    """Server-sent events: replays events with seq > after, then streams live ones."""
    if not await session.get(Investigation, inv_id):
        raise HTTPException(404, "Not found")
    queue = bus.subscribe(inv_id)
    backlog = (await session.scalars(select(InvestigationEvent).where(
        InvestigationEvent.investigation_id == inv_id, InvestigationEvent.seq > after)
        .order_by(InvestigationEvent.seq))).all()
    backlog_payload = [{"seq": e.seq, "type": e.type, "data": e.data, "created_at": e.created_at.isoformat()}
                       for e in backlog]

    async def gen():
        last = after
        try:
            for ev in backlog_payload:
                last = ev["seq"]
                yield {"id": str(ev["seq"]), "event": "message", "data": json.dumps(ev)}
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
                    continue
                if ev.get("ephemeral"):  # live token stream: no id, never replayed
                    yield {"event": "message", "data": json.dumps(ev)}
                    continue
                if ev["seq"] <= last:
                    continue
                last = ev["seq"]
                yield {"id": str(ev["seq"]), "event": "message", "data": json.dumps(ev)}
        finally:
            bus.unsubscribe(inv_id, queue)

    return EventSourceResponse(gen())


@router.post("/investigations/{inv_id}/messages")
async def follow_up(inv_id: str, body: MessageIn, session: AsyncSession = Depends(get_session),
                    principal: Principal = Depends(require(Role.viewer))):
    inv = await session.get(Investigation, inv_id)
    if not inv:
        raise HTTPException(404, "Not found")
    if runner.is_running(inv_id):
        raise HTTPException(409, "The copilot is still working on this thread")
    inv.status = "queued"
    await session.commit()
    await EventRecorder(inv_id).emit("user_message", {"text": body.prompt, "by": principal.name})
    runner.start(inv_id, body.prompt)
    return _summary(inv)


@router.post("/investigations/{inv_id}/cancel", dependencies=[Depends(require(Role.viewer))])
async def cancel(inv_id: str):
    return {"cancelled": await runner.cancel(inv_id)}


@router.delete("/investigations/{inv_id}", status_code=204)
async def delete(inv_id: str, session: AsyncSession = Depends(get_session),
                 principal: Principal = Depends(require(Role.operator))):
    inv = await session.get(Investigation, inv_id)
    if not inv:
        raise HTTPException(404, "Not found")
    await runner.cancel(inv_id)
    await session.delete(inv)
    await session.commit()
    await audit(principal.name, "investigation.deleted", inv_id)


# ------------------------------------------------------------------------------------- remediations
async def _rem(session: AsyncSession, rem_id: str) -> Remediation:
    rem = await session.get(Remediation, rem_id)
    if not rem:
        raise HTTPException(404, "Remediation not found")
    return rem


def _handle(exc: Exception) -> HTTPException:
    if isinstance(exc, rsvc.RemediationError):
        return HTTPException(409, str(exc))
    if isinstance(exc, (LookupError, PermissionError)):
        return HTTPException(409, str(exc))
    return HTTPException(500, f"{type(exc).__name__}: {exc}")


@router.post("/remediations/{rem_id}/dry-run")
async def remediation_dry_run(rem_id: str, session: AsyncSession = Depends(get_session),
                              principal: Principal = Depends(require(Role.operator))):
    try:
        return rsvc.serialize(await rsvc.dry_run(session, await _rem(session, rem_id), principal))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc


@router.post("/remediations/{rem_id}/approve")
async def remediation_approve(rem_id: str, session: AsyncSession = Depends(get_session),
                              principal: Principal = Depends(require(Role.operator))):
    try:
        return rsvc.serialize(await rsvc.approve_and_execute(session, await _rem(session, rem_id), principal))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc


@router.post("/remediations/{rem_id}/reject")
async def remediation_reject(rem_id: str, body: RejectIn, session: AsyncSession = Depends(get_session),
                             principal: Principal = Depends(require(Role.operator))):
    try:
        return rsvc.serialize(await rsvc.reject(session, await _rem(session, rem_id), principal, body.reason))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
