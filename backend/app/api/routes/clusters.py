from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.kubernetes.base import KubernetesConnector
from app.core.auth import Role, require
from app.db.models import Integration
from app.db.session import get_session
from app.services.integrations import get_plugin

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
