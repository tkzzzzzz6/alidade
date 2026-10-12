# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Alidade as an MCP server: let Claude Desktop, Cursor and other MCP clients
use the analyst on local data files.

Run it as a stdio server (the client starts it)::

    python -m alidade.mcp_server            # or: alidade-mcp

Tools:

* ``load_file`` — load a local CSV / Excel / Parquet / JSON file as a table;
* ``list_tables`` / ``describe_table`` — what is loaded, columns, sample rows,
  data-quality notes;
* ``ask`` — ask the analyst a question; returns its answer, the charts (as PNG
  images) and the numbers behind them. If the analyst needs to clarify, it
  returns the question and a ``run_id``; reply with ``answer``;
* ``find_insights`` — the statistical scan (trends, shifts, group differences,
  correlations, outliers) without an LLM;
* ``list_definitions`` — meanings of vague words the user settled earlier;
* ``last_run`` — model calls, tokens and time of the most recent run.

Configuration (environment, or a ``.env`` next to the project):

* ``ALIDADE_MCP_MODEL`` — model name, or the id of a model configured for the
  web app (``{PROVIDER}_ENABLED`` / ``_MODELS`` ...);
* ``ALIDADE_MCP_ENDPOINT`` (default ``openai``), ``ALIDADE_MCP_API_BASE``,
  ``ALIDADE_MCP_API_KEY`` — when the model is not one of the app's;
* ``ALIDADE_FAST_MODEL`` — optional model routing, as in the app;
* ``ALIDADE_MCP_ROOTS`` — optional ``os.pathsep``-separated folders that
  ``load_file`` may read from (default: any file the user names).

The transport is MCP over stdio (JSON-RPC 2.0, one message per line); it is
implemented here directly so the server needs no extra dependency. Nothing but
protocol messages is written to stdout — logs and stray prints go to stderr.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, TextIO

logger = logging.getLogger("alidade.mcp")

SERVER_NAME = "alidade"
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
IDENTITY = os.environ.get("ALIDADE_MCP_IDENTITY", "mcp-local")
MAX_IMAGES = 4
MAX_ROWS_SHOWN = 20

INSTRUCTIONS = (
    "Alidade is a data-analysis agent for local tables. Load files with load_file, then use "
    "ask for questions (it writes and runs the analysis code, draws charts and checks its "
    "numbers). If ask returns a clarifying question, show it to the user and pass their reply "
    "to answer. Use find_insights for a quick statistical overview of a table."
)


# ── tool definitions ──────────────────────────────────────────────────────

def _schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


TOOLS: list[dict[str, Any]] = [
    {"name": "load_file",
     "description": "Load a local data file (CSV, TSV, Excel, Parquet, JSON) as a table the analyst can use.",
     "inputSchema": _schema({
         "path": {"type": "string", "description": "Absolute path of the file."},
         "table_name": {"type": "string", "description": "Name for the table (default: the file name)."},
         "sheet": {"type": "string", "description": "Excel sheet name (default: the first sheet)."},
     }, ["path"])},
    {"name": "list_tables", "description": "Tables loaded so far, with row and column counts.",
     "inputSchema": _schema({}, [])},
    {"name": "describe_table",
     "description": "Columns, types, sample rows and data-quality notes (duplicates, numbers stored as text, ...).",
     "inputSchema": _schema({"table": {"type": "string"}}, ["table"])},
    {"name": "ask",
     "description": ("Ask the data analyst a question about the loaded tables. Returns the answer, charts "
                     "(PNG) and the data behind them. May return a clarifying question with a run_id "
                     "instead — then call `answer`."),
     "inputSchema": _schema({
         "question": {"type": "string"},
         "tables": {"type": "array", "items": {"type": "string"},
                    "description": "Tables to use (default: all loaded tables)."},
     }, ["question"])},
    {"name": "answer",
     "description": "Reply to the analyst's clarifying question and let it continue.",
     "inputSchema": _schema({"run_id": {"type": "string"}, "reply": {"type": "string"}}, ["run_id", "reply"])},
    {"name": "find_insights",
     "description": ("Statistical scan of a table for what stands out: trends, level shifts, group "
                     "differences, correlations, outliers, concentration — with false-discovery control."),
     "inputSchema": _schema({
         "table": {"type": "string"},
         "focus": {"type": "string", "description": "Optional column to prefer."},
         "max_findings": {"type": "integer", "minimum": 1, "maximum": 12},
     }, ["table"])},
    {"name": "list_definitions",
     "description": "What the user said vague words mean (saved when the analyst asked).",
     "inputSchema": _schema({}, [])},
    {"name": "last_run",
     "description": "Model calls, tokens and time of the most recent analyst run.",
     "inputSchema": _schema({}, [])},
]


class ToolError(Exception):
    """A tool failed in a way the user should see (returned with isError)."""


# ── configuration ─────────────────────────────────────────────────────────

def _load_env_files() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    here = Path(__file__).resolve().parent
    for candidate in (here.parent.parent / ".env", here / ".env"):
        if candidate.exists():
            load_dotenv(candidate, override=False)


def make_client() -> Any:
    """The LLM client from ALIDADE_MCP_* settings (or the app's model registry)."""
    from alidade.agents.client_utils import Client
    name = os.environ.get("ALIDADE_MCP_MODEL", "").strip()
    try:
        from alidade.model_registry import model_registry
        public = model_registry.list_public()
        match = next((m for m in public if name and name in (m.get("id"), m.get("model"))), None)
        if match is None and not name and len(public) == 1:
            match = public[0]
        if match is not None:
            config = model_registry.get_config(match["id"])
            if config:
                return Client.from_config(dict(config))
    except Exception:  # noqa: BLE001 — fall back to explicit settings
        logger.debug("model registry not usable", exc_info=True)
    if not name:
        raise ToolError("No model configured: set ALIDADE_MCP_MODEL (and ALIDADE_MCP_API_KEY / _API_BASE).")
    return Client(os.environ.get("ALIDADE_MCP_ENDPOINT", "openai"), name,
                  os.environ.get("ALIDADE_MCP_API_KEY") or None,
                  os.environ.get("ALIDADE_MCP_API_BASE") or None)


def _allowed(path: Path) -> bool:
    roots = [Path(r).expanduser().resolve() for r in os.environ.get("ALIDADE_MCP_ROOTS", "").split(os.pathsep) if r.strip()]
    if not roots:
        return True
    return any(path == r or r in path.parents for r in roots)


# ── the server ────────────────────────────────────────────────────────────

class AlidadeMCP:
    def __init__(self, workspace: Any = None, client_factory: Callable[[], Any] = make_client,
                 checkpoint_db_path: Any = None):
        if workspace is None:
            from alidade.datalake.workspace import Workspace, get_alidade_home
            workspace = Workspace(IDENTITY, root_dir=get_alidade_home() / "mcp-workspaces")
        self.workspace = workspace
        self.client_factory = client_factory
        self.checkpoint_db_path = checkpoint_db_path
        self._client = None
        self._last_run_id: str | None = None
        self._pending_tables: dict[str, list[str]] = {}   # run_id -> tables, for `answer`

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = self.client_factory()
        return self._client

    # ── JSON-RPC ──

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """One JSON-RPC message in, the response out (None for notifications)."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, -32600, "Invalid Request")
        method, msg_id, params = message.get("method"), message.get("id"), message.get("params") or {}
        if msg_id is None:                       # a notification: never answered
            return None
        try:
            if method == "initialize":
                asked = params.get("protocolVersion")
                version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
                try:
                    from importlib.metadata import version as _pkg_version
                    version_str = _pkg_version("alidade")
                except Exception:  # noqa: BLE001
                    version_str = "0.1.0"
                return _result(msg_id, {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": str(version_str)},
                    "instructions": INSTRUCTIONS,
                })
            if method == "ping":
                return _result(msg_id, {})
            if method == "tools/list":
                return _result(msg_id, {"tools": TOOLS})
            if method == "tools/call":
                return _result(msg_id, self.call_tool(params.get("name", ""), params.get("arguments") or {}))
            return _error(msg_id, -32601, f"Method not found: {method}")
        except Exception as exc:  # noqa: BLE001
            logger.error("MCP request failed", exc_info=True)
            return _error(msg_id, -32603, f"Internal error: {exc}")

    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"tool_{name}", None)
        if handler is None or name not in {t["name"] for t in TOOLS}:
            return {"content": [_text(f"Unknown tool: {name}")], "isError": True}
        try:
            with contextlib.redirect_stdout(sys.stderr):      # keep stdout for protocol messages
                content = handler(**args)
            return {"content": content, "isError": False}
        except ToolError as exc:
            return {"content": [_text(str(exc))], "isError": True}
        except TypeError as exc:
            return {"content": [_text(f"Bad arguments for {name}: {exc}")], "isError": True}
        except Exception as exc:  # noqa: BLE001
            logger.error("tool %s failed", name, exc_info=True)
            return {"content": [_text(f"{name} failed: {type(exc).__name__}: {exc}")], "isError": True}

    def serve(self, stdin: TextIO, stdout: TextIO) -> None:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                response: dict[str, Any] | None = _error(None, -32700, "Parse error")
            else:
                if isinstance(message, list):        # a batch (older protocol versions)
                    replies = [r for r in (self.handle(m) for m in message) if r is not None]
                    response = replies or None       # type: ignore[assignment]
                else:
                    response = self.handle(message)
            if response is not None:
                stdout.write(json.dumps(response, ensure_ascii=False, default=str) + "\n")
                stdout.flush()

    # ── tools ──

    def tool_load_file(self, path: str, table_name: str | None = None, sheet: str | None = None) -> list[dict]:
        import pandas as pd
        p = Path(path).expanduser()
        if not p.is_absolute():
            raise ToolError("Give the absolute path of the file.")
        p = p.resolve()
        if not _allowed(p):
            raise ToolError("This folder is not allowed (see ALIDADE_MCP_ROOTS).")
        if not p.is_file():
            raise ToolError(f"No such file: {p}")
        suffix = p.suffix.lower()
        if suffix in (".csv", ".txt"):
            df = pd.read_csv(p)
        elif suffix == ".tsv":
            df = pd.read_csv(p, sep="\t")
        elif suffix in (".xlsx", ".xls", ".xlsm"):
            df = pd.read_excel(p, sheet_name=sheet or 0)
        elif suffix == ".parquet":
            df = pd.read_parquet(p)
        elif suffix in (".json", ".jsonl"):
            df = pd.read_json(p, lines=suffix == ".jsonl")
        else:
            raise ToolError(f"Unsupported file type: {suffix}")
        name = _table_name(table_name or p.stem)
        self.workspace.write_parquet(df, name)
        return [_text(f"Loaded {p.name} as table `{name}`: {len(df):,} rows × {len(df.columns)} columns "
                      f"({', '.join(map(str, df.columns[:30]))}{'…' if len(df.columns) > 30 else ''}).")]

    def tool_list_tables(self) -> list[dict]:
        names = self.workspace.list_tables()
        if not names:
            return [_text("No tables yet — use load_file.")]
        lines = []
        for n in names:
            try:
                df = self.workspace.read_data_as_df(n)
                lines.append(f"- {n}: {len(df):,} rows × {len(df.columns)} columns")
            except Exception:  # noqa: BLE001
                lines.append(f"- {n}")
        return [_text("\n".join(lines))]

    def tool_describe_table(self, table: str) -> list[dict]:
        from alidade.agents.context import handle_inspect_source_data
        self._require_table(table)
        return [_text(handle_inspect_source_data([table], [{"name": table}], self.workspace))]

    def tool_find_insights(self, table: str, focus: str | None = None, max_findings: int = 6) -> list[dict]:
        from alidade.analyst.engine import insights
        self._require_table(table)
        df = self.workspace.read_data_as_df(table)
        result = insights.scan(df, max_findings=max(1, min(int(max_findings), 12)), focus=focus)
        return [_text(insights.describe(table, result))]

    def tool_list_definitions(self) -> list[dict]:
        from alidade.knowledge.definitions import DefinitionStore
        items = DefinitionStore(self.workspace.user_home).list()
        if not items:
            return [_text("No saved definitions yet.")]
        return [_text("\n".join(f"- \"{d['term']}\" = {d['meaning']}"
                                + (f" (tables: {', '.join(d['tables'])})" if d.get("tables") else "")
                                for d in items))]

    def tool_last_run(self) -> list[dict]:
        from alidade.analyst.engine import trace
        items = trace.list_traces(self.workspace.user_home, 1)
        if not items:
            return [_text("No runs yet.")]
        s = items[0]["summary"]
        models = "; ".join(f"{m}: {u['calls']} calls, {u['prompt']:,} in ({u['cached']:,} cached) / "
                           f"{u['completion']:,} out" for m, u in (s.get("by_model") or {}).items())
        return [_text(f"Last run ({items[0]['run_id']}): {items[0]['question']!r} — {s['status']}, "
                      f"{s['duration_ms'] / 1000:.1f} s, {s['llm_calls']} model calls ({models}), "
                      f"{s['tool_calls']} tool calls, {s['charts']} charts.")]

    def tool_ask(self, question: str, tables: list[str] | None = None) -> list[dict]:
        names = tables or self.workspace.list_tables()
        if not names:
            raise ToolError("No tables loaded — use load_file first.")
        for n in names:
            self._require_table(n)
        return self._run(question, names, resume_run_id=None)

    def tool_answer(self, run_id: str, reply: str) -> list[dict]:
        names = self._pending_tables.get(run_id) or self.workspace.list_tables()
        return self._run(reply, names, resume_run_id=run_id)

    # ── helpers ──

    def _require_table(self, name: str) -> None:
        if name not in self.workspace.list_tables():
            raise ToolError(f"No table named {name!r}. Loaded: {', '.join(self.workspace.list_tables()) or 'none'}.")

    def _run(self, text: str, tables: list[str], resume_run_id: str | None) -> list[dict]:
        from alidade.analyst.agent import AnalystAgent
        agent = AnalystAgent(client=self.client, workspace=self.workspace,
                             identity_id=getattr(self.workspace, "_identity_id", IDENTITY))
        events = list(agent.run(input_tables=[{"name": n} for n in tables], user_question=text,
                                resume_run_id=resume_run_id, checkpoint_db_path=self.checkpoint_db_path))
        content: list[dict] = []
        charts = [e for e in events if e.get("type") == "result" and e.get("status") == "success"]
        last = events[-1] if events else {}
        if last.get("type") == "interact":
            run_id = last.get("run_id", "")
            self._pending_tables[run_id] = tables
            self._last_run_id = run_id
            lines = ["The analyst needs your input before continuing:"]
            if last.get("context"):
                lines.append(str(last["context"]))
            for q in last.get("questions") or []:
                lines.append(f"- {q.get('text', '')}")
                for i, opt in enumerate(q.get("options") or [], 1):
                    label = opt.get("label") if isinstance(opt, dict) else opt
                    lines.append(f"    {i}. {label}")
            lines.append(f"Reply with the `answer` tool (run_id: {run_id}).")
            content.append(_text("\n".join(lines)))
        else:
            completion = next((e for e in reversed(events) if e.get("type") == "completion"), {})
            summary = ((completion.get("content") or {}).get("summary") or "").strip()
            errors = [e.get("message") for e in events if e.get("type") == "error"]
            if not summary and errors:
                raise ToolError(f"The analyst failed: {errors[-1]}")
            content.append(_text(summary or "(no written answer)"))
        for i, ev in enumerate(charts[:MAX_IMAGES], 1):
            content.extend(_chart_content(i, ev))
        if len(charts) > MAX_IMAGES:
            content.append(_text(f"({len(charts) - MAX_IMAGES} more chart(s) not shown.)"))
        return content


def _chart_content(index: int, event: dict[str, Any]) -> list[dict]:
    """A chart as an image (when it can be rendered) plus its title and first rows."""
    import pandas as pd
    res = (event.get("content") or {}).get("result") or {}
    goal = res.get("refined_goal") or {}
    rows = (res.get("content") or {}).get("rows") or []
    chart = goal.get("chart") or {}
    title = goal.get("title") or goal.get("display_instruction") or f"Chart {index}"
    out: list[dict] = []
    df = pd.DataFrame(rows)
    try:
        from alidade.workflows.create_vl_plots import assemble_vegailte_chart, vl_spec_to_png
        encodings = {ch: (v if isinstance(v, dict) else {"field": v})
                     for ch, v in (chart.get("encodings") or {}).items() if v}
        spec = assemble_vegailte_chart(df, chart.get("chart_type") or "Bar Chart", encodings)
        spec["title"] = title
        png = vl_spec_to_png(spec, scale=1.0)
        out.append({"type": "image", "data": base64.b64encode(png).decode("ascii"), "mimeType": "image/png"})
    except Exception:  # noqa: BLE001 — the data below still describes the chart
        logger.debug("chart rendering failed", exc_info=True)
    preview = df.head(MAX_ROWS_SHOWN).to_csv(index=False) if not df.empty else "(no rows)"
    out.append(_text(f"Chart {index}: {title} ({chart.get('chart_type', '?')}, {len(rows):,} rows)\n"
                     f"```csv\n{preview}```"))
    return out


def _table_name(raw: str) -> str:
    import re
    name = re.sub(r"[^0-9A-Za-z_]+", "_", raw).strip("_").lower() or "table"
    return name if not name[0].isdigit() else f"t_{name}"


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _result(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def main() -> int:
    logging.basicConfig(level=os.environ.get("ALIDADE_MCP_LOG", "WARNING"), stream=sys.stderr,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    _load_env_files()
    try:
        server = AlidadeMCP()
        server.serve(sys.stdin, sys.stdout)
    except KeyboardInterrupt:
        pass
    except Exception:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
