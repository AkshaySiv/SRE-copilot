"""Turns plugin ToolSpecs into LLM tools and executes calls with validation, timeouts and redaction."""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import ValidationError

from app.core.security import redact
from app.plugins import Plugin, ToolSpec


@dataclass
class BoundTool:
    exposed_name: str
    spec: ToolSpec
    integration: str
    kind: str


def slug(value: str, max_len: int = 24) -> str:
    return (re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or "x")[:max_len]


def bind(plugins: list[Plugin], internal: list[ToolSpec] | None = None) -> dict[str, BoundTool]:
    tools: dict[str, BoundTool] = {}
    for plugin in plugins:
        for spec in plugin.tools():
            name = f"{slug(plugin.name)}__{spec.name}"[:64]
            tools[name] = BoundTool(name, spec, plugin.name, str(plugin.kind))
    for spec in internal or []:
        tools[spec.name] = BoundTool(spec.name, spec, "copilot", "internal")
    return tools


def as_langchain(tools: dict[str, BoundTool]) -> list[StructuredTool]:
    out = []
    for name, bt in tools.items():
        out.append(StructuredTool.from_function(
            coroutine=bt.spec.handler, name=name, args_schema=bt.spec.args_model,
            description=f"[{bt.kind}: {bt.integration}] {bt.spec.description}"[:1000],
        ))
    return out


def _to_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    return json.dumps(result, default=str, ensure_ascii=False, separators=(",", ":"))


class ToolExecutor:
    def __init__(self, tools: dict[str, BoundTool], emit: Callable[[str, dict], Awaitable[Any]], *,
                 timeout: int, max_chars: int, max_parallel: int):
        self.tools = tools
        self.emit = emit
        self.timeout = timeout
        self.max_chars = max_chars
        self._sem = asyncio.Semaphore(max_parallel)
        self.calls = 0

    async def __call__(self, call: dict[str, Any]) -> str:
        name, args, call_id = call.get("name", ""), call.get("args") or {}, call.get("id") or ""
        bt = self.tools.get(name)
        if bt is None:
            return f"ERROR: unknown tool '{name}'. Available: {', '.join(self.tools)}"
        label = bt.spec.render_label(args)
        await self.emit("tool_call", {"id": call_id, "tool": bt.spec.name, "integration": bt.integration,
                                      "kind": bt.kind, "label": label, "args": args})
        started = time.monotonic()
        ok = True
        async with self._sem:
            self.calls += 1
            try:
                parsed = bt.spec.args_model.model_validate(args)
                result = await asyncio.wait_for(bt.spec.handler(**parsed.model_dump()), timeout=self.timeout)
                text = _to_text(result)
            except ValidationError as exc:
                ok, text = False, f"ERROR: invalid arguments: {exc.errors(include_url=False)}"
            except asyncio.TimeoutError:
                ok, text = False, f"ERROR: tool timed out after {self.timeout}s"
            except Exception as exc:  # noqa: BLE001 - errors are information for the agent
                ok, text = False, f"ERROR: {type(exc).__name__}: {exc}"
        text = redact(text)
        if len(text) > self.max_chars:
            text = text[: self.max_chars] + f"\n…[truncated {len(text) - self.max_chars} chars - narrow the query]"
        await self.emit("tool_result", {"id": call_id, "tool": bt.spec.name, "integration": bt.integration,
                                        "label": label, "ok": ok, "duration_ms": int((time.monotonic() - started) * 1000),
                                        "output": text[:6000]})
        return text
