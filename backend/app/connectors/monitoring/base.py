from __future__ import annotations

import base64
from abc import abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from pydantic import BaseModel, Field, SecretStr

from app.plugins import Plugin, PluginKind


class HttpAuthConfig(BaseModel):
    url: str = Field(description="Base URL, e.g. http://prometheus-operated.monitoring:9090")
    bearer_token: SecretStr | None = None
    username: str | None = None
    password: SecretStr | None = None
    headers: dict[str, str] = Field(default_factory=dict, description="Extra headers (e.g. X-Scope-OrgID)")
    verify_tls: bool = True
    ca_bundle_path: str | None = None
    timeout_seconds: int = Field(30, ge=3, le=300)


class MonitoringPlugin(Plugin):
    """Base for metrics / logs / alerts backends. Capabilities: metrics, logs, alerts."""

    kind = PluginKind.MONITORING

    def __init__(self, name: str, config: HttpAuthConfig):
        super().__init__(name, config)
        self._client: httpx.AsyncClient | None = None

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            c = self.config
            headers = dict(c.headers)
            if c.bearer_token:
                headers["Authorization"] = f"Bearer {c.bearer_token.get_secret_value()}"
            elif c.username and c.password:
                token = base64.b64encode(f"{c.username}:{c.password.get_secret_value()}".encode()).decode()
                headers["Authorization"] = f"Basic {token}"
            self._client = httpx.AsyncClient(base_url=c.url.rstrip("/"), headers=headers,
                                             verify=c.ca_bundle_path or c.verify_tls, timeout=c.timeout_seconds)
        return self._client

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = await self.client().get(path, params=params)
        if resp.status_code >= 400:
            raise RuntimeError(f"{resp.status_code} from {path}: {resp.text[:400]}")
        return resp.json()

    async def _post_json(self, path: str, body: Any) -> Any:
        resp = await self.client().post(path, json=body)
        if resp.status_code >= 400:
            raise RuntimeError(f"{resp.status_code} from {path}: {resp.text[:400]}")
        return resp.json()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    @abstractmethod
    async def health(self): ...


def window(minutes: int) -> tuple[datetime, datetime]:
    end = datetime.now(timezone.utc)
    return end - timedelta(minutes=minutes), end


def auto_step(minutes: int, points: int = 60) -> str:
    return f"{max(15, int(minutes * 60 / points))}s"
