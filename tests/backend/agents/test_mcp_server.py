"""The MCP server: protocol handshake, tools, and a full ask → answer round trip."""
from __future__ import annotations

import io
import json

import pandas as pd
import pytest

from alidade import mcp_server
from alidade.mcp_server import AlidadeMCP

from test_analyst_graph import FakeClient, call, text, visualize_args, workspace  # noqa: F401

pytestmark = [pytest.mark.backend]


def rpc(server, method, params=None, msg_id=1):
    return server.handle({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})


def texts(result):
    return "\n".join(c["text"] for c in result["result"]["content"] if c["type"] == "text")


@pytest.fixture
def server(workspace, tmp_path):  # noqa: F811
    return AlidadeMCP(workspace=workspace, client_factory=lambda: FakeClient([]),
                      checkpoint_db_path=tmp_path / "cp.sqlite")


def test_handshake_and_tool_list(server):
    init = rpc(server, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                      "clientInfo": {"name": "test", "version": "1"}})
    assert init["result"]["protocolVersion"] == "2025-03-26"
    assert init["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert rpc(server, "initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"] == "2025-06-18"
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = [t["name"] for t in rpc(server, "tools/list")["result"]["tools"]]
    assert names == ["load_file", "list_tables", "describe_table", "ask", "answer", "find_insights",
                     "list_definitions", "last_run"]
    assert rpc(server, "nope")["error"]["code"] == -32601
    assert rpc(server, "ping")["result"] == {}


def test_stdio_transport_writes_only_protocol_lines(server):
    stdin = io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\n\nnot json\n'
                        '{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
    stdout = io.StringIO()
    server.serve(stdin, stdout)
    lines = [json.loads(x) for x in stdout.getvalue().splitlines()]
    assert lines == [{"jsonrpc": "2.0", "id": 1, "result": {}},
                     {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}]


def test_load_describe_and_scan_a_file(server, tmp_path):
    path = tmp_path / "Shop Orders.csv"
    pd.DataFrame({"region": ["N", "S"] * 30, "amount": range(60)}).to_csv(path, index=False)
    loaded = texts(rpc(server, "tools/call", {"name": "load_file", "arguments": {"path": str(path)}}))
    assert "table `shop_orders`: 60 rows × 2 columns" in loaded
    assert "shop_orders: 60 rows" in texts(rpc(server, "tools/call", {"name": "list_tables", "arguments": {}}))
    assert "amount" in texts(rpc(server, "tools/call", {"name": "describe_table", "arguments": {"table": "shop_orders"}}))
    assert "Insight scan of shop_orders" in texts(
        rpc(server, "tools/call", {"name": "find_insights", "arguments": {"table": "shop_orders"}}))
    bad = rpc(server, "tools/call", {"name": "load_file", "arguments": {"path": "relative.csv"}})
    assert bad["result"]["isError"] and "absolute path" in texts(bad)
    assert rpc(server, "tools/call", {"name": "describe_table", "arguments": {"table": "nope"}})["result"]["isError"]


def test_roots_limit_what_can_be_loaded(server, tmp_path, monkeypatch):
    allowed, other = tmp_path / "ok", tmp_path / "no"
    allowed.mkdir(), other.mkdir()
    (other / "x.csv").write_text("a\n1\n")
    monkeypatch.setenv("ALIDADE_MCP_ROOTS", str(allowed))
    res = rpc(server, "tools/call", {"name": "load_file", "arguments": {"path": str(other / "x.csv")}})
    assert res["result"]["isError"] and "not allowed" in texts(res)


def test_ask_answer_round_trip_with_a_chart_image(server, workspace):  # noqa: F811
    server._client = FakeClient([
        call("ask_user", {"questions": [{"text": "Total or average?", "options": ["Total", "Average"]}]}),
        call("visualize", visualize_args(title="Sales by region")),
        text("By total sales, W leads with 40."),
    ])
    first = rpc(server, "tools/call", {"name": "ask", "arguments": {"question": "Which region is best?"}})
    body = texts(first)
    assert "needs your input" in body and "1. Total" in body
    run_id = body.split("run_id: ")[1].rstrip(").")
    second = rpc(server, "tools/call", {"name": "answer", "arguments": {"run_id": run_id, "reply": "Total"}})
    content = second["result"]["content"]
    assert content[0]["text"] == "By total sales, W leads with 40."
    images = [c for c in content if c["type"] == "image"]
    assert images and images[0]["mimeType"] == "image/png"
    assert "Chart 1: Sales by region" in texts(second)
    assert "Last run" in texts(rpc(server, "tools/call", {"name": "last_run", "arguments": {}}))


def test_unknown_tool_and_bad_arguments(server):
    assert rpc(server, "tools/call", {"name": "rm_rf", "arguments": {}})["result"]["isError"]
    res = rpc(server, "tools/call", {"name": "list_tables", "arguments": {"surprise": 1}})
    assert res["result"]["isError"] and "Bad arguments" in texts(res)


def test_client_from_settings(monkeypatch):
    monkeypatch.setenv("ALIDADE_MCP_MODEL", "deepseek-flash")
    monkeypatch.setenv("ALIDADE_MCP_API_BASE", "https://example.com/v1")
    monkeypatch.setenv("ALIDADE_MCP_API_KEY", "k")
    c = mcp_server.make_client()
    assert c.model == "openai/deepseek-flash" and c.params["api_base"] == "https://example.com/v1"
    monkeypatch.delenv("ALIDADE_MCP_MODEL")
    with pytest.raises(mcp_server.ToolError):
        mcp_server.make_client()
