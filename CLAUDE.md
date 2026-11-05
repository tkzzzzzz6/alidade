@AGENTS.md

# Claude Code

All project instructions live in [AGENTS.md](AGENTS.md) (imported above) — read it
first. Keep repository rules there, not here, so every agent sees the same ones;
this file only holds notes specific to Claude Code.

- `.claude/settings.json` pre-approves `uv run …`, so prefer `uv run python -m pytest …`
  and `uv run alidade …` over bare `python` / `pytest`.
- When working from an issue, end with a pull request (`Closes #<n>`), not a push to
  `master`. Put the verification you ran in the PR description.
