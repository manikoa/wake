"""Checks check.py's completeness rule against docs/events.md on made-up events. Runs with
pytest, or on its own: python3 test_check.py."""

import json
import os
import subprocess
import sys
import tempfile

from check import rooted


def event(event_id, kind, parents=(), origins=()):
    return {"id": event_id, "kind": kind, "parents": list(parents), "origins": list(origins)}


def graph(*events):
    return {e["id"]: e for e in events}


def test_an_action_traced_back_to_the_user_is_complete():
    events = graph(
        event("ask", "model_call", origins=["user"]),
        event("fetch", "tool_call", ["ask"], ["tool:fetch_page"]),
        event("pay", "tool_call", ["fetch"], ["tool:refund"]),
    )
    assert rooted(events, "pay")


def test_an_action_linked_to_nothing_isnt_complete_because_of_its_own_origin():
    events = graph(event("pay", "tool_call", origins=["tool:refund"]))
    assert not rooted(events, "pay")


def test_what_follows_from_an_unlabeled_memory_read_isnt_complete():
    events = graph(
        event("ask", "model_call", origins=["user"]),
        event("planted", "memory_read", origins=["memory:file/CLAUDE.md"]),
        event("plan", "model_call", ["ask", "planted"]),
        event("pay", "tool_call", ["plan"], ["tool:refund"]),
    )
    assert not rooted(events, "pay"), "even though it also descends from the user"


def entry(event_id, kind, *, parents=(), origins=(), capture="sdk", tool="", agent="support", payload=None):
    attributes = {"wake.event.id": event_id, "wake.event.kind": kind, "wake.agent.id": agent, "wake.capture": capture,
                  "wake.parents": list(parents), "wake.origins": list(origins)}
    if tool:
        attributes["gen_ai.tool.name"] = tool
    return {"signed": True, "event": {"trace_id": "t", "attributes": attributes}, "payload": payload}


def test_two_payments_to_the_account_fail_with_a_message_not_a_crash():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check.py")
    entries = [entry("ask", "model_call", origins=["user"])]
    for agent, capture in [("support", "sdk"), ("research", "sdk"), ("billing", "sdk"), ("support", "memory_adapter"),
                           ("billing", "memory_adapter"), ("research", "proxy"), ("billing", "proxy"),
                           ("support", "proxy")]:
        entries.append(entry(f"{agent}-{capture}", "model_call", parents=["ask"], capture=capture, agent=agent))
    for i, tool in enumerate(["fetch_page", "lookup_order", "refund", "refund"]):
        entries.append(entry(f"tool-{i}", "tool_call", parents=["ask"], capture="proxy", tool=tool, agent="billing",
                             payload={"account": "999-1234"}))
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        f.write("\n".join(json.dumps(e) for e in entries))
    try:
        result = subprocess.run([sys.executable, path, f.name], capture_output=True, text=True)
    finally:
        os.unlink(f.name)
    assert result.returncode == 1
    assert "FAIL: 2 recorded payments to account 999-1234, not one" in result.stdout
    assert "Traceback" not in result.stderr


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            test()
    print("check.py's tests passed")
