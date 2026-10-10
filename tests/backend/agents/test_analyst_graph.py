"""End-to-end tests for the LangGraph analyst engine.

The LLM is replaced by a scripted fake that streams OpenAI-shaped chunks, so
these tests exercise the real graph, skills, sandbox, workspace and checkpoint
store without any network access.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from alidade.analyst.agent import AnalystAgent
from alidade.datalake.workspace import Workspace

pytestmark = [pytest.mark.backend]


# ── fake LLM ──────────────────────────────────────────────────────────────


def text(content: str) -> dict[str, Any]:
    return {"content": content, "tool_calls": []}


def call(name: str, args: dict[str, Any], content: str = "") -> dict[str, Any]:
    return {"content": content, "tool_calls": [(name, args)]}


class FakeClient:
    """Streams one scripted response per ``get_completion_with_tools`` call and
    records the messages it was given."""

    model = "fake-model"

    def __init__(self, script: list[dict[str, Any]]):
        self.script = list(script)
        self.seen: list[list[dict]] = []
        self.plain_replies: list[str] = []
        self.plain_seen: list[list[dict]] = []

    def get_completion_with_tools(self, messages, tools=None, stream=True, **kwargs):
        self.seen.append(json.loads(json.dumps(messages, default=str)))
        if not self.script:
            raise AssertionError("LLM called more times than scripted")
        step = self.script.pop(0)
        return iter(_chunks(step))

    def get_completion(self, messages, stream=False, **kwargs):
        self.plain_seen.append(messages)
        reply = self.plain_replies.pop(0) if self.plain_replies else ""
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])


def _chunks(step: dict[str, Any]):
    def chunk(delta, finish=None):
        return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)])

    if step["content"]:
        yield chunk(SimpleNamespace(content=step["content"], tool_calls=None))
    for i, (name, args) in enumerate(step["tool_calls"]):
        payload = json.dumps(args)
        half = len(payload) // 2
        yield chunk(SimpleNamespace(content=None, tool_calls=[SimpleNamespace(
            index=i, id=f"call_{name}_{i}",
            function=SimpleNamespace(name=name, arguments=payload[:half]))]))
        yield chunk(SimpleNamespace(content=None, tool_calls=[SimpleNamespace(
            index=i, id=None, function=SimpleNamespace(name=None, arguments=payload[half:]))]))
    yield chunk(SimpleNamespace(content=None, tool_calls=None),
                "tool_calls" if step["tool_calls"] else "stop")


# ── fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    ws = Workspace("graph-test-user", root_dir=tmp_path / "ws")
    ws.write_parquet(
        pd.DataFrame({"region": ["N", "S", "E", "W"], "sales": [10, 20, 30, 40]}),
        "sales",
    )
    return ws


@pytest.fixture
def cp_path(tmp_path: Path) -> Path:
    return tmp_path / "checkpoints.sqlite"


def run_agent(client, workspace, cp_path, question="q", max_iterations=5, **kwargs):
    agent = AnalystAgent(client=client, workspace=workspace, max_iterations=max_iterations)
    agent.registry  # build eagerly
    events = list(agent.run(
        input_tables=[{"name": "sales"}],
        user_question=question,
        checkpoint_db_path=cp_path,
        **kwargs,
    ))
    return events


def types(events):
    return [e["type"] for e in events]


VIS_CODE = (
    "import pandas as pd\n"
    "df = pd.read_parquet('data/sales.parquet')\n"
    "by_region = df.groupby('region', as_index=False)['sales'].sum()\n"
)


def visualize_args(**over):
    args = {
        "title": "Sales by region",
        "display_instruction": "Which region sells most?",
        "input_tables": ["sales"],
        "code": VIS_CODE,
        "output_variable": "by_region",
        "chart": {"chart_type": "Bar Chart", "encodings": {"x": "region", "y": "sales"}},
    }
    args.update(over)
    return args


# ── tests ─────────────────────────────────────────────────────────────────


def test_plain_answer_completes_and_cleans_up(workspace, cp_path):
    client = FakeClient([text("The answer is 42.")])
    events = run_agent(client, workspace, cp_path)
    assert types(events)[-1] == "completion"
    completion = events[-1]
    assert completion["status"] == "success"
    assert completion["content"]["summary"] == "The answer is 42."
    assert completion["run_id"]
    # finished runs leave no checkpoint behind
    import sqlite3
    with sqlite3.connect(cp_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 0


def test_exploration_variables_persist_across_calls(workspace, cp_path):
    client = FakeClient([
        call("execute_python_script", {"purpose": "set", "code": "x = 41"}),
        call("execute_python_script", {"purpose": "use", "code": "print(x + 1)"}),
        text("done"),
    ])
    events = run_agent(client, workspace, cp_path)
    results = [e for e in events if e["type"] == "tool_result"]
    assert results[1]["status"] == "ok"
    assert results[1]["stdout"].strip() == "42"


def test_failed_exploration_keeps_earlier_variables_and_partial_output(workspace, cp_path):
    client = FakeClient([
        call("execute_python_script", {"purpose": "a", "code": "y = 7\nprint('before')\nraise ValueError('boom')"}),
        call("execute_python_script", {"purpose": "b", "code": "print(y)"}),
        text("done"),
    ])
    events = run_agent(client, workspace, cp_path)
    results = [e for e in events if e["type"] == "tool_result"]
    assert results[0]["status"] == "error"
    assert "boom" in results[0]["error"]
    assert "before" in results[0]["stdout"]
    assert results[1]["stdout"].strip() == "7"


def test_visualize_emits_result_and_feeds_observation_back(workspace, cp_path):
    client = FakeClient([
        call("visualize", visualize_args(), content="Let me chart it."),
        text("West leads."),
    ])
    events = run_agent(client, workspace, cp_path)
    assert "thinking_text" in types(events)
    result = next(e for e in events if e["type"] == "result")
    tr = result["content"]["result"]
    assert tr["chart_id"].startswith("chart-")
    assert {r["region"] for r in tr["content"]["rows"]} == {"N", "S", "E", "W"}
    # the next LLM call saw the observation as the action's tool result
    tool_msgs = [m for m in client.seen[1] if m.get("role") == "tool"]
    assert "[OBSERVATION" in tool_msgs[-1]["content"]
    assert events[-1]["content"]["total_steps"] == 1


def test_visualize_error_is_fed_back_not_streamed(workspace, cp_path):
    client = FakeClient([
        call("visualize", visualize_args(chart={"chart_type": "Bar Chart",
                                                "encodings": {"x": "nope", "y": "sales"}})),
        text("fixed"),
    ])
    events = run_agent(client, workspace, cp_path)
    assert "error" not in types(events)
    tool_msgs = [m for m in client.seen[1] if m.get("role") == "tool"]
    assert "not found" in tool_msgs[-1]["content"]


def test_missing_required_fields_asks_model_to_retry(workspace, cp_path):
    client = FakeClient([
        call("visualize", {"title": "x"}),
        text("ok"),
    ])
    events = run_agent(client, workspace, cp_path)
    err = next(e for e in events if e["type"] == "tool_result")
    assert err["status"] == "error" and "Missing fields" in err["error"]
    assert events[-1]["type"] == "completion"


def test_only_first_action_runs(workspace, cp_path):
    step = {"content": "", "tool_calls": [
        ("visualize", visualize_args()),
        ("visualize", visualize_args(title="second")),
    ]}
    client = FakeClient([step, text("done")])
    events = run_agent(client, workspace, cp_path)
    assert sum(1 for e in events if e["type"] == "result") == 1
    notes = [m["content"] for m in client.seen[1] if m.get("role") == "user"]
    assert any("exactly one action" in str(n) for n in notes)


def test_gated_action_requires_skill_load(workspace, cp_path):
    client = FakeClient([
        call("write_report", {"report": "# Report"}),
        text("ok"),
    ])
    run_agent(client, workspace, cp_path)
    tool_msgs = [m for m in client.seen[1] if m.get("role") == "tool"]
    assert "[GATED]" in tool_msgs[-1]["content"]


def test_action_budget_ends_run(workspace, cp_path):
    client = FakeClient([call("visualize", visualize_args())])
    events = run_agent(client, workspace, cp_path, max_iterations=1)
    completion = events[-1]
    assert completion["type"] == "completion"
    assert completion["status"] == "max_iterations"
    assert completion["content"]["summary_code"] == "agent.maxIterationsSummary"


def test_ask_user_pauses_and_resumes_with_run_id(workspace, cp_path):
    question = {"questions": [{"text": "Which metric?", "options": [{"label": "Sales"}, {"label": "Units"}]}]}
    client = FakeClient([
        call("execute_python_script", {"purpose": "remember", "code": "picked_table = 'sales'"}),
        call("ask_user", question),
    ])
    events = run_agent(client, workspace, cp_path)
    assert types(events)[-1] == "interact"
    interact = events[-1]
    run_id = interact["run_id"]
    assert run_id and "trajectory" not in interact

    # Resume in a brand-new agent (a new HTTP request) with only the run id.
    client2 = FakeClient([
        call("execute_python_script", {"purpose": "reuse", "code": "print(picked_table)"}),
        text("Using sales."),
    ])
    events2 = run_agent(client2, workspace, cp_path, question="Sales", resume_run_id=run_id)
    assert client2.seen[0][-1] == {"role": "user", "content": "Sales"}
    # the exploration session survived the pause
    assert next(e for e in events2 if e["type"] == "tool_result")["stdout"].strip() == "sales"
    assert events2[-1]["type"] == "completion"
    assert events2[-1]["run_id"] == run_id


def test_ask_user_carries_the_reply_text_as_context(workspace, cp_path):
    question = {"questions": [{"text": "Which metric?", "options": ["Sales", "Units"]}]}
    client = FakeClient([
        call("ask_user", question, content="Sales and units both look complete; they rank regions differently."),
    ])
    interact = run_agent(client, workspace, cp_path)[-1]
    assert interact["type"] == "interact"
    assert interact["context"] == "Sales and units both look complete; they rank regions differently."
    assert [o["label"] for o in interact["questions"][0]["options"]] == ["Sales", "Units"]

    bare = run_agent(FakeClient([call("ask_user", question)]), workspace, cp_path)[-1]
    assert "context" not in bare


def test_ask_user_without_options_goes_back_to_the_model(workspace, cp_path):
    client = FakeClient([
        call("ask_user", {"questions": [{"text": "Which direction next?"}]}),
        call("ask_user", {"questions": [{"text": "Which direction next?",
                                         "options": ["Profit margin", "Growth trend"]}]}),
    ])
    events = run_agent(client, workspace, cp_path)
    interacts = [e for e in events if e["type"] == "interact"]
    assert len(interacts) == 1
    assert [o["label"] for o in interacts[0]["questions"][0]["options"]] == ["Profit margin", "Growth trend"]
    feedback = client.seen[1][-1]["content"]
    assert "recommend most FIRST" in feedback and "Which direction next?" in feedback


def test_resuming_unknown_run_reports_expired(workspace, cp_path):
    events = run_agent(FakeClient([]), workspace, cp_path, question="x", resume_run_id="nope")
    assert events == [{
        "type": "error", "iteration": 0,
        "message": "This conversation step has expired. Please ask your question again.",
        "message_code": "agent.runExpired",
    }]


def test_llm_failure_becomes_error_event(workspace, cp_path):
    class Boom(FakeClient):
        def get_completion_with_tools(self, *a, **k):
            raise RuntimeError("bad request")

    events = run_agent(Boom([]), workspace, cp_path)
    assert events[-1]["type"] == "error"
    assert events[-1]["message_code"] == "agent.llmApiError"


def test_load_skill_then_streamed_report(workspace, cp_path):
    client = FakeClient([
        call("load_skill", {"name": "report"}),
        call("write_report", {"report": "# Findings\n\nWest leads."}),
        text("Report written."),
    ])
    events = run_agent(client, workspace, cp_path)
    assert "skill_loaded" in types(events)
    deltas = "".join(e["content"] for e in events
                     if e["type"] == "text_delta" and e.get("channel") == "report")
    assert deltas.startswith("# Findings")
    # streamed live exactly once (the buffered re-emission is suppressed)
    assert deltas.count("# Findings") == 1
