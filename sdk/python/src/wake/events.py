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

ORIGIN_USER = "user"


def payload_attributes(payload: dict[str, Any]) -> dict[str, str]:
    """Serialize a payload and its digest as span attributes."""
    body = json.dumps(payload, default=str, sort_keys=True)
    return {
        PAYLOAD: body,
        PAYLOAD_SHA256: hashlib.sha256(body.encode()).hexdigest(),
    }
