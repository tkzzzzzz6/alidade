# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Keep the analyst's context inside a token budget.

Before each LLM call the graph estimates the conversation size. Above the
budget it compresses in two stages, cheapest first:

1. **Trim** — older tool results and observations (code listings, data
   samples, printed frames) are cut to a short head + tail. The recent tail of
   the conversation is never touched.
2. **Summarise** — if still over budget, the middle of the conversation (after
   the system prompt and the initial context message, before the recent tail)
   is replaced by one ``[EARLIER STEPS SUMMARY]`` message, written by the LLM.
   If that call fails, a deterministic digest of the same steps is used.

The cut never separates an assistant ``tool_calls`` turn from its ``tool``
results, so the conversation stays valid for every provider.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable

logger = logging.getLogger(__name__)

DEFAULT_BUDGET_TOKENS = 60_000
KEEP_TAIL_MESSAGES = 10
TRIM_KEEP_CHARS = 600
TRIMMED_MARK = "\n…[trimmed to save context]…\n"


def budget_tokens() -> int:
    try:
        return max(4_000, int(os.environ.get("ALIDADE_AGENT_CONTEXT_BUDGET", DEFAULT_BUDGET_TOKENS)))
    except ValueError:
        return DEFAULT_BUDGET_TOKENS


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return "" if content is None else str(content)


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Rough token estimate: ~4 chars/token for Latin text, ~1 per CJK char,
    plus a flat cost per image."""
    total = 0
    for m in messages:
        text = _content_text(m.get("content"))
        for tc in m.get("tool_calls") or []:
            text += (tc.get("function") or {}).get("arguments", "")
        cjk = sum(1 for ch in text if "⺀" <= ch <= "鿿")
        total += cjk + (len(text) - cjk) // 4 + 4
        if isinstance(m.get("content"), list):
            total += 800 * sum(1 for p in m["content"] if isinstance(p, dict) and p.get("type") == "image_url")
    return total


def _tail_start(messages: list[dict[str, Any]], keep: int) -> int:
    """Index where the protected recent tail begins (never on a ``tool`` msg)."""
    start = max(2, len(messages) - keep)
    while start > 2 and messages[start].get("role") == "tool":
        start -= 1
    return start


def _trim(text: str) -> str:
    if len(text) <= TRIM_KEEP_CHARS * 2 + len(TRIMMED_MARK):
        return text
    return text[:TRIM_KEEP_CHARS] + TRIMMED_MARK + text[-TRIM_KEEP_CHARS // 2:]


def trim_old_results(messages: list[dict[str, Any]], keep_tail: int = KEEP_TAIL_MESSAGES) -> list[dict[str, Any]]:
    """Stage 1: shorten bulky tool results/observations outside the tail."""
    tail = _tail_start(messages, keep_tail)
    out = []
    for i, m in enumerate(messages):
        if 2 <= i < tail and m.get("role") == "tool" and isinstance(m.get("content"), str):
            m = {**m, "content": _trim(m["content"])}
        elif 2 <= i < tail and isinstance(m.get("content"), list):
            # Older image turns: drop the pixels, keep the caption.
            texts = [p for p in m["content"] if isinstance(p, dict) and p.get("type") == "text"]
            m = {**m, "content": texts or "[image removed]"}
        out.append(m)
    return out


def _digest(middle: list[dict[str, Any]]) -> str:
    """Deterministic fallback summary of the compressed steps."""
    lines = []
    for m in middle:
        role = m.get("role")
        if role == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                label = args.get("purpose") or args.get("title") or args.get("display_instruction") or ""
                lines.append(f"- called {fn.get('name')}" + (f": {label}" if label else ""))
            text = _content_text(m.get("content")).strip()
            if text:
                lines.append(f"- noted: {text[:300]}")
        elif role == "tool":
            text = _content_text(m.get("content")).strip().replace("\n", " ")
            if text:
                lines.append(f"  → {text[:300]}")
        elif role == "user":
            text = _content_text(m.get("content")).strip()
            if text and not text.startswith("[SKILL LOADED"):
                lines.append(f"- user: {text[:300]}")
    return "\n".join(lines)


SUMMARY_PROMPT = """\
You compress the working memory of a data-analysis agent. Below are the earlier
steps of its current run (tool calls, results, charts it produced, user replies).
Write a compact summary the agent can rely on to continue the work:

- the user's goal and any answers/preferences the user gave;
- which tables/columns were inspected and the key facts and numbers found;
- each chart already produced (its question, chart id if present, and takeaway);
- dead ends and errors worth not repeating;
- what remains to be done.

Keep exact table names, column names, chart ids and numbers. Use terse bullet
points, at most ~400 words. Output only the summary."""


def summarise_middle(
    messages: list[dict[str, Any]],
    complete: Callable[[list[dict[str, Any]]], str] | None,
    keep_tail: int = KEEP_TAIL_MESSAGES,
) -> tuple[list[dict[str, Any]], int]:
    """Stage 2: replace the middle with one summary message.

    Returns ``(new_messages, n_replaced)``; ``n_replaced == 0`` means there was
    nothing to compress.
    """
    tail = _tail_start(messages, keep_tail)
    middle = messages[2:tail]
    if len(middle) < 2:
        return messages, 0

    transcript = _digest(middle)
    summary = ""
    if complete is not None:
        try:
            raw_middle = json.dumps(
                [{"role": m.get("role"), "content": _content_text(m.get("content"))[:4000],
                  "tool_calls": [(tc.get("function") or {}) for tc in m.get("tool_calls") or []] or None}
                 for m in middle],
                ensure_ascii=False,
            )
            summary = complete([
                {"role": "system", "content": SUMMARY_PROMPT},
                {"role": "user", "content": raw_middle[:120_000]},
            ]).strip()
        except Exception:  # noqa: BLE001 — fall back to the digest
            logger.warning("[AnalystGraph] Context summary call failed; using digest", exc_info=True)
    if not summary:
        summary = transcript or "(earlier steps omitted)"

    note = {
        "role": "user",
        "content": (
            "[EARLIER STEPS SUMMARY] To save context, the earlier steps of this run were "
            "condensed. Rely on this summary; re-inspect data if you need exact values.\n\n"
            + summary
        ),
    }
    # Loaded skill bodies carry instructions the agent still needs — keep them.
    kept_skill_bodies = [
        m for m in middle
        if m.get("role") == "user" and _content_text(m.get("content")).startswith("[SKILL LOADED")
    ]
    return messages[:2] + kept_skill_bodies + [note] + messages[tail:], len(middle)


def compress_if_needed(
    messages: list[dict[str, Any]],
    complete: Callable[[list[dict[str, Any]]], str] | None,
    budget: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Return ``(messages, info)``; ``info`` is ``None`` when nothing changed."""
    budget = budget or budget_tokens()
    before = estimate_tokens(messages)
    if before <= budget:
        return messages, None

    trimmed = trim_old_results(messages)
    after = estimate_tokens(trimmed)
    stage = "trim"
    replaced = 0
    if after > budget:
        trimmed, replaced = summarise_middle(trimmed, complete)
        after = estimate_tokens(trimmed)
        stage = "summary" if replaced else "trim"
    if after >= before:
        return messages, None
    return trimmed, {
        "stage": stage,
        "tokens_before": before,
        "tokens_after": after,
        "messages_summarised": replaced,
    }
