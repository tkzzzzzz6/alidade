# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Saved definitions: what the user means by a vague word, remembered.

When a question uses a word that has several reasonable readings ("the best
product", "active customers", "recently"), the analyst asks first. Once the
user's answer settles it, the agent saves a definition — the term, what it
means, and the tables it applies to — with the ``save_definition`` tool. On a
later question that uses the same term on the same tables, the definition is
put in front of the agent, which then answers directly and says which saved
definition it used (instead of asking again).

Stored per user in ``<user home>/knowledge/definitions.json``. The user can
list and delete definitions in the knowledge panel. ``ALIDADE_DEFINITIONS=0``
turns saving and applying off (used for A/B evaluation).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

FILE_NAME = "definitions.json"
MAX_DEFINITIONS = 200
MAX_TERM = 60
MAX_MEANING = 400
MAX_ALIASES = 6
MAX_APPLIED = 5

_lock = threading.Lock()


def definitions_enabled() -> bool:
    return os.environ.get("ALIDADE_DEFINITIONS", "1").strip().lower() not in ("0", "false", "no", "off")


def _norm(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[\"'“”‘’「」『』《》（）()【】\[\]]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _base(name: str) -> str:
    """Table name without a file extension ("sales.csv" → "sales")."""
    return re.sub(r"\.[A-Za-z0-9]+$", "", str(name or "")).strip().lower()


_CJK = re.compile(r"[\u3400-\u9fff]")
_CJK_FILLER = set("的之地得了")
_STOPWORDS = frozenset({"the", "and", "for", "with", "that", "this", "which", "what", "our", "most", "best"})


def _words(text: str) -> set[str]:
    """English words, lower case, a trailing plural "s" dropped."""
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in re.findall(r"[a-z0-9]+", text)}


def _mentions(question: str, term: str) -> bool:
    """Does the question use this term? Exact phrase, or all of its words.

    "最好的行业" matches "哪个行业最好" (all its characters, filler 的 aside),
    "active customer" matches "active customers" — users rarely repeat a
    phrase word for word. Short terms (one word / two characters) must match
    exactly.
    """
    q, t = _norm(question), _norm(term)
    if len(t) < 2:
        return False
    if re.fullmatch(r"[a-z0-9 _-]+", t):
        if re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", q):
            return True
        words = {w for w in _words(t) if len(w) >= 3 and w not in _STOPWORDS}
        return len(words) >= 2 and words <= _words(q)
    if t in q:
        return True
    chars = {c for c in t if _CJK.match(c) and c not in _CJK_FILLER}
    return len(chars) >= 3 and all(c in q for c in chars)


class DefinitionStore:
    def __init__(self, user_home: Path | str):
        self.path = Path(user_home) / "knowledge" / FILE_NAME

    # ── storage ──────────────────────────────────────────────────────────

    def list(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [d for d in data if isinstance(d, dict) and d.get("term") and d.get("meaning")] if isinstance(data, list) else []

    def _write(self, items: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def save(self, term: str, meaning: str, *, tables: list[str] | None = None,
             aliases: list[str] | None = None, question: str = "") -> dict[str, Any]:
        """Add a definition, or replace the one with the same term on the same tables."""
        term = str(term or "").strip()[:MAX_TERM]
        meaning = str(meaning or "").strip()[:MAX_MEANING]
        if len(_norm(term)) < 2 or not meaning:
            raise ValueError("term (at least 2 characters) and meaning are required")
        tables = sorted({_base(t) for t in tables or [] if _base(t)})
        aliases = [a.strip()[:MAX_TERM] for a in aliases or [] if isinstance(a, str) and len(_norm(a)) >= 2]
        aliases = [a for a in dict.fromkeys(aliases) if _norm(a) != _norm(term)][:MAX_ALIASES]
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        with _lock:
            items = self.list()
            for d in items:
                if _norm(d["term"]) == _norm(term) and sorted(d.get("tables") or []) == tables:
                    d.update({"meaning": meaning, "aliases": aliases, "updated_at": now,
                              "question": question[:300] or d.get("question", "")})
                    self._write(items)
                    return d
            entry = {"id": uuid.uuid4().hex[:12], "term": term, "meaning": meaning, "aliases": aliases,
                     "tables": tables, "question": question[:300], "created_at": now, "updated_at": now,
                     "uses": 0, "last_used_at": None}
            items.append(entry)
            self._write(items[-MAX_DEFINITIONS:])
            return entry

    def delete(self, definition_id: str) -> bool:
        with _lock:
            items = self.list()
            kept = [d for d in items if d.get("id") != definition_id]
            if len(kept) == len(items):
                return False
            self._write(kept)
            return True

    def mark_used(self, ids: list[str]) -> None:
        if not ids:
            return
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        with _lock:
            items = self.list()
            for d in items:
                if d.get("id") in ids:
                    d["uses"] = int(d.get("uses") or 0) + 1
                    d["last_used_at"] = now
            self._write(items)

    # ── use ──────────────────────────────────────────────────────────────

    def match(self, question: str, table_names: list[str]) -> list[dict[str, Any]]:
        """Definitions whose term (or an alias) the question uses, on these tables.

        A definition saved without tables applies everywhere; one saved for
        some tables applies when any of them is loaded. Most recent first.
        """
        names = {_base(t) for t in table_names or []}
        found = []
        for d in self.list():
            tables = set(d.get("tables") or [])
            if tables and names and not (tables & names):
                continue
            if any(_mentions(question, t) for t in [d["term"], *(d.get("aliases") or [])]):
                found.append(d)
        found.sort(key=lambda d: d.get("updated_at") or "", reverse=True)
        return found[:MAX_APPLIED]


def format_block(definitions: list[dict[str, Any]]) -> str:
    """The context block that tells the agent to use saved definitions."""
    if not definitions:
        return ""
    lines = [
        "[SAVED DEFINITIONS]",
        "",
        "The user settled what these words mean in an earlier conversation. Use these "
        "meanings without asking again, unless the question itself says otherwise. In your "
        "answer, say in one short sentence which saved definition you used, so the user can "
        "change it.",
        "",
    ]
    for d in definitions:
        alias = f" (also: {', '.join(d['aliases'])})" if d.get("aliases") else ""
        scope = f" — for tables: {', '.join(d['tables'])}" if d.get("tables") else ""
        lines.append(f"- \"{d['term']}\"{alias}: {d['meaning']}{scope}")
    return "\n".join(lines)


SAVE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "save_definition",
        "description": (
            "Remember what the user means by a vague word in their question, so you do not have "
            "to ask again next time. Call it once, right after the user's answer to your question "
            "settles the meaning (it may go in the same reply as your next action). Only for "
            "meanings the USER chose — not for your own assumptions. Example: the user answered "
            "that \"active customer\" means \"placed an order in the last 90 days\"."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "term": {"type": "string",
                         "description": "The word or phrase exactly as the user used it in the question."},
                "meaning": {"type": "string",
                            "description": "What it means in data terms: metric, filters, time range, threshold."},
                "aliases": {"type": "array", "items": {"type": "string"},
                            "description": "Other ways the user may say the same thing (optional, up to 6)."},
            },
            "required": ["term", "meaning"],
        },
    },
}
