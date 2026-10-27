"""Authentication and role-based authorization.

Roles are ordered: viewer < operator < admin.
  viewer   - browse clusters, run investigations / ask questions
  operator - dry-run, approve and execute remediations
  admin    - manage integrations
"""
from __future__ import annotations

import hmac
from dataclasses import dataclass
from enum import IntEnum

from fastapi import Depends, HTTPException, Request, status

from .config import get_settings


class Role(IntEnum):
    viewer = 1
    operator = 2
    admin = 3


@dataclass(frozen=True)
class Principal:
    name: str
    role: Role

    def to_dict(self) -> dict:
        return {"name": self.name, "role": self.role.name}


def _parse_tokens(raw: str) -> list[tuple[str, Principal]]:
    out = []
    for entry in filter(None, (e.strip() for e in raw.split(","))):
        parts = entry.split(":")
        if len(parts) < 2:
            continue
        token, role = parts[0], parts[1]
        name = parts[2] if len(parts) > 2 else role
        out.append((token, Principal(name=name, role=Role[role])))
    return out


def get_principal(request: Request) -> Principal:
    settings = get_settings()
    if settings.auth_mode == "none":
        return Principal(name="local-admin", role=Role.admin)

    if settings.auth_mode == "header":
        user = request.headers.get(settings.header_user)
        if not user:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing identity header")
        groups = {g.strip() for g in request.headers.get(settings.header_groups, "").split(",") if g.strip()}
        role = Role.viewer
        if groups & set(settings.header_operator_groups):
            role = Role.operator
        if groups & set(settings.header_admin_groups):
            role = Role.admin
        return Principal(name=user, role=role)

    header = request.headers.get("Authorization", "")
    supplied = header[7:] if header.lower().startswith("bearer ") else ""
    if supplied:
        for token, principal in _parse_tokens(settings.auth_tokens):
            if hmac.compare_digest(token, supplied):
                return principal
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing bearer token",
                        headers={"WWW-Authenticate": "Bearer"})


def require(role: Role):
    def checker(principal: Principal = Depends(get_principal)) -> Principal:
        if principal.role < role:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Requires role '{role.name}'")
        return principal

    return checker
