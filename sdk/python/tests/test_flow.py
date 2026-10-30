"""The flow policy in the agent's own process (wake.flow): the decisions every enforcer makes
alike, what's kept of what entered each agent, and what a refused call is told."""

import json
from pathlib import Path

import pytest

from wake import flow, stop

# Shared with wake-proxy's tests and the TypeScript SDK's, so the three decide alike.
VECTORS = json.loads((Path(__file__).resolve().parents[2] / "testdata" / "flow_policy.json").read_text(encoding="utf-8"))
PAY = VECTORS["policies"]["pay"]


def seen_in(case) -> flow.Seen:
    return flow.Seen((), "every origin") if case["origins"] == "*" else flow.Seen(tuple(case["origins"]))


def test_decides_as_the_shared_vectors_say():
    cases = VECTORS["cases"]
    assert len(cases) >= 20
    for case in cases:
        policy = flow.parse(VECTORS["policies"][case["policy"]])
        denial = policy.deciding(case["caller"], case["lineage"], seen_in(case), tool=case.get("tool"),
                                 receiver=case.get("receiver"))
        assert (denial.halt if denial else None) == case["denied_by"], case["name"]
        assert list(denial.origins if denial else []) == case["kept"], case["name"]


def test_every_policy_the_shared_vectors_call_invalid_is_refused_and_refuses_every_call():
    for case in VECTORS["invalid"]:
        with pytest.raises(ValueError):
            flow.parse(case["policy"])
        policy = flow.served(case["policy"])
        assert policy.unreadable, case["name"]
        for call in ({"tool": "pay"}, {"receiver": "billing"}):
            assert policy.deciding("billing", [], flow.Seen(), **call).halt == flow.UNREADABLE, case["name"]


@pytest.mark.parametrize("bad", [
    [],
    {"version": 7, "rules": []},
    {"window_seconds": True, "rules": []},
    {"window_seconds": 900.0, "rules": []},
    {"window_seconds": None, "rules": []},
    {"rules": None},
    {"rules": [{"forbid_origins": ["*"]}]},
    {"rules": [{"id": 5, "forbid_origins": ["*"]}]},
    {"rules": [{"id": "x\n", "forbid_origins": ["*"]}]},
    {"rules": [{"id": "café", "forbid_origins": ["*"]}]},
    {"rules": [{"id": "x", "forbid_origins": None}]},
    {"rules": [{"id": "x", "forbid_origins": [5]}]},
    {"rules": [{"id": "x", "forbid_origins": ["tool:**"]}]},
    {"rules": [{"id": "x", "tools": ["*pay"], "forbid_origins": ["*"]}]},
    {"rules": [{"id": "x", "receivers": ["bill*"], "forbid_origins": ["*"]}]},
    {"rules": [{"id": "x", "path_prefixes": ["/a2a*"], "forbid_origins": ["*"]}]},
    {"rules": [{"id": "x", "receivers": [""], "forbid_origins": ["*"]}]},
    {"unreadable": False},
], ids=lambda bad: json.dumps(bad))
def test_a_policy_wake_proxy_wouldnt_take_is_refused(bad):
    with pytest.raises(ValueError):
        flow.parse(bad)
    assert flow.served(bad).unreadable


def test_reads_the_policy_as_wake_proxy_serves_it():
    assert flow.served(None) is None, "wake-proxy enforces none: nothing is checked"
    assert flow.served({"unreadable": True}).unreadable == "wake-proxy can't use its policy file"
    policy = flow.served({"version": "ab" * 32, "window_seconds": 600, "rules": [PAY["rules"][0]]})
    assert (policy.version, policy.window_seconds, policy.unreadable) == ("ab" * 32, 600, None)
    (rule,) = policy.rules
    assert rule.callers == ("billing",) and rule.receivers == () and rule.tools == ("pay", "wire_*")
    empty = flow.parse({})
    assert (empty.rules, empty.window_seconds) == ((), flow.DEFAULT_WINDOW)


def test_a_bare_star_matches_every_origin_and_a_prefix_its_rest():
    policy = flow.parse({"rules": [{"id": "r", "tools": ["refund_*"], "forbid_origins": ["*"]}]})
    seen = flow.Seen(("user:anything",))
    assert policy.deciding("a", [], seen, tool="refund_eur").origins == ("user:anything",)
    assert policy.deciding("a", [], seen, tool="refund") is None, "a prefix needs what comes before the *"
    assert policy.deciding("a", [], seen, tool="refunds") is None


class Clock:
    """A clock tests move by hand."""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    """The process's origins, on a clock the test moves."""
    clock = Clock()
    monkeypatch.setattr(flow, "_entered", flow.Origins(clock))
    return clock


def test_an_origin_counts_for_a_window_from_when_it_last_entered():
    clock = Clock()
    origins = flow.Origins(clock)
    start = clock.now
    origins.note("billing", "tool:fetch_page")
    clock.now = start + 10
    origins.note("billing", "agent:research")
    clock.now = start + 20
    assert origins.seen("billing") == flow.Seen(("agent:research", "tool:fetch_page")), "newest first"
    # Entering again, it counts from then.
    clock.now = start + 800
    origins.note("billing", "tool:fetch_page")
    clock.now = start + 905
    assert origins.seen("billing").origins == ("tool:fetch_page", "agent:research")
    clock.now = start + 1_000
    assert origins.seen("billing").origins == ("tool:fetch_page",), "agent:research is past the window"
    clock.now = start + 1_800
    assert origins.seen("billing") == flow.Seen()
    assert origins.seen("support") == flow.Seen(), "nothing entered another agent"


def test_origins_that_entered_at_once_are_ordered_by_name():
    clock = Clock()
    origins = flow.Origins(clock)
    origins.note("a", "tool:b")
    origins.note("a", "tool:a")
    clock.now += 1
    origins.note("a", "tool:c")
    assert origins.seen("a").origins == ("tool:c", "tool:a", "tool:b")


def test_the_window_is_the_policys():
    clock = Clock()
    origins = flow.Origins(clock)
    origins.set_window(60)
    origins.note("a", "tool:t")
    clock.now += 61
    assert origins.seen("a") == flow.Seen()
    origins.set_window(0)
    origins.note("a", "tool:t")
    clock.now += 1
    assert origins.seen("a").origins == ("tool:t",), "a window is a second at least"


def test_past_its_bound_an_agent_has_every_origin_for_a_window():
    clock = Clock()
    origins = flow.Origins(clock)
    for i in range(flow.MAX_ORIGINS):
        origins.note("a", f"tool:t{i}")
    assert len(origins.seen("a").origins) == flow.MAX_ORIGINS, "the bound itself is kept"
    origins.note("a", "tool:one-too-many")
    clock.now += 1
    assert origins.seen("a") == flow.Seen((), flow.SATURATED)
    # Once the window has passed with nothing new, it's known again.
    clock.now += flow.DEFAULT_WINDOW
    assert origins.seen("a") == flow.Seen()
    # Origins older than the window don't count toward the bound.
    for i in range(flow.MAX_ORIGINS):
        origins.note("a", f"tool:later{i}")
    assert len(origins.seen("a").origins) == flow.MAX_ORIGINS


def test_past_its_bound_of_agents_one_it_cant_keep_has_every_origin():
    clock = Clock()
    origins = flow.Origins(clock)
    start = clock.now
    for i in range(flow.MAX_AGENTS):
        origins.note(f"agent-{i}", "tool:t")
    clock.now = start + 1
    origins.note("one-too-many", "tool:t")
    clock.now = start + 2
    assert origins.seen("one-too-many") == flow.Seen((), flow.OVERFLOWED)
    assert origins.seen("never-heard-of") == flow.Seen((), flow.OVERFLOWED), "it could be one Wake couldn't keep"
    assert origins.seen("agent-0") == flow.Seen(("tool:t",)), "the ones kept are still known"
    # Agents nothing entered for a window make room.
    clock.now = start + flow.DEFAULT_WINDOW + 10
    origins.note("newcomer", "tool:t")
    assert origins.seen("newcomer") == flow.Seen(("tool:t",))
    assert origins.seen("never-heard-of") == flow.Seen()


def test_a_message_carries_what_entered_its_sender_as_of_when_it_did():
    clock = Clock()
    origins = flow.Origins(clock)
    start = clock.now
    origins.note("research", "tool:fetch_page")
    clock.now = start + 5
    origins.note("research", "agent:billing")
    # Research answers billing: the page comes with it, as of when research fetched it, and
    # billing's own message doesn't come back to it.
    clock.now = start + 100
    origins.carry("research", "billing")
    origins.note("billing", "agent:research")
    assert origins.seen("billing").origins == ("agent:research", "tool:fetch_page")
    # Passing it on never makes it newer: it leaves billing when it leaves research.
    clock.now = start + 200
    origins.carry("billing", "support")
    clock.now = start + 901
    for agent in ("research", "billing", "support"):
        assert "tool:fetch_page" not in origins.seen(agent).origins, agent
    # Nothing to carry keeps no agent.
    origins.carry("idle", "listener")
    assert "listener" not in origins._agents


def test_a_message_from_an_agent_with_every_origin_gives_every_origin_for_as_long():
    clock = Clock()
    origins = flow.Origins(clock)
    start = clock.now
    for i in range(flow.MAX_ORIGINS + 1):
        origins.note("research", f"tool:t{i}")
    clock.now = start + 500
    origins.carry("research", "billing")
    clock.now = start + 600
    origins.carry("billing", "support")
    for agent in ("billing", "support"):
        assert origins.seen(agent) == flow.Seen((), flow.CARRIED), agent
    clock.now = start + flow.DEFAULT_WINDOW + 1
    for agent in ("research", "billing", "support"):
        assert origins.seen(agent) == flow.Seen(), f"{agent}: not kept alive past research's window"


def test_message_notes_the_sender_and_carries_what_entered_it(clock):
    flow.note("research", "tool:fetch_page")
    clock.now += 1
    flow.message("research", "billing")
    assert flow.entered().seen("billing").origins == ("agent:research", "tool:fetch_page")
    flow.message("billing", "billing")
    assert "agent:billing" not in flow.entered().seen("billing").origins, "an agent's own message isn't an origin"


class Rules:
    """The brake's rules, carrying this flow policy, or none."""

    def __init__(self, policy) -> None:
        self.policy = policy

    def rules(self):
        return {"halts": []} if self.policy is None else {"halts": [], "flow": self.policy}


def test_a_refusal_names_the_rule_who_it_holds_back_and_after_what(clock):
    rules = Rules(PAY)
    assert flow.refusal(rules, "billing", "billing", tool="pay") is None, "nothing entered billing"
    flow.note("billing", "tool:search")
    clock.now += 1
    flow.note("billing", "agent:research")
    clock.now += 1
    flow.note("billing", "tool:fetch_page")
    refused = flow.refusal(rules, "billing", "billing", tool="pay")
    assert refused == ("policy:no-pay-after-web", "Wake's flow policy (policy:no-pay-after-web) holds billing back "
                       "after tool:fetch_page, agent:research, so this tool call wasn't run.")
    assert isinstance(refused, stop.Refusal) and refused.halt == "policy:no-pay-after-web"
    assert flow.payload(refused) == {"origins": ["tool:fetch_page", "agent:research"]}
    assert flow.refusal(rules, "billing", "billing", tool="lookup_order") is None, "a tool the rule doesn't name"

    # Reaching another agent, told as the integration tells it.
    flow.note("research", "tool:fetch_page")
    refused = flow.refusal(rules, "research#run-1", "research", receiver="billing",
                           outcome="the handoff to billing wasn't made")
    assert refused.reason == ("Wake's flow policy (policy:billing-hears-support-only) holds research#run-1 back after "
                              "tool:fetch_page, so the handoff to billing wasn't made.")


def test_a_rule_for_an_agent_holds_its_subagents_and_instances(clock):
    rules = Rules(PAY)
    flow.note("billing/refunds", "tool:fetch_page")
    assert flow.refusal(rules, "billing/refunds", "billing/refunds", tool="pay") is None
    refused = flow.refusal(rules, "billing/refunds#toolu_1", "billing/refunds", ["billing#run-7"], tool="pay")
    assert refused.halt == "policy:no-pay-after-web", "an instance of billing started it"


def test_every_origin_is_said_and_why(clock):
    rules = Rules(PAY)
    for i in range(flow.MAX_ORIGINS + 1):
        flow.note("billing", f"tool:t{i}")
    refused = flow.refusal(rules, "billing", "billing", tool="wire_eur")
    assert refused == ("policy:no-pay-after-web", "Wake's flow policy (policy:no-pay-after-web) holds billing back, "
                       "because more origins entered it than Wake keeps, so this tool call wasn't run.")
    assert flow.payload(refused) == {"origins": ["*"], "every_origin": flow.SATURATED}
    assert flow.refusal(rules, "billing", "billing", tool="lookup_order") is None, "no rule applies"


def test_the_policys_window_is_the_one_kept(clock):
    rules = Rules(PAY)  # a window of 600 seconds
    flow.note("billing", "tool:fetch_page")
    clock.now += 599
    assert flow.refusal(rules, "billing", "billing", tool="pay") is not None
    clock.now += 2
    assert flow.refusal(rules, "billing", "billing", tool="pay") is None


class Broken:
    def rules(self):
        raise RuntimeError("the rules can't be read")


@pytest.mark.parametrize("rules", [
    Rules({"unreadable": True}),
    Rules({"rules": [{"id": "x", "tools": ["pay"]}]}),
    Broken(),
], ids=["unreadable", "invalid", "check_fails"])
def test_a_policy_that_cant_be_used_refuses_every_call(rules):
    for call, outcome in (({"tool": "lookup_order"}, "this tool call wasn't run"),
                          ({"receiver": "notify"}, "this run of notify wasn't started")):
        refused = flow.refusal(rules, "billing", "billing", outcome=outcome, **call)
        assert refused == (flow.UNREADABLE, f"Wake's flow policy can't be read, so {outcome}.")
        assert flow.payload(refused) == {"origins": []}


def test_no_rules_or_no_policy_checks_nothing():
    flow.note("billing", "tool:fetch_page")
    assert flow.refusal(None, "billing", "billing", tool="pay") is None
    assert flow.refusal(Rules(None), "billing", "billing", tool="pay") is None
    assert flow.refusal(object(), "billing", "billing", tool="pay") is None, "rules that can't say carry none"
    assert flow.payload(stop.Refusal("halt-billing", "Wake halted billing (halt-billing), so this tool call wasn't run.")) == {}
