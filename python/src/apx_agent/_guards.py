"""Local lightweight guards — zero-latency runtime checks.

apx-agent's hook surface (``input_guardrails``, ``before_tool``,
``before_model``) wants pre-built callables for the most common
runtime checks: rate limit a tool by user, reject obvious prompt
injection, gate a tool to an allowlist. These guards are deliberately
*not* a competing policy engine — Watchdog handles compliance posture
(cross-domain policies, violation tracking, ontology). What lives
here is the in-process zero-latency layer: no network hop, no
database, just a function call.

Pair with WatchdogGuard for layered governance::

    agent = Agent(
        ...,
        input_guardrails=[
            prompt_injection_heuristic(),     # fast local check
            WatchdogGuard(watchdog).for_input(),  # slower full posture eval
        ],
        before_tool=compose(
            RateLimit(per_minute=60),
            ToolAllowlist({"classify_intent", "get_recent_orders"}),
            WatchdogGuard(watchdog).for_tool(),
        ),
    )

Each guard raises (for ``before_*`` callbacks) or returns a reject
string (for ``*_guardrails`` callbacks). Exceptions propagate so the
runtime can surface them as 4xx-equivalent rejections.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Iterable, NamedTuple

if TYPE_CHECKING:
    from ._models import GuardrailsConfig

logger = logging.getLogger(__name__)


class _GuardComponents(NamedTuple):
    input_guardrails: list[Any]
    before_tool: Any


# ---------------------------------------------------------------------------
# Rate limit — token bucket per principal
# ---------------------------------------------------------------------------


class RateLimit:
    """Token-bucket rate limit for the ``before_tool`` (or ``before_model``) hook.

    Refills at ``per_minute / 60`` tokens per second, up to ``burst``
    tokens. Per-principal — the key extractor pulls a principal string
    from the tool arguments (defaults to ``None`` for a single global
    bucket). When the bucket runs dry, raises ``PermissionError``.

    Per-principal bucket state is bounded so a request-derived
    ``principal_key`` (e.g. a per-user or per-session id) cannot grow the
    in-memory state without limit. Buckets idle for longer than
    ``idle_ttl`` seconds are evicted, and the total number of live buckets
    is capped at ``max_principals`` (least-recently-used eviction). Eviction
    is safe: a bucket idle past ``idle_ttl`` has refilled to full, so a
    re-created bucket starts in the identical (full) state.

    Thread-safe: a lock guards the per-principal state.

    Example::

        # 60 calls per minute per (agent-wide); burst 10
        before_tool=RateLimit(per_minute=60, burst=10)

        # 30 calls per minute scoped to the ``user_id`` argument
        before_tool=RateLimit(per_minute=30,
                              principal_key=lambda name, args: args.get("user_id"))
    """

    def __init__(
        self,
        *,
        per_minute: int = 60,
        burst: int | None = None,
        principal_key: Callable[[str, dict[str, Any]], Any] | None = None,
        message: str | None = None,
        max_principals: int = 10_000,
        idle_ttl: float = 3600.0,
    ) -> None:
        if per_minute <= 0:
            raise ValueError(f"per_minute must be positive; got {per_minute!r}")
        if max_principals <= 0:
            raise ValueError(f"max_principals must be positive; got {max_principals!r}")
        if idle_ttl <= 0:
            raise ValueError(f"idle_ttl must be positive; got {idle_ttl!r}")
        self.per_minute = per_minute
        self.burst = burst if burst is not None else per_minute
        self.principal_key = principal_key
        self.message = message or f"Rate limit exceeded: {per_minute}/min."
        self.max_principals = max_principals
        self.idle_ttl = idle_ttl
        # principal -> (tokens, last_refill). OrderedDict gives O(1) LRU
        # eviction; most-recently-touched buckets sit at the end.
        self._buckets: "OrderedDict[Any, tuple[float, float]]" = OrderedDict()
        self._lock = threading.Lock()

    def _evict_idle(self, now: float) -> None:
        """Drop buckets untouched for longer than ``idle_ttl``.

        Caller must hold ``self._lock``. Buckets are ordered oldest-first,
        so we can stop scanning at the first non-idle entry.
        """
        cutoff = now - self.idle_ttl
        while self._buckets:
            principal, (_tokens, last_refill) = next(iter(self._buckets.items()))
            if last_refill > cutoff:
                break
            self._buckets.popitem(last=False)

    def _take_token(self, principal: Any) -> bool:
        """Return True if a token was available; False if rate-limited."""
        now = time.monotonic()
        rate = self.per_minute / 60.0
        with self._lock:
            self._evict_idle(now)
            tokens, last = self._buckets.get(principal, (float(self.burst), now))
            tokens = min(self.burst, tokens + (now - last) * rate)
            took = tokens >= 1.0
            if took:
                tokens -= 1.0
            # Re-insert at the end so this principal is treated as most
            # recently used for LRU eviction.
            self._buckets[principal] = (tokens, now)
            self._buckets.move_to_end(principal)
            # Cap total live buckets; evict least-recently-used first.
            while len(self._buckets) > self.max_principals:
                self._buckets.popitem(last=False)
            return took

    def __call__(self, name: str, args: dict[str, Any]) -> None:
        principal = (
            self.principal_key(name, args) if self.principal_key else None
        )
        if not self._take_token(principal):
            raise PermissionError(self.message)


# ---------------------------------------------------------------------------
# Prompt injection heuristic
# ---------------------------------------------------------------------------


# Common injection patterns. Intentionally small and conservative — this is
# a fast-path heuristic, not a full classifier. False negatives are expected;
# downstream (Watchdog, LLM-as-judge) catches what slips through. False
# positives matter more, so patterns require some specificity.
_INJECTION_PATTERNS = [
    re.compile(r"ignore (?:all (?:your )?)?(?:previous|prior|above) (?:instructions|prompts|rules)", re.IGNORECASE),
    re.compile(r"disregard (?:all (?:of )?)?(?:the )?(?:above|previous|prior)", re.IGNORECASE),
    re.compile(r"forget (?:everything|all) (?:before|above)", re.IGNORECASE),
    re.compile(r"you are now (?:a |an )?(?!helpful|kind|polite|professional)", re.IGNORECASE),
    re.compile(r"system\s*prompt\s*:", re.IGNORECASE),
    re.compile(r"<\|(?:im_start|im_end|system|user|assistant)\|>", re.IGNORECASE),
    re.compile(r"\[\[SYSTEM\]\]", re.IGNORECASE),
    re.compile(r"reveal (?:your |the )(?:system|hidden|secret) prompt", re.IGNORECASE),
    re.compile(r"print (?:your |the )(?:instructions|system prompt|initial prompt)", re.IGNORECASE),
]


def prompt_injection_heuristic(
    *,
    patterns: Iterable[re.Pattern[str]] | None = None,
    message: str = "Request blocked: suspected prompt injection.",
) -> Callable[[Any], str | None]:
    """Return an ``input_guardrails``-compatible callable that flags injection patterns.

    Scans every message's ``content`` for known injection-attempt
    patterns. Returns ``message`` to short-circuit the request when a
    pattern matches; returns ``None`` otherwise.

    Pass custom ``patterns`` to extend or replace the default set. The
    defaults are deliberately small and high-specificity — false
    positives hurt UX more than false negatives, and the slower-loop
    layer (Watchdog) is meant to catch the long tail.

    Example::

        agent = Agent(
            ...,
            input_guardrails=[prompt_injection_heuristic()],
        )
    """
    active_patterns = list(patterns) if patterns is not None else _INJECTION_PATTERNS

    def _check(messages: Any) -> str | None:
        texts = _texts_from_messages(messages)
        for text in texts:
            for pat in active_patterns:
                if pat.search(text):
                    logger.info("prompt_injection_heuristic matched pattern %s", pat.pattern)
                    return message
        return None

    return _check


def _texts_from_messages(messages: Any) -> list[str]:
    """Pull text content out of whatever message shape was passed.

    Accepts: a string, a list of dicts with ``content``, a list of
    objects with a ``.content`` attribute. Defensive — returns an empty
    list rather than raising on unfamiliar shapes.
    """
    if isinstance(messages, str):
        return [messages]
    if not isinstance(messages, list):
        return []
    out: list[str] = []
    for m in messages:
        content = None
        if isinstance(m, dict):
            content = m.get("content")
        else:
            content = getattr(m, "content", None)
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            # Multimodal/structured shape, e.g. [{"type": "text", "text": ...}].
            # Without this branch the guard never inspects the standard content
            # shape and returns "allowed" for exactly what it should screen.
            for part in content:
                text = part.get("text") if isinstance(part, dict) else None
                if isinstance(text, str):
                    out.append(text)
    return out


# ---------------------------------------------------------------------------
# Tool allowlist / denylist
# ---------------------------------------------------------------------------


class ToolAllowlist:
    """Reject any tool call whose name isn't in the allowlist.

    Plugs into ``before_tool``. Cheaper than a Watchdog round-trip for
    a fixed, well-known set of allowed tools.
    """

    def __init__(self, names: Iterable[str], *, message: str | None = None) -> None:
        self.allowed = set(names)
        self.message = message

    def __call__(self, name: str, args: dict[str, Any]) -> None:
        if name not in self.allowed:
            raise PermissionError(
                self.message or f"Tool {name!r} not in allowlist."
            )


class ToolDenylist:
    """Reject any tool call whose name IS in the denylist."""

    def __init__(self, names: Iterable[str], *, message: str | None = None) -> None:
        self.denied = set(names)
        self.message = message

    def __call__(self, name: str, args: dict[str, Any]) -> None:
        if name in self.denied:
            raise PermissionError(
                self.message or f"Tool {name!r} is denied."
            )


# ---------------------------------------------------------------------------
# Feature flag gate
# ---------------------------------------------------------------------------


class FeatureFlagGuard:
    """Gate tool calls by external feature flags.

    Plugs into ``before_tool``. Tools listed in ``gates`` are checked
    against ``provider`` before execution; tools not in ``gates`` are
    always allowed (gates are opt-in, not opt-out).

    The provider is any callable ``(flag_name: str) -> bool``. Wire it to
    whatever flag system the host environment provides — env vars,
    LaunchDarkly, Statsig, an internal admin DB — by closing the relevant
    client/context into a function.

    Per-user / per-tenant gating works the same way: capture the user
    identity in the provider's closure (e.g. via ``getRequestContext()`` /
    ``Dependencies.Headers``). This class deliberately keeps the
    identity concern *out* of its own signature so the gate composes
    with any context system.

    On a disabled flag the guard raises ``PermissionError``; the framework
    surfaces this to the LLM as a tool failure with the rejection message,
    which the LLM can read and react to ("this feature isn't enabled for
    you yet, try X instead").

    Example::

        import os

        def env_flag(name: str) -> bool:
            return os.environ.get(f"APX_FLAG_{name.upper()}") == "true"

        agent = Agent(
            tools=[premium_search, basic_search],
            before_tool=FeatureFlagGuard(
                provider=env_flag,
                gates={"premium_search": "premium_search_v2"},
            ),
        )

    Compose with other guards via :func:`compose`::

        before_tool=compose(
            RateLimit(per_minute=60),
            FeatureFlagGuard(provider=env_flag,
                             gates={"premium_search": "premium_search_v2"}),
        )
    """

    def __init__(
        self,
        *,
        provider: Callable[[str], bool],
        gates: dict[str, str],
        message: str | None = None,
    ) -> None:
        if not callable(provider):
            raise TypeError("provider must be a callable (flag_name: str) -> bool")
        self.provider = provider
        self.gates = dict(gates)
        self.message = message

    def __call__(self, name: str, args: dict[str, Any]) -> None:
        flag_name = self.gates.get(name)
        if flag_name is None:
            return  # ungated tool — always allowed
        if not self.provider(flag_name):
            raise PermissionError(
                self.message
                or f"Tool {name!r} is disabled (feature flag {flag_name!r} is off)."
            )


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def compose(*callbacks: Callable[..., Any]) -> Callable[..., Any]:
    """Chain multiple callbacks into one.

    The returned callable invokes each callback in order with the same
    arguments. If any callback raises, the chain stops and the
    exception propagates — useful for short-circuiting on the first
    rejection.

    Works for any hook signature ``(name, args)``, ``(messages)``,
    ``(prompts)``, etc. — the inputs flow through verbatim.

    Example::

        before_tool=compose(
            RateLimit(per_minute=60),
            ToolAllowlist({"classify_intent"}),
            WatchdogGuard(watchdog).for_tool(),
        )
    """
    cb_list = list(callbacks)

    def _composed(*args: Any, **kwargs: Any) -> Any:
        result = None
        for cb in cb_list:
            result = cb(*args, **kwargs)
            # Convention from input_guardrails: a non-None / non-empty return
            # short-circuits with that value. before_tool / before_model
            # signal via exceptions instead, so they always return None and
            # this branch is a no-op for them.
            if result is not None and result != "":
                return result
        return result

    # Expose the composed parts for introspection — the dev UI walks
    # before_tool hooks to find a PolicyGate's ApprovalStore.
    _composed.callbacks = cb_list  # type: ignore[attr-defined]
    return _composed


# ---------------------------------------------------------------------------
# Declarative config builder (E3c)
# ---------------------------------------------------------------------------


def build_config_guards(
    cfg: "GuardrailsConfig",
) -> _GuardComponents:
    """Translate a ``GuardrailsConfig`` into built-in guard callables.

    Returns ``(input_guardrails, before_tool_gate)`` where:

    - ``input_guardrails`` is a list of ``(messages) -> str | None`` callables
      to *append* to ``LlmAgent._input_guardrails``.
    - ``before_tool_gate`` is a single composed callable (or ``None``) to
      merge with any existing ``LlmAgent._before_tool`` hook via ``compose``.

    Composition order for ``before_tool_gate`` (first raise wins):
    1. ``ToolDenylist`` — blocked tools are rejected before consuming a
       rate-limit token.
    2. ``ToolAllowlist`` — tools not on the allow list are rejected next.
    3. ``RateLimit`` — rate limit is checked last (so blocked calls don't
       burn tokens).

    ``input_guardrails`` order: injection heuristic only (code-defined guards
    run first when merged by the caller).

    This function raises ``ValueError`` immediately (at build time) when
    ``rate_limit <= 0``, propagated from ``RateLimit.__init__``.
    """
    input_guards: list[Any] = []
    tool_gates: list[Any] = []

    if cfg.injection_detection:
        input_guards.append(prompt_injection_heuristic())

    # Intentional asymmetry: blocked_tools is ``list[str]`` defaulting to ``[]``,
    # so an empty denylist is a no-op (truthiness check). allowed_tools is
    # ``list[str] | None``, so an empty allowlist (``[]``) is a real,
    # intended block-all state distinct from "absent" (``None``) — hence the
    # ``is not None`` check. Do not "simplify" this to matching truthiness.
    if cfg.blocked_tools:
        tool_gates.append(ToolDenylist(cfg.blocked_tools))
    if cfg.allowed_tools is not None:
        tool_gates.append(ToolAllowlist(cfg.allowed_tools))
    if cfg.rate_limit is not None:
        kw: dict[str, Any] = {"per_minute": cfg.rate_limit}
        if cfg.rate_limit_burst is not None:
            kw["burst"] = cfg.rate_limit_burst
        tool_gates.append(RateLimit(**kw))

    before_tool = compose(*tool_gates) if tool_gates else None
    return _GuardComponents(input_guardrails=input_guards, before_tool=before_tool)
