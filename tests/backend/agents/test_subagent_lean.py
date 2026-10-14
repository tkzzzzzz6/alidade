"""Sub-agents start lean: a short prompt, no re-inspection, the lead's checks."""
from __future__ import annotations

import pytest

from alidade.analyst.agent import AnalystAgent
from alidade.analyst.engine import subagent
from alidade.analyst.engine.graph import _lead_checks
from alidade.analyst.engine.run_settings import RunSettings

from test_analyst_graph import call, cp_path, text, visualize_args, workspace  # noqa: F401
from test_analyst_subagents import RoutingClient, fan_out_plan, py, sub_events

pytestmark = [pytest.mark.backend]


def test_sub_agent_prompt_keeps_charting_and_drops_the_rest(workspace):  # noqa: F811
    kw = dict(language_instruction="Answer in Chinese.", agent_coding_rules="Round to 2 decimals.",
              agent_exploration_rules="Prefer the latest year.")
    main = AnalystAgent(RoutingClient([]), workspace, **kw)
    sub = subagent.SubtaskAgent(RoutingClient([]), workspace, **kw)
    full, lean = main._build_system_prompt(), sub._build_system_prompt()
    assert len(lean) < 0.65 * len(full)
    for kept in ("### `visualize`", "### A. Code Execution Rules", "### B. Chart Type Reference",
                 "Data quality check", "You are working on one step of a larger analysis", "at most 2 chart(s)",
                 "Answer in Chinese.", "Round to 2 decimals.", "Prefer the latest year."):
        assert kept in lean, kept
    for dropped in ("ask_user", "update_plan", "load_skill", "save_definition", "find_insights",
                    "write_report", "inspect_source_data"):
        assert dropped not in lean, dropped
    tools = {t["function"]["name"] for t in sub._current_tools({"core"})}
    assert tools == {"execute_python_script", "visualize"}


def test_section_extracts_up_to_the_next_heading_of_the_same_level():
    body = "# Top\n\n## A\na1\n### A.1\nx\n## B\nb1\n"
    assert subagent._section(body, "## A") == "## A\na1\n### A.1\nx"
    assert subagent._section(body, "### A.1") == "### A.1\nx"
    assert subagent._section(body, "## Missing") == ""


def _tool_round(call_id: str, name: str, output: str) -> list[dict]:
    return [{"role": "assistant", "content": "", "tool_calls": [{"id": call_id, "function": {"name": name}}]},
            {"role": "tool", "tool_call_id": call_id, "content": output}]


def test_lead_checks_keep_recent_data_checks_within_budget():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
    msgs += _tool_round("a", "execute_python_script", "old check " + "x" * 3000)
    msgs += _tool_round("b", "update_plan", "Plan updated")
    msgs += _tool_round("c", "execute_python_script", "[ERROR] boom")
    msgs += _tool_round("d", "inspect_source_data", "## Table 1: sales")
    checks = _lead_checks(msgs, budget=1500)
    assert checks[-1] == "## Table 1: sales"                     # newest kept, oldest first
    assert all("Plan updated" not in c and "boom" not in c for c in checks)
    assert sum(len(c) for c in checks) <= 1500 + 2
    assert checks[0].startswith("old check") and checks[0].endswith("…")
    assert _lead_checks(msgs[:2]) == []


def test_sub_agents_see_what_the_lead_already_checked(workspace, cp_path):  # noqa: F811
    client = RoutingClient(
        main=[py("print('rows after dropping duplicates: 3,190')"), fan_out_plan(), text("A 40, B 30.")],
        steps={"s1": [text("A is 40.")], "s2": [text("B is 30.")]},
    )
    agent = AnalystAgent(client=client, workspace=workspace, settings=RunSettings(subagents=True))
    events = list(agent.run(input_tables=[{"name": "sales"}], user_question="Compare",
                            checkpoint_db_path=cp_path))
    assert len(sub_events(events, "started")) == 2
    first = next(m for m in client.seen["s1"][0] if m["role"] == "user")["content"]
    assert "[WHAT THE LEAD ANALYST ALREADY CHECKED" in first
    assert "rows after dropping duplicates: 3,190" in first
    # the shared table summary comes before the step, so sibling prompts share a prefix
    assert first.index("sales") < first.index("[YOUR STEP]")


def test_a_sub_agent_that_still_inspects_does_not_fail(workspace, cp_path):  # noqa: F811
    client = RoutingClient([], {"s1": [call("inspect_source_data", {"table_names": ["sales"]}),
                                       call("visualize", visualize_args()), text("North leads with 40.")]})
    parent = AnalystAgent(client, workspace)
    result = subagent.run_subtask(parent, {"step_id": "s1", "title": "T", "goal": "G", "user_question": "q",
                                           "input_tables": [{"name": "sales"}], "parent_run_id": "p"},
                                  lambda e: None)
    assert result["status"] == "success" and result["chart_titles"] == ["Sales by region"]
