"""Optional LangSmith tracing (ALIDADE_LANGSMITH=1): off by default; when on,
LangGraph nodes and every LiteLLM call land in one tree."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from alidade import langsmith_tracing as lt
from alidade.agents import client_utils
from alidade.agents.client_utils import Client
from alidade.analyst.agent import AnalystAgent
from alidade.analyst.engine.run_settings import RunSettings

from test_analyst_graph import _chunks, call, cp_path, text, visualize_args, workspace  # noqa: F401
from test_analyst_subagents import RoutingClient, fan_out_plan, sub_events

pytestmark = [pytest.mark.backend]


class Recorder:
    """A LangSmith client that keeps the runs instead of sending them."""

    def __new__(cls):
        import langsmith

        class _Client(langsmith.Client):
            def create_run(self, name, inputs, run_type, **kw):
                self.recorded[str(kw["id"])] = {"name": name, "run_type": run_type, "inputs": inputs,
                                            "parent": str(kw.get("parent_run_id")), "extra": kw.get("extra"),
                                                 "tags": kw.get("tags") or []}

            def update_run(self, run_id, **kw):
                self.recorded.setdefault(str(run_id), {}).update(
                    outputs=kw.get("outputs"), error=kw.get("error"), ended=True)

            def create_feedback(self, run_id=None, key=None, score=None, feedback_id=None, comment=None,
                                trace_id=None, **kw):
                from langsmith import utils as ls_utils
                if str(feedback_id) in self.feedback:
                    raise ls_utils.LangSmithConflictError("exists")
                self.feedback[str(feedback_id)] = {"run_id": str(run_id), "trace_id": str(trace_id), "key": key,
                                                   "score": score, "comment": comment}

            def update_feedback(self, feedback_id, score=None, comment=None, **kw):
                self.feedback[str(feedback_id)].update(score=score, comment=comment)

            def delete_feedback(self, feedback_id):
                from langsmith import utils as ls_utils
                if self.feedback.pop(str(feedback_id), None) is None:
                    raise ls_utils.LangSmithNotFoundError("gone")

        client = _Client(api_key="test", api_url="http://127.0.0.1:9", auto_batch_tracing=False)
        client.recorded, client.feedback = {}, {}
        return client


def chain(runs, run):
    """Names from the run up to the root."""
    out = []
    while run:
        out.append(run.get("name"))
        run = runs.get(run.get("parent"))
    return out


@pytest.fixture
def tracing_on(monkeypatch):
    from langsmith import run_helpers
    from langsmith import utils as ls_utils

    for k in ("LANGSMITH_TRACING", "LANGSMITH_PROJECT", "LANGCHAIN_TRACING_V2", "LANGCHAIN_PROJECT"):
        monkeypatch.setenv(k, "")              # remembered, so configure()'s writes are undone
        monkeypatch.delenv(k)
    monkeypatch.setenv(lt.ENV, "1")
    monkeypatch.setenv("LANGSMITH_API_KEY", "test")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "http://127.0.0.1:9")
    rec = Recorder()
    with run_helpers.tracing_context(client=rec):
        yield rec
    monkeypatch.undo()
    ls_utils.get_env_var.cache_clear()
    assert os.environ.get("LANGSMITH_TRACING") != "true" or os.environ.get(lt.ENV)


def fake_litellm(scripts: dict[str, list[dict]], usage=None):
    """``litellm.completion`` answering each agent from its own script."""
    def completion(**kw):
        key = RoutingClient.who(kw["messages"])
        if not kw.get("stream"):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=""))], usage=None)
        step = scripts[key].pop(0)

        def gen():
            yield from _chunks(step)
            if usage:
                yield SimpleNamespace(choices=[], usage=usage)
        return gen()
    return completion


# ── the switch ────────────────────────────────────────────────────────────

def test_off_by_default(monkeypatch):
    monkeypatch.delenv(lt.ENV, raising=False)
    monkeypatch.setattr(lt, "_start_run", lambda **kw: pytest.fail("traced while off"))
    assert not lt.enabled() and not lt.configure()
    assert lt.traced_call(lambda: "resp", model="m", endpoint="openai", messages=[], tools=None,
                          params={}, stream=False) == "resp"
    cfg = {"configurable": {"thread_id": "r"}}
    assert lt.run_config(cfg, "x", {"a": 1}) == {"configurable": {"thread_id": "r"}}


def test_on_without_a_key_stays_off(monkeypatch, caplog):
    monkeypatch.setenv(lt.ENV, "1")
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)
    lt._warned.discard("key")
    assert not lt.enabled()
    assert "LANGSMITH_API_KEY" in caplog.text


def test_configure_turns_langgraph_tracing_on(tracing_on):
    assert lt.configure()
    assert os.environ["LANGSMITH_TRACING"] == "true" and os.environ["LANGSMITH_PROJECT"] == "alidade"


# ── LiteLLM calls ─────────────────────────────────────────────────────────

def test_a_streamed_call_is_recorded_with_reply_tool_calls_and_usage(tracing_on, monkeypatch):
    usage = {"prompt_tokens": 120, "completion_tokens": 30, "prompt_cache_hit_tokens": 100}
    monkeypatch.setattr(client_utils.litellm, "completion", fake_litellm(
        {"main": [call("visualize", visualize_args(title="Sales"), content="Drawing it.")]}, usage))
    client = Client("openai", "deepseek-flash", api_key="k")
    messages = [{"role": "user", "content": "chart sales"}]
    chunks = list(client.get_completion_with_tools(messages, tools=[{"type": "function"}], stream=True))
    assert len(chunks) == 5                                    # passed through unchanged
    (run,) = tracing_on.recorded.values()
    assert run["run_type"] == "llm" and run["name"] == "litellm openai/deepseek-flash"
    assert run["inputs"]["messages"] == messages and run["inputs"]["tools"]
    assert run["extra"]["metadata"]["ls_model_name"] == "openai/deepseek-flash"
    msg = run["outputs"]["choices"][0]["message"]
    assert msg["content"] == "Drawing it."
    (tc,) = msg["tool_calls"]
    assert tc["function"]["name"] == "visualize" and json.loads(tc["function"]["arguments"])["title"] == "Sales"
    assert run["outputs"]["usage_metadata"] == {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150,
                                                "input_token_details": {"cache_read": 100}}


def test_a_failed_call_is_recorded_and_still_raises(tracing_on, monkeypatch):
    def boom(**kw):
        raise RuntimeError("rate limited")
    monkeypatch.setattr(client_utils.litellm, "completion", boom)
    with pytest.raises(RuntimeError):
        Client("openai", "deepseek-flash").get_completion_with_tools([{"role": "user", "content": "q"}],
                                                                     tools=[], stream=True)
    (run,) = tracing_on.recorded.values()
    assert "rate limited" in run["error"]


def test_a_stream_read_halfway_is_closed(tracing_on, monkeypatch):
    monkeypatch.setattr(client_utils.litellm, "completion", fake_litellm({"main": [text("long answer")]}))
    stream = Client("openai", "m").get_completion_with_tools([{"role": "user", "content": "q"}], tools=[], stream=True)
    next(iter(stream))
    stream.close()
    (run,) = tracing_on.recorded.values()
    assert run["ended"] and run["outputs"]["stopped_early"]


def test_tracing_errors_never_break_the_call(tracing_on, monkeypatch):
    monkeypatch.setattr(lt, "_start_run", lambda **kw: None)       # e.g. LangSmith unreachable
    monkeypatch.setattr(client_utils.litellm, "completion", fake_litellm({"main": [text("ok")]}))
    out = list(Client("openai", "m").get_completion_with_tools([{"role": "user", "content": "q"}], tools=[], stream=True))
    assert out and not tracing_on.recorded


# ── one tree per analyst run ──────────────────────────────────────────────

def test_an_analyst_run_is_one_tree_with_subagents_and_model_calls(tracing_on, workspace, cp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(client_utils.litellm, "completion", fake_litellm({
        "main": [fan_out_plan(), text("A 40, B 30.")],
        "s1": [call("visualize", visualize_args(title="A")), text("A is 40.")],
        "s2": [text("B is 30.")],
    }))
    agent = AnalystAgent(client=Client("openai", "deepseek-flash", api_key="k"), workspace=workspace,
                         settings=RunSettings(subagents=True, max_parallel=2))
    events = list(agent.run(input_tables=[{"name": "sales"}], user_question="Compare",
                            checkpoint_db_path=cp_path))
    assert events[-1]["type"] == "completion" and len(sub_events(events, "success")) == 2

    runs = tracing_on.recorded
    (root,) = [r for r in runs.values() if r.get("parent") in (None, "None")]
    assert root["name"] == "alidade analyst" and root["extra"]["metadata"]["subagents"] is True
    # readable in the LangSmith list: the question in, the answer out
    assert root["inputs"] == {"question": "Compare", "tables": ["sales"]}
    assert root["outputs"] == {"status": "success", "answer": "A 40, B 30."}
    graph = next(r for r in runs.values() if r["name"] == "graph")
    assert runs[graph["parent"]] is root
    # the answer carries the trace it can be rated on
    root_id = next(i for i, r in runs.items() if r is root)
    assert events[-1]["trace_id"] == root_id
    llm = [r for r in runs.values() if r.get("run_type") == "llm"]
    assert len(llm) == 2 + 2 + 1                                # main ×2, s1 ×2, s2 ×1
    assert all(chain(runs, r)[-1] == "alidade analyst" for r in llm)
    s1 = [r for r in llm if "subagent s1" in chain(runs, r)]
    assert len(s1) == 2 and all("subagent" in chain(runs, r) for r in s1)    # inside its Send branch
    assert any(r["name"] == "merge" for r in runs.values())
    assert all(r.get("ended") for r in llm)


def test_a_paused_run_and_its_resume_share_a_thread(tracing_on, workspace, cp_path, monkeypatch):  # noqa: F811
    question = {"questions": [{"text": "Which metric?", "options": [{"label": "Sales"}, {"label": "Units"}]}]}
    monkeypatch.setattr(client_utils.litellm, "completion", fake_litellm(
        {"main": [call("ask_user", question), text("Using sales.")]}))
    def run(q, **kw):
        agent = AnalystAgent(client=Client("openai", "m", api_key="k"), workspace=workspace)
        return list(agent.run(input_tables=[{"name": "sales"}], user_question=q, checkpoint_db_path=cp_path, **kw))
    paused = run("Rank the regions")
    run_id = paused[-1]["run_id"]
    done = run("Sales", resume_run_id=run_id)

    roots = [r for r in tracing_on.recorded.values() if r.get("parent") in (None, "None")]
    first, second = sorted(roots, key=lambda r: "reply" in r["inputs"])
    assert first["outputs"] == {"status": "paused", "questions": ["Which metric?"]} and not first["error"]
    assert second["inputs"] == {"question": "Rank the regions", "tables": ["sales"], "reply": "Sales"}
    assert second["outputs"]["answer"] == "Using sales." and "resumed" in second["tags"]
    assert {r["extra"]["metadata"]["thread_id"] for r in roots} == {run_id}
    assert paused[-1]["trace_id"] != done[-1]["trace_id"]


def test_without_tracing_answers_carry_no_trace_id(workspace, cp_path, monkeypatch):  # noqa: F811
    monkeypatch.delenv(lt.ENV, raising=False)
    monkeypatch.setattr(client_utils.litellm, "completion", fake_litellm({"main": [text("Done.")]}))
    agent = AnalystAgent(client=Client("openai", "m", api_key="k"), workspace=workspace)
    events = list(agent.run(input_tables=[{"name": "sales"}], user_question="q", checkpoint_db_path=cp_path))
    assert events[-1]["type"] == "completion" and "trace_id" not in events[-1]


def test_a_helper_agent_call_is_named_after_the_agent(tracing_on, monkeypatch):
    monkeypatch.setattr(client_utils.litellm, "completion", fake_litellm({"main": [text("ok")]}))
    client = Client("openai", "m", api_key="k")

    def ask(self):
        return list(client.get_completion_with_tools([{"role": "user", "content": "q"}], tools=[], stream=True))
    HelperAgent = type("StarterQuestionsAgent", (), {"__module__": "alidade.agents.fake", "ask": ask})
    HelperAgent().ask()
    (run,) = tracing_on.recorded.values()
    assert run["name"] == "StarterQuestionsAgent · openai/m"
    assert run["extra"]["metadata"]["agent"] == "StarterQuestionsAgent"


# ── 👍 / 👎 → LangSmith feedback ──────────────────────────────────────────

def feedback_app(identity="u1"):
    from unittest.mock import patch

    import flask

    from alidade.error_handler import register_error_handlers
    from alidade.routes.agents import agent_bp

    app = flask.Flask(__name__)
    app.register_blueprint(agent_bp)
    register_error_handlers(app)
    return app.test_client(), patch("alidade.routes.agents.get_identity_id", return_value=identity)


def test_rating_an_answer_records_one_vote_per_user(tracing_on):
    trace = "0f8fad5b-d9cb-469f-a165-70867728950e"
    token = lt.feedback_token(trace, "u1")
    c, as_user = feedback_app()
    with as_user:
        r = c.post("/api/agent/feedback", json={"trace_id": trace, "feedback_token": token, "score": 1}).get_json()
        assert r["data"] == {"recorded": True, "action": "created"}
        (fb,) = tracing_on.feedback.values()
        assert fb == {"run_id": trace, "trace_id": trace, "key": "user_rating", "score": 1, "comment": None}
        # changing the vote replaces it, with a comment
        r = c.post("/api/agent/feedback", json={"trace_id": trace, "feedback_token": token, "score": 0,
                                                "comment": " wrong year "}).get_json()
        assert r["data"]["action"] == "updated"
        (fb,) = tracing_on.feedback.values()
        assert fb["score"] == 0 and fb["comment"] == "wrong year"
        # withdrawing it
        r = c.post("/api/agent/feedback", json={"trace_id": trace, "feedback_token": token, "score": None}).get_json()
        assert r["data"]["action"] == "deleted" and tracing_on.feedback == {}


def test_a_user_cannot_rate_someone_elses_answer(tracing_on):
    trace = "0f8fad5b-d9cb-469f-a165-70867728950e"
    c, as_other = feedback_app("u2")
    with as_other:
        r = c.post("/api/agent/feedback", json={"trace_id": trace, "feedback_token": lt.feedback_token(trace, "u1"),
                                                "score": 1})
        assert r.get_json()["status"] != "success" and not tracing_on.feedback


@pytest.mark.parametrize("body", [{"trace_id": "not-a-uuid", "score": 1},
                                  {"trace_id": "0f8fad5b-d9cb-469f-a165-70867728950e", "score": 5},
                                  {"trace_id": "0f8fad5b-d9cb-469f-a165-70867728950e", "score": True},
                                  {"trace_id": "0f8fad5b-d9cb-469f-a165-70867728950e", "score": 1, "comment": "x" * 2001}])
def test_bad_ratings_are_refused(tracing_on, body):
    c, as_user = feedback_app()
    with as_user:
        assert c.post("/api/agent/feedback", json=body).get_json()["status"] != "success"
    assert not tracing_on.feedback


def test_rating_with_tracing_off_is_a_no_op(monkeypatch):
    monkeypatch.delenv(lt.ENV, raising=False)
    c, as_user = feedback_app()
    with as_user:
        r = c.post("/api/agent/feedback", json={"trace_id": "0f8fad5b-d9cb-469f-a165-70867728950e", "score": 1})
        assert r.get_json()["data"] == {"recorded": False, "reason": "tracing_off"}


def test_the_answer_carries_a_token_for_its_own_user(tracing_on, workspace, cp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(client_utils.litellm, "completion", fake_litellm({"main": [text("Done.")]}))
    agent = AnalystAgent(client=Client("openai", "m", api_key="k"), workspace=workspace, identity_id="u1")
    done = list(agent.run(input_tables=[{"name": "sales"}], user_question="q", checkpoint_db_path=cp_path))[-1]
    assert lt.check_feedback_token(done["trace_id"], "u1", done["feedback_token"])
    assert not lt.check_feedback_token(done["trace_id"], "someone-else", done["feedback_token"])
