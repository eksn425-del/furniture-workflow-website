"""Agent 循环 + 配额锁的单元测试（不依赖真实网络/大脑）。"""
from __future__ import annotations

import json

import pytest

from app.services.brain_provider import AgentLoopOptions, BrainSettings, WebsiteBrainProvider
from app.services.native_contracts import BrainTaxonomyResponse, TaxonomyCategoryContract
from app.services.native_site_analysis import NativeSiteAnalyzer


class ScriptedBrain(WebsiteBrainProvider):
    """Override _chat_once to return a scripted message sequence."""

    def __init__(self, script: list[dict], settings: BrainSettings) -> None:
        super().__init__(settings=settings)
        self.script = list(script)

    def _chat_once(self, *, messages, tools=None, response_format=None, input_payload=None):
        return self.script.pop(0), {}


def _settings() -> BrainSettings:
    return BrainSettings(
        api_key="test-key",
        base_url="https://example.test/v1",
        model="qwen3.6-flash",
        model_mode="MULTIMODAL_SINGLE_MODEL",
        agent_enabled=True,
        agent_max_steps=6,
    )


def _browse_msg(tool_id: str = "c0") -> dict:
    return {
        "content": None,
        "role": "assistant",
        "tool_calls": [
            {"id": tool_id, "type": "function", "function": {"name": "browse", "arguments": '{"url": "https://example.test"}'}}
        ],
    }


def _finish_msg() -> dict:
    payload = {
        "categories": [
            {"native_name": "Chairs", "canonical_name": "Chairs", "path": "/chairs", "level": 2, "parent_path": "/seating"},
            {"native_name": "Seating", "canonical_name": "Seating", "path": "/seating", "level": 1},
        ],
        "reasoning": "flat nav grouped under Seating",
    }
    return {
        "content": None,
        "role": "assistant",
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "finish", "arguments": json.dumps(payload)}}
        ],
    }


def _run(brain, options=None):
    executed: list[tuple] = []

    def executor(name: str, args: dict):
        executed.append((name, args))
        return {"url": args.get("url"), "nav_items": []}

    result = brain.run_agent_loop(
        system_prompt="analyze the site",
        input_payload={"source_url": "https://example.test"},
        tools=[{"type": "function", "function": {"name": "browse", "parameters": {"type": "object", "properties": {}}}}],
        tool_executor=executor,
        options=options or AgentLoopOptions(response_schema=BrainTaxonomyResponse, finish_tool_name="finish"),
    )
    return result, executed


def test_agent_loop_multiturn_finish() -> None:
    brain = ScriptedBrain([_browse_msg(), _finish_msg()], _settings())
    result, executed = _run(brain)
    assert result.stopped_reason == "FINISH"
    assert result.validated is not None
    assert result.turns == 2
    # 先 browse 再 finish；browse 的工具结果被回填给模型。
    assert [name for name, _ in executed] == ["browse"]


def test_agent_loop_validate_taxonomy_levels() -> None:
    brain = ScriptedBrain([_finish_msg()], _settings())
    result, _ = _run(brain)
    assert result.validated is not None
    cats = result.validated.categories
    assert any(c.level == 2 and c.parent_path == "/seating" for c in cats)


def test_agent_loop_max_steps_truncates() -> None:
    # 只返回 browse，永不 finish；步数上限应截断并返回 None（走规则兜底）。
    brain = ScriptedBrain([_browse_msg() for _ in range(10)], _settings())
    result, _ = _run(brain, AgentLoopOptions(response_schema=BrainTaxonomyResponse, finish_tool_name="finish", max_steps=3))
    assert result.stopped_reason == "MAX_STEPS"
    assert result.validated is None


def test_agent_loop_not_configured_falls_back() -> None:
    brain = WebsiteBrainProvider(settings=BrainSettings())  # 未配置
    result, _ = _run(brain)
    assert result.stopped_reason == "BRAIN_NOT_CONFIGURED"
    assert result.validated is None


def test_apply_agent_counts_backfills_unknown() -> None:
    cats = [
        TaxonomyCategoryContract(
            category_id="c1", native_name="Chairs", canonical_name="Chairs",
            path="/chairs", source_url="https://example.test/chairs",
            count_value=None, count_kind="UNKNOWN",
        )
    ]
    counts = {"https://example.test/chairs": {"count_value": 5, "count_kind": "EXACT"}}
    NativeSiteAnalyzer._apply_agent_counts(cats, counts)
    assert cats[0].count_value == 5
    assert cats[0].count_kind == "EXACT"
    assert any(e.get("role") == "agent_get_count" for e in cats[0].evidence)


def test_apply_agent_counts_ignores_zero_unknown() -> None:
    cats = [
        TaxonomyCategoryContract(
            category_id="c1", native_name="Chairs", canonical_name="Chairs",
            path="/chairs", source_url="https://example.test/chairs",
            count_value=None, count_kind="UNKNOWN",
        )
    ]
    counts = {"https://example.test/chairs": {"count_value": 0, "count_kind": "UNKNOWN"}}
    NativeSiteAnalyzer._apply_agent_counts(cats, counts)
    assert cats[0].count_value is None
    assert cats[0].count_kind == "UNKNOWN"


def test_order_policy_lock_raises_quota_validation() -> None:
    from packages.workflow_core.locks import QuotaValidationError, make_order_policy_lock

    with pytest.raises(QuotaValidationError):
        make_order_policy_lock(
            source="s", categories={"a": 3, "b": 3}, exact_n=10, provider="OFF",
            ruleset="r", image_policy="i", five_year_policy="f", naming_policy="n",
            dimension_policy="d", registry_identity="ri", registry_version="v",
            authorization_mode="a", quality_policy="q", category_quota_mode="REQUIRED",
        )


# --- recoverable tool-argument errors must not end the agent run ----------
# Regression for the 45-site run: the simulated Brain called get_count with
# {"category": ...} instead of the required {"url": ...}. The executor raised
# AgentToolError("BAD_ARGS"), which killed the whole agent loop on turn 1 —
# 39 runs, 158 tool calls, never a second turn. A model-side argument mistake
# should be returned as an observation so the Brain can correct itself, while
# compliance gates (robots/access/budget) must stay terminal.


def test_bad_args_is_returned_to_the_brain_and_loop_continues() -> None:
    from app.services.brain_provider import AgentToolError

    brain = ScriptedBrain([_browse_msg("c0"), _finish_msg()], _settings())
    seen: list[str] = []

    def executor(name: str, args: dict) -> dict:
        seen.append(name)
        if len(seen) == 1:
            raise AgentToolError("BAD_ARGS", "browse requires a same-site 'url' argument", terminal=False)
        return {"ok": True}

    result = brain.run_agent_loop(
        system_prompt="s",
        input_payload={"source_url": "https://example.test"},
        tools=[{"type": "function", "function": {"name": "browse"}}],
        tool_executor=executor,
        options=AgentLoopOptions(response_schema=BrainTaxonomyResponse, finish_tool_name="finish", max_steps=6),
    )

    # The loop survived the rejected call and reached a validated finish.
    assert result.stopped_reason == "FINISH"
    assert result.validated is not None
    # Reaching FINISH required a second Brain turn, i.e. the rejected call did
    # not end the run.
    assert result.turns == 2
    assert result.tool_calls[0]["result_status"] == "TOOL_ARGS_REJECTED"


def test_terminal_compliance_error_still_stops_the_loop() -> None:
    from app.services.brain_provider import AgentToolError

    brain = ScriptedBrain([_browse_msg("c0"), _finish_msg()], _settings())
    calls: list[str] = []

    def executor(name: str, args: dict) -> dict:
        calls.append(name)
        raise AgentToolError("ROBOTS_DENIED", "robots.txt disallows this URL")

    result = brain.run_agent_loop(
        system_prompt="s",
        input_payload={"source_url": "https://example.test"},
        tools=[{"type": "function", "function": {"name": "browse"}}],
        tool_executor=executor,
        options=AgentLoopOptions(response_schema=BrainTaxonomyResponse, finish_tool_name="finish", max_steps=6),
    )

    assert result.stopped_reason == "TERMINAL_TOOL"
    assert result.stop_code == "ROBOTS_DENIED"
    assert len(calls) == 1, "a compliance gate must not be retried"
