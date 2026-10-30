from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select, update

from app.agent.engine import RunRequest, run_investigation
from app.agent.report import IncidentReport
from app.connectors.llm.base import LLMProvider
from app.core.config import get_settings
from app.db.models import Investigation, InvestigationEvent, Remediation
from app.db.session import sessionmaker
from app.plugins import ToolSpec
from app.remediation.base import ACTIONS, catalog
from app.timeline import summarize, to_text
from app.timeline.service import build_timeline

from .events import EventRecorder
from .integrations import pool, resolve_scope

log = logging.getLogger(__name__)
_WORD = re.compile(r"[a-z0-9][a-z0-9_.-]{2,}")
_STOP = {"the", "and", "for", "with", "not", "are", "was", "pod", "pods", "from", "this", "that", "into", "after"}


def _keywords(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP}


async def similar_incidents(query: str, exclude_id: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
    """Lightweight lexical similarity over past reports. Swap for pgvector embeddings at scale."""
    words = _keywords(query)
    if not words:
        return []
    async with sessionmaker()() as s:
        rows = (await s.scalars(select(Investigation).where(
            Investigation.kind == "incident", Investigation.status == "completed")
            .order_by(Investigation.created_at.desc()).limit(500))).all()
        scored = []
        for inv in rows:
            if inv.id == exclude_id:
                continue
            overlap = words & _keywords(f"{inv.title} {inv.summary} {inv.root_cause} {inv.prompt}")
            if len(overlap) >= 2:
                scored.append((len(overlap), inv))
        scored.sort(key=lambda t: -t[0])
        out = []
        for _, inv in scored[:limit]:
            fixes = (await s.scalars(select(Remediation).where(Remediation.investigation_id == inv.id,
                                                               Remediation.status == "executed"))).all()
            out.append({"id": inv.id, "date": inv.created_at.date().isoformat(), "title": inv.title,
                        "root_cause": inv.root_cause, "fixes_applied": [f.title for f in fixes]})
        return out


class SearchIncidentsArgs(BaseModel):
    query: str = Field(description="Symptoms or component names, e.g. 'payments-api CrashLoopBackOff'")


class StartRequest(BaseModel):
    kind: str = "incident"
    prompt: str
    scope: dict[str, Any] = Field(default_factory=dict)


def _focus(scope: dict[str, Any]) -> str:
    bits = []
    if scope.get("namespace"):
        bits.append(f"namespace '{scope['namespace']}'")
    res = scope.get("resource")
    if isinstance(res, dict) and res.get("name"):
        bits.append(f"{res.get('kind', 'resource')} '{res['name']}'")
    if scope.get("host"):
        bits.append(f"host '{scope['host']}'")
    if not bits:
        return ""
    return f"The user is looking at {' / '.join(bits)}. Start there, but follow the evidence wherever it leads."


class InvestigationRunner:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}

    def is_running(self, inv_id: str) -> bool:
        t = self._tasks.get(inv_id)
        return bool(t and not t.done())

    def start(self, inv_id: str, prompt: str) -> None:
        if self.is_running(inv_id):
            raise RuntimeError("Investigation already running")
        task = asyncio.create_task(self._run(inv_id, prompt), name=f"investigation-{inv_id}")
        self._tasks[inv_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(inv_id, None))

    async def cancel(self, inv_id: str) -> bool:
        task = self._tasks.get(inv_id)
        if task and not task.done():
            task.cancel()
            return True
        return False

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    @staticmethod
    async def recover_interrupted() -> None:
        async with sessionmaker()() as s:
            await s.execute(update(Investigation).where(Investigation.status.in_(("queued", "running")))
                            .values(status="failed", error="Interrupted by a server restart"))
            await s.commit()

    async def _history(self, inv_id: str) -> list[tuple[str, str]]:
        async with sessionmaker()() as s:
            events = (await s.scalars(select(InvestigationEvent).where(
                InvestigationEvent.investigation_id == inv_id,
                InvestigationEvent.type.in_(("user_message", "answer", "report")))
                .order_by(InvestigationEvent.seq))).all()
        turns: list[tuple[str, str]] = []
        for ev in events:
            if ev.type == "user_message":
                turns.append(("user", ev.data.get("text", "")))
            elif ev.type == "answer":
                turns.append(("assistant", ev.data.get("markdown", "")))
            else:
                r = ev.data.get("report") or {}
                turns.append(("assistant", f"Incident report: {r.get('title')}\nSummary: {r.get('summary')}\n"
                                           f"Root cause: {(r.get('root_cause') or {}).get('description')}"))
        return turns[:-1] if turns and turns[-1][0] == "user" else turns  # current prompt is passed separately

    async def _run(self, inv_id: str, prompt: str) -> None:
        settings = get_settings()
        rec = EventRecorder(inv_id)
        async with sessionmaker()() as s:
            inv = await s.get(Investigation, inv_id)
            assert inv is not None
            inv.status = "running"
            await s.commit()
            mode = inv.kind if inv.kind in ("incident", "question") else "incident"
            is_followup = bool(inv.report or inv.summary)
            scope = dict(inv.scope or {})
            scoped = await resolve_scope(s, scope)
        try:
            llm_plugin: LLMProvider | None = None
            if scoped.get("llm"):
                llm_plugin = await pool.get(scoped["llm"][0])
                assert isinstance(llm_plugin, LLMProvider)
            effective_mode = "question" if (mode == "question" or is_followup) else "incident"
            if llm_plugin is None and effective_mode == "question":
                raise RuntimeError("No LLM configured. Add one under Integrations to ask questions.")
            plugins = []
            for kind in ("kubernetes", "vm", "monitoring"):
                for integ in scoped.get(kind, []):
                    try:
                        plugins.append(await pool.get(integ))
                    except Exception as exc:  # noqa: BLE001
                        await rec.emit("thinking", {"text": f"Skipping integration {integ.name}: {exc}"})
            await rec.emit("status", {"phase": "starting", "llm": llm_plugin.name if llm_plugin else None,
                                      "integrations": [p.name for p in plugins]})

            # 1. The deterministic cross-layer timeline - needs no LLM.
            tl = None
            timeline_text = ""
            if effective_mode == "incident" and settings.timeline_enabled and plugins:
                await rec.emit("status", {"phase": "collecting"})
                try:
                    tl = await build_timeline(
                        plugins, prompt, scope, lookback_minutes=settings.timeline_lookback_minutes,
                        timeout=settings.timeline_timeout_seconds, max_log_pods=settings.timeline_max_log_pods,
                        progress=rec.emit)
                    await rec.emit("timeline", {"timeline": tl.model_dump(mode="json")})
                    timeline_text = to_text(tl)
                except Exception as exc:  # noqa: BLE001 - fall back to the classic investigation
                    log.exception("Timeline build failed for %s", inv_id)
                    tl = None
                    await rec.emit("thinking", {"text": f"Timeline collection failed ({exc}); investigating step by step."})

            if llm_plugin is None:
                if tl is None:
                    raise RuntimeError("No LLM configured and no timeline could be collected. "
                                       "Add an LLM and at least one cluster, VM or monitoring integration.")
                await self._finish_timeline_only(inv_id, rec, tl, "No AI model connected - showing the correlated "
                                                 "timeline only. Add an LLM under Integrations for an AI report "
                                                 "and suggested fixes.")
                return

            # 2. The model reviews the timeline (or investigates step by step) and writes the report.
            async def search_past(query: str):
                return await similar_incidents(query, exclude_id=inv_id) or "no similar past incidents"

            internal = [ToolSpec("search_past_incidents", "Search previously investigated incidents and how they were "
                                 "fixed.", SearchIncidentsArgs, search_past, "Searched past incidents for '{query}'",
                                 "history")]
            past = await similar_incidents(prompt, exclude_id=inv_id, limit=3)
            past_text = "\n".join(f"- {p['date']}: {p['title']} - cause: {p['root_cause']} - fixed by: "
                                  f"{', '.join(p['fixes_applied']) or 'n/a'}" for p in past)
            kinds_in_scope = {str(p.kind) for p in plugins}

            try:
                result = await asyncio.wait_for(run_investigation(RunRequest(
                    mode=effective_mode, prompt=prompt, llm=llm_plugin.chat_model(), plugins=plugins, emit=rec.emit,
                    history=await self._history(inv_id), focus=_focus(scope), past_incidents=past_text,
                    action_catalog=catalog(kinds_in_scope) if settings.remediation_mode != "disabled" else [],
                    internal_tools=internal, max_steps=settings.agent_max_steps,
                    tool_timeout=settings.tool_timeout_seconds, tool_output_max_chars=settings.tool_output_max_chars,
                    max_parallel_tools=settings.max_parallel_tools,
                    live=rec.live if settings.stream_responses else None, timeline=timeline_text,
                )), timeout=settings.agent_timeout_seconds)
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except Exception as exc:  # noqa: BLE001
                if tl is None:
                    raise
                # The model failed, but the timeline is still the answer - keep it rather than failing the run.
                log.exception("AI review failed for %s; keeping the timeline", inv_id)
                why = f"Timed out after {settings.agent_timeout_seconds}s" if isinstance(exc, asyncio.TimeoutError) \
                    else f"{type(exc).__name__}: {exc}"
                await self._finish_timeline_only(inv_id, rec, tl, f"The AI review failed ({why}). "
                                                 "The correlated timeline below is still valid.", error=why)
                return

            async with sessionmaker()() as s:
                inv = await s.get(Investigation, inv_id)
                assert inv is not None
                if result.report is not None:
                    report = result.report
                    proposals = await self._create_remediations(s, inv_id, report, plugins)
                    inv.report = report.model_dump(mode="json")
                    inv.title = report.title[:300] or inv.title
                    inv.severity = report.severity
                    inv.summary = report.summary
                    inv.root_cause = report.root_cause.description if report.root_cause else None
                    await s.commit()
                    await rec.emit("report", {"report": inv.report})
                    for p in proposals:
                        await rec.emit("remediation", p)
                else:
                    if not inv.summary:
                        inv.summary = (result.answer or "")[:2000]
                    await rec.emit("answer", {"markdown": result.answer or ""})
                inv.status = "completed"
                inv.error = None
                inv.completed_at = datetime.now(timezone.utc)
                await s.commit()
            await rec.emit("done", {"status": "completed", "tool_calls": result.tool_calls, "steps": result.steps,
                                    "usage": result.usage})
        except asyncio.CancelledError:
            await self._finish(inv_id, "cancelled", "Cancelled by user")
            await rec.emit("done", {"status": "cancelled"})
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("Investigation %s failed", inv_id)
            msg = f"{type(exc).__name__}: {exc}" if not isinstance(exc, asyncio.TimeoutError) else \
                f"Timed out after {settings.agent_timeout_seconds}s"
            await self._finish(inv_id, "failed", msg)
            await rec.emit("error", {"message": msg})
            await rec.emit("done", {"status": "failed"})

    @staticmethod
    async def _finish_timeline_only(inv_id: str, rec: EventRecorder, tl, notice: str, error: str | None = None) -> None:
        verdict = summarize(tl)
        async with sessionmaker()() as s:
            inv = await s.get(Investigation, inv_id)
            assert inv is not None
            inv.summary = verdict["summary"]
            inv.root_cause = verdict["root_cause"]
            inv.severity = verdict["severity"]
            inv.status = "completed"
            inv.error = f"AI review failed: {error}" if error else None
            inv.completed_at = datetime.now(timezone.utc)
            await s.commit()
        await rec.emit("notice", {"text": notice, "level": "warning" if error else "info"})
        await rec.emit("done", {"status": "completed", "timeline_only": True, "tool_calls": 0})

    @staticmethod
    async def _finish(inv_id: str, status: str, error: str) -> None:
        async with sessionmaker()() as s:
            inv = await s.get(Investigation, inv_id)
            if inv:
                inv.status = status
                inv.error = error
                inv.completed_at = datetime.now(timezone.utc)
                await s.commit()

    @staticmethod
    async def _create_remediations(session, inv_id: str, report: IncidentReport, plugins) -> list[dict]:
        if get_settings().remediation_mode == "disabled":
            return []
        by_name = {p.name: p for p in plugins}
        created = []
        for idx, rec in enumerate(report.recommendations):
            pa = rec.automated_action
            if pa is None:
                continue
            action = ACTIONS.get(pa.action)
            plugin = by_name.get(pa.integration)
            note = None
            if action is None:
                note = f"unknown action '{pa.action}'"
            elif plugin is None or str(plugin.kind) != str(action.target_kind):
                note = f"integration '{pa.integration}' is not a {action.target_kind} integration in scope"
            if note is None:
                try:
                    params = action.Params.model_validate(pa.params)
                except ValidationError as exc:
                    note = f"invalid parameters: {exc.errors(include_url=False)[:2]}"
            if note:
                report.recommendations[idx].description += f"\n\n(Not automatable: {note})"
                continue
            integ_row = (await pool_integration_id(session, pa.integration))
            rem = Remediation(investigation_id=inv_id, action=action.name, integration_id=integ_row,
                              params=params.model_dump(mode="json"), title=rec.title[:300],
                              rationale=rec.description, risk=action.risk_for(params), plan=action.plan(params))
            session.add(rem)
            await session.flush()
            created.append({"id": rem.id, "action": rem.action, "title": rem.title, "risk": rem.risk,
                            "plan": rem.plan, "params": rem.params, "integration": pa.integration,
                            "status": rem.status})
        return created


async def pool_integration_id(session, name: str) -> str:
    from app.db.models import Integration

    integ = await session.scalar(select(Integration).where(Integration.name == name))
    return integ.id if integ else ""


runner = InvestigationRunner()
