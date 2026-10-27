"""Token streaming: text arrives incrementally, tool calls are still assembled from the chunks."""
from __future__ import annotations

from langchain_core.messages import AIMessage

from app.agent.engine import RunRequest, run_investigation
from app.connectors.kubernetes.demo import DemoConfig, DemoKubernetes
from tests import scripted_llm


async def test_streams_text_and_keeps_tool_calls():
    scripted_llm.SCRIPT[:] = [
        AIMessage(content="Let me look at what is unhealthy first.",
                  tool_calls=[{"name": "demo__find_unhealthy", "args": {"namespace": "payments"}, "id": "t1"}]),
        AIMessage(content="The payments api is crash looping because DB_POOL_SIZE is not a number."),
    ]
    live: list[tuple[str, str, str]] = []
    persisted: list[tuple[str, dict]] = []

    async def emit(type_, data):
        persisted.append((type_, data))

    result = await run_investigation(RunRequest(
        mode="question", prompt="What is broken?", llm=scripted_llm.ScriptedChatModel(),
        plugins=[DemoKubernetes("demo", DemoConfig())], emit=emit,
        live=lambda kind, sid, text: live.append((kind, sid, text)),
    ))

    assert result.answer and "DB_POOL_SIZE" in result.answer
    tool_results = [d for t, d in persisted if t == "tool_result"]
    assert tool_results and tool_results[0]["tool"] == "find_unhealthy" and tool_results[0]["ok"]
    final_stream = [t for k, sid, t in live if "DB_POOL_SIZE" in t]
    assert final_stream and final_stream[-1] == result.answer
    # the final answer is delivered once (as the answer), not duplicated as a "thinking" note
    assert not any(t == "thinking" and "DB_POOL_SIZE" in d["text"] for t, d in persisted)
