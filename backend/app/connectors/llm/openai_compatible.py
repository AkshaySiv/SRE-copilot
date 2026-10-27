from __future__ import annotations

import httpx
from langchain_core.language_models import BaseChatModel
from pydantic import Field, SecretStr

from app.plugins import register_plugin

from .base import CommonLLMConfig, LLMProvider


class OpenAICompatibleConfig(CommonLLMConfig):
    base_url: str = Field("https://api.openai.com/v1",
                          description="Any OpenAI-compatible endpoint: OpenAI, vLLM, LiteLLM, LocalAI, TGI, LM Studio")
    api_key: SecretStr | None = Field(None, description="Leave empty for endpoints without auth")
    verify_tls: bool = True
    ca_bundle_path: str | None = Field(None, description="Custom CA file for internal endpoints")
    extra_headers: dict[str, str] = Field(default_factory=dict)


@register_plugin
class OpenAICompatibleLLM(LLMProvider):
    type = "openai_compatible"
    display_name = "OpenAI-compatible API"
    description = "OpenAI or any self-hosted OpenAI-compatible server (vLLM, LiteLLM, LocalAI…). Works air-gapped."
    Config = OpenAICompatibleConfig

    def chat_model(self) -> BaseChatModel:
        from langchain_openai import ChatOpenAI

        c: OpenAICompatibleConfig = self.config
        verify: bool | str = c.ca_bundle_path or c.verify_tls
        return ChatOpenAI(
            model=c.model,
            base_url=c.base_url,
            api_key=c.api_key.get_secret_value() if c.api_key else "not-needed",
            temperature=c.temperature,
            max_tokens=c.max_tokens,
            timeout=c.timeout_seconds,
            default_headers=c.extra_headers or None,
            http_async_client=httpx.AsyncClient(verify=verify, timeout=c.timeout_seconds),
            max_retries=2,
        )
