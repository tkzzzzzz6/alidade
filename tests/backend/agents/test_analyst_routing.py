"""Model routing: the cheap model for routine steps, the strong one where it pays."""
from __future__ import annotations

import pytest

from alidade.analyst.engine import routing
from alidade.analyst.engine.routing import FAST, MAIN, STRONG

from test_analyst_graph import FakeClient, call, cp_path, run_agent, text, visualize_args, workspace  # noqa: F401

pytestmark = [pytest.mark.backend]


def u(content):
    return {"role": "user", "content": content}


def a(content="", calls=()):
    return {"role": "assistant", "content": content, "tool_calls": list(calls) or None}


def t(content):
    return {"role": "tool", "tool_call_id": "x", "content": content}


SYSTEM = {"role": "system", "content": "You are an analyst."}


# ── pick ──────────────────────────────────────────────────────────────────

def test_routing_off_uses_the_main_model():
    assert routing.pick([SYSTEM, u("q")], routing_on=False).tier == MAIN


@pytest.mark.parametrize("messages,tier,reason", [
    ([SYSTEM, u("q")], STRONG, "read the question"),
    ([SYSTEM, u("q"), a(), t("region,sales\nN,10")], FAST, "routine step"),
    ([SYSTEM, u("q"), a(), t("ok"), a(), u("按回报率")], STRONG, "new input from the user"),
    ([SYSTEM, u("q"), a(), t("ok"), u("[SYSTEM] You have 1 action left")], STRONG, "follow a system note"),
    ([SYSTEM, u("q"), a(), t("ok"), u("[SUBTASK RESULTS] ...")], STRONG, "merge sub-agent results"),
    ([SYSTEM, u("q"), a(), t("out\n\nError: KeyError 'sales'")], STRONG, "recover from a failed step"),
    ([SYSTEM, u("q"), a(), t("[CHART CHECK FAILED] The chart was NOT shown")], STRONG, "recover from a failed step"),
    ([SYSTEM, u("q"), a(), t("[OBSERVATION – Step 2 FAILED]\n\nError: empty")], STRONG, "recover from a failed step"),
    ([SYSTEM, u("q"), a(), t("ERROR: The 'visualize' action is missing required field(s)")], STRONG, "recover from a failed step"),
])
def test_pick(messages, tier, reason):
    choice = routing.pick(messages, routing_on=True)
    assert (choice.tier, choice.reason) == (tier, reason)


def test_an_old_failure_does_not_keep_the_strong_model():
    msgs = [SYSTEM, u("q"), a(), t("Error: boom"), a(), t("fixed: 42")]
    assert routing.pick(msgs, routing_on=True).tier == FAST


def test_sub_agents_use_the_fast_model_unless_something_failed():
    assert routing.pick([SYSTEM, u("[YOUR STEP] s1: x")], routing_on=True, sub_agent=True).tier == FAST
    assert routing.pick([SYSTEM, u("x"), a(), t("Error: boom")], routing_on=True, sub_agent=True).tier == STRONG


# ── reviewing a cheap model's final answer ─────────────────────────────────

def test_final_answer_numbers_must_come_from_the_run():
    msgs = [SYSTEM, u("哪个地区销售额最高？"), a(), t("region  sales\nW  40\nE  30\ntotal 100")]
    assert routing.review_final("W 最高，销售额 40，占总额 40%。", msgs) == ""
    why = routing.review_final("W 最高，销售额 4,213.5。", msgs)
    assert "no source" in why and "4,213.5" in why
    assert routing.review_final("  ", msgs) == "empty final answer"


# ── in the running graph ───────────────────────────────────────────────────

class Pool:
    """A main (strong) fake client whose ``with_model`` hands out a fast sibling."""

    def __init__(self, strong: list, fast: list):
        self.strong = FakeClient(strong)
        self.fast = FakeClient(fast)
        self.strong.endpoint = self.fast.endpoint = "openai"
        self.strong.model_name, self.fast.model_name = "big-model", "small-model"
        self.strong.with_model = lambda name: self.fast


def llm_calls(events):
    return [(e["model"], e["tier"], e["reason"]) for e in events if e["type"] == "llm_call"]


def test_routine_steps_go_to_the_fast_model(workspace, cp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ALIDADE_FAST_MODEL", "small-model")
    pool = Pool(
        strong=[call("execute_python_script", {"code": "print(df_sum := 100)", "purpose": "total"})],
        fast=[call("visualize", visualize_args()), text("Total sales are 100.")],
    )
    events = run_agent(pool.strong, workspace, cp_path)
    assert llm_calls(events) == [
        ("big-model", STRONG, "read the question"),
        ("small-model", FAST, "routine step"),
        ("small-model", FAST, "routine step"),
    ]
    assert events[-1]["type"] == "completion" and events[-1]["content"]["summary"] == "Total sales are 100."


def test_untraceable_final_answer_is_redone_by_the_strong_model(workspace, cp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ALIDADE_FAST_MODEL", "small-model")
    pool = Pool(
        strong=[call("execute_python_script", {"code": "print(100)", "purpose": "total"}),
                text("Total sales are 100.")],
        fast=[text("Total sales are 4,213.5.")],
    )
    events = run_agent(pool.strong, workspace, cp_path)
    calls = llm_calls(events)
    assert calls[1] == ("small-model", FAST, "routine step")
    assert calls[2][0:2] == ("big-model", STRONG) and "4,213.5" in calls[2][2]
    assert events[-1]["content"]["summary"] == "Total sales are 100."


def test_no_fast_model_means_one_model(workspace, cp_path, monkeypatch):  # noqa: F811
    monkeypatch.delenv("ALIDADE_FAST_MODEL", raising=False)
    client = FakeClient([text("Done.")])
    events = run_agent(client, workspace, cp_path)
    assert [c[1] for c in llm_calls(events)] == [MAIN]


def test_llm_call_reports_usage(workspace, cp_path):  # noqa: F811
    from types import SimpleNamespace
    from test_analyst_graph import _chunks

    class WithUsage(FakeClient):
        def get_completion_with_tools(self, messages, tools=None, stream=True, **kwargs):
            assert kwargs.get("stream_options") == {"include_usage": True}
            chunks = list(super().get_completion_with_tools(messages, tools, stream, **kwargs))
            usage = SimpleNamespace(prompt_tokens=1200, completion_tokens=30,
                                    prompt_tokens_details=SimpleNamespace(cached_tokens=1024))
            return iter([*chunks, SimpleNamespace(choices=[], usage=usage)])

    events = run_agent(WithUsage([text("Done.")]), workspace, cp_path)
    ev = next(e for e in events if e["type"] == "llm_call")
    assert (ev["prompt_tokens"], ev["completion_tokens"], ev["cached_tokens"]) == (1200, 30, 1024)
    assert ev["duration_ms"] >= 0 and ev["tool_calls"] == []
    assert _chunks  # imported helper stays in use


def test_client_with_model_keeps_the_provider():
    from alidade.agents.client_utils import Client
    c = Client("openai", "deepseek-pro", api_key="k", api_base="https://example.com/v1")
    f = c.with_model("deepseek-flash")
    assert f.model == "openai/deepseek-flash" and f.model_name == "deepseek-flash"
    assert f.params == c.params and f.params is not c.params and c.model == "openai/deepseek-pro"


def test_same_model_with_different_reasoning_effort(workspace, cp_path, monkeypatch):  # noqa: F811
    """DeepSeek-style routing: one model, high effort where it matters, low elsewhere."""
    monkeypatch.delenv("ALIDADE_FAST_MODEL", raising=False)
    monkeypatch.setenv("ALIDADE_STRONG_EFFORT", "high")
    monkeypatch.setenv("ALIDADE_FAST_EFFORT", "low")

    class Recording(FakeClient):
        efforts: list = []

        def get_completion_with_tools(self, messages, tools=None, stream=True, **kwargs):
            self.efforts.append(kwargs.get("reasoning_effort"))
            assert kwargs.get("allowed_openai_params") == ["reasoning_effort"]   # not dropped by LiteLLM
            return super().get_completion_with_tools(messages, tools, stream, **kwargs)

    client = Recording([call("execute_python_script", {"code": "print(100)", "purpose": "total"}),
                        text("Total sales are 100.")])
    events = run_agent(client, workspace, cp_path)
    assert client.efforts == ["high", "low"]
    calls = [(e["tier"], e["effort"]) for e in events if e["type"] == "llm_call"]
    assert calls == [(STRONG, "high"), (FAST, "low")]


def test_unknown_effort_values_are_ignored(monkeypatch):
    monkeypatch.setenv("ALIDADE_STRONG_EFFORT", "turbo")
    monkeypatch.setenv("ALIDADE_FAST_EFFORT", "LOW")
    assert routing.tier_efforts() == {STRONG: None, FAST: "low", MAIN: None}
