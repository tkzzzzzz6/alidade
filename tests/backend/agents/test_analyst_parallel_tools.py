"""Several inspection calls in one LLM response (stage 1 of the parallel plan).

One response may now carry several tool calls. Inspection calls all run, in
order, in the shared exploration session; actions stay one per turn.
"""
from __future__ import annotations

import pytest

from alidade.analyst.engine import llm
from alidade.analyst.engine.prompts import SYSTEM_PROMPT

from test_analyst_graph import (  # noqa: F401 — fixtures
    FakeClient,
    cp_path,
    run_agent,
    text,
    types,
    visualize_args,
    workspace,
)

pytestmark = [pytest.mark.backend]


def batch(*calls, content: str = "") -> dict:
    return {"content": content, "tool_calls": list(calls)}


def py(code: str, purpose: str = "look") -> tuple[str, dict]:
    return ("execute_python_script", {"code": code, "purpose": purpose})


class RecordingClient(FakeClient):
    def __init__(self, script):
        super().__init__(script)
        self.kwargs: list[dict] = []

    def get_completion_with_tools(self, messages, tools=None, stream=True, **kwargs):
        self.kwargs.append(kwargs)
        return super().get_completion_with_tools(messages, tools=tools, stream=stream, **kwargs)


def _tool_messages(messages):
    return [m for m in messages if m.get("role") == "tool"]


def test_batched_inspections_all_run_in_order(workspace, cp_path):
    client = RecordingClient([
        batch(py("x = 41\nprint('first', x)"),
              py("print('second', x + 1)"),
              ("inspect_source_data", {"table_names": ["sales"]})),
        text("Done."),
    ])
    events = run_agent(client, workspace, cp_path)
    assert types(events).count("tool_start") == 3
    results = _tool_messages(client.seen[1])
    assert len(results) == 3                                # one answer per call, same order
    assert "first 41" in results[0]["content"]
    assert "second 42" in results[1]["content"]             # same session, ran after the first
    assert "sales" in results[2]["content"]
    assert events[-1]["type"] == "completion"


def test_action_alongside_inspection_still_commits_only_the_action(workspace, cp_path):
    client = RecordingClient([
        batch(py("print('ignored')"), ("visualize", visualize_args())),
        text("Done."),
    ])
    events = run_agent(client, workspace, cp_path)
    assert "tool_start" not in types(events)                # the inspection was dropped …
    assert any(e["type"] == "result" for e in events)      # … the action ran
    notes = [m["content"] for m in client.seen[1] if m.get("role") == "user"]
    assert any("commits exactly one action" in str(n) for n in notes)


def test_two_actions_in_one_reply_keep_the_first(workspace, cp_path):
    client = RecordingClient([
        batch(("visualize", visualize_args()), ("visualize", visualize_args(title="Second"))),
        text("Done."),
    ])
    events = run_agent(client, workspace, cp_path)
    assert sum(1 for e in events if e["type"] == "result") == 1


def test_parallel_flag_omitted_by_default(workspace, cp_path, monkeypatch):
    monkeypatch.delenv("ALIDADE_PARALLEL_TOOLS", raising=False)
    client = RecordingClient([text("Done.")])
    run_agent(client, workspace, cp_path)
    assert "parallel_tool_calls" not in client.kwargs[0]


@pytest.mark.parametrize("value", ["0", "false", "off"])
def test_parallel_flag_can_be_switched_off(workspace, cp_path, monkeypatch, value):
    monkeypatch.setenv("ALIDADE_PARALLEL_TOOLS", value)
    assert llm.parallel_tools_enabled() is False
    client = RecordingClient([text("Done.")])
    run_agent(client, workspace, cp_path)
    assert client.kwargs[0]["parallel_tool_calls"] is False


def test_prompt_asks_for_batched_inspection_and_drops_stale_delegate():
    assert "Batch independent inspections into one reply" in SYSTEM_PROMPT
    assert "delegate" not in SYSTEM_PROMPT
