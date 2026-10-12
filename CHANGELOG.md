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
- When a question is partly outside the loaded data (a year or field that is not there), the agent answers what the data supports and says what is missing instead of stopping to ask.
- `ALIDADE_PLAN_AUTO_ADVANCE=0` switches plan auto-advance off, for A/B evaluation only.
- **Model routing** (`ALIDADE_FAST_MODEL`): the model the user picked handles reading the question, the user's answers and recovery after a failed step; a cheaper model of the same provider does routine steps. A final answer from the cheaper model whose numbers have no source in the run is redone by the strong model. Tiers can also differ by reasoning effort on one model (`ALIDADE_STRONG_EFFORT` / `ALIDADE_FAST_EFFORT`, e.g. DeepSeek high / low); the tier's effort is passed through LiteLLM, which otherwise drops it for models it does not know.
- Data quality check: each table in the agent's context comes with code-computed notes on duplicate rows, numbers or dates stored as text (and mixed date formats), the same label spelled differently, and missing values, each with the fix.

### Evaluation
- `eval/`: a harness that runs a question set against a real model and compares two versions (success, chart errors, clarification, tokens, time), with one-command scripts (`eval.ps1` / `eval.bat` / `eval.sh`) and keys in `eval/.env`.
- Token accounting: provider-reported prefix-cache hits, uncached input tokens, and tokens spent before the user confirms a direction.
- Number provenance check (`analyst/engine/number_check.py`): every number in a conclusion is traced to the run's printed output, chart data, input tables or simple derived values; the evaluation reports how many cannot be traced, with grounding questions that invite made-up numbers. Measurement only for now.
- Simulated user (`--user-sim`): clarification questions carry a hidden intent; the model plays the user when the agent asks, and the run is graded on whether the conclusion matches what the user actually wanted, split by "asked first" vs "guessed".
- Three-step questions (`planned`) to measure plan auto-advance.
- Multi-angle questions (two independent analyses in one request) with duration, LLM-call and parallel-plan metrics.
- Harder questions: dirty data (string amounts, mixed date formats, duplicates), multi-table joins, and questions that must be clarified first; numeric and multi-part answer checks.

### Interface
- **Run traces**: every run records its model calls (model, routing tier and why, time, tokens and cache hits), tool calls, charts, chart checks and sub-agents; a timeline viewer in the toolbar shows them.
- The chat box stays usable while the agent runs: a one-line status shows what it is doing, and messages typed meanwhile wait in an editable queue.
- Question panel shows the agent's context and tags the recommended option.
- Fixed: running the agent in an empty workspace (no source tables).
- New name, icon, About page and attribution.

### Removed
- Desktop packaging, Microsoft CI and repo files, marketing media.
- Azure Blob workspace backend, Kusto Microsoft sign-in, the Superset connector.

### Renamed
- Package `data_formulator` → `alidade`, command `data_formulator` → `alidade`.
- Environment variables `DATA_FORMULATOR_*` / `DF_*` → `ALIDADE_*` (old names still work).
- Default data directory `~/.data_formulator` → `~/.alidade` (copied automatically on first start).
