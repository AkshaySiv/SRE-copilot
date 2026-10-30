from __future__ import annotations

import hashlib
import json
import re
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.kubernetes.base import KubernetesConnector
from app.connectors.kubernetes.diagnose import diagnose, explain_context
from app.connectors.llm.base import LLMProvider
from app.core.security import redact
from app.core.auth import Role, require
from app.db.models import Integration
from app.db.session import get_session
from app.services.integrations import get_plugin, pool, resolve_scope

router = APIRouter(prefix="/clusters", tags=["clusters"], dependencies=[Depends(require(Role.viewer))])


async def _cluster(session: AsyncSession, cluster_id: str) -> KubernetesConnector:
    try:
        integ, plugin = await get_plugin(session, cluster_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not isinstance(plugin, KubernetesConnector):
        raise HTTPException(400, f"Integration '{integ.name}' is not a Kubernetes cluster")
    return plugin


def _wrap(exc: Exception) -> HTTPException:
    if isinstance(exc, PermissionError):
        return HTTPException(403, str(exc))
    if isinstance(exc, (LookupError, ValueError)):
        return HTTPException(404 if isinstance(exc, LookupError) else 400, str(exc))
    return HTTPException(502, f"Cluster error: {type(exc).__name__}: {exc}")


@router.get("")
async def list_clusters(session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(Integration).where(Integration.kind == "kubernetes")
                                  .order_by(Integration.is_default.desc(), Integration.name))).all()
    return [{"id": r.id, "name": r.name, "type": r.type, "enabled": r.enabled,
             "environment": (r.config or {}).get("environment", ""), "is_default": r.is_default} for r in rows]


@router.get("/{cluster_id}/overview")
async def overview(cluster_id: str, session: AsyncSession = Depends(get_session)):
    k = await _cluster(session, cluster_id)
    try:
        return await k.overview()
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


@router.get("/{cluster_id}/topology")
async def topology(cluster_id: str, namespace: str | None = None, include_config: bool = False,
                   session: AsyncSession = Depends(get_session)):
    k = await _cluster(session, cluster_id)
    try:
        return await k.topology(namespace, include_config)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


@router.get("/{cluster_id}/resources/{kind}/{name}")
async def resource(cluster_id: str, kind: str, name: str, namespace: str | None = None,
                   session: AsyncSession = Depends(get_session)):
    k = await _cluster(session, cluster_id)
    try:
        detail = await k.resource_detail(kind, name, namespace)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc
    if detail is None:
        raise HTTPException(404, f"{kind} {namespace}/{name} not found")
    return detail


@router.get("/{cluster_id}/logs")
async def logs(cluster_id: str, pod: str, namespace: str, container: str | None = None, tail: int = 300,
               previous: bool = False, session: AsyncSession = Depends(get_session)):
    k = await _cluster(session, cluster_id)
    try:
        return {"logs": await k.pod_logs(pod, namespace, container, min(tail, 5000), previous)}
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


@router.get("/{cluster_id}/events")
async def events(cluster_id: str, namespace: str | None = None, warnings_only: bool = False,
                 session: AsyncSession = Depends(get_session)):
    k = await _cluster(session, cluster_id)
    try:
        return await k.list_events(namespace, None, warnings_only, 200)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


@router.get("/{cluster_id}/resources/{kind}/{name}/diagnosis")
async def diagnosis(cluster_id: str, kind: str, name: str, namespace: str | None = None,
                    session: AsyncSession = Depends(get_session)):
    """Rule-based, instant: the exact reason a resource is failing (missing Secret, bad image tag, OOM, ...)."""
    k = await _cluster(session, cluster_id)
    try:
        out = await diagnose(k, kind, name, namespace)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc
    llms = (await resolve_scope(session, {})).get("llm") or []
    return {**out, "ai_available": bool(llms)}


_EXPLAIN_CACHE: dict[str, tuple[float, dict]] = {}
_EXPLAIN_TTL = 600
_EXPLAIN_PROMPT = """You are an SRE. From the Kubernetes evidence below, state in ONE short sentence the most likely root cause of the failure, and in ONE short sentence the fix. Be specific: name the exact secret, variable, image, file, host or error. Do not speculate beyond the evidence; if it is insufficient, say what to check. Reply with ONLY a JSON object: {"cause": "...", "fix": "...", "confidence": "high|medium|low"}"""


@router.post("/{cluster_id}/resources/{kind}/{name}/explain")
async def explain(cluster_id: str, kind: str, name: str, namespace: str | None = None,
                  session: AsyncSession = Depends(get_session)):
    """On-demand one-line AI explanation, for failures the rules cannot pin down (e.g. app-specific exit codes)."""
    from langchain_core.messages import HumanMessage, SystemMessage

    k = await _cluster(session, cluster_id)
    llms = (await resolve_scope(session, {})).get("llm") or []
    if not llms:
        raise HTTPException(409, "No LLM configured. Add one under Integrations.")
    try:
        context = redact(await explain_context(k, kind, name, namespace))
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc
    if not context:
        raise HTTPException(404, f"{kind} {namespace}/{name} not found")
    key = hashlib.sha256(f"{cluster_id}|{llms[0].id}|{context}".encode()).hexdigest()
    hit = _EXPLAIN_CACHE.get(key)
    if hit and time.time() - hit[0] < _EXPLAIN_TTL:
        return {**hit[1], "cached": True}
    llm = await pool.get(llms[0])
    assert isinstance(llm, LLMProvider)
    try:
        msg = await llm.chat_model().ainvoke([SystemMessage(content=_EXPLAIN_PROMPT), HumanMessage(content=context)])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"AI model error: {type(exc).__name__}: {exc}") from exc
    text = msg.content if isinstance(msg.content, str) else "".join(
        p.get("text", "") for p in msg.content if isinstance(p, dict))
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    match = re.search(r"\{.*\}", text, re.S)
    try:
        data = json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        data = {}
    out = {"cause": str(data.get("cause") or text[:300]), "fix": data.get("fix"),
           "confidence": data.get("confidence") if data.get("confidence") in ("high", "medium", "low") else "medium",
           "model": llm.name}
    _EXPLAIN_CACHE[key] = (time.time(), out)
    if len(_EXPLAIN_CACHE) > 500:
        _EXPLAIN_CACHE.pop(next(iter(_EXPLAIN_CACHE)))
    return {**out, "cached": False}
