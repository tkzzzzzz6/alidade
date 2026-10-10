# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""LLM call layer for the analyst graph.

One streaming, tool-enabled LLM call per ``agent`` node execution. The call
always streams (``stream=True``) so a *streaming action* (``write_report``)
can forward its growing argument to the frontend live as ``text_delta``
events; the full response is then reassembled into a non-streaming-shaped
object so the graph's routing logic stays identical to a buffered call.

Model access goes through the existing LiteLLM-backed
:class:`~alidade.agents.client_utils.Client`; LangGraph only
orchestrates — it never wraps the model.
"""

from __future__ import annotations

import json
import logging
import re
import time
from types import SimpleNamespace
from typing import Any, Callable

from alidade.agent_config import reasoning_effort_for
from alidade.agents.agent_utils import accumulate_reasoning_content

logger = logging.getLogger(__name__)

AGENT_ID = "analyst"
MAX_LLM_RETRIES = 3

EventWriter = Callable[[dict[str, Any]], None]


# ── Action-argument coercion ──────────────────────────────────────────────
# Weaker models sometimes JSON-encode a nested action argument as a string
# (e.g. ``"chart": "{...}"``). Parse those back to objects before dispatch.

_JSON_STRING_KEYS = (
    "chart", "input_tables", "questions", "options", "followups",
    "field_metadata", "field_display_names",
)


def rescue_unpack_json_strings(data: dict) -> None:
    """In-place: parse values that are JSON-encoded strings back to objects."""
    for key in _JSON_STRING_KEYS:
        val = data.get(key)
        if isinstance(val, str) and val.strip()[:1] in ("{", "["):
            try:
                data[key] = json.loads(val)
            except (json.JSONDecodeError, ValueError):
                pass


# ── Live tool-argument streaming ──────────────────────────────────────────


class StreamingArgExtractor:
    """Incrementally extract the decoded string value of a top-level JSON key
    from a growing tool-call ``arguments`` fragment.

    ``feed`` is given the full accumulated arguments so far and returns only the
    newly-decoded suffix of the target field's value (``""`` while nothing new
    can be safely decoded yet). Partial trailing escapes are held back until
    the next chunk completes them.
    """

    def __init__(self, field: str):
        self._open_re = re.compile(r'"' + re.escape(field) + r'"\s*:\s*"')
        self._emitted = 0

    def feed(self, args_so_far: str) -> str:
        decoded = self._decode(args_so_far)
        if decoded is None or len(decoded) <= self._emitted:
            return ""
        new = decoded[self._emitted:]
        self._emitted = len(decoded)
        return new

    def _decode(self, args: str) -> str | None:
        m = self._open_re.search(args)
        if not m:
            return None
        rest = args[m.end():]
        out: list[str] = []
        i, n = 0, len(rest)
        while i < n:
            ch = rest[i]
            if ch == "\\":
                if i + 1 >= n:
                    break  # dangling escape — wait for the next chunk
                out.append(rest[i:i + 2])
                i += 2
                continue
            if ch == '"':
                break  # closing quote — value complete
            out.append(ch)
            i += 1
        try:
            return json.loads('"' + "".join(out) + '"')
        except (json.JSONDecodeError, ValueError):
            return None  # e.g. partial ``\\uXXXX`` — wait for more


# ── Errors / retries ──────────────────────────────────────────────────────


def is_transient_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    if any(kw in msg for kw in (
        "timeout", "timed out", "rate limit", "rate_limit",
        "429", "503", "502", "connection", "reset by peer",
    )):
        return True
    name = type(exc).__name__.lower()
    return any(kw in name for kw in ("timeout", "ratelimit", "connection"))


def open_stream(client: Any, messages: list[dict], tools: list[dict]):
    """Open a streaming LLM call with tools, retrying transient errors before
    any token is consumed.

    ``parallel_tool_calls=False`` forces one tool call per response — the
    structural backstop for the one-action-per-turn rule. Providers that do not
    support the flag drop it (LiteLLM ``drop_params``); the graph's first-wins
    cardinality guard remains as a second net.
    """
    last_exc: Exception | None = None
    for attempt in range(MAX_LLM_RETRIES):
        try:
            kwargs: dict[str, Any] = {
                "tools": tools,
                "stream": True,
                "reasoning_effort": reasoning_effort_for(AGENT_ID, client.model),
                "parallel_tool_calls": False,
            }
            return client.get_completion_with_tools(messages, **kwargs)
        except Exception as e:  # noqa: BLE001 — classified below
            last_exc = e
            if is_transient_error(e) and attempt < MAX_LLM_RETRIES - 1:
                wait = 2 ** attempt
                logger.warning(
                    "[AnalystGraph] Transient LLM error (attempt %d/%d), retrying in %ds: %s",
                    attempt + 1, MAX_LLM_RETRIES, wait, e,
                )
                time.sleep(wait)
                continue
            raise
    raise last_exc  # pragma: no cover


def plain_completion(client: Any, messages: list[dict]) -> str:
    """A non-streaming, tool-less completion (used by planner / compressor).

    Returns the response text ("" on an empty response). Transient errors are
    retried like the main call; anything else propagates to the caller, which
    treats these auxiliary calls as best-effort.
    """
    last_exc: Exception | None = None
    for attempt in range(MAX_LLM_RETRIES):
        try:
            resp = client.get_completion(messages, stream=False)
            if not getattr(resp, "choices", None):
                return ""
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if is_transient_error(e) and attempt < MAX_LLM_RETRIES - 1:
                time.sleep(2 ** attempt)
                continue
            raise
    raise last_exc  # pragma: no cover


# ── Streaming call with live forwarding ───────────────────────────────────


class StreamResult(SimpleNamespace):
    """Reassembled response: ``content``, ``tool_calls``, ``reasoning_content``,
    ``finish_reason`` and ``streamed_channels`` (tool_call_id -> channel whose
    argument was already forwarded live)."""


def stream_completion(
    client: Any,
    messages: list[dict],
    tools: list[dict],
    stream_spec_for: Callable[[str], tuple[str, str] | None],
    write: EventWriter,
) -> StreamResult:
    """Run one streaming LLM call and return the reassembled response.

    ``stream_spec_for(action_name)`` returns ``(field, channel)`` for streaming
    actions (``write_report``) and ``None`` otherwise. For a streaming action the
    ``action`` commitment event is written once, then the growing ``field``
    argument is forwarded as ``text_delta`` events on ``channel``.
    """
    stream = open_stream(client, messages, tools)

    content_parts: list[str] = []
    reasoning_acc: str | None = None
    finish_reason = "stop"
    tool_calls_acc: dict[int, dict[str, Any]] = {}
    streamers: dict[int, dict[str, Any]] = {}
    streamed_channels: dict[str, str] = {}

    for chunk in stream:
        if not getattr(chunk, "choices", None):
            continue
        choice0 = chunk.choices[0]
        delta = getattr(choice0, "delta", None)
        if delta is None:
            continue
        if getattr(choice0, "finish_reason", None):
            finish_reason = choice0.finish_reason

        reasoning_acc = accumulate_reasoning_content(reasoning_acc, delta)

        content = getattr(delta, "content", None)
        if content:
            content_parts.append(content)

        for tcd in getattr(delta, "tool_calls", None) or []:
            idx = getattr(tcd, "index", 0) or 0
            slot = tool_calls_acc.setdefault(idx, {"id": None, "name": "", "arguments": ""})
            if getattr(tcd, "id", None):
                slot["id"] = tcd.id
            fn = getattr(tcd, "function", None)
            if fn is not None:
                if getattr(fn, "name", None):
                    slot["name"] = fn.name
                arg_delta = getattr(fn, "arguments", None)
                if arg_delta:
                    slot["arguments"] += arg_delta
            _forward_stream_delta(idx, slot, streamers, streamed_channels, stream_spec_for, write)

    tool_calls: list[dict[str, Any]] = []
    for i in sorted(tool_calls_acc):
        tc = tool_calls_acc[i]
        tool_calls.append({
            "id": tc["id"] or f"call_{i}",
            "type": "function",
            "function": {"name": tc["name"], "arguments": tc["arguments"]},
        })

    return StreamResult(
        content="".join(content_parts) or None,
        tool_calls=tool_calls,
        reasoning_content=reasoning_acc,
        finish_reason=finish_reason,
        streamed_channels=streamed_channels,
    )


def _forward_stream_delta(
    idx: int,
    slot: dict[str, Any],
    streamers: dict[int, dict[str, Any]],
    streamed_channels: dict[str, str],
    stream_spec_for: Callable[[str], tuple[str, str] | None],
    write: EventWriter,
) -> None:
    name = slot.get("name") or ""
    if not name:
        return
    st = streamers.get(idx)
    if st is None:
        spec = stream_spec_for(name)
        if spec is None:
            streamers[idx] = {"active": False}
            return
        field, channel = spec
        st = {
            "active": True,
            "channel": channel,
            "extractor": StreamingArgExtractor(field),
            "announced": False,
        }
        streamers[idx] = st
    if not st["active"]:
        return

    if not st["announced"]:
        write({"type": "action", "action": name})
        st["announced"] = True

    new_text = st["extractor"].feed(slot["arguments"])
    if new_text:
        write({"type": "text_delta", "channel": st["channel"], "content": new_text})
        tcid = slot.get("id")
        if tcid:
            streamed_channels[tcid] = st["channel"]


def assistant_message(
    content: str | None,
    tool_calls: list[dict[str, Any]] | None,
    reasoning_content: str | None,
) -> dict[str, Any]:
    """Build a provider-valid assistant message (with reasoning echoed back for
    providers that require it on later turns, e.g. DeepSeek thinking mode)."""
    msg: dict[str, Any] = {"role": "assistant", "content": content or None}
    if reasoning_content is not None:
        msg["reasoning_content"] = reasoning_content
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg
