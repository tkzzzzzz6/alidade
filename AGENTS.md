# AGENTS.md

Instructions for AI coding agents (Claude Code, Codex, Cursor, Copilot, …) working
in this repository. Humans: see [CONTRIBUTING.md](CONTRIBUTING.md).

Alidade is a local AI data-analysis agent: a Flask + LangGraph backend (Python,
models via LiteLLM) and a React + Redux + Vega-Lite frontend, built on Microsoft
Data Formulator. The owner develops on **Windows**; CI and Docker run **Linux**.
Code must work on both.

## Repository map

| Path | What lives there |
|---|---|
| `py-src/alidade/app.py` | Flask app, CLI (`alidade`, `--dev`, `--desktop`) |
| `py-src/alidade/analyst/engine/` | LangGraph agent: `graph.py`, `planning.py`, `chart_check.py`, `execution.py`, `subagent.py`, `routing.py`, `number_check.py`, `table_access.py`, `insights.py` |
| `py-src/alidade/analyst/skills/` | Agent skills (`SKILL.md` + `tools.json` per skill) |
| `py-src/alidade/agents/` | Context builders and helper agents |
| `py-src/alidade/sandbox/` | Execution sandbox for model-written code (`local_sandbox.py`, `guards.py`, `docker_sandbox.py`) |
| `py-src/alidade/routes/`, `security/`, `datalake/`, `data_loader/` | API routes, path safety / log sanitising, workspace storage, data connectors |
| `src/` | Frontend (React, TypeScript) |
| `tests/backend/`, `tests/frontend/` | pytest and vitest suites |
| `eval/` | Real-model evaluation harness (costs API money — see below) |
| `docs/dev-guides/` | Cross-cutting design contracts — read before changing those areas |
| `packaging/`, `.github/workflows/` | Desktop build (PyInstaller + pywebview), CI and release |

## Commands

```bash
# backend (Python 3.11+, uv)
uv sync                                   # create .venv with all deps
uv run alidade                            # full app on http://localhost:5567
uv run alidade --dev                      # backend only (use with `yarn start`)

# frontend (Yarn 4 via Corepack — never npm, pnpm or global Yarn 1)
corepack enable
yarn install
yarn start                                # Vite dev server on :5173
yarn build                                # -> py-src/alidade/dist (served by Flask)

# checks — run the ones your change touches before opening a PR
uv run python -m pytest tests/backend -q  # backend tests (use -q, not -v)
yarn test                                 # frontend tests (vitest)
uv run ruff check --select E9,F63,F7,F82 py-src eval   # the lint CI runs
```

Run a single backend test file with `uv run python -m pytest tests/backend/<path>.py -q`.
Tests marked `live` hit real services and skip without credentials.

## How work flows

1. Work starts from a **GitHub issue**. Restate the goal and acceptance criteria
   before writing code; if the issue is ambiguous, ask in the issue first.
2. Branch from an up-to-date `master`: `<type>/<issue-number>-<short-slug>`
   (e.g. `fix/12-sandbox-fork-deadlock`). Never push to `master`.
3. Keep the PR to **one concern**. No drive-by refactors or formatting churn.
4. Commit with Conventional Commits, imperative, English:
   `type(scope): summary` — types `feat`, `fix`, `docs`, `test`, `ci`, `refactor`,
   `perf`, `chore`; scopes follow the code (`agent`, `sandbox`, `ui`, `eval`, `ci`, …).
5. Open the PR with `Closes #<n>`, what changed, **how you verified it** (commands
   and results), and any trade-off left open. CI must be green; the owner reviews
   and merges.

## Rules that are easy to get wrong

- **Tests travel with code.** New behaviour or a bug fix comes with a test (for a
  bug, one that fails before the fix). When a test fails, do not edit, skip or
  loosen it to make it pass — diagnose whether the test, the code or the spec is
  wrong and say which.
- **Secrets.** Keys live in `.env` and `eval/.env` (git-ignored). Never read, print,
  log or commit them, never put a key on a command line, and never copy one into
  results or docs.
- **Evaluation costs money.** `eval/` runs real models with the owner's keys. Do not
  run it unless asked; offline checks (pytest, `eval/bench_large_data.py`,
  `eval/insights_benchmark.py`) are fine.
- **Numbers must be measured.** Any figure in README, CHANGELOG, docs or a PR comes
  from a real run you can point to (a result directory or command output). Report
  regressions and flat results too — e.g. "time −44%, tokens +7%" — never only
  the flattering half.
- **Cross-platform.** Do not rely on POSIX-only modules or the `fork` start method;
  give Windows (PowerShell) equivalents for user-facing commands.
- **User-visible text** in the frontend goes through i18n, with keys added to both
  `en` and `zh` locales.
- **Errors and logs.** Application errors return HTTP 200 with
  `{"status": "error", ...}`; never send raw exception text to the client; sanitise
  URLs, params and tokens before logging.
- **Paths.** Any user- or model-controlled path goes through `ConfinedDir`.
- **DataFrames to JSON** go through `df_to_safe_records` (ISO dates).
- **Docs follow code.** If behaviour, a command or a convention changes, update
  README / `docs/` and add a CHANGELOG line in the same PR.

The detailed versions of these rules are in `.cursor/rules/*.mdc` (they apply to
every agent, not only Cursor) and in `docs/dev-guides/`:

| Touching… | Read first |
|---|---|
| streaming endpoints / NDJSON | `docs/dev-guides/1-streaming-protocol.md` |
| logging, credentials, connectors | `docs/dev-guides/2-log-sanitization.md` |
| agent prompts, i18n | `docs/dev-guides/6-i18n-language-injection.md` |
| API errors | `docs/dev-guides/7-unified-error-handling.md` |
| file access, workspace paths, sandbox config | `docs/dev-guides/8-path-safety.md` |
| sandbox execution, agent tool loop | `docs/dev-guides/12-sandbox-session.md`, `docs/sandbox.md` |
| large tables / DuckDB | `docs/plans/large-data.md` |

## Before you say "done"

Read your own `git diff` as a reviewer would: right files, right layer, no
leftovers, tests and docs updated, nothing unrelated. If a trade-off remains,
state it in the PR rather than hiding it.
