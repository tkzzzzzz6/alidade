# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Optional LangSmith tracing — off unless ``ALIDADE_LANGSMITH=1``.

When on (and ``LANGSMITH_API_KEY`` is set), an analyst run shows up in
LangSmith as one tree: a readable root (the question in, the answer out —
:func:`start_trace`), then the LangGraph nodes (``agent``, ``tools``,
``dispatch``, each parallel ``subagent`` branch with the sub-agent's own
graph inside it, ``merge`` …) and, under the node that made it, every model
call that goes through LiteLLM — the messages and tools sent, the reply
(text, reasoning, tool calls) and the token usage.

LangGraph traces its nodes by itself once ``LANGSMITH_TRACING`` is on; the
model calls are not LangChain models, so :func:`traced_call` adds them as
``llm`` runs under whatever run is current (a call made outside any graph
becomes a run of its own).

Everything here is best effort: a tracing failure is logged and never fails
the model call. Inputs and outputs can be withheld with LangSmith's own
``LANGSMITH_HIDE_INPUTS`` / ``LANGSMITH_HIDE_OUTPUTS``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

logger = logging.getLogger(__name__)

ENV = "ALIDADE_LANGSMITH"
DEFAULT_PROJECT = "alidade"
_TRUE = ("1", "true", "yes", "on")
_warned: set[str] = set()


def _warn_once(key: str, msg: str) -> None:
    if key not in _warned:
        _warned.add(key)
        logger.warning(msg)


def enabled() -> bool:
    """``ALIDADE_LANGSMITH`` is on, an API key is set and ``langsmith`` imports."""
    if os.environ.get(ENV, "").strip().lower() not in _TRUE:
        return False
    if not (os.environ.get("LANGSMITH_API_KEY") or os.environ.get("LANGCHAIN_API_KEY")):
        _warn_once("key", f"{ENV} is on but LANGSMITH_API_KEY is not set; LangSmith tracing stays off")
        return False
    try:
        import langsmith  # noqa: F401
    except ImportError:
        _warn_once("import", f"{ENV} is on but the langsmith package is not installed; tracing stays off")
        return False
    return True


def configure() -> bool:
    """Turn LangGraph's own tracing on when the switch is on. Idempotent."""
    if not enabled():
        return False
    changed = os.environ.get("LANGSMITH_TRACING", "").strip().lower() not in _TRUE
    os.environ["LANGSMITH_TRACING"] = "true"
    if not os.environ.get("LANGSMITH_PROJECT", "").strip():
        os.environ["LANGSMITH_PROJECT"] = DEFAULT_PROJECT
    if changed:
        try:                                  # langsmith caches env lookups
            from langsmith import utils as ls_utils
            ls_utils.get_env_var.cache_clear()
        except Exception:  # noqa: BLE001
            pass
        logger.info("LangSmith tracing on (project %s)", os.environ["LANGSMITH_PROJECT"])
    return True


def run_config(config: dict[str, Any], name: str, metadata: dict[str, Any] | None = None,
               tags: list[str] | None = None) -> dict[str, Any]:
    """Name and label a graph run in LangSmith (``config`` unchanged when off)."""
    if not enabled():
        return config
    config["run_name"] = name
    config["metadata"] = {**(config.get("metadata") or {}), **(metadata or {})}
    config["tags"] = [*(config.get("tags") or []), "alidade", *(tags or [])]
    return config


# ── the run's root ────────────────────────────────────────────────────────

def start_trace(name: str, inputs: dict[str, Any], metadata: dict[str, Any] | None = None,
                tags: list[str] | None = None):
    """A root run with readable inputs, or ``None`` when tracing is off.

    Give the graph run to :func:`attach` so it nests under it, and close it
    with :func:`end_trace`. ``metadata["thread_id"]`` groups the runs of one
    conversation (a run paused for the user and its resume) in LangSmith's
    Threads view.
    """
    if not enabled():
        return None
    try:
        from langsmith.run_trees import RunTree

        run = RunTree(name=name, run_type="chain", inputs=inputs,
                      extra={"metadata": dict(metadata or {})}, tags=["alidade", *(tags or [])],
                      project_name=os.environ.get("LANGSMITH_PROJECT") or DEFAULT_PROJECT,
                      **_context_client())
        run.post()
        return run
    except Exception as exc:  # noqa: BLE001
        logger.debug("LangSmith: could not start a trace: %s", exc)
        return None


def attach(stream: Iterator[Any], parent) -> Iterator[Any]:
    """``stream`` (a LangGraph stream) with its run nested under ``parent``.

    The graph picks its parent up when it starts, i.e. on the first ``next``;
    the tracing context is set only around that call, so nothing leaks to the
    code that reads the stream between events.
    """
    if parent is None:
        yield from stream
        return
    from langsmith import run_helpers

    with run_helpers.tracing_context(parent=parent, **_context_client()):
        try:
            first = next(stream)
        except StopIteration:
            return
    yield first
    yield from stream


def end_trace(run, outputs: dict[str, Any] | None = None, error: str | None = None) -> None:
    _end_run(run, outputs=outputs, error=error)


def trace_id(run) -> str | None:
    """The id feedback is attached to (``None`` when not traced)."""
    return str(run.id) if run is not None else None


# ── user feedback (👍 / 👎 on an answer) ────────────────────────────────────

FEEDBACK_KEY = "user_rating"
_FEEDBACK_NS = uuid.UUID("5d0b7f2e-6a8b-4c3e-9e1d-2f4a6b8c0d1e")


def _api_key() -> bytes:
    return (os.environ.get("LANGSMITH_API_KEY") or os.environ.get("LANGCHAIN_API_KEY") or "").encode()


def feedback_token(trace: str, identity: str | None) -> str:
    """Proof that ``trace`` was handed to ``identity`` (sent with the answer).

    Keyed with the LangSmith API key, which only the server has, so a client
    can rate its own answers and not someone else's trace.
    """
    msg = f"{identity or ''}:{trace}".encode()
    return hmac.new(_api_key(), msg, hashlib.sha256).hexdigest()[:32]


def check_feedback_token(trace: str, identity: str | None, token: str) -> bool:
    return bool(token) and hmac.compare_digest(feedback_token(trace, identity), str(token))


def send_feedback(trace: str, identity: str | None, score: int | None, comment: str = "") -> str:
    """Record (or change, or with ``score=None`` withdraw) one user's rating of
    an answer in LangSmith. Returns ``"created"``, ``"updated"`` or ``"deleted"``.

    Each user has one rating per answer (a fixed feedback id), so voting
    again replaces the earlier vote. Raises when LangSmith rejects it.
    """
    import langsmith
    from langsmith import utils as ls_utils

    client = _context_client().get("client") or langsmith.Client()
    fid = uuid.uuid5(_FEEDBACK_NS, f"{identity or ''}:{trace}")
    if score is None:
        try:
            client.delete_feedback(fid)
        except ls_utils.LangSmithNotFoundError:
            pass
        return "deleted"
    try:
        client.create_feedback(run_id=trace, trace_id=trace, key=FEEDBACK_KEY, score=score,
                               comment=comment or None, feedback_id=fid,
                               source_info={"source": "alidade-ui"})
        return "created"
    except ls_utils.LangSmithConflictError:
        client.update_feedback(fid, score=score, comment=comment or None)
        return "updated"


def _context_client() -> dict[str, Any]:
    """The client set with ``tracing_context(client=…)`` (tests), if any."""
    try:
        from langsmith import run_helpers

        client = run_helpers.get_tracing_context().get("client")
        return {"client": client} if client is not None else {}
    except Exception:  # noqa: BLE001
        return {}


# ── model calls ───────────────────────────────────────────────────────────

def traced_call(call: Callable[[], Any], *, model: str, endpoint: str, messages: list,
                tools: list | None, params: dict[str, Any], stream: bool) -> Any:
    """Run ``call()`` (a LiteLLM completion) and record it as an ``llm`` run.

    A streamed reply is recorded when the caller has read it to the end (or
    stopped reading); the stream itself is passed through unchanged.
    """
    if not enabled():
        return call()
    run = _start_run(model=model, endpoint=endpoint, messages=messages, tools=tools,
                     params=params, stream=stream)
    try:
        resp = call()
    except BaseException as exc:
        _end_run(run, error=f"{type(exc).__name__}: {exc}")
        raise
    if run is None:
        return resp
    if stream:
        return _traced_stream(resp, run)
    _end_run(run, outputs=_response_outputs(resp))
    return resp


def _start_run(*, model, endpoint, messages, tools, params, stream):
    try:
        from langsmith import run_helpers
        from langsmith.run_trees import RunTree

        inputs: dict[str, Any] = {"messages": messages}
        if tools:
            inputs["tools"] = tools
        invocation = {k: v for k, v in params.items()
                      if k in ("reasoning_effort", "temperature", "max_tokens", "parallel_tool_calls")}
        extra = {"metadata": {"ls_provider": endpoint or "litellm", "ls_model_name": model,
                              "ls_model_type": "chat", "stream": stream},
                 "invocation_params": invocation}
        parent = run_helpers.get_current_run_tree()
        if parent is not None:
            run = parent.create_child(name=f"litellm {model}", run_type="llm", inputs=inputs, extra=extra,
                                      start_time=datetime.now(timezone.utc))
        else:
            # outside any graph: one of the helper agents (data loading, starter
            # questions …) — name the run after it so the list says who called
            caller = _caller_name()
            extra["metadata"]["agent"] = caller or "unknown"
            run = RunTree(name=f"{caller} · {model}" if caller else f"litellm {model}", run_type="llm",
                          inputs=inputs, extra=extra, tags=["alidade", *([caller] if caller else [])],
                          project_name=os.environ.get("LANGSMITH_PROJECT") or DEFAULT_PROJECT,
                          **_context_client())
        run.post()
        return run
    except Exception as exc:  # noqa: BLE001
        logger.debug("LangSmith: could not start an llm run: %s", exc)
        return None


def _caller_name() -> str | None:
    """The agent class (``DataLoadAgent`` …) that made this model call."""
    import sys

    frame = sys._getframe(2)
    for _ in range(25):
        if frame is None:
            return None
        owner = frame.f_locals.get("self")
        if owner is not None:
            cls = type(owner)
            if cls.__module__.startswith("alidade.") and cls.__name__ != "Client" \
                    and cls.__name__.endswith(("Agent", "Agents")):
                return cls.__name__
        frame = frame.f_back
    return None


def _end_run(run, outputs: dict[str, Any] | None = None, error: str | None = None) -> None:
    if run is None:
        return
    try:
        run.end(outputs=outputs, error=error)
        run.patch()
    except Exception as exc:  # noqa: BLE001
        logger.debug("LangSmith: could not end an llm run: %s", exc)


def _traced_stream(resp: Any, run) -> Iterator[Any]:
    acc = _StreamAccumulator()
    finished = False
    try:
        for chunk in resp:
            acc.add(chunk)
            yield chunk
        finished = True
    except BaseException as exc:
        if not isinstance(exc, GeneratorExit):
            _end_run(run, outputs=acc.outputs(), error=f"{type(exc).__name__}: {exc}")
            finished = None                   # already recorded
        raise
    finally:
        if finished is not None:
            out = acc.outputs()
            if not finished:
                out["stopped_early"] = True   # the caller stopped reading
            _end_run(run, outputs=out)


def _get(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _usage(usage: Any) -> dict[str, Any] | None:
    if usage is None:
        return None
    prompt, completion = _get(usage, "prompt_tokens"), _get(usage, "completion_tokens")
    if not isinstance(prompt, int) and not isinstance(completion, int):
        return None
    prompt, completion = prompt or 0, completion or 0
    out: dict[str, Any] = {"input_tokens": prompt, "output_tokens": completion,
                           "total_tokens": prompt + completion}
    cached = _get(_get(usage, "prompt_tokens_details"), "cached_tokens")
    if not isinstance(cached, int):
        cached = _get(usage, "prompt_cache_hit_tokens")
    if isinstance(cached, int):
        out["input_token_details"] = {"cache_read": cached}
    return out


class _StreamAccumulator:
    """Rebuilds the assistant message from streamed chunks."""

    def __init__(self) -> None:
        self.content: list[str] = []
        self.reasoning: list[str] = []
        self.tool_calls: dict[int, dict[str, Any]] = {}
        self.finish_reason: str | None = None
        self.usage: Any = None

    def add(self, chunk: Any) -> None:
        try:
            if _get(chunk, "usage") is not None:
                self.usage = _get(chunk, "usage")
            for choice in _get(chunk, "choices") or []:
                if _get(choice, "finish_reason"):
                    self.finish_reason = _get(choice, "finish_reason")
                delta = _get(choice, "delta")
                if delta is None:
                    continue
                if _get(delta, "content"):
                    self.content.append(_get(delta, "content"))
                if _get(delta, "reasoning_content"):
                    self.reasoning.append(_get(delta, "reasoning_content"))
                for i, tc in enumerate(_get(delta, "tool_calls") or []):
                    idx = _get(tc, "index")
                    slot = self.tool_calls.setdefault(idx if isinstance(idx, int) else i, {
                        "id": None, "type": "function", "function": {"name": "", "arguments": ""}})
                    if _get(tc, "id"):
                        slot["id"] = _get(tc, "id")
                    fn = _get(tc, "function")
                    if _get(fn, "name"):
                        slot["function"]["name"] = _get(fn, "name")
                    if _get(fn, "arguments"):
                        slot["function"]["arguments"] += _get(fn, "arguments")
        except Exception as exc:  # noqa: BLE001
            logger.debug("LangSmith: could not read a stream chunk: %s", exc)

    def outputs(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": "".join(self.content)}
        if self.reasoning:
            message["reasoning_content"] = "".join(self.reasoning)
        if self.tool_calls:
            message["tool_calls"] = [self.tool_calls[k] for k in sorted(self.tool_calls)]
        out: dict[str, Any] = {"choices": [{"index": 0, "message": message,
                                            "finish_reason": self.finish_reason}]}
        usage = _usage(self.usage)
        if usage:
            out["usage_metadata"] = usage
        return out


def _response_outputs(resp: Any) -> dict[str, Any]:
    try:
        out = resp.model_dump() if hasattr(resp, "model_dump") else dict(resp)
    except Exception:  # noqa: BLE001
        out = {"response": str(resp)[:4000]}
    usage = _usage(_get(resp, "usage"))
    if usage:
        out["usage_metadata"] = usage
    return out
