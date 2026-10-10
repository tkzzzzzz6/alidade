# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""LangGraph engine behind :class:`~alidade.analyst.agent.AnalystAgent`.

Modules:
    graph        the state graph (nodes + routing)
    state        the checkpointed state schema
    llm          streaming LLM call via the LiteLLM client
    execution    sandbox execution (persistent exploration session)
    checkpoints  per-user SQLite checkpoint store for paused runs
    prompts      system prompt text and pure message helpers
"""
