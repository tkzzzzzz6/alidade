"""Saved definitions: ask once what a vague word means, then remember it."""
from __future__ import annotations

import json

import pytest

from alidade.analyst.agent import AnalystAgent
from alidade.knowledge.definitions import DefinitionStore, format_block

from test_analyst_graph import FakeClient, call, cp_path, text, visualize_args, workspace  # noqa: F401

pytestmark = [pytest.mark.backend]


# ── the store ─────────────────────────────────────────────────────────────

def test_save_match_and_replace(tmp_path):
    store = DefinitionStore(tmp_path)
    d = store.save("活跃客户", "过去 90 天内下过单的客户", tables=["orders.csv"], aliases=["活跃用户"])
    assert d["tables"] == ["orders"] and d["aliases"] == ["活跃用户"]
    assert [x["id"] for x in store.match("上个月活跃客户有多少？", ["orders"])] == [d["id"]]
    assert store.match("活跃用户的平均订单额？", ["orders.parquet"])[0]["id"] == d["id"]   # alias, other extension
    assert store.match("活跃客户有多少？", ["products"]) == []                               # other tables
    assert store.match("客户有多少？", ["orders"]) == []
    again = store.save("活跃客户", "过去 30 天内下过单", tables=["orders"])
    assert again["id"] == d["id"] and len(store.list()) == 1 and store.list()[0]["meaning"] == "过去 30 天内下过单"


def test_english_terms_match_whole_words(tmp_path):
    store = DefinitionStore(tmp_path)
    store.save("active customer", "ordered in the last 90 days")
    assert len(store.match("How many Active Customers do we have?", [])) == 1     # plural, other case
    assert len(store.match("how many active customer accounts?", [])) == 1
    assert store.match("inactive customer", []) == []
    assert store.match("customers who were active once", []) != []                # all words, any order
    assert store.match("how many customers?", []) == []


def test_chinese_terms_match_when_reworded(tmp_path):
    store = DefinitionStore(tmp_path)
    store.save("最好的行业", "危机中失业率上升最少")
    assert len(store.match("除了政府部门，哪个行业最好？", [])) == 1
    assert store.match("哪个行业最差？", []) == []
    store.save("最好", "利润率最高", tables=["sales"])
    assert [d["term"] for d in store.match("最好的地区是哪个？", ["sales"])] == ["最好"]


def test_bad_definitions_are_refused_and_delete_works(tmp_path):
    store = DefinitionStore(tmp_path)
    with pytest.raises(ValueError):
        store.save("x", "something")
    with pytest.raises(ValueError):
        store.save("good term", "  ")
    d = store.save("good term", "meaning")
    assert store.delete(d["id"]) is True and store.delete(d["id"]) is False and store.list() == []


def test_block_tells_the_agent_to_use_and_mention_it(tmp_path):
    d = DefinitionStore(tmp_path).save("best seller", "most units sold in the period", tables=["sales"])
    block = format_block([d])
    assert block.startswith("[SAVED DEFINITIONS]") and "without asking again" in block
    assert '"best seller": most units sold in the period — for tables: sales' in block


# ── in the running graph ───────────────────────────────────────────────────

def run(client, workspace, cp_path, question, **kw):  # noqa: F811
    return list(AnalystAgent(client=client, workspace=workspace).run(
        input_tables=[{"name": "sales"}], user_question=question, checkpoint_db_path=cp_path, **kw))


def tool_names(messages):
    return [t["function"]["name"] for t in messages or []]


def test_ask_then_save_then_reuse(workspace, cp_path):  # noqa: F811
    ask = call("ask_user", {"questions": [{"text": "按什么算最好？", "options": ["销售额", "增长"]}]})
    save_and_chart = {"content": "", "tool_calls": [
        ("save_definition", {"term": "最好的地区", "meaning": "销售额最高的地区", "aliases": ["最强的地区"]}),
        ("visualize", visualize_args(title="各地区销售额")),
    ]}
    client = FakeClient([ask, save_and_chart, text("按销售额，W 最好（40）。")])
    first = run(client, workspace, cp_path, "哪个是最好的地区？")
    events = first + run(client, workspace, cp_path, "销售额", resume_run_id=first[-1]["run_id"])
    saved = [e for e in events if e["type"] == "tool_result" and e["tool"] == "save_definition"]
    assert saved and saved[0]["status"] == "ok" and "最好的地区" in saved[0]["stdout"]
    assert any(e["type"] == "result" for e in events)            # the chart still went through
    [d] = DefinitionStore(workspace.user_home).list()
    assert d["tables"] == ["sales"] and d["question"] == "哪个是最好的地区？"

    # a later question with the same word: the definition is in the context, no question asked
    client2 = FakeClient([text("按已保存的口径（销售额最高），最强的地区是 W。")])
    later = run(client2, workspace, cp_path, "今年最强的地区是哪个？")
    applied = next(e for e in later if e["type"] == "definitions_applied")
    assert applied["terms"] == ["最好的地区"]
    user_msg = client2.seen[0][1]["content"]
    assert "[SAVED DEFINITIONS]" in user_msg and "销售额最高的地区" in user_msg
    assert user_msg.index("[SAVED DEFINITIONS]") < user_msg.index("[USER QUESTION]")
    assert DefinitionStore(workspace.user_home).list()[0]["uses"] == 1


def test_switch_off_hides_the_tool_and_the_block(workspace, cp_path, monkeypatch):  # noqa: F811
    DefinitionStore(workspace.user_home).save("最好的地区", "销售额最高")
    monkeypatch.setenv("ALIDADE_DEFINITIONS", "0")
    client = FakeClient([text("Done.")])
    events = run(client, workspace, cp_path, "最好的地区？")
    assert not any(e["type"] == "definitions_applied" for e in events)
    assert "[SAVED DEFINITIONS]" not in json.dumps(client.seen[0][1], ensure_ascii=False)
    agent = AnalystAgent(client=client, workspace=workspace)
    assert "save_definition" not in tool_names(agent._current_tools({"core"}))
    monkeypatch.setenv("ALIDADE_DEFINITIONS", "1")
    assert "save_definition" in tool_names(agent._current_tools({"core"}))


def test_routes(tmp_path):
    from unittest.mock import patch

    import flask

    from alidade.error_handler import register_error_handlers
    from alidade.routes.knowledge import knowledge_bp

    app = flask.Flask(__name__)
    app.register_blueprint(knowledge_bp)
    register_error_handlers(app)
    with patch("alidade.routes.knowledge.get_identity_id", return_value="u"), \
         patch("alidade.routes.knowledge.get_user_home", return_value=tmp_path):
        c = app.test_client()
        saved = c.post("/api/knowledge/definitions/save",
                       json={"term": "大客户", "meaning": "年采购额 ≥ 100 万", "tables": ["orders"]}).get_json()
        did = saved["data"]["definition"]["id"]
        listed = c.post("/api/knowledge/definitions/list", json={}).get_json()["data"]["definitions"]
        assert [d["term"] for d in listed] == ["大客户"]
        assert c.post("/api/knowledge/definitions/save", json={"term": "x"}).get_json()["status"] != "success"
        assert c.post("/api/knowledge/definitions/delete", json={"id": did}).get_json()["status"] == "success"
        assert c.post("/api/knowledge/definitions/list", json={}).get_json()["data"]["definitions"] == []
