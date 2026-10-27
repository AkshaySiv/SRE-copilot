from __future__ import annotations

import asyncio
from abc import abstractmethod

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from app.plugins import HealthStatus, Plugin, PluginKind


class CommonLLMConfig(BaseModel):
    model: str = Field(description="Model name / deployment id")
    temperature: float = Field(0.1, ge=0, le=2, description="Low values give more deterministic investigations")
    max_tokens: int = Field(4096, ge=256, le=64000)
    timeout_seconds: int = Field(120, ge=5, le=900)


class LLMProvider(Plugin):
    kind = PluginKind.LLM
    capabilities = frozenset({"chat", "tool_calling"})

    @abstractmethod
    def chat_model(self) -> BaseChatModel: ...

    async def health(self) -> HealthStatus:
        try:
            resp = await asyncio.wait_for(
                self.chat_model().ainvoke([HumanMessage(content="Reply with the single word: ok")]), timeout=60
            )
            text = resp.content if isinstance(resp.content, str) else str(resp.content)
            return HealthStatus(True, f"Model responded: {text[:60]!r}")
        except Exception as exc:  # noqa: BLE001
            return HealthStatus(False, f"{type(exc).__name__}: {exc}")
