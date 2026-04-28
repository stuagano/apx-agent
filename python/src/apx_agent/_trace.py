"""Lightweight agent trace system for apx-agent.

Captures the conversation flow through an agent: incoming requests,
LLM calls, tool invocations, sub-agent calls, and responses. Stored
in a ring buffer and viewable via /_apx/traces.

Python parity for typescript/src/trace.ts. Traces are attached to
``request.state.trace`` so any code in the request path can add spans
without explicit passing.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Literal

SpanType = Literal["request", "llm", "tool", "agent_call", "response", "error"]
TraceStatus = Literal["in_progress", "completed", "error"]

MAX_TRACES = 200


@dataclass
class TraceSpan:
    type: SpanType
    name: str
    start_time: float
    duration_ms: float | None = None
    input: Any = None
    output: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "name": self.name,
            "start_time": self.start_time,
            "duration_ms": self.duration_ms,
            "input": self.input,
            "output": self.output,
            "metadata": self.metadata,
        }


@dataclass
class Trace:
    id: str
    agent_name: str
    start_time: float
    spans: list[TraceSpan] = field(default_factory=list)
    end_time: float | None = None
    duration_ms: float | None = None
    status: TraceStatus = "in_progress"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent_name": self.agent_name,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "spans": [s.to_dict() for s in self.spans],
        }


_buffer: list[Trace] = []
_buffer_lock = Lock()
_id_counter = 0
_id_lock = Lock()


def _next_id() -> str:
    global _id_counter
    with _id_lock:
        _id_counter += 1
        return f"tr-{int(time.time() * 1000)}-{_id_counter}"


def create_trace(agent_name: str) -> Trace:
    return Trace(id=_next_id(), agent_name=agent_name, start_time=time.time())


def add_span(
    trace: Trace,
    type: SpanType,
    name: str,
    input: Any = None,
    output: Any = None,
    metadata: dict[str, Any] | None = None,
) -> TraceSpan:
    span = TraceSpan(
        type=type,
        name=name,
        start_time=time.time(),
        input=input,
        output=output,
        metadata=metadata or {},
    )
    trace.spans.append(span)
    return span


def end_span(span: TraceSpan, output: Any = None, metadata: dict[str, Any] | None = None) -> None:
    span.duration_ms = (time.time() - span.start_time) * 1000
    if output is not None:
        span.output = output
    if metadata:
        span.metadata.update(metadata)


def end_trace(trace: Trace, status: TraceStatus = "completed") -> None:
    trace.end_time = time.time()
    trace.duration_ms = (trace.end_time - trace.start_time) * 1000
    trace.status = status
    _store(trace)


def _store(trace: Trace) -> None:
    with _buffer_lock:
        _buffer.append(trace)
        if len(_buffer) > MAX_TRACES:
            _buffer.pop(0)


def get_traces() -> list[Trace]:
    with _buffer_lock:
        return list(reversed(_buffer))


def get_trace(trace_id: str) -> Trace | None:
    with _buffer_lock:
        for t in _buffer:
            if t.id == trace_id:
                return t
    return None


def truncate(value: Any, max_len: int = 200) -> str:
    s = value if isinstance(value, str) else json.dumps(value, default=str)
    if not s:
        return ""
    return s[:max_len] + "..." if len(s) > max_len else s
