"""Sub-agents run as LangGraph ``Send`` branches: a crashed run picks up where
it stopped, and only the steps that had not finished run again."""
from __future__ import annotations

import time

import pytest

from alidade.analyst.agent import AnalystAgent
from alidade.analyst.engine import checkpoints
from alidade.analyst.engine.run_settings import RunSettings

from test_analyst_graph import cp_path, text, workspace  # noqa: F401
from test_analyst_subagents import RoutingClient, fan_out_plan, sub_events

pytestmark = [pytest.mark.backend]


class ProcessDied(BaseException):
    """Stands in for the process going away mid-run (not an ordinary error,
    which the sub-agent would catch and report as a failed step)."""


class CrashingClient(RoutingClient):
    """Step ``s2``'s first model call dies — after ``s1`` has finished."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.crashed = False

    def get_completion_with_tools(self, messages, tools=None, stream=True, **kwargs):
        if self.who(messages) == "s2" and not self.crashed:
            deadline = time.time() + 10
            while "s1" not in self.seen and time.time() < deadline:
                time.sleep(0.05)
            time.sleep(0.5)                       # let s1's branch land in the checkpoint
            self.crashed = True
            raise ProcessDied()
        return super().get_completion_with_tools(messages, tools, stream, **kwargs)


def agent(client, workspace):  # noqa: F811
    return AnalystAgent(client=client, workspace=workspace, settings=RunSettings(subagents=True, max_parallel=2))


def test_resume_reruns_only_the_unfinished_step(workspace, cp_path, monkeypatch):  # noqa: F811
    monkeypatch.delenv(checkpoints.DURABILITY_ENV, raising=False)
    client = CrashingClient(main=[fan_out_plan(), text("A 40, B 30.")],
                            steps={"s1": [text("A is 40.")], "s2": [text("B is 30.")]})
    events: list[dict] = []
    with pytest.raises(ProcessDied):
        for e in agent(client, workspace).run(input_tables=[{"name": "sales"}], user_question="Compare",
                                              checkpoint_db_path=cp_path):
            events.append(e)
    run_id = sub_events(events, "started")[0]["run_id"]
    assert len(client.seen["s1"]) == 1

    resumed = list(agent(client, workspace).run(input_tables=[{"name": "sales"}], user_question="",
                                                resume_run_id=run_id, checkpoint_db_path=cp_path))
    assert len(client.seen["s1"]) == 1                     # s1's finished branch was kept
    assert client.crashed and len(client.seen["s2"]) == 1  # only s2 ran again (its crash wasn't recorded)
    assert [e["step_id"] for e in sub_events(resumed, "started")] == ["s2"]
    plan = [e["steps"] for e in resumed if e["type"] == "plan"][-1]
    records = {s["id"]: s.get("subagent") or {} for s in plan}
    assert records["s1"]["findings"] == "A is 40." and records["s2"]["findings"] == "B is 30."
    assert resumed[-1]["type"] == "completion"


@pytest.mark.parametrize("raw, mode", [(None, "async"), ("sync", "sync"), ("EXIT", "exit"), ("bogus", "async")])
def test_durability_comes_from_the_environment(monkeypatch, raw, mode):
    if raw is None:
        monkeypatch.delenv(checkpoints.DURABILITY_ENV, raising=False)
    else:
        monkeypatch.setenv(checkpoints.DURABILITY_ENV, raw)
    assert checkpoints.durability() == mode


# ── the process is killed outright (no chance to save on the way out) ───────

_CHILD = r'''
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
from alidade.analyst.agent import AnalystAgent
from alidade.analyst.engine.run_settings import RunSettings
from alidade.datalake.workspace import Workspace
from test_analyst_graph import text
from test_analyst_subagents import RoutingClient, fan_out_plan

out, ready = sys.argv[3], sys.argv[4]

class Client(RoutingClient):
    def get_completion_with_tools(self, messages, tools=None, stream=True, **kw):
        if self.who(messages) == "s2":
            while '"step_id": "s1", "status": "success"' not in open(out).read():
                time.sleep(0.05)                   # s1's branch has finished ...
            time.sleep(0.5)                        # ... and its result is being saved
            open(ready, "w").close()
            time.sleep(120)                        # killed here
        return super().get_completion_with_tools(messages, tools, stream, **kw)

ws = Workspace("graph-test-user", root_dir=sys.argv[2])
client = Client(main=[fan_out_plan()], steps={"s1": [text("A is 40.")]})
agent = AnalystAgent(client=client, workspace=ws, settings=RunSettings(subagents=True, max_parallel=2))
with open(out, "w") as f:
    for e in agent.run(input_tables=[{"name": "sales"}], user_question="Compare", checkpoint_db_path=sys.argv[5]):
        f.write(json.dumps(e, default=str) + "\n")
        f.flush()
'''


def _killed_mid_run(tmp_path, workspace, cp_path, mode):  # noqa: F811
    import json
    import os
    import signal
    import subprocess
    import sys
    from pathlib import Path

    out, ready = tmp_path / "events.jsonl", tmp_path / "ready"
    env = {**os.environ, checkpoints.DURABILITY_ENV: mode}
    here = str(Path(__file__).parent)                     # the fixture's workspace is tmp_path / "ws"
    proc = subprocess.Popen([sys.executable, "-c", _CHILD, here, str(tmp_path / "ws"), str(out), str(ready), str(cp_path)], env=env,
                            start_new_session=True)     # its code workers die with it
    try:
        deadline = time.time() + 60
        while not ready.exists() and proc.poll() is None and time.time() < deadline:
            time.sleep(0.1)
        assert ready.exists(), "child never reached step s2"
    finally:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(10)
    events = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    return sub_events(events, "started")[0]["run_id"]


@pytest.mark.parametrize("mode, s1_kept", [("async", True), ("sync", True), ("exit", False)])
def test_a_killed_process_keeps_finished_steps(tmp_path, workspace, cp_path, monkeypatch, mode, s1_kept):  # noqa: F811
    run_id = _killed_mid_run(tmp_path, workspace, cp_path, mode)
    monkeypatch.setenv(checkpoints.DURABILITY_ENV, mode)
    client = RoutingClient(main=[fan_out_plan(), text("A 40, B 30.")],
                           steps={"s1": [text("A is 40.")], "s2": [text("B is 30.")]})
    events = list(agent(client, workspace).run(input_tables=[{"name": "sales"}], user_question="",
                                               resume_run_id=run_id, checkpoint_db_path=cp_path))
    if s1_kept:
        assert "s1" not in client.seen and len(client.seen["s2"]) == 1
        assert events[-1]["type"] == "completion"
    else:
        # nothing was saved before the kill: the work done before it is lost
        assert "s1" in client.seen or any(e["type"] == "error" for e in events)
