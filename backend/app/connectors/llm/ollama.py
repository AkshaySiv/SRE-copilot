from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from pydantic import Field

from app.plugins import register_plugin

from .base import CommonLLMConfig, LLMProvider


class OllamaConfig(CommonLLMConfig):
    model: str = Field("qwen2.5:14b", description="A model with tool-calling support (qwen2.5, llama3.1, mistral-nemo…)")
    base_url: str = "http://ollama:11434"
    num_ctx: int = Field(32768, description="Context window; investigations need a large one")


@register_plugin
class OllamaLLM(LLMProvider):
    type = "ollama"
    display_name = "Ollama (local)"
    description = "Locally hosted models via Ollama - fully air-gapped."
    Config = OllamaConfig

    def chat_model(self) -> BaseChatModel:
        from langchain_ollama import ChatOllama

        c: OllamaConfig = self.config
        return ChatOllama(model=c.model, base_url=c.base_url, temperature=c.temperature,
                          num_ctx=c.num_ctx, num_predict=c.max_tokens)
