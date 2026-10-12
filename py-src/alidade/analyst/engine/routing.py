# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Model routing: a cheap model for routine steps, the strong one where it pays.

The model the user picked is the *strong* model. When ``ALIDADE_FAST_MODEL``
names another model of the same provider (same endpoint and key), each LLM
call of a run is routed:

* **strong** — the first call after new input from the user (the question,
  an answer to ``ask_user``, a system correction or sub-agent results): this is
  where the agent decides whether to ask, what to plan and how to read the
  user's words;
* **strong** — right after something failed (a tool error, a chart withheld by
  the self-check, a rejected action), so a weak model does not loop on its own
  mistake;
* **fast** — everything else: inspecting tables, writing the next chart's
  code, routine steps of a plan, sub-agent steps.

A final answer written by the fast model is checked before it is accepted:
if it is empty or quotes numbers that appear nowhere in what the run printed
or was given (see :mod:`.number_check`), the turn is redone by the strong
model. That is the one place where a cheap model's mistake would reach the
user directly.

The tiers can also differ by **reasoning effort** instead of (or as well as)
by model: ``ALIDADE_STRONG_EFFORT`` / ``ALIDADE_FAST_EFFORT`` (``low``,
``medium``, ``high``, ``max`` ...) set the effort sent with each tier's calls.
With DeepSeek, for example, one model can think hard where it matters
(``high``) and briefly elsewhere (``low``)::

    ALIDADE_STRONG_EFFORT=high  ALIDADE_FAST_EFFORT=low

A tier's effort is sent explicitly (LiteLLM would otherwise drop
``reasoning_effort`` for models it does not know, such as DeepSeek through an
OpenAI-compatible endpoint, and the provider would use its own default).

Without any of these every call uses the user's model at the agent's usual
effort (tier ``main``) and nothing here changes behaviour.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from alidade.analyst.engine import number_check

FAST_MODEL_ENV = "ALIDADE_FAST_MODEL"
FAST_EFFORT_ENV = "ALIDADE_FAST_EFFORT"
STRONG_EFFORT_ENV = "ALIDADE_STRONG_EFFORT"
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

STRONG, FAST, MAIN = "strong", "fast", "main"

# How a failure reads in the observation the model gets next.
_FAILURE = re.compile(
    r"^\s*(?:\[?ERROR\]?|\[GATED\]|\[CHART CHECK FAILED\]|\[SKILL ERROR\]|\[OBSERVATION – Step \d+ FAILED\])"
    r"|Traceback \(most recent call last\)|^Error: |\n\nError: |^Tool '[^']+' failed:|^Unknown tool:",
    re.MULTILINE,
)
MAX_UNTRACEABLE_SHOWN = 3


def fast_model_name() -> str:
    return os.environ.get(FAST_MODEL_ENV, "").strip()


def _effort(env: str) -> str | None:
    value = os.environ.get(env, "").strip().lower()
    return value if value in EFFORTS else None


def tier_efforts() -> dict[str, str | None]:
    """Reasoning effort per tier (None = the agent's usual effort)."""
    return {STRONG: _effort(STRONG_EFFORT_ENV), FAST: _effort(FAST_EFFORT_ENV), MAIN: None}


@dataclass(frozen=True)
class Choice:
    tier: str      # strong / fast / main
    reason: str    # short, shown in the run trace


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return "" if content is None else str(content)


def _since_last_assistant(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tail: list[dict[str, Any]] = []
    for m in reversed(messages):
        if m.get("role") == "assistant":
            break
        tail.append(m)
    return list(reversed(tail))


def failed(messages: list[dict[str, Any]]) -> bool:
    """Did anything fail since the model's last turn?"""
    return any(m.get("role") == "tool" and _FAILURE.search(_text(m.get("content")))
               for m in _since_last_assistant(messages))


def pick(messages: list[dict[str, Any]], *, routing_on: bool, sub_agent: bool = False) -> Choice:
    """Which model the next call should use."""
    if not routing_on:
        return Choice(MAIN, "")
    tail = _since_last_assistant(messages)
    if failed(messages):
        return Choice(STRONG, "recover from a failed step")
    users = [_text(m.get("content")) for m in tail if m.get("role") == "user"]
    if not sub_agent and users:
        if not any(m.get("role") == "assistant" for m in messages):
            return Choice(STRONG, "read the question")
        if any(x.startswith("[SUBTASK RESULTS]") for x in users):
            return Choice(STRONG, "merge sub-agent results")
        if all(x.startswith("[SYSTEM]") for x in users):
            return Choice(STRONG, "follow a system note")
        return Choice(STRONG, "new input from the user")
    if sub_agent:
        return Choice(FAST, "sub-agent step")
    return Choice(FAST, "routine step")


def untraceable_numbers(answer: str, messages: list[dict[str, Any]]) -> list[str]:
    """Numbers in ``answer`` that nothing in the conversation backs.

    Everything the model saw counts as evidence: tool output (printed results,
    chart data notes) and user / system messages (the question, table previews,
    data-quality notes). Derived values (differences, ratios, changes) of
    printed numbers are accepted as the number check allows.
    """
    printed: list[float] = []
    given: list[float] = []
    for m in messages:
        role = m.get("role")
        text = _text(m.get("content"))
        if not text:
            continue
        if role == "tool":
            printed.extend(number_check.numbers_in(text))
        elif role in ("user", "system"):
            given.extend(number_check.numbers_in(text))
    report = number_check.check(answer, number_check.Evidence(computed=[], printed=printed, given=given))
    return [c.text for c in report.unsupported]


def review_final(answer: str, messages: list[dict[str, Any]]) -> str:
    """Why a fast model's final answer should be redone ("" if it is fine)."""
    if not (answer or "").strip():
        return "empty final answer"
    bad = untraceable_numbers(answer, messages)
    if bad:
        shown = ", ".join(bad[:MAX_UNTRACEABLE_SHOWN]) + ("…" if len(bad) > MAX_UNTRACEABLE_SHOWN else "")
        return f"final answer has numbers with no source ({shown})"
    return ""
