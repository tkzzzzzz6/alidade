"""Prompt text must not mirror the evaluation questions.

Examples in the system prompt that match an eval question (its wording, its
criterion or its exact numbers) turn the eval into a test of memorised hints.
This list is the phrases that once leaked; keep it growing when a new one is
found.
"""
import json

import pytest

from alidade.analyst.engine import planning
from alidade.analyst.engine.prompts import SYSTEM_PROMPT
from alidade.analyst.skills import build_registry

LEAKED = [
    "most successful", "performance", "last year", "87 duplicate", "volatility", "trading volume",
    "renewable", "co₂", "co2 ", "stock returned", "box office", "gasoline", "unemployment",
]


@pytest.mark.parametrize("subagents", ["0", "1"])
def test_prompts_do_not_mirror_eval_questions(monkeypatch, subagents):
    monkeypatch.setenv("ALIDADE_SUBAGENTS", subagents)
    text = "\n".join([
        SYSTEM_PROMPT, planning.planning_prompt(), json.dumps(planning.plan_tool()),
        build_registry().load_body("core"),
    ]).lower()
    found = [w for w in LEAKED if w in text]
    assert found == []
