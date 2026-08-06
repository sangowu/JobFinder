"""Token telemetry must survive the tool-call path, not just the fallback."""
from __future__ import annotations

from pydantic import BaseModel

from jobradar import llm_backend
from jobradar.llm_backend import NormalizedResponse, ToolUseBlock, complete_via_tool
from jobradar.telemetry import telemetry


class _Payload(BaseModel):
    title: str = ""


def test_tool_call_path_records_real_token_usage(monkeypatch):
    telemetry.reset()

    def fake_complete_with_tools(*, messages, tools, system, provider, model):
        return NormalizedResponse(
            stop_reason="tool_use",
            content=[ToolUseBlock(id="1", name="extract", input={"title": "Backend Engineer"})],
            input_tokens=1234,
            output_tokens=567,
        )

    monkeypatch.setattr(llm_backend, "complete_with_tools", fake_complete_with_tools)

    result = complete_via_tool(
        prompt="p",
        args_schema=_Payload,
        tool_name="extract",
        tool_description="d",
        provider="gemini",
        model="test-model",
        _step="Unit Step",
    )

    assert result.title == "Backend Engineer"
    summary = telemetry.summarize_llm_by_step()["Unit Step"]
    assert summary["calls"] == 1
    # Regression guard: this used to be hard-coded to 0/0, silently hiding the
    # token cost of every step whose tool call succeeded.
    assert summary["input_tokens"] == 1234
    assert summary["output_tokens"] == 567
    telemetry.reset()


def test_providers_without_usage_default_to_zero():
    response = NormalizedResponse(stop_reason="end_turn", content=[])
    assert response.input_tokens == 0
    assert response.output_tokens == 0
