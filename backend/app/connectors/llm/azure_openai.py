from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from pydantic import Field, SecretStr

from app.plugins import register_plugin

from .base import CommonLLMConfig, LLMProvider


class AzureOpenAIConfig(CommonLLMConfig):
    model: str = Field(description="Azure deployment name")
    endpoint: str = Field(description="https://<resource>.openai.azure.com")
    api_version: str = "2024-10-21"
    api_key: SecretStr


@register_plugin
class AzureOpenAILLM(LLMProvider):
    type = "azure_openai"
    display_name = "Azure OpenAI"
    description = "OpenAI models hosted in your Azure tenant."
    Config = AzureOpenAIConfig

    def chat_model(self) -> BaseChatModel:
        from langchain_openai import AzureChatOpenAI

        c: AzureOpenAIConfig = self.config
        return AzureChatOpenAI(
            azure_deployment=c.model, azure_endpoint=c.endpoint, api_version=c.api_version,
            api_key=c.api_key.get_secret_value(), temperature=c.temperature, max_tokens=c.max_tokens,
            timeout=c.timeout_seconds, max_retries=2,
        )
