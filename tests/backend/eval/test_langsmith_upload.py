"""Uploading eval results to LangSmith as experiments (eval/langsmith_upload.py).

Runs against a fake client (what gets created, linked and scored) and once
through the real LangSmith SDK with its HTTP intercepted (that the calls are
ones the SDK accepts and serialises).
"""

import json
import os
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

EVAL_DIR = Path(__file__).resolve().parents[3] / "eval"
sys.path.insert(0, str(EVAL_DIR))

import langsmith_upload as lu  # noqa: E402
import run_eval  # noqa: E402

QUESTIONS = [
    {"id": "q-chart", "dataset": "gas", "category": "trend", "question": "Price trend?", "chart": True,
     "mentions_any": ["rose"], "note": "rises"},
    {"id": "q-ask", "dataset": ["gas", "movies"], "category": "clarify", "question": "Best one?", "expect_ask": True},
]


def _run(qid, repeat, success, duration, **extra):
    r = {"id": qid, "dataset": "gas", "category": "trend", "engine": "alidade", "model": "m", "repeat": repeat,
         "success": success, "completed": True, "status": "success", "chart_required": qid == "q-chart",
         "chart_ok": success, "mentions_ok": success, "expect_ask": qid == "q-ask", "clarified_first": True,
         "duration_s": duration, "llm_calls": 4, "prompt_tokens": 1000, "completion_tokens": 200,
         "total_tokens": 1200, "cached_prompt_tokens": 600, "charts_shown": 1,
         "charts": [{"title": "Gas price by year"}], "summary": "Prices rose 12%.", "question": "Price trend?",
         "subagents": [{"step_id": "s1"}, {"step_id": "s2"}],
         "by_model": {"m": {"calls": 4, "prompt": 1000, "cached": 600, "completion": 200}}}
    r.update(extra)
    return r


@pytest.fixture
def results(tmp_path, monkeypatch):
    # grading is re-run with the current rules; keep the test's own records as they are
    import report
    monkeypatch.setattr(report, "rescore_runs", lambda runs: runs)
    d = tmp_path / "new-sub-on4"
    (d / "logs").mkdir(parents=True)
    runs = [_run("q-chart", 1, True, 20.0), _run("q-chart", 2, False, 30.0, error=None),
            _run("q-ask", 1, True, 5.0, trace_id="abc")]
    (d / "runs.jsonl").write_text("\n".join(json.dumps(r) for r in runs) + "\n", encoding="utf-8")
    (d / "config.json").write_text(json.dumps({
        "model": "m", "repeat": 2, "api_key": "sk-secret", "started": "2026-10-14 10:00:00",
        "alidade_env": {"ALIDADE_SUBAGENTS": "1", "ALIDADE_LANGSMITH_KEY": "x"},
    }), encoding="utf-8")
    end = time.mktime(time.strptime("2026-10-14 10:05:00", "%Y-%m-%d %H:%M:%S"))
    for name in ("q-chart_1.json", "q-chart_2.json", "q-ask_1.json"):
        f = d / "logs" / name
        f.write_text("{}", encoding="utf-8")
        os.utime(f, (end, end))
    return d


class FakeClient:
    def __init__(self):
        self.datasets, self.examples, self.projects = {}, {}, {}
        self.runs, self.feedback, self.updated = [], [], []
        self.flushed = 0

    def has_dataset(self, *, dataset_name):
        return dataset_name in self.datasets

    def read_dataset(self, *, dataset_name):
        return self.datasets[dataset_name]

    def create_dataset(self, name, *, description=None):
        ds = SimpleNamespace(id=uuid.uuid4(), name=name, url=f"https://ls/datasets/{name}")
        self.datasets[name] = ds
        return ds

    def list_examples(self, *, dataset_id):
        return [e for e in self.examples.values() if e.dataset_id == dataset_id]

    def create_examples(self, *, dataset_id, examples):
        for e in examples:
            self.examples[e["id"]] = SimpleNamespace(id=e["id"], dataset_id=dataset_id, inputs=e["inputs"],
                                                     outputs=e["outputs"], metadata=e["metadata"])

    def update_example(self, example_id, *, inputs, outputs, metadata):
        self.updated.append(example_id)
        self.examples[example_id].inputs = inputs

    def has_project(self, name):
        return name in self.projects

    def delete_project(self, *, project_name):
        del self.projects[project_name]

    def create_project(self, name, **kw):
        p = SimpleNamespace(id=uuid.uuid4(), name=name, url=None, **kw)
        self.projects[name] = p
        return p

    def create_run(self, **kw):
        self.runs.append(kw)

    def create_feedback(self, run_id, key, *, score=None, comment=None, trace_id=None, session_id=None, start_time=None):
        assert session_id is not None and start_time is not None
        self.feedback.append((run_id, key, score, comment))

    def update_project(self, project_id, *, end_time):
        self.end_time = end_time

    def flush(self):
        self.flushed += 1


def test_one_experiment_per_results_directory_on_shared_question_examples(results):
    client = FakeClient()
    exp = lu.build_experiment(results, QUESTIONS, {"m": (0.14, 0.035, 0.28)})
    out = lu.upload_experiment(client, exp)
    assert out["runs"] == 3

    ds = client.datasets["alidade-eval"]
    assert len(client.examples) == 2                                   # one per question, not per run
    ex = {e.inputs["id"]: e for e in client.examples.values()}
    assert ex["q-chart"].inputs == {"id": "q-chart", "question": "Price trend?", "datasets": ["gas"]}
    assert ex["q-chart"].outputs == {"chart": True, "mentions_any": ["rose"], "note": "rises"}
    assert ex["q-ask"].inputs["datasets"] == ["gas", "movies"]

    project = client.projects["new-sub-on4"]
    assert project.reference_dataset_id == ds.id and project.num_repetitions == 2
    assert "api_key" not in project.metadata                           # never uploaded
    assert project.metadata["alidade_env"] == {"ALIDADE_SUBAGENTS": "1"}

    # both repeats of q-chart are runs on the same example
    refs = [r["reference_example_id"] for r in client.runs]
    assert refs[0] == refs[1] == ex["q-chart"].id and refs[2] == ex["q-ask"].id
    r0 = client.runs[0]
    assert (r0["end_time"] - r0["start_time"]).total_seconds() == pytest.approx(20.0)
    assert "trace_id" not in r0                                         # posted directly, so rejections raise
    assert r0["outputs"]["answer"] == "Prices rose 12%." and r0["outputs"]["charts"] == ["Gas price by year"]
    assert client.runs[2]["extra"]["metadata"]["trace_id"] == "abc"   # the agent's own trace

    scores = {(rid, k): (s, c) for rid, k, s, c in client.feedback}
    rid0, rid1 = r0["id"], client.runs[1]["id"]
    assert scores[(rid0, "success")][0] == 1.0 and scores[(rid1, "success")][0] == 0.0
    assert scores[(rid1, "success")][1]                                # why it failed
    assert scores[(rid0, "uncached_input_tokens_k")][0] == 0.4
    assert scores[(rid0, "total_tokens_k")][0] == 1.2
    assert scores[(rid0, "subagents")][0] == 2
    assert scores[(rid0, "cost")][0] == pytest.approx((400 * 0.14 + 600 * 0.035 + 200 * 0.28) / 1e6)
    assert (rid0, "clarified_first") not in scores and (client.runs[2]["id"], "clarified_first") in scores
    assert client.flushed >= 1


def test_a_second_experiment_reuses_the_examples_and_names_must_be_new(results):
    client = FakeClient()
    exp = lu.build_experiment(results, QUESTIONS)
    lu.upload_experiment(client, exp)
    examples_before = dict(client.examples)
    lu.upload_experiment(client, exp, name="new-sub-off3")
    assert client.examples == examples_before
    assert set(client.projects) == {"new-sub-on4", "new-sub-off3"}
    with pytest.raises(RuntimeError, match="already exists"):
        lu.upload_experiment(client, exp)
    lu.upload_experiment(client, exp, replace=True)                    # overwrite on request


def test_a_question_edited_in_yaml_updates_its_example(results):
    client = FakeClient()
    lu.upload_experiment(client, lu.build_experiment(results, QUESTIONS))
    edited = [{**QUESTIONS[0], "question": "Price trend since 2000?"}, QUESTIONS[1]]
    lu.upload_experiment(client, lu.build_experiment(results, edited), name="again")
    assert len(client.updated) == 1 and len(client.examples) == 2


def test_runs_of_a_removed_question_still_upload(results):
    client = FakeClient()
    exp = lu.build_experiment(results, QUESTIONS[:1])
    assert exp["examples"]["q-ask"]["metadata"]["removed_from_questions"] is True
    lu.upload_experiment(client, exp)
    assert len(client.runs) == 3


def test_compare_link_selects_every_uploaded_experiment():
    ds = SimpleNamespace(url="https://ls/o/t/datasets/d1")
    a, b = SimpleNamespace(id="a"), SimpleNamespace(id="b")
    assert lu.compare_url(ds, [a, b]) == "https://ls/o/t/datasets/d1/compare?selectedSessions=a,b"
    assert lu.compare_url(ds, [a]) is None


def test_no_api_key_is_a_clear_error(monkeypatch, tmp_path):
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.setattr(lu, "EVAL_DIR", tmp_path)
    monkeypatch.setattr(lu, "ROOT", tmp_path)
    with pytest.raises(RuntimeError, match="LANGSMITH_API_KEY"):
        lu.make_client()


def test_run_eval_flag_and_config_switches(monkeypatch):
    monkeypatch.delenv("EVAL_LANGSMITH", raising=False)
    args = run_eval.parse_args(["--model", "m", "--env-file", "/nonexistent", "--langsmith"])
    assert args.langsmith is True and args.langsmith_dataset == "alidade-eval"
    assert run_eval.parse_args(["--model", "m", "--env-file", "/nonexistent"]).langsmith is False


def test_through_the_real_sdk(results, monkeypatch):
    """The calls are ones langsmith.Client accepts; check what it sends."""
    from langsmith import Client
    sent = []
    ds_id, proj_id = str(uuid.uuid4()), str(uuid.uuid4())
    tenant = str(uuid.uuid4())

    class Resp:
        def __init__(self, status, body):
            self.status_code, self._body = status, body
            self.headers, self.content = {}, json.dumps(body).encode()
            self.text, self.reason, self.ok = self.content.decode(), "OK", status < 400

        def json(self):
            return self._body

        def raise_for_status(self):
            pass

    def request(method, url, **kw):
        path = url.split("ls.test", 1)[-1].split("?")[0]
        body = kw.get("data") or kw.get("json")
        sent.append((method, path, body))
        now = "2026-10-14T10:00:00"
        dataset = {"id": ds_id, "name": "alidade-eval", "tenant_id": tenant, "created_at": now, "modified_at": now,
                   "data_type": "kv"}
        if method == "GET" and path == "/datasets":
            return Resp(200, [])
        if method == "POST" and path == "/datasets":
            return Resp(200, dataset)
        if method == "GET" and path == "/examples":
            return Resp(200, [])
        if method == "POST" and "examples" in path:
            return Resp(200, [{"id": str(uuid.uuid4())}, {"id": str(uuid.uuid4())}])
        if method == "GET" and path == "/sessions":
            return Resp(200, [])
        if method == "POST" and path == "/sessions":
            return Resp(200, {"id": proj_id, "name": "new-sub-on4", "tenant_id": tenant, "start_time": now,
                              "reference_dataset_id": ds_id})
        if method == "PATCH" and path.startswith("/sessions/"):
            return Resp(200, {"id": proj_id, "name": "new-sub-on4", "tenant_id": tenant, "start_time": now,
                              "reference_dataset_id": ds_id})
        if path in ("/info",):
            return Resp(200, {"version": "test"})
        return Resp(200, {"id": str(uuid.uuid4())})

    client = Client(api_url="http://ls.test", api_key="test-key", auto_batch_tracing=False)
    monkeypatch.setattr(client.session, "request", request)
    out = lu.upload_experiment(client, lu.build_experiment(results, QUESTIONS))
    assert out["runs"] == 3

    runs = [b for m, p, b in sent if m == "POST" and p == "/runs"]
    assert len(runs) == 3
    first = json.loads(runs[0]) if isinstance(runs[0], (str, bytes)) else runs[0]
    assert first["reference_example_id"] and first["session_name"] == "new-sub-on4"
    assert first["outputs"]["answer"] == "Prices rose 12%."
    feedback = [b for m, p, b in sent if m == "POST" and p == "/feedback"]
    assert len(feedback) == sum(len(r["scores"]) for r in lu.build_experiment(results, QUESTIONS)["rows"])
    # the API key is a header, never in a body
    assert all("test-key" not in (b.decode() if isinstance(b, bytes) else json.dumps(b) if b else "")
               for _, _, b in sent)


def test_old_results_move_forward_but_keep_their_real_times(results):
    import datetime as dt
    exp = lu.build_experiment(results, QUESTIONS)
    ran = exp["start"]
    assert lu.time_shift(exp, now=exp["end"] + dt.timedelta(hours=1)) == dt.timedelta(0)
    later = exp["end"] + dt.timedelta(days=2)
    shift = lu.time_shift(exp, now=later)
    assert exp["end"] + shift == later - dt.timedelta(minutes=1)

    client = FakeClient()
    out = lu.upload_experiment(client, exp)              # the fixture is dated 2026-10-14 10:00
    if out["shifted"]:
        r0 = client.runs[0]
        assert r0["extra"]["metadata"]["ran_at"] == ran.isoformat(timespec="seconds")
        assert client.projects["new-sub-on4"].metadata["times_shifted"] is True
        assert (r0["end_time"] - r0["start_time"]).total_seconds() == pytest.approx(20.0)
        assert dt.datetime.now(dt.timezone.utc) - r0["start_time"] < dt.timedelta(hours=24)


def test_scores_stay_inside_langsmiths_range_and_rejections_are_reported(results):
    big = lu.scores_for({"success": True, "completed": True, "total_tokens": 195_042, "completion_tokens": 6031,
                         "llm_calls": 11, "duration_s": 30.2})
    by_key = {s["key"]: s["score"] for s in big}
    assert by_key["total_tokens_k"] == 195.04 and all(abs(v) <= lu.SCORE_LIMIT for v in by_key.values())

    class Rejecting(FakeClient):
        def create_feedback(self, run_id, key, **kw):
            if key == "cost":
                raise RuntimeError("422 score out of range")
            super().create_feedback(run_id, key, **kw)

    client = Rejecting()
    out = lu.upload_experiment(client, lu.build_experiment(results, QUESTIONS, {"m": (1, 1, 1)}))
    assert len(out["failures"]) == 3 and "cost" in out["failures"][0]
