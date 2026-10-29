"""Attribute names and helpers for Wake events. See docs/events.md."""

import hashlib
import json
from typing import Any

EVENT_ID = "wake.event.id"
EVENT_KIND = "wake.event.kind"
AGENT_ID = "wake.agent.id"
AGENT_INSTANCE = "wake.agent.instance"
AGENT_LINEAGE = "wake.agent.lineage"
CAPTURE = "wake.capture"
PARENTS = "wake.parents"
ORIGINS = "wake.origins"
PAYLOAD = "wake.payload"
PAYLOAD_SHA256 = "wake.payload.sha256"

MODEL_CALL = "model_call"
TOOL_CALL = "tool_call"
AGENT_MESSAGE = "agent_message"
MEMORY_READ = "memory_read"
MEMORY_WRITE = "memory_write"
DENIED = "denied"

ORIGIN_USER = "user"

# The largest payload sent whole. A larger one, such as a model's context late in a long
# run, is cut to a prefix and marked truncated (docs/events.md), so one event always fits a
# batch the collector takes. wake-proxy keeps 1 MiB of each body it records.
MAX_PAYLOAD_BYTES = 4 << 20


def dumps(value: Any, *, ensure_ascii: bool = True) -> str:
    """JSON for any value, with keys sorted. Never raises.

    What json can hold comes out as it always has, so digests of it don't change. Past
    that, keys become their str(), values json can't hold their str() or repr(), and a
    value inside itself its repr().
    """
    try:
        return json.dumps(value, sort_keys=True, default=str, ensure_ascii=ensure_ascii)
    except Exception:
        pass
    try:
        return json.dumps(_plain(value, set()), sort_keys=True, ensure_ascii=ensure_ascii)
    except Exception:
        return json.dumps(_repr(value), ensure_ascii=ensure_ascii)


def _plain(value: Any, inside: set[int]) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (dict, list, tuple)):
        if id(value) in inside:
            return _repr(value)
        inside.add(id(value))
        try:
            if isinstance(value, dict):
                return {_str(k): _plain(v, inside) for k, v in value.items()}
            return [_plain(v, inside) for v in value]
        finally:
            inside.discard(id(value))
    return _str(value)


def _str(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:
        return _repr(value)


def _repr(value: Any) -> str:
    try:
        return repr(value)
    except Exception:
        return f"<{type(value).__name__}>"


def instance_attributes(instance: str | None, lineage: "list[str] | tuple[str, ...]") -> dict[str, Any]:
    """The attributes naming one running instance of an agent, and what started it, for an
    agent that can run as several at once, such as a subagent. Empty for any other."""
    if not instance:
        return {}
    return {AGENT_INSTANCE: instance, AGENT_LINEAGE: list(lineage)}


def payload_attributes(payload: Any) -> dict[str, str]:
    """Serialize a payload and its digest as span attributes. Never raises."""
    body = dumps(payload)
    if len(body) > MAX_PAYLOAD_BYTES:  # ASCII, so its length is its size in bytes
        body = _truncated(body)
    return {
        PAYLOAD: body,
        PAYLOAD_SHA256: hashlib.sha256(body.encode()).hexdigest(),
    }


def _truncated(body: str) -> str:
    """A payload too large to send whole: its size and digest, and as much of its start as
    fits, as a string. Escaping at most doubles the prefix, so half the limit always fits."""
    return json.dumps(
        {
            "truncated": True,
            "bytes": len(body),
            "sha256": hashlib.sha256(body.encode()).hexdigest(),
            "prefix": body[: MAX_PAYLOAD_BYTES // 2 - 256],
        },
        sort_keys=True,
    )
