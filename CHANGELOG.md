# Changelog

Alidade is built on Microsoft Data Formulator 0.8.0b1. The upstream changelog
is kept in [docs/upstream/CHANGELOG.md](docs/upstream/CHANGELOG.md).

## 0.1.0 (unreleased)

### Agent
- The analyst agent loop was rebuilt on **LangGraph** (`prepare → agent ⇄ tools`, `agent → act → agent / await_user / finish`); models are still reached through LiteLLM.
- **Plan, then act**: an `update_plan` tool streams a live analysis checklist.
- **Chart self-check**: rule-based checks withhold broken charts and send them back for repair (up to 2 retries, not charged to the action budget).
- **Persistent Python session** across exploration steps; `visualize` code still runs fresh, so saved charts stay reproducible.
- **Context compression** above `ALIDADE_AGENT_CONTEXT_BUDGET` tokens (default 60000).
- **Pause / resume by `run_id`** with SQLite checkpoints, instead of shipping the trajectory back and forth.
- `ask_user` questions carry the agent's context and 2–3 options with the recommended one first.
- Vague requests and requests for investment advice are clarified with `ask_user` (2–3 recommended options) before any chart or plan.
- Line-chart check: more than 10 lines is a warning, more than 20 is sent back for repair.
- Plan progress advances by itself when a chart is shown (`plan_step` names the step when it is not the one in progress), a plan change can ride along with an action, and plans start at three steps: no more LLM round trips just to tick a checklist.
- Several inspection calls (table structure, queries) can come back in one LLM reply instead of one per reply (`ALIDADE_PARALLEL_TOOLS`, on by default); actions are still one per turn.
- Parallel sub-agents (`ALIDADE_SUBAGENTS=1`, experimental): independent plan steps are handed to sub-agents that run at the same time (own Python session, 2-chart budget, no questions to the user, timeout and failure isolation), and their findings are merged by the main agent.
- Sub-agent progress in the UI: lines from each parallel step are tagged ("[Step 2] …") and closed per step, each chart is labelled with its step, and the status line says which steps run at the same time. Stopping a run now also stops its running sub-agents.
- Sub-agents are dispatched with LangGraph `Send` (`dispatch → subagent × N → merge`) instead of a thread pool inside one node, and runs are checkpointed after every step (`ALIDADE_CHECKPOINT_DURABILITY`, default `async`; was save-on-exit). Each parallel step's result is saved as it finishes, so a run whose process was killed continues with the same `run_id` and only re-runs the steps that had not finished. Each step's end is reported as soon as it finishes, not when the whole round does.
- **Dispatch panel**: while sub-agents work, the canvas shows a live diagram of the round — main agent → dispatch → one card per step → merge — with each step's state, what it is doing and its clock, a timeline of queued / running time with "one after another vs in parallel" totals, a flag on a step slowing the round, and the round's event log. Everything is derived from the steps' sub-agent records (`src/app/dispatchView.ts`), nothing is simulated; dots move only along links that are working (none with "reduce motion"). A switch goes back to the charts, and the canvas returns to them when the round ends. Steps can be stopped from their card.
- Dispatch panel, rounds and replay: every fan-out round is drawn as its own column (a "merge, then dispatch" node between rounds), and steps a later round will take wait locked in theirs until they are handed out; time saved is counted per round. A finished answer that used sub-agents has "Show how it was dispatched": the canvas plays the run back from the saved records (play / pause, 1× / 2× / 4×, a scrubber marked at every start and end). Nothing new is stored, and the replay clock stays in the panel (not the store).
- Sub-agent records and end events now carry the step's model calls, tokens (input / cached / output) and tool calls, its round, when it was queued and the parallel limit; the eval report adds per-sub-agent time, calls, output tokens and uncached input.
- Optional **LangSmith tracing** (`ALIDADE_LANGSMITH=1` with `LANGSMITH_API_KEY`; off by default): a run is one tree in LangSmith — the LangGraph nodes, each parallel sub-agent branch with its own graph, and under each node its LiteLLM calls (messages, tools, reply, tool calls, token usage incl. cache hits), which LangGraph alone does not see. Tracing failures never fail a model call. See `docs/langsmith.md`.
- LangSmith traces are readable at a glance: the root shows the question and tables in and the answer (or the clarifying question) out, a paused run and its resume share a thread (`thread_id` = run id), and helper-agent calls are named after the agent (`StarterQuestionsAgent · <model>`).
- **👍 / 👎 on answers** (when LangSmith tracing is on): the rating, and after a 👎 an optional "what was wrong", is recorded as `user_rating` feedback on the run's trace (`POST /api/agent/feedback`, one vote per user per answer, a server-signed token so users can only rate their own answers). A LangSmith automation can then collect thumbs-down runs into a dataset for regression tests (see `docs/langsmith.md`).
- Leaner sub-agents: a sub-agent gets a prompt of its own (about half the main agent's — no asking, planning, skills or reports), no `inspect_source_data` (its table summary already has every column, value samples and data-quality notes), and the outputs of the checks the main agent already ran, so it goes straight to its chart.
- **Sub-agent controls in the UI**: Settings → Agent turns parallel sub-agents on or off and sets how many run at once, charts per sub-agent, the time limit and plan auto-advance, per user (unset = the server's env default). The settings travel with each request and are saved with the run, so a resumed run keeps them. In the plan checklist each sub-agent step shows its status, elapsed time and current activity, and can be stopped on its own (`POST /api/agent/subagent-stop`); the main agent is told not to redo a step the user stopped. After the run, each step keeps the sub-agent's findings behind a toggle.
- Demo sessions on the start page are imported on the server (`POST /api/sessions/import-demo`) instead of being downloaded by the page and uploaded again, which failed with "File is not a zip file" when a download manager intercepted the .zip request. A session upload that is not a zip now says so (and how many bytes arrived).
- `ask_user` accepts the argument shapes models actually send (nested JSON strings, one object instead of a list, `question` instead of `text`, option objects); a call it still cannot read tells the model what it sent and the expected shape, and is recorded in the run trace (`action_rejected`).
- When a question is partly outside the loaded data (a year or field that is not there), the agent answers what the data supports and says what is missing instead of stopping to ask.
- `ALIDADE_PLAN_AUTO_ADVANCE=0` switches plan auto-advance off, for A/B evaluation only.
- **Model routing** (`ALIDADE_FAST_MODEL`): the model the user picked handles reading the question, the user's answers and recovery after a failed step; a cheaper model of the same provider does routine steps. A final answer from the cheaper model whose numbers have no source in the run is redone by the strong model. Tiers can also differ by reasoning effort on one model (`ALIDADE_STRONG_EFFORT` / `ALIDADE_FAST_EFFORT`, e.g. DeepSeek high / low); the tier's effort is passed through LiteLLM, which otherwise drops it for models it does not know.
- **Saved definitions**: once the user answers what a vague word means ("best" = lowest price), the agent saves it (`save_definition`); later questions using the same word on the same tables use it without asking and say so. Listed and deletable in the knowledge panel (`ALIDADE_DEFINITIONS=0` turns it off).
- **Insight scan** (`find_insights`): trends, level shifts, group differences, correlations, outliers and concentration found with statistical tests and Benjamini–Hochberg false-discovery control, aware of one-series-per-entity tables; a "Find insights" chip in the chat box.
- **MCP server** (`python -m alidade.mcp_server`, see `docs/mcp.md`): load files, ask questions (charts come back as PNG), answer clarifying questions, run the insight scan — from Claude Desktop, Cursor or any MCP client.
- Data quality check: each table in the agent's context comes with code-computed notes on duplicate rows, numbers or dates stored as text (and mixed date formats), the same label spelled differently, and missing values, each with the fix.
- **Code sandbox hardened**: model-written code runs with memory and wall-clock limits enforced by the host (`ALIDADE_SANDBOX_MEMORY_MB` 4096, `ALIDADE_SANDBOX_TIMEOUT` 120 s; a killed persistent session continues in a fresh worker and the model is told its variables are gone), file reads confined to the workspace and all writes, deletes, renames, directory listings outside it, process spawning and network blocked, including the native-code file I/O of DuckDB and PyArrow that the audit hook cannot see; environment secrets scrubbed from the worker. Threat model and attack tests in `docs/sandbox.md` / `tests/backend/security/test_sandbox_attacks.py`. The Docker sandbox also drops all capabilities and the network.
- **Large tables via DuckDB** (`ALIDADE_BIGDATA`, on by default; `ALIDADE_BIGDATA_ROWS` 200k): a table past the row threshold is summarised from its true row count (Parquet footer / `COUNT(*)`), a bounded DuckDB sample for schema and sample values, and one DuckDB aggregate for exact numeric min/max/mean — instead of loading the whole table into pandas just to describe it. The model is told the table is large and to aggregate it with DuckDB SQL over the file rather than read millions of rows into a DataFrame. Small tables and `ALIDADE_BIGDATA=0` keep the old pandas path. Building a 10M-row table's context dropped from ~37 s to ~4 s. Benchmark `eval/bench_large_data.py`, big-data eval questions (`eval/data/make_large.py`), notes in `docs/plans/large-data.md`.

### Evaluation
- `eval/`: a harness that runs a question set against a real model and compares two versions (success, chart errors, clarification, tokens, time), with one-command scripts (`eval.ps1` / `eval.bat` / `eval.sh`) and keys in `eval/.env`.
- Token accounting: provider-reported prefix-cache hits, uncached input tokens, and tokens spent before the user confirms a direction.
- Number provenance check (`analyst/engine/number_check.py`): every number in a conclusion is traced to the run's printed output, chart data, input tables or simple derived values; the evaluation reports how many cannot be traced, with grounding questions that invite made-up numbers. Measurement only for now.
- Simulated user (`--user-sim`): clarification questions carry a hidden intent; the model plays the user when the agent asks, and the run is graded on whether the conclusion matches what the user actually wanted, split by "asked first" vs "guessed".
- Three-step questions (`planned`) to measure plan auto-advance.
- Model routing in the eval (`--fast-model` / `-FastModel`): tokens per model, strong-model share, answers redone by the strong model, and an estimated cost from `EVAL_PRICES`.
- Memory questions (`memory`): a vague question, then a follow-up with the same word in the same user home — does the agent ask again?
- Insight questions (`insights`) and `eval/insights_benchmark.py`: recall and false findings of the insight scan on synthetic tables with planted patterns.
- Each eval question runs with its own `ALIDADE_HOME`, so the user's real rules, definitions and traces never leak into a run.
- Multi-angle questions (two independent analyses in one request) with duration, LLM-call and parallel-plan metrics.
- Harder questions: dirty data (string amounts, mixed date formats, duplicates), multi-table joins, and questions that must be clarified first; numeric and multi-part answer checks.
- Eval results can be uploaded to LangSmith as experiments (`eval/langsmith_upload.py`, `eval.ps1 upload -Run a,b` or `-LangSmith` / `--langsmith` after a run) to compare runs side by side there: one dataset example per question, one experiment per results directory, grades and costs as feedback. Local `runs.jsonl` / `report.md` stay the record; `config.json` now also records the `ALIDADE_*` switches of the run.

### Interface
- **Run traces**: every run records its model calls (model, routing tier and why, time, tokens and cache hits), tool calls, charts, chart checks and sub-agents; a timeline viewer in the toolbar shows them.
- The chat box stays usable while the agent runs: a one-line status shows what it is doing, and messages typed meanwhile wait in an editable queue.
- Question panel shows the agent's context and tags the recommended option.
- Fixed: running the agent in an empty workspace (no source tables).
- New name, icon, About page and attribution.
- Threads in the data-thread panel fold and unfold from their header. A folded thread shows how many steps and charts it holds and its latest question, and keeps a dot while it is still running; the choice is remembered per thread in the browser, and a thread opens again when you focus into it.

### Removed
- Desktop packaging, Microsoft CI and repo files, marketing media.
- Azure Blob workspace backend, Kusto Microsoft sign-in, the Superset connector.

### Renamed
- Package `data_formulator` → `alidade`, command `data_formulator` → `alidade`.
- Environment variables `DATA_FORMULATOR_*` / `DF_*` → `ALIDADE_*` (old names still work).
- Default data directory `~/.data_formulator` → `~/.alidade` (copied automatically on first start).
