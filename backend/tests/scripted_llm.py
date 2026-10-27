"""A deterministic LLM plugin used by tests - also a worked example of registering a custom plugin."""
from __future__ import annotations

import json
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import BaseModel

from app.connectors.llm.base import LLMProvider
from app.plugins import register_plugin

SCRIPT: list[AIMessage] = []


class ScriptedChatModel(BaseChatModel):
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        msg = SCRIPT[min(self.calls, len(SCRIPT) - 1)]
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs: Any):
        """Streams the scripted message word by word; tool calls arrive in the last chunk."""
        msg = SCRIPT[min(self.calls, len(SCRIPT) - 1)]
        self.calls += 1
        words = msg.content.split(" ") if isinstance(msg.content, str) and msg.content else []
        for i, w in enumerate(words):
            yield ChatGenerationChunk(message=AIMessageChunk(content=w if i == 0 else " " + w))
        chunks = [{"name": tc["name"], "args": json.dumps(tc["args"]), "id": tc["id"], "index": i}
                  for i, tc in enumerate(msg.tool_calls or [])]
        yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_call_chunks=chunks))

    def bind_tools(self, tools, **kwargs):
        return self


class ScriptedConfig(BaseModel):
    pass


@register_plugin
class ScriptedLLM(LLMProvider):
    type = "scripted"
    display_name = "Scripted (tests)"
    description = "Replays a fixed list of messages."
    Config = ScriptedConfig

    def __init__(self, name, config):
        super().__init__(name, config)
        self._model = ScriptedChatModel()

    def chat_model(self) -> BaseChatModel:
        self._model.calls = 0
        return self._model
