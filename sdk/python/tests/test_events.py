"""Payloads are serialized whatever they hold, and what serialized before comes out the same."""

import datetime
import hashlib
import json
import uuid

from wake import events


def test_what_json_holds_comes_out_as_it_always_has():
    payload = {"b": [1, 2.5, None, True], "a": {"text": "café"}, "when": datetime.date(2026, 10, 27)}
    assert events.dumps(payload) == json.dumps(payload, sort_keys=True, default=str)
    assert events.dumps(payload, ensure_ascii=False) == json.dumps(payload, sort_keys=True, default=str,
                                                                    ensure_ascii=False)


def test_keys_json_cant_hold_become_strings():
    key = uuid.uuid4()
    when = datetime.datetime(2026, 10, 27, 9, 30)
    body = events.dumps({key: 1, when: 2, 3: "three", "four": 4, (5, 6): 7})
    assert json.loads(body) == {str(key): 1, str(when): 2, "3": "three", "four": 4, "(5, 6)": 7}


def test_a_value_inside_itself_is_its_repr():
    loop: dict = {"name": "loop"}
    loop["self"] = loop
    body = json.loads(events.dumps({"payload": loop}))
    assert body["payload"]["name"] == "loop"
    assert body["payload"]["self"] == repr(loop)


def test_a_value_whose_str_raises_is_its_repr():
    class Odd:
        def __str__(self):
            raise ValueError("no")

        def __repr__(self):
            return "<Odd>"

    assert json.loads(events.dumps({1: Odd(), "a": 1})) == {"1": "<Odd>", "a": 1}


def test_payload_attributes_never_raise():
    deep: list = []
    for _ in range(100_000):
        deep = [deep]
    attributes = events.payload_attributes({"deep": deep, uuid.uuid4(): 1, "x": 2})
    json.loads(attributes[events.PAYLOAD])
    assert len(attributes[events.PAYLOAD_SHA256]) == 64


def test_a_payload_too_large_to_send_whole_says_so():
    page = "x" * (events.MAX_PAYLOAD_BYTES + 10)
    whole = events.dumps({"output": page})
    attributes = events.payload_attributes({"output": page})
    body = attributes[events.PAYLOAD]
    assert len(body) <= events.MAX_PAYLOAD_BYTES
    assert attributes[events.PAYLOAD_SHA256] == hashlib.sha256(body.encode()).hexdigest(), "the digest is of what's sent"
    payload = json.loads(body)
    assert payload["truncated"] is True
    assert payload["bytes"] == len(whole)
    assert payload["sha256"] == hashlib.sha256(whole.encode()).hexdigest(), "anyone holding the whole can match it"
    assert whole.startswith(payload["prefix"]) and len(payload["prefix"]) > events.MAX_PAYLOAD_BYTES // 3

    # Quotes double when escaped; it still fits.
    attributes = events.payload_attributes({"output": '"' * events.MAX_PAYLOAD_BYTES})
    assert len(attributes[events.PAYLOAD]) <= events.MAX_PAYLOAD_BYTES
    assert json.loads(attributes[events.PAYLOAD])["truncated"] is True
    small = events.payload_attributes({"output": "fine"})[events.PAYLOAD]
    assert "truncated" not in json.loads(small)
