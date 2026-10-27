from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from pydantic import Field

from app.plugins import register_plugin

from .base import CommonLLMConfig, LLMProvider


class OllamaConfig(CommonLLMConfig):
    model: str = Field("qwen2.5:7b", description="A model with tool-calling support (qwen2.5, qwen3, llama3.1, mistral-nemo…)")
    base_url: str = "http://localhost:11434"
    num_ctx: int = Field(16384, ge=2048, le=131072,
                         description="Context window. Larger = more memory and slower steps; 8192-16384 suits laptops")
    thinking: bool = Field(False, description="Let reasoning models (qwen3, deepseek-r1…) think before answering. "
                                              "Much slower; leave off for investigations")
    keep_alive: str = Field("30m", description="How long Ollama keeps the model loaded between steps")


@register_plugin
class OllamaLLM(LLMProvider):
    type = "ollama"
    display_name = "Ollama (local)"
    description = "Locally hosted models via Ollama - fully air-gapped."
    Config = OllamaConfig

    def chat_model(self) -> BaseChatModel:
        from langchain_ollama import ChatOllama

        c: OllamaConfig = self.config
        return ChatOllama(model=c.model, base_url=c.base_url, temperature=c.temperature, num_ctx=c.num_ctx,
                          num_predict=c.max_tokens, keep_alive=c.keep_alive, reasoning=c.thinking)
