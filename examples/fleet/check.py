"""Checks a fleet run's provenance from its signed log, read with wake-log verify -entries:

    wake-log verify ... -entries > entries.jsonl
    python check.py entries.jsonl

It checks what the Record gate asks for: every agent and every hop recorded, one trace
across the agents, every parent in the log, the customer's request entering only where it
came in, and every action traced back to where its content came from. Then it traces the payment the poisoned page caused back to the page,
and prints the chain: the issue the game day catches. Exits 1 if anything is missing.
"""

import json
import sys
from collections import deque

ACTIONS = ("tool_call", "agent_message", "memory_write")
AGENTS = ("support", "research", "billing", "notify")
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
                "start": int(event.get("start_time_unix_nano") or 0),
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


def rooted(events, event_id):
    """Whether every chain of parents behind an event ends where content entered from a
    user. Every other origin enters at an event with parents (docs/events.md): a tool
    result at the call the model asked for, a message at the step that sent it, a memory
    entry at the write its label names. So an event with no parents and no user origin is
    where a chain broke, such as an action linked to nothing or a memory entry no label
    vouches for, and nothing that follows from it is complete. That includes the action
    itself: its own origin doesn't make it a root. So is a parent that never reached the
    log: nothing shows where its content came from."""
    seen, queue = {event_id}, [event_id]
    while queue:
        event = events[queue.pop()]
        if not event["parents"] and "user" not in event["origins"]:
            return False
        for parent in event["parents"]:
            if parent not in events:
                return False
            if parent not in seen:
                seen.add(parent)
                queue.append(parent)
    return True


def stray_user_origins(events):
    """Events labeled with a user's request other than support's first step, where the
    customer's request comes in. A receiver that loses the link to the message it got
    takes the message for a plain request and labels it user, which looks complete, so in
    this run any other user origin is a hop whose link broke."""
    support = [e for e in events.values() if e["agent"] == "support" and e["capture"] == "sdk"]
    first = min(support, key=lambda e: e["start"])["id"] if support else None
    return [e for e in events.values() if "user" in e["origins"] and e["id"] != first]


def main(path):
    events = load(path)
    print(f"{len(events)} events in the signed log")

    recorded = {(e["agent"], e["capture"]) for e in events.values()}
    for want in [*((agent, "sdk") for agent in AGENTS), *((agent, "proxy") for agent in AGENTS),
                 ("support", "memory_adapter"), ("billing", "memory_adapter"), ("notify", "memory_adapter")]:
        if want not in recorded:
            fail(f"nothing recorded for {want[0]} by the {want[1]}")
    proxy_tools = sorted(e["tool"] for e in events.values() if e["capture"] == "proxy" and e["kind"] == "tool_call")
    if proxy_tools != ["fetch_page", "lookup_order", "refund", "refund", "send_email"]:
        fail(f"the proxy recorded these MCP calls: {proxy_tools}")
    print("every agent and every hop was recorded: SDKs, the memory adapter, and the proxy for A2A and MCP")

    traces = {e["trace"] for e in events.values()}
    if len(traces) != 1:
        fail(f"the run spans {len(traces)} traces; stitching broke")
    print(f"one trace across the {len(AGENTS)} agents")

    dangling = [(e["id"], p) for e in events.values() for p in e["parents"] if p not in events]
    if dangling:
        fail(f"{len(dangling)} parents aren't in the log, such as {dangling[0]}")

    stray = stray_user_origins(events)
    if stray:
        first = stray[0]
        fail(f"{len(stray)} {'event' if len(stray) == 1 else 'events'} besides support's first step took in a user's"
             f" request, such as {first['agent']}'s {first['kind']} {first['id']}: a hop lost its link")
    print("the customer's request entered once, at support's first step")

    # The gate metric: actions with an unbroken chain back to where their content came from.
    actions = [e for e in events.values() if e["kind"] in ACTIONS]
    complete = [e for e in actions if rooted(events, e["id"])]
    share = len(complete) / len(actions)
    print(f"complete provenance: {len(complete)} of {len(actions)} actions ({share:.0%})")
    if share < 1:
        fail("some actions don't trace back to an origin")

    payments = [e for e in events.values()
                if e["capture"] == "proxy" and e["tool"] == "refund" and POISONED_ACCOUNT in e["payload"]]
    if len(payments) != 1:
        fail(f"{len(payments)} recorded payments to account {POISONED_ACCOUNT}, not one")
    payment = payments[0]
    chain = path_to(events, payment["id"], lambda e: "tool:fetch_page" in e["origins"])
    if chain is None:
        fail("the payment to the poisoned page's account doesn't trace back to the page")
    print(f"\nThe $500 payment to account {POISONED_ACCOUNT}, traced back to the page that asked for it:")
    for event_id in chain:
        print("  " + describe(events[event_id]))
    user = path_to(events, payment["id"], lambda e: "user" in e["origins"])
    if user is None:
        fail("the payment doesn't trace back to the customer's request")
    print(f"\nIt also descends from the customer's request ({len(user) - 1} hops), so the view shows both: what the")
    print("customer asked for, and the page that added a payment nobody asked for.")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "entries.jsonl")
