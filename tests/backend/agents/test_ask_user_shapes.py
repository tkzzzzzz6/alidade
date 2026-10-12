"""ask_user accepts the argument shapes models actually send, and a call it
cannot read tells the model exactly what was wrong."""
from __future__ import annotations

import pytest

from alidade.analyst.skills.core.skill import CoreSkill

from test_analyst_graph import (  # noqa: F401 — fixtures
    FakeClient,
    call,
    cp_path,
    run_agent,
    text,
    types,
    workspace,
)

pytestmark = [pytest.mark.backend]

OPTS = ["按州", "按机构类型"]


@pytest.mark.parametrize("questions", [
    [{"text": "按什么分组？", "options": OPTS}],                                  # schema shape
    '[{"text": "按什么分组？", "options": ["按州", "按机构类型"]}]',               # whole list as a JSON string
    [{"text": "按什么分组？", "options": '["按州", "按机构类型"]'}],               # nested options as a JSON string
    {"text": "按什么分组？", "options": OPTS},                                    # one object, not a list
    [{"question": "按什么分组？", "options": OPTS}],                              # "question" instead of "text"
    [{"text": "按什么分组？", "options": [{"text": "按州"}, {"value": "按机构类型"}]}],  # option objects
    [{"text": "按什么分组？", "options": "按州 | 按机构类型"}],                    # options as one delimited string
    [{"text": "按什么分组？", "options": "按州\n按机构类型"}],
])
def test_lenient_shapes_normalize(questions):
    payload = CoreSkill._normalize_interact_action({"questions": questions})
    [q] = payload["questions"]
    assert q["text"] == "按什么分组？"
    assert [o["label"] for o in q["options"]] == OPTS
    assert q["responseType"] == "single_choice"


def test_plain_string_question_is_kept():
    payload = CoreSkill._normalize_interact_action({"questions": "按什么分组？"})
    assert payload["questions"][0]["text"] == "按什么分组？"


def test_nested_string_options_pause_instead_of_erroring(workspace, cp_path):
    client = FakeClient([
        call("ask_user", {"questions": [{"text": "按什么分组？", "options": '["按州", "按机构类型"]'}]}),
    ])
    events = run_agent(client, workspace, cp_path)
    assert "error" not in types(events)
    interact = next(e for e in events if e["type"] == "interact")
    assert [o["label"] for o in interact["questions"][0]["options"]] == OPTS


def test_unreadable_call_tells_the_model_what_it_sent(workspace, cp_path):
    client = FakeClient([
        call("ask_user", {"questions": [{"text": "按什么分组？"}]}),
        call("ask_user", {"questions": [{"text": "按什么分组？", "options": OPTS}]}),
    ])
    events = run_agent(client, workspace, cp_path)
    feedback = [m for m in client.seen[1] if m.get("role") == "tool"][-1]["content"]
    assert feedback.startswith("[ERROR]")
    assert "按什么分组？" in feedback and "Expected shape" in feedback
    # the chat shows no error; the trace gets the rejection; the retry pauses
    assert "error" not in types(events)
    rejected = next(e for e in events if e["type"] == "action_rejected")
    assert rejected["action"] == "ask_user"
    assert "Expected shape" not in rejected["message"] and "Expected shape" in rejected["detail"]
    assert "interact" in types(events)


def test_invalid_json_arguments_are_named(workspace, cp_path):
    client = FakeClient([
        call("ask_user", "{\"questions\": [{\"text\": \"按什么"),   # cut-off arguments
        call("ask_user", {"questions": [{"text": "按什么分组？", "options": OPTS}]}),
    ])
    run_agent(client, workspace, cp_path)
    feedback = [m for m in client.seen[1] if m.get("role") == "tool"][-1]["content"]
    assert "not a valid JSON object" in feedback
