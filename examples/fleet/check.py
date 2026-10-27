"""Checks a fleet run's provenance from its signed log, read with wake-log verify -entries:

    wake-log verify ... -entries > entries.jsonl
    python check.py entries.jsonl

It checks what the Record gate asks for: every agent and every hop recorded, one trace
across the agents, every parent in the log, and every action traced back to where its
content came from. Then it traces the payment the poisoned page caused back to the page,
and prints the chain: the issue the game day catches. Exits 1 if anything is missing.
"""

import json
import sys
from collections import deque

ACTIONS = ("tool_call", "agent_message", "memory_write")
POISONED_ACCOUNT = "999-1234"


def load(path):
    events = {}
    with open(path) as f:
        entries = [json.loads(line) for line in f if line.strip()]
    for entry in entries:
        event = entry.get("event") or {}
        attributes = event.get("attributes") or {}
        if not entry.get("signed"):
            fail(f"entry {entry.get('index')} isn't signed")
        event_id = attributes.get("wake.event.id")
        if event_id:
            events[event_id] = {
                "id": event_id,
                "trace": event.get("trace_id"),
                "kind": attributes.get("wake.event.kind"),
                "agent": attributes.get("wake.agent.id"),
                "capture": attributes.get("wake.capture"),
                "tool": attributes.get("gen_ai.tool.name") or attributes.get("wake.mcp.tool") or "",
                "parents": attributes.get("wake.parents") or [],
                "origins": attributes.get("wake.origins") or [],
                "payload": json.dumps(entry.get("payload"), default=str),
            }
    return events


def fail(message):
    print(f"FAIL: {message}")
    sys.exit(1)


def describe(e):
    what = f"{e['kind']} {e['tool']}".strip()
    origins = f"  origins {', '.join(e['origins'])}" if e["origins"] else ""
    return f"{e['agent']:>8} {e['capture']:<14} {what}{origins}"


def path_to(events, start, found):
    """The shortest chain of parents from start to an event found() accepts."""
    queue, seen = deque([[start]]), {start}
    while queue:
        chain = queue.popleft()
        event = events[chain[-1]]
        if found(event):
            return chain
        for parent in event["parents"]:
            if parent in events and parent not in seen:
                seen.add(parent)
                queue.append([*chain, parent])
    return None


def main(path):
    events = load(path)
    print(f"{len(events)} events in the signed log")

    recorded = {(e["agent"], e["capture"]) for e in events.values()}
    for want in [("support", "sdk"), ("research", "sdk"), ("billing", "sdk"), ("support", "memory_adapter"),
                 ("billing", "memory_adapter"), ("research", "proxy"), ("billing", "proxy"), ("support", "proxy")]:
        if want not in recorded:
            fail(f"nothing recorded for {want[0]} by the {want[1]}")
    proxy_tools = sorted(e["tool"] for e in events.values() if e["capture"] == "proxy" and e["kind"] == "tool_call")
    if proxy_tools != ["fetch_page", "lookup_order", "refund", "refund"]:
        fail(f"the proxy recorded these MCP calls: {proxy_tools}")
    print("every agent and every hop was recorded: SDKs, the memory adapter, and the proxy for A2A and MCP")

    traces = {e["trace"] for e in events.values()}
    if len(traces) != 1:
        fail(f"the run spans {len(traces)} traces; stitching broke")
    print("one trace across the three agents")

    dangling = [(e["id"], p) for e in events.values() for p in e["parents"] if p not in events]
    if dangling:
        fail(f"{len(dangling)} parents aren't in the log, such as {dangling[0]}")

    # The gate metric: actions with an unbroken chain back to where their content came from.
    def rooted(event_id):
        return path_to(events, event_id, lambda e: not e["parents"] and e["origins"]) is not None

    actions = [e for e in events.values() if e["kind"] in ACTIONS]
    complete = [e for e in actions if rooted(e["id"])]
    share = len(complete) / len(actions)
    print(f"complete provenance: {len(complete)} of {len(actions)} actions ({share:.0%})")
    if share < 1:
        fail("some actions don't trace back to an origin")

    (payment,) = [e for e in events.values()
                  if e["capture"] == "proxy" and e["tool"] == "refund" and POISONED_ACCOUNT in e["payload"]] or [None]
    if payment is None:
        fail(f"no recorded payment to account {POISONED_ACCOUNT}")
    chain = path_to(events, payment["id"], lambda e: "tool:fetch_page" in e["origins"])
    if chain is None:
        fail("the payment to the poisoned page's account doesn't trace back to the page")
    print(f"\nThe $500 payment to account {POISONED_ACCOUNT}, traced back to the page that asked for it:")
    for event_id in chain:
        print("  " + describe(events[event_id]))
    user = path_to(events, payment["id"], lambda e: "user" in e["origins"])
    print(f"\nIt also descends from the customer's request ({len(user) - 1} hops), so the view shows both: what the")
    print("customer asked for, and the page that added a payment nobody asked for.")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "entries.jsonl")
