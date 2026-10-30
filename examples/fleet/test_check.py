"""Checks check.py's completeness rule against docs/events.md on made-up events. Runs with
pytest, or on its own: python3 test_check.py."""

import json
import os
import subprocess
import sys
import tempfile

from check import AGENTS, POLICY_RULE, rooted, stray_user_origins

CHECK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check.py")
RECORDERS = [*((a, "sdk") for a in AGENTS), *((a, "proxy") for a in AGENTS), ("support", "memory_adapter"),
             ("billing", "memory_adapter"), ("notify", "memory_adapter")]


def event(event_id, kind, parents=(), origins=(), agent="support", capture="sdk", start=0):
    return {"id": event_id, "kind": kind, "parents": list(parents), "origins": list(origins), "agent": agent,
            "capture": capture, "start": start}


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


def test_an_action_whose_parent_never_reached_the_log_isnt_complete():
    events = graph(
        event("ask", "model_call", origins=["user"]),
        event("pay", "tool_call", ["ask", "lost"], ["tool:refund"]),
    )
    assert not rooted(events, "pay"), "even though its other parent descends from the user"


def test_a_receiver_that_took_its_message_for_a_users_request_is_caught():
    events = graph(
        event("ask", "model_call", origins=["user"], start=1),
        event("ask-research", "tool_call", ["ask"], ["tool:ask_research"], start=2),
        # research lost the link, so its first step looks like a user's request: complete,
        # but not stitched to support.
        event("research", "model_call", origins=["user"], agent="research", start=3),
        event("answer", "model_call", ["ask", "ask-research"], start=4),
    )
    assert all(rooted(events, e) for e in events)
    assert [e["id"] for e in stray_user_origins(events)] == ["research"]
    del events["research"]
    assert stray_user_origins(events) == []


def entry(event_id, kind, *, parents=(), origins=(), capture="sdk", tool="", agent="support", payload=None, start=2,
          trace="t", halt=""):
    attributes = {"wake.event.id": event_id, "wake.event.kind": kind, "wake.agent.id": agent, "wake.capture": capture,
                  "wake.parents": list(parents), "wake.origins": list(origins)}
    if tool:
        attributes["gen_ai.tool.name"] = tool
    if halt:
        attributes["wake.brake.halt_id"] = halt
    return {"signed": True, "event": {"trace_id": trace, "start_time_unix_nano": str(start), "attributes": attributes},
            "payload": payload}


def check(entries, *args):
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        f.write("\n".join(json.dumps(e) for e in entries))
    try:
        return subprocess.run([sys.executable, CHECK, *args, f.name], capture_output=True, text=True)
    finally:
        os.unlink(f.name)


def request(trace, start, refused=False):
    """One request's events, made up so check.py passes them: the page research read,
    billing's two payments, and with refused, wake-proxy refusing both under policy.json."""
    ask = f"{trace}-ask"
    entries = [entry(ask, "model_call", origins=["user"], start=start, trace=trace)]
    for agent, capture in RECORDERS:
        entries.append(entry(f"{trace}-{agent}-{capture}", "model_call", parents=[ask], capture=capture, agent=agent,
                             start=start + 1, trace=trace))
    page = f"{trace}-page"
    entries.append(entry(page, "tool_call", parents=[ask], origins=["tool:fetch_page"], capture="proxy", tool="fetch_page",
                         agent="research", start=start + 1, trace=trace))
    for tool in ["lookup_order", "send_email"]:
        entries.append(entry(f"{trace}-{tool}", "tool_call", parents=[ask], capture="proxy", tool=tool, start=start + 1,
                             trace=trace))
    for i, to in enumerate([{"order": "1234"}, {"account": "999-1234"}]):
        attempt = f"{trace}-pay-{i}"
        entries.append(entry(attempt, "tool_call", parents=[page], tool="mcp__billing__issue_refund", agent="billing",
                             payload={"input": to}, start=start + 2, trace=trace))
        if refused:
            entries.append(entry(f"{trace}-refused-{i}", "denied", parents=[attempt], capture="proxy", agent="billing",
                                 halt=POLICY_RULE, payload={"origins": ["tool:fetch_page"]}, start=start + 3, trace=trace))
        else:
            entries.append(entry(f"{trace}-refund-{i}", "tool_call", parents=[attempt], capture="proxy", tool="refund",
                                 agent="billing", payload=to, start=start + 3, trace=trace))
    return entries


def test_two_payments_to_the_account_fail_with_a_message_not_a_crash():
    entries = [entry("ask", "model_call", origins=["user"], start=1)]
    for agent, capture in RECORDERS:
        entries.append(entry(f"{agent}-{capture}", "model_call", parents=["ask"], capture=capture, agent=agent))
    for i, tool in enumerate(["fetch_page", "lookup_order", "refund", "refund", "send_email"]):
        entries.append(entry(f"tool-{i}", "tool_call", parents=["ask"], capture="proxy", tool=tool, agent="billing",
                             payload={"account": "999-1234"}))
    result = check(entries)
    assert result.returncode == 1
    assert "FAIL: 2 recorded payments to account 999-1234, not one" in result.stdout
    assert "Traceback" not in result.stderr


def test_a_request_then_the_same_under_the_policy_passes():
    result = check([*request("first", 1), *request("second", 100, refused=True)], "--policy")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "The $500 payment to account 999-1234, traced back" in result.stdout
    assert "The $500 payment to account 999-1234, refused, traced back" in result.stdout


def test_a_payment_made_under_the_policy_fails():
    result = check([*request("first", 1), *request("second", 100)], "--policy")
    assert result.returncode == 1
    assert "FAIL: the proxy recorded these MCP calls" in result.stdout
    assert "Traceback" not in result.stderr


def test_the_policy_check_needs_both_requests():
    result = check(request("first", 1), "--policy")
    assert result.returncode == 1
    assert "FAIL: the log spans 1 traces, for two requests; stitching broke" in result.stdout
    assert "Traceback" not in result.stderr


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            test()
    print("check.py's tests passed")
