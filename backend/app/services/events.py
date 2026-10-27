"""Persisted, replayable event stream per investigation.

Events are written to the DB (so a page refresh or another user can replay them) and fanned out to live
subscribers in-process. For multi-replica deployments, swap InMemoryBus for a Redis/NATS pub-sub
implementation with the same three methods - the API layer only depends on this interface.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from sqlalchemy import func, select

from app.db.models import InvestigationEvent
from app.db.session import sessionmaker


class InMemoryBus:
    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)

    def subscribe(self, key: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs[key].add(q)
        return q

    def unsubscribe(self, key: str, q: asyncio.Queue) -> None:
        self._subs[key].discard(q)
        if not self._subs[key]:
            self._subs.pop(key, None)

    def publish(self, key: str, event: dict[str, Any]) -> None:
        for q in list(self._subs.get(key, ())):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass


bus = InMemoryBus()


class EventRecorder:
    """Appends ordered events for one investigation."""

    def __init__(self, investigation_id: str):
        self.investigation_id = investigation_id
        self._seq: int | None = None
        self._lock = asyncio.Lock()

    async def emit(self, type_: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        async with self._lock:
            async with sessionmaker()() as s:
                if self._seq is None:
                    self._seq = (await s.scalar(select(func.max(InvestigationEvent.seq)).where(
                        InvestigationEvent.investigation_id == self.investigation_id))) or 0
                self._seq += 1
                ev = InvestigationEvent(investigation_id=self.investigation_id, seq=self._seq, type=type_, data=data or {})
                s.add(ev)
                await s.commit()
                payload = {"seq": ev.seq, "type": type_, "data": ev.data, "created_at": ev.created_at.isoformat()}
        bus.publish(self.investigation_id, payload)
        return payload
