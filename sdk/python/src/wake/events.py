"""Attribute names and helpers for Wake events. See docs/events.md."""

import hashlib
import json
from typing import Any

EVENT_ID = "wake.event.id"
EVENT_KIND = "wake.event.kind"
AGENT_ID = "wake.agent.id"
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


def payload_attributes(payload: Any) -> dict[str, str]:
    """Serialize a payload and its digest as span attributes. Never raises."""
    body = dumps(payload)
    return {
        PAYLOAD: body,
        PAYLOAD_SHA256: hashlib.sha256(body.encode()).hexdigest(),
    }
