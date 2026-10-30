"""The flow policy in the agent's own process: the rules wake-proxy enforces, applied to the
calls it never sees, with the origins Wake saw enter each agent. See docs/flow-everywhere.md.

wake-proxy serves the policy it enforces with the brake's rules (wake.quarantine), and each
integration checks it where it checks halts (wake.stop), once they let the call through:
before a tool call, a handoff, and the start of an agent's run. The memory adapter
(wake.memory) checks the rules that name stores before each write, delete and rollback,
which never pass wake-proxy. A rule decides on what entered the calling agent within the
policy's window, as wake-proxy's rules do:

- a tool's result enters the agent that called it as tool:<name>, an MCP tool by its MCP
  name, as wake-proxy records it;
- a message, a handoff, a subagent's start or its answer enters the agent it reaches as
  agent:<sender>, with whatever had entered the sender;
- a memory read enters the origins its label carries, or memory:unlabeled when no label
  vouches for what was read (wake.memory);
- the user's own prompt isn't an origin.

What entered each agent is kept for the whole process, since one agent's origins can come
from several integrations and from its memory reads. It's coarse, as wake-proxy is:
everything that entered an agent within the window counts for each call it makes.

It fails closed, as halts do. A policy wake-proxy can't use, or one that isn't valid,
refuses every call under policy:unreadable-policy, and so does a check that fails. An agent
past what Wake keeps of it counts as having every origin, so any rule that applies to it
refuses. Stale rules already refuse every call, under the halts.

Like halts, this runs in the agent's own process, so it holds only while the agent's code
does: a compromised agent can leave it out. wake-proxy goes on enforcing the policy on
every call that passes it, and the network, closed to all but wake-proxy, keeps agents from
going around it.
"""

import logging
import os
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any, NamedTuple

from wake.stop import Refusal

log = logging.getLogger("wake")

# The halt id every call is refused under while the policy can't be used.
UNREADABLE = "policy:unreadable-policy"
# The window when a policy doesn't set one, and the longest one may set, in seconds.
DEFAULT_WINDOW = 900
MAX_WINDOW = 86_400
# The most distinct origins kept for one agent within the window. Past it, the agent counts
# as having every origin until the window passes.
MAX_ORIGINS = 256
# The most agents kept at once. Past it, an agent not kept counts as having every origin.
MAX_AGENTS = 10_000
# What a memory read enters when no label vouches for what was read.
UNLABELED = "memory:unlabeled"

# Why an agent counts as having every origin.
SATURATED = "more origins entered it than Wake keeps"
OVERFLOWED = "too many agents to remember this one"
CARRIED = "a message or reply reached it from one with every origin"
UNKNOWN = "Wake can't tell which agent is acting"

_LISTS = ("callers", "receivers", "path_prefixes", "tools", "stores", "forbid_origins", "allow_origins")
# The lists whose entries may be a prefix ending in *; the rest are exact.
_PATTERNS = frozenset({"tools", "stores", "forbid_origins", "allow_origins"})
_ID = re.compile(r"[A-Za-z0-9._:-]{1,64}")


class Rule(NamedTuple):
    """One rule: the calls it applies to, and the origins it keeps from them. An empty list
    leaves that out, so a rule naming no callers applies to every caller. A rule naming
    stores applies only to changes to those memory stores, and a rule naming none never
    does."""

    id: str
    callers: tuple[str, ...] = ()
    receivers: tuple[str, ...] = ()
    path_prefixes: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    stores: tuple[str, ...] = ()
    forbid_origins: tuple[str, ...] = ()
    allow_origins: tuple[str, ...] = ()

    def keeps(self, origins: Iterable[str]) -> tuple[str, ...]:
        """The origins, of these, the rule keeps from a call, in the order given: those a
        forbid_origins pattern matches, and, with allow_origins, those none of its patterns
        matches. So an allowlist passes a caller nothing entered."""
        return tuple(
            o for o in origins
            if any(_matches(p, o) for p in self.forbid_origins)
            or (self.allow_origins and not any(_matches(p, o) for p in self.allow_origins))
        )


class Seen(NamedTuple):
    """What entered an agent within the window: its origins, newest first, or, when every is
    set, every origin as far as a policy knows, and why."""

    origins: tuple[str, ...] = ()
    every: str | None = None


class Denial(NamedTuple):
    """A rule refusing a call: the halt it's refused under, policy:<id>, and the origins behind
    the call it keeps from it, or * for every origin, with why."""

    halt: str
    origins: tuple[str, ...]
    every: str | None = None


class Policy:
    """A flow policy as wake-proxy serves it: its rules, in file order, and its window. One that
    can't be used, unreadable saying why, refuses every call."""

    def __init__(self, rules: Iterable[Rule] = (), window_seconds: int = DEFAULT_WINDOW, version: str | None = None,
                 unreadable: str | None = None) -> None:
        self.rules = tuple(rules)
        self.window_seconds = window_seconds
        self.version = version
        self.unreadable = unreadable

    def deciding(self, caller: str | None, lineage: Iterable[str], seen: Seen, *, tool: str | None = None,
                 receiver: str | None = None, store: str | None = None) -> Denial | None:
        """The first rule that refuses a call by caller, given what entered it, or None to let
        it through. lineage names the agents caller acts under, such as those that started it.
        A caller of None is one Wake can't name, which any rule's callers could be, as for
        wake-proxy. The call is a tool call (tool), reaches another agent (receiver), as a
        message, a handoff or the start of its run does, or changes a memory store (store): a
        write, a delete or a rollback.

        A tool call meets the rules wake-proxy applies to an MCP call: first those naming no
        tools that name no receivers, path_prefixes or stores, which its ext_authz applies to
        every call, then those whose tools match the tool. Reaching an agent meets those
        naming no tools, path_prefixes or stores whose receivers are empty or name it. A
        change to a store meets only those whose stores match it. A rule for paths only ever
        meets HTTP requests at wake-proxy. Each is in file order."""
        if self.unreadable is not None:
            return Denial(UNREADABLE, ())
        if sum(call is not None for call in (tool, receiver, store)) != 1:
            raise ValueError("a call is a tool call, reaches an agent or changes a store")
        if tool is not None:
            rules = [r for r in self.rules if not (r.tools or r.receivers or r.path_prefixes or r.stores)]
            rules += [r for r in self.rules if any(_matches(p, tool) for p in r.tools)]
        elif receiver is not None:
            rules = [r for r in self.rules if not (r.tools or r.path_prefixes or r.stores)
                     and (not r.receivers or receiver in r.receivers)]
        else:
            rules = [r for r in self.rules if any(_matches(p, store) for p in r.stores)]
        names = {caller, *lineage}
        for rule in rules:
            # A rule for an agent covers the calls made under it, a subagent's included.
            if rule.callers and caller is not None and names.isdisjoint(rule.callers):
                continue
            if seen.every is not None:
                return Denial(f"policy:{rule.id}", ("*",), seen.every)
            kept = rule.keeps(seen.origins)
            if kept:
                return Denial(f"policy:{rule.id}", kept)
        return None


def _matches(pattern: str, value: str) -> bool:
    """Exact, or a prefix match for a pattern ending in *: a bare * matches every value."""
    return value.startswith(pattern[:-1]) if pattern.endswith("*") else value == pattern


def parse(value: Any) -> Policy:
    """A policy, as wake-proxy serves it, checked as wake-proxy checks its file
    (proxy/src/policy.rs). Raises ValueError, saying why, for one wake-proxy wouldn't take: a
    field it doesn't know, a value of the wrong type, a rule that keeps nothing, names tools
    with receivers or path_prefixes, names stores with tools, receivers or path_prefixes, or
    has an id that isn't 1 to 64 of A-Z a-z 0-9 . _ : - or that another rule has, an entry
    that's empty or has a * anywhere a pattern can't, or a window outside 1 to 86,400
    seconds."""
    if not isinstance(value, Mapping):
        raise ValueError("a policy must be a JSON object")
    unknown = sorted(set(value) - {"version", "window_seconds", "rules"})
    if unknown:
        raise ValueError(f"unknown field {unknown[0]!r}")
    version = value.get("version")
    if "version" in value and not isinstance(version, str):
        raise ValueError("version must be a string")
    window = value.get("window_seconds", DEFAULT_WINDOW)
    # A whole number: JSON's true isn't one, nor is 900.0, which wake-proxy refuses.
    if isinstance(window, bool) or not isinstance(window, int) or not 1 <= window <= MAX_WINDOW:
        raise ValueError(f"window_seconds must be a whole number from 1 to {MAX_WINDOW}, not {window!r}")
    rules = value.get("rules", [])
    if not isinstance(rules, list):
        raise ValueError("rules must be a list")
    parsed: list[Rule] = []
    for rule in rules:
        rule = _rule(rule)
        if any(r.id == rule.id for r in parsed):
            raise ValueError(f"two rules are {rule.id!r}")
        parsed.append(rule)
    return Policy(parsed, window, version)


def _rule(value: Any) -> Rule:
    if not isinstance(value, Mapping):
        raise ValueError("each rule must be a JSON object")
    unknown = sorted(set(value) - {"id", *_LISTS})
    if unknown:
        raise ValueError(f"unknown field {unknown[0]!r} in a rule")
    rule_id = value.get("id")
    if not isinstance(rule_id, str) or not _ID.fullmatch(rule_id):
        raise ValueError(f"rule id {rule_id!r} must be 1 to 64 of A-Z a-z 0-9 . _ : -")
    lists: dict[str, tuple[str, ...]] = {}
    for name in _LISTS:
        entries = value.get(name, [])
        if not isinstance(entries, list) or not all(isinstance(e, str) for e in entries):
            raise ValueError(f"rule {rule_id}: {name} must be a list of strings")
        for entry in entries:
            star = entry.find("*")
            if not entry or (star >= 0 and (name not in _PATTERNS or star != len(entry) - 1)):
                allowed = ", or a prefix ending in *" if name in _PATTERNS else ""
                raise ValueError(f"rule {rule_id}: {name} entry {entry!r} must be exact{allowed}")
        lists[name] = tuple(entries)
    if not lists["forbid_origins"] and not lists["allow_origins"]:
        raise ValueError(f"rule {rule_id} keeps nothing: give it forbid_origins or allow_origins")
    if lists["tools"] and (lists["receivers"] or lists["path_prefixes"]):
        raise ValueError(f"rule {rule_id} names tools with receivers or path_prefixes")
    if lists["stores"] and (lists["tools"] or lists["receivers"] or lists["path_prefixes"]):
        raise ValueError(f"rule {rule_id} names stores with tools, receivers or path_prefixes: a memory write has none")
    return Rule(rule_id, **lists)


def served(value: Any) -> Policy | None:
    """The policy in the flow field of the rules wake-proxy serves. None when there's none, as
    from a wake-proxy that enforces no policy, or one older than this: nothing is checked then.
    A policy wake-proxy can't use, or that isn't valid, can't be used here either. Never
    raises."""
    if value is None:
        return None
    if isinstance(value, Mapping) and set(value) == {"unreadable"} and value["unreadable"] is True:
        return Policy(unreadable="wake-proxy can't use its policy file")
    try:
        return parse(value)
    except ValueError as e:
        return Policy(unreadable=f"the flow policy isn't valid: {e}")


# The flow field last read, and the policy it is: the rules are fetched every couple of
# seconds, and checked far more often.
_last: tuple[Any, Policy | None] = (None, None)
_warned: str | None = None


def _policy(quarantine: Any) -> Policy | None:
    """The flow policy in the brake's latest rules, from their flow field. Rules that can't
    say, such as a test's, carry none."""
    global _last, _warned
    rules_of = getattr(quarantine, "rules", None)
    rules = rules_of() if callable(rules_of) else None
    value = rules.get("flow") if isinstance(rules, Mapping) else None
    if value is None:
        return None
    last, policy = _last
    if value is not last:
        policy = served(value)
        _last = (value, policy)
        why = policy.unreadable if policy is not None else None
        if why is not None and why != _warned:
            log.warning("wake: %s; refusing every tool call, handoff, run start and memory write until it can be "
                        "used", why)
        _warned = why
    return policy


def enforcing(quarantine: Any) -> bool:
    """Whether the brake's rules carry a flow policy, one that can be used or not. Never
    raises: rules that can't say whether they do are taken to, so the check that follows
    fails closed."""
    try:
        return quarantine is not None and _policy(quarantine) is not None
    except Exception:
        return True


class HeldBack(Refusal):
    """A call the flow policy refuses: the halt it's refused under, policy:<id>, and what the
    agent is told, as for a halt's refusal. For its denied event, it keeps the origins behind
    the call the rule keeps from it, * for every origin, and why every origin counted, when it
    did."""

    def __new__(cls, halt: str, reason: str, origins: Iterable[str] = (), every: str | None = None) -> "HeldBack":
        held = super().__new__(cls, halt, reason)
        held.origins = list(origins)
        held.every = every
        return held


def refusal(quarantine: Any, who: str, caller: str | None, lineage: Iterable[str] = (), *, tool: str | None = None,
            receiver: str | None = None, store: str | None = None,
            outcome: str = "this tool call wasn't run") -> HeldBack | None:
    """The refusal of a call caller is about to make, if the flow policy in the brake's rules
    holds it back: a tool call (tool), reaching another agent (receiver), or a change to a
    memory store (store). who is caller as the agent is told, such as the instance it is.
    lineage names the agents caller acts under: the agent and instances that started it, each
    instance <agent>#<id> acting as its agent too. A caller of None is one Wake can't tell: it
    counts as having every origin, and any rule's callers could be it. None without rules, or
    when they carry no policy. Never raises: when the check itself fails, it refuses, as a
    policy that can't be read does."""
    if quarantine is None:
        return None
    try:
        policy = _policy(quarantine)
        if policy is None:
            return None
        if policy.unreadable is not None:
            return HeldBack(UNREADABLE, f"Wake's flow policy can't be read, so {outcome}.")
        origins = entered()
        origins.set_window(policy.window_seconds)
        acting = {name for n in lineage for name in (n, _agent_of(n))}
        seen = Seen((), UNKNOWN) if caller is None else origins.seen(caller)
        denial = policy.deciding(caller, acting, seen, tool=tool, receiver=receiver, store=store)
    except Exception:
        log.warning("wake: couldn't check the flow policy for %s; refusing", who, exc_info=True)
        return HeldBack(UNREADABLE, f"Wake's flow policy can't be read, so {outcome}.")
    if denial is None:
        return None
    if denial.every is not None:
        reason = f"Wake's flow policy ({denial.halt}) holds {who} back, because {denial.every}, so {outcome}."
    else:
        reason = f"Wake's flow policy ({denial.halt}) holds {who} back after {', '.join(denial.origins)}, so {outcome}."
    return HeldBack(denial.halt, reason, denial.origins, denial.every)


def _agent_of(name: str) -> str:
    """The agent an instance, <agent>#<id>, is a run of; any other name is its own."""
    agent, at, _ = name.rpartition("#")
    return agent if at and agent else name


def payload(refused: Refusal | None) -> dict[str, Any]:
    """What the denied event of a call the flow policy refused says beside its input and
    output, as wake-proxy's denied events do: the origins behind the call the rule keeps from
    it, and why every origin counted, when it did. Empty for a halt's refusal."""
    if not isinstance(refused, HeldBack):
        return {}
    out: dict[str, Any] = {"origins": list(refused.origins)}
    if refused.every is not None:
        out["every_origin"] = refused.every
    return out


# What entered each agent.


class _Every(NamedTuple):
    """An agent with every origin: since when, and why."""

    since: float
    why: str


class _Agent:
    """What entered one agent: each origin with when it last entered, and since when, and why,
    it has had every origin, if it has."""

    __slots__ = ("every", "origins")

    def __init__(self) -> None:
        self.origins: dict[str, float] = {}
        self.every: _Every | None = None

    def newest(self) -> float:
        times = list(self.origins.values()) + ([self.every.since] if self.every is not None else [])
        return max(times, default=float("-inf"))

    def note(self, origin: str, at: float, oldest: float) -> None:
        """Notes that origin entered at at. Origins older than oldest don't count toward
        MAX_ORIGINS; past it, the agent has every origin from at."""
        seen = self.origins.get(origin)
        if seen is not None:
            self.origins[origin] = max(seen, at)
            return
        if len(self.origins) >= MAX_ORIGINS:
            self.origins = {o: t for o, t in self.origins.items() if t >= oldest}
            if len(self.origins) >= MAX_ORIGINS:
                self.saturate(at, SATURATED)
                return
        self.origins[origin] = at

    def saturate(self, at: float, why: str) -> None:
        """Notes that the agent has every origin from at, for why, unless it does from later."""
        if self.every is None or self.every.since <= at:
            self.every = _Every(at, why)


class Origins:
    """What entered each agent, by name, and when, kept as wake-proxy keeps what entered each
    agent through it (proxy/src/origins.rs): each origin with the last time it entered, and
    only those within the policy's window count. Past MAX_ORIGINS within the window an agent
    counts as having every origin, and once MAX_AGENTS are kept, so does an agent that can't
    be. clock gives the time in seconds, and is only ever set by tests."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._agents: dict[str, _Agent] = {}
        self._overflowed: float | None = None  # when an agent last couldn't be kept
        self._window: float = DEFAULT_WINDOW

    def _forked(self) -> None:
        """In a forked child, whose lock may have been held by a thread that didn't come along.
        What entered each agent before the fork entered it in the child too."""
        self._lock = threading.Lock()

    def set_window(self, seconds: float) -> None:
        """Keeps origins for the policy's window, in seconds, from now on."""
        with self._lock:
            self._window = min(max(seconds, 1), MAX_WINDOW)

    def note(self, agent: str, origin: str) -> None:
        """Notes that origin entered agent now."""
        at = self._clock()
        with self._lock:
            kept = self._agent(agent, at)
            if kept is not None:
                kept.note(origin, at, at - self._window)

    def carry(self, sender: str, receiver: str) -> None:
        """Notes that content from sender reached receiver now: whatever had entered sender
        within the window enters receiver too. Each origin keeps when it entered, so passing
        content on never makes it newer, and agents talking back and forth don't keep it alive.
        receiver's own messages, agent:<receiver>, don't come back to it. When sender has every
        origin, so does receiver, for as long as sender does, and what entered sender after
        that time enters receiver as well, so it still counts once every origin no longer
        does."""
        at = self._clock()
        with self._lock:
            since = at - self._window
            every = self._every(sender, since)
            own = f"agent:{receiver}"
            # What entered by the time every origin did counts no longer than every origin does.
            carried = [(o, t) for o, t in self._recorded(sender, since).items()
                       if o != own and (every is None or t > every.since)]
            if every is None and not carried:
                return  # nothing to carry keeps no agent
            kept = self._agent(receiver, at)
            if kept is None:
                return
            if every is not None:
                kept.saturate(every.since, CARRIED)
            for origin, seen in carried:
                kept.note(origin, seen, since)

    def seen(self, agent: str) -> Seen:
        """What entered agent within the window: each origin once, newest first, ties by name."""
        at = self._clock()
        with self._lock:
            found = self._entered(agent, at - self._window)
        if isinstance(found, _Every):
            return Seen((), found.why)
        return Seen(tuple(o for o, _ in sorted(found.items(), key=lambda item: (-item[1], item[0]))))

    def _agent(self, name: str, at: float) -> _Agent | None:
        """The agent kept by name, made room for if need be. None when it can't be kept:
        MAX_AGENTS others had something enter them within the window. Call with the lock held."""
        kept = self._agents.get(name)
        if kept is not None:
            return kept
        if len(self._agents) >= MAX_AGENTS:
            # Agents nothing has entered for a window can be forgotten.
            oldest = at - self._window
            self._agents = {n: a for n, a in self._agents.items() if a.newest() >= oldest}
            if len(self._agents) >= MAX_AGENTS:
                self._overflowed = at
                return None
        kept = self._agents[name] = _Agent()
        return kept

    def _every(self, name: str, since: float) -> _Every | None:
        """Since when, and why, an agent has had every origin, if it has since since. An agent
        not kept could be one that couldn't be, while that has happened within the window. Call
        with the lock held."""
        kept = self._agents.get(name)
        if kept is None:
            if self._overflowed is not None and self._overflowed >= since:
                return _Every(self._overflowed, OVERFLOWED)
            return None
        if kept.every is not None and kept.every.since >= since:
            return kept.every
        return None

    def _recorded(self, name: str, since: float) -> dict[str, float]:
        """Each origin noted for an agent since since, with when it last entered, whether or
        not the agent has every origin. Call with the lock held."""
        kept = self._agents.get(name)
        return {} if kept is None else {o: t for o, t in kept.origins.items() if t >= since}

    def _entered(self, name: str, since: float) -> "dict[str, float] | _Every":
        """What entered an agent since since, each origin with when it last entered, or every
        origin, since when and why. Call with the lock held."""
        every = self._every(name, since)
        return every if every is not None else self._recorded(name, since)


_entered = Origins()


def entered() -> Origins:
    """What entered each agent in this process."""
    return _entered


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=lambda: entered()._forked())


def note(agent: str, origin: str) -> None:
    """Notes that origin entered agent now, as a tool's result or a memory read does. Never
    raises: noting runs beside the agent."""
    try:
        entered().note(agent, origin)
    except Exception:
        log.warning("wake: failed to note what entered %s", agent, exc_info=True)


def message(sender: str, receiver: str) -> None:
    """Notes that something from sender reached receiver now: a message, a handoff, the start
    of its run or a subagent's answer. agent:<sender> enters receiver, with whatever had
    entered sender. An agent's own messages aren't an origin of its own. Never raises."""
    if not sender or not receiver or sender == receiver:
        return
    try:
        origins = entered()
        origins.carry(sender, receiver)
        origins.note(receiver, f"agent:{sender}")
    except Exception:
        log.warning("wake: failed to note what reached %s from %s", receiver, sender, exc_info=True)
