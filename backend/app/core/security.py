"""Secret encryption at rest and output redaction."""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings

log = logging.getLogger(__name__)
ENC_PREFIX = "enc:v1:"
MASK = "••••••••"


class SecretBox:
    def __init__(self, key: str | bytes):
        self._fernet = Fernet(key if isinstance(key, bytes) else key.encode())

    def encrypt(self, value: str) -> str:
        if value.startswith(ENC_PREFIX):
            return value
        return ENC_PREFIX + self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        if not isinstance(value, str) or not value.startswith(ENC_PREFIX):
            return value
        try:
            return self._fernet.decrypt(value[len(ENC_PREFIX):].encode()).decode()
        except InvalidToken as exc:  # key rotated / wrong key
            raise ValueError("Unable to decrypt secret - was SRECOPILOT_SECRET_KEY changed?") from exc


_box: SecretBox | None = None


def get_secret_box() -> SecretBox:
    global _box
    if _box is None:
        settings = get_settings()
        key = settings.secret_key
        if not key:
            if settings.env == "production":
                raise RuntimeError("SRECOPILOT_SECRET_KEY must be set in production")
            path = Path(settings.data_dir) / ".secret_key"
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text(Fernet.generate_key().decode())
                os.chmod(path, 0o600)
                log.warning("Generated a development secret key at %s - set SRECOPILOT_SECRET_KEY in production", path)
            key = path.read_text().strip()
        _box = SecretBox(key)
    return _box


# --- Redaction of tool output before it is sent to an LLM or stored ------------------------------
_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[REDACTED PRIVATE KEY]"),
    (re.compile(r"(?i)\b(authorization:\s*(?:bearer|basic)\s+)[A-Za-z0-9._~+/=-]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)(\"?\s*[:=]\s*\"?)([^\s\"',;]{4,})"),
     r"\1\2[REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), "[REDACTED JWT]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED AWS KEY]"),
    (re.compile(r"(?i)(://[^:/\s]+:)([^@/\s]+)(@)"), r"\1[REDACTED]\3"),
]


def redact(text: str) -> str:
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text
