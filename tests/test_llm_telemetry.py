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
    assert response.served_model == ""


def test_tool_call_path_records_the_model_the_provider_served(monkeypatch):
    telemetry.reset()

    def fake_complete_with_tools(*, messages, tools, system, provider, model):
        return NormalizedResponse(
            stop_reason="tool_use",
            content=[ToolUseBlock(id="1", name="extract", input={"title": "x"})],
            input_tokens=10,
            output_tokens=5,
            # Provider answers with a resolved alias, not the requested string.
            served_model="gemini-3.5-flash-lite-002",
        )

    monkeypatch.setattr(llm_backend, "complete_with_tools", fake_complete_with_tools)
    complete_via_tool(
        prompt="p",
        args_schema=_Payload,
        tool_name="extract",
        tool_description="d",
        provider="gemini",
        model="gemini-3.5-flash-lite",
        _step="Served Step",
    )

    summary = telemetry.summarize_llm_by_step()["Served Step"]
    assert summary["model"] == "gemini-3.5-flash-lite"
    assert summary["served_models"] == "gemini-3.5-flash-lite-002"
    telemetry.reset()


def test_structured_path_records_served_model_and_reports_distinct_values(monkeypatch):
    telemetry.reset()
    served = iter(["model-a", "model-b", "model-a"])

    def fake_gemini_structured(prompt, schema, system, model):
        return _Payload(title="x"), 7, 3, next(served)

    monkeypatch.setattr(llm_backend, "_gemini_structured", fake_gemini_structured)
    for _ in range(3):
        llm_backend.complete_structured(
            prompt="p",
            response_schema=_Payload,
            provider="gemini",
            model="requested-model",
            _step="Fallback Step",
        )

    summary = telemetry.summarize_llm_by_step()["Fallback Step"]
    assert summary["calls"] == 3
    assert summary["input_tokens"] == 21
    # A step served by more than one model must surface both, not the last one.
    assert summary["served_models"] == "model-a, model-b"
    telemetry.reset()
