"""A deterministic LLM plugin used by tests - also a worked example of registering a custom plugin."""
from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
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
