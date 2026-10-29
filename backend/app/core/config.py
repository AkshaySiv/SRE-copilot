"""Application settings. Every value can be overridden with an env var prefixed SRECOPILOT_."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SRECOPILOT_", env_file=".env", extra="ignore")

    env: Literal["development", "production"] = "development"
    log_level: str = "INFO"
    log_json: bool = False

    # Persistence. SQLite for single-node / demo, PostgreSQL (postgresql+asyncpg://...) for production.
    database_url: str = "sqlite+aiosqlite:///./data/sre-copilot.db"

    # Fernet key used to encrypt integration secrets at rest. Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    secret_key: str | None = None
    data_dir: str = "./data"

    # Auth. "token" = static bearer tokens "token:role:name" comma-separated; roles: viewer|operator|admin.
    # For SSO, put the app behind oauth2-proxy and use auth_mode=header (trusts X-Forwarded-User / -Groups).
    auth_mode: Literal["none", "token", "header"] = "none"
    auth_tokens: str = ""
    header_user: str = "X-Forwarded-User"
    header_groups: str = "X-Forwarded-Groups"
    header_admin_groups: list[str] = Field(default_factory=lambda: ["sre-admins"])
    header_operator_groups: list[str] = Field(default_factory=lambda: ["sre-operators"])

    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])

    # Extensibility: extra python modules to import so their @register_plugin / @register_action run.
    plugin_modules: list[str] = Field(default_factory=list)
    # GitOps-style seed of integrations (YAML). Upserted at startup, marked managed_by=bootstrap.
    bootstrap_file: str | None = None
    # Create the simulated demo cluster + monitoring on first start (empty database only).
    seed_demo: bool = False

    # Agent budget & guardrails
    agent_max_steps: int = 30
    agent_timeout_seconds: int = 900
    tool_timeout_seconds: int = 60
    tool_output_max_chars: int = 12_000
    max_parallel_tools: int = 4
    # Time-based investigation: collect + correlate evidence from every layer before the model reasons.
    timeline_enabled: bool = True
    timeline_lookback_minutes: int = 60
    timeline_timeout_seconds: int = 60
    timeline_max_log_pods: int = 6
    # Stream model output token-by-token to the UI (falls back automatically if a provider can't stream).
    stream_responses: bool = True

    # Remediation: "disabled" = report only, "approval" = human approves each action after a dry-run.
    remediation_mode: Literal["disabled", "approval"] = "approval"
    require_dry_run_before_execute: bool = True

    # Serve the built frontend from this directory (set in the container image).
    static_dir: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
