"""LangGraph investigation loop: agent <-> tools until done, then a provider-agnostic synthesis step."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from app.plugins import Plugin, ToolSpec

from . import prompts
from .report import IncidentReport, parse_report, report_schema_hint
from .toolkit import ToolExecutor, as_langchain, bind

log = logging.getLogger(__name__)
Emit = Callable[[str, dict], Awaitable[Any]]


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    steps: int


@dataclass
class RunRequest:
    mode: Literal["incident", "question"]
    prompt: str
    llm: BaseChatModel
    plugins: list[Plugin]
    emit: Emit
    history: list[tuple[str, str]] = field(default_factory=list)  # (role, text) of earlier turns
    focus: str = ""
    past_incidents: str = ""
    action_catalog: list[dict] = field(default_factory=list)
    internal_tools: list[ToolSpec] = field(default_factory=list)
    max_steps: int = 30
    tool_timeout: int = 60
    tool_output_max_chars: int = 12000
    max_parallel_tools: int = 4


@dataclass
class RunResult:
    report: IncidentReport | None = None
    answer: str | None = None
    tool_calls: int = 0
    steps: int = 0
    usage: dict[str, int] = field(default_factory=dict)


def text_of(msg: AnyMessage) -> str:
    content = msg.content
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


def _usage_add(acc: dict[str, int], msg: AnyMessage) -> None:
    for k, v in (getattr(msg, "usage_metadata", None) or {}).items():
        if isinstance(v, int):
            acc[k] = acc.get(k, 0) + v


def build_system_prompt(req: RunRequest) -> str:
    integrations = "\n".join(f"- {p.name} ({p.kind}/{p.type}): {p.context_hint()}" for p in req.plugins) or "- none"
    text = prompts.SYSTEM_PROMPT.format(
        integrations=integrations,
        focus=f"## Focus\n{req.focus}\n" if req.focus else "",
        past_incidents=f"## Similar past incidents (may or may not be related)\n{req.past_incidents}\n"
        if req.past_incidents else "",
        now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
    )
    if req.mode == "question":
        text += "\n" + prompts.QUESTION_GUIDANCE
    return text


def transcript_digest(messages: list[AnyMessage], budget: int = 60_000) -> str:
    """Flatten the tool conversation to plain text so synthesis works on any provider/tool format."""
    parts: list[str] = []
    for msg in messages:
        if isinstance(msg, HumanMessage):
            parts.append(f"USER: {text_of(msg)}")
        elif isinstance(msg, AIMessage):
            if text_of(msg).strip():
                parts.append(f"ANALYST NOTE: {text_of(msg).strip()}")
            for tc in msg.tool_calls or []:
                parts.append(f"CALL {tc['name']}({json.dumps(tc.get('args'), default=str)})")
        elif isinstance(msg, ToolMessage):
            parts.append(f"RESULT {msg.name}: {text_of(msg)[:5000]}")
    text = "\n".join(parts)
    return text if len(text) <= budget else "…[earlier steps omitted]…\n" + text[-budget:]


async def run_investigation(req: RunRequest) -> RunResult:
    bound = bind(req.plugins, req.internal_tools)
    if not bound:
        raise RuntimeError("No tools available - configure at least one Kubernetes, VM or monitoring integration")
    executor = ToolExecutor(bound, req.emit, timeout=req.tool_timeout, max_chars=req.tool_output_max_chars,
                            max_parallel=req.max_parallel_tools)
    llm_tools = req.llm.bind_tools(as_langchain(bound))
    usage: dict[str, int] = {}

    async def agent_node(state: AgentState):
        await req.emit("status", {"phase": "thinking", "step": state["steps"] + 1})
        started = time.monotonic()
        resp = await llm_tools.ainvoke(state["messages"])
        await req.emit("llm_call", {"step": state["steps"] + 1,
                                    "duration_ms": int((time.monotonic() - started) * 1000)})
        _usage_add(usage, resp)
        note = text_of(resp).strip()
        if note:
            await req.emit("thinking", {"text": note})
        return {"messages": [resp], "steps": state["steps"] + 1}

    async def tools_node(state: AgentState):
        last = state["messages"][-1]
        calls = list(getattr(last, "tool_calls", None) or [])
        results = await asyncio.gather(*(executor(tc) for tc in calls))
        return {"messages": [ToolMessage(content=r, tool_call_id=tc["id"], name=tc["name"])
                             for tc, r in zip(calls, results)]}

    def route(state: AgentState):
        last = state["messages"][-1]
        if getattr(last, "tool_calls", None) and state["steps"] < req.max_steps:
            return "tools"
        return END

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    app = graph.compile()

    system = SystemMessage(content=build_system_prompt(req))
    history_msgs: list[AnyMessage] = [
        HumanMessage(content=t) if role == "user" else AIMessage(content=t) for role, t in req.history
    ]
    initial = [system, *history_msgs, HumanMessage(content=req.prompt)]
    await req.emit("status", {"phase": "investigating", "tools": len(bound)})
    final_state = await app.ainvoke({"messages": initial, "steps": 0},
                                    config={"recursion_limit": req.max_steps * 2 + 10})
    messages: list[AnyMessage] = final_state["messages"]
    last = messages[-1]
    budget_hit = bool(getattr(last, "tool_calls", None))
    if budget_hit:
        await req.emit("thinking", {"text": f"Step budget of {req.max_steps} reached - concluding with the evidence gathered."})

    result = RunResult(tool_calls=executor.calls, steps=final_state["steps"], usage=usage)
    digest = transcript_digest(messages[1:])
    synth_system = SystemMessage(content=build_system_prompt(req))

    if req.mode == "question":
        if not budget_hit and isinstance(last, AIMessage) and text_of(last).strip():
            result.answer = text_of(last).strip()
        else:
            await req.emit("status", {"phase": "writing_answer"})
            resp = await req.llm.ainvoke([synth_system, HumanMessage(content=f"Investigation transcript:\n{digest}\n\n"
                                                                             f"{prompts.ANSWER_FINAL_PROMPT}")])
            _usage_add(usage, resp)
            result.answer = text_of(resp).strip()
        return result

    await req.emit("status", {"phase": "writing_report"})
    final_prompt = prompts.INCIDENT_FINAL_PROMPT.format(
        schema=report_schema_hint(), actions=json.dumps(req.action_catalog, separators=(",", ":")) or "[]")
    convo: list[AnyMessage] = [synth_system, HumanMessage(content=f"Investigation transcript:\n{digest}\n\n{final_prompt}")]
    error = ""
    for attempt in range(2):
        resp = await req.llm.ainvoke(convo)
        _usage_add(usage, resp)
        raw = text_of(resp)
        try:
            result.report = parse_report(raw)
            return result
        except ValueError as exc:
            error = str(exc)
            log.warning("Report parse failed (attempt %d): %s", attempt + 1, error[:200])
            convo += [AIMessage(content=raw), HumanMessage(
                content=f"That was not valid against the schema: {error}\nReturn ONLY the corrected JSON object.")]
    result.report = IncidentReport(
        title="Investigation summary", status="inconclusive", severity="medium",
        summary=(text_of(messages[-1]) or "The model did not produce a structured report.")[:3000],
        open_questions=[f"Structured report generation failed: {error[:300]}"])
    return result
