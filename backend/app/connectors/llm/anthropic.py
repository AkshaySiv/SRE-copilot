from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from pydantic import Field, SecretStr

from app.plugins import register_plugin

from .base import CommonLLMConfig, LLMProvider


class AnthropicConfig(CommonLLMConfig):
    model: str = Field("claude-sonnet-5-5", description="Claude model id")
    temperature: float | None = Field(None, ge=0, le=1, description="Leave empty - newer Claude models only accept the default")
    api_key: SecretStr
    base_url: str | None = Field(None, description="Override for a gateway / proxy")


@register_plugin
class AnthropicLLM(LLMProvider):
    type = "anthropic"
    display_name = "Anthropic Claude"
    description = "Claude models via the Anthropic API."
    Config = AnthropicConfig

    def chat_model(self) -> BaseChatModel:
        from langchain_anthropic import ChatAnthropic

        c: AnthropicConfig = self.config
        kwargs: dict = {"base_url": c.base_url} if c.base_url else {}
        if c.temperature is not None:
            kwargs["temperature"] = c.temperature
        return ChatAnthropic(
            model=c.model,
            api_key=c.api_key.get_secret_value(),
            max_tokens=c.max_tokens,
            timeout=c.timeout_seconds,
            max_retries=2,
            **kwargs,
        )
