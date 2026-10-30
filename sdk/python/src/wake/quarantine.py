"""The brake's memory quarantine, as the memory adapter enforces it.

    quarantine = Quarantine("http://wake-proxy:9003", agent="billing")
    memory = Memory(handler, quarantine=quarantine)

The brake puts quarantines on wake-proxy, and this fetches the agent's rules from it: once
when it starts, with a short timeout, then in the background every couple of seconds, so
it never holds up the agent after that. With them, the memory adapter refuses the agent's
writes when they're locked (MemoryLocked) and stops serving entries the quarantine covers:
by the agent that wrote them, their origins, the write that made them, their key, or
because no label vouches for them. Quarantined entries stay in the store, as evidence;
they're only not served, and not deleted.

The brake fails closed. When wake-proxy can't read the quarantines it holds, it says so
(hidden_by), and no entry is served. When a fetch fails the last rules stand, but only for a while:
once they're older than ttl seconds, or if no fetch has succeeded yet, every write is
refused and no entry is served, under the id STALE, until a fetch succeeds again. health()
says whether the rules are fresh, and going stale is logged as a warning.

One process can host several agents, as named runs (handler.agent("email")) or Claude
subagents. The brake locks each by its name, so the rules are fetched for each name the
adapter writes under, from its first write on, and a run's writes are refused when its own
name or the process's agent is locked.

The rules carry the halts in force too, so an integration can stop its agent, or one
instance of it such as an in-process subagent, in the agent's own process: the Claude
Agent SDK integration, OpenAI Agents SDK's guard() and a LangChain handler given these rules
refuse their tool calls, and the runs they'd start (halted, wake.stop). That reaches what
the proxy deny can't, such as built-in tools and subagents that call with their parent's
credentials, but like the rest of this it holds only while the agent's code does. They
carry the flow policy wake-proxy enforces too, when it enforces one (flow), which those
integrations apply to the same calls, and the memory adapter to memory writes (wake.flow).

The proxy notes each fetch by the agent and the process it names, which is how the brake
confirms an adapter, or an integration's stop, has a quarantine or a halt: only once every
process of the agent it still waits on has it. Each fetch names the agent and this
process, and gives the process's ttl in whole seconds (ttl=), rounded up: the proxy holds a
process's note back for that long, since until then the process may still act on the rules
it last fetched, and after it the process refuses everything as stale. Once an integration
that refuses halted instances' calls uses these rules, each fetch says so too (stops=1), and
the brake calls an in-process stop of an instance confirmed only when the agent's processes
say they stop instances. A forked process starts fetching on its own. Fetches go straight to
the URL given: the app's proxy settings, such as HTTP_PROXY, never apply. All of this runs in
the agent's own process, so it holds only as long as the agent's code does; the proxy deny
doesn't depend on it.
"""

import json
import logging
import math
import os
import threading
import time
import urllib.parse
import urllib.request
import uuid
import weakref
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("wake")

# The id writes and reads are refused under while the rules are stale.
STALE = "stale-rules"
# The largest rules accepted. A larger answer is a failed fetch.
MAX_RULES_BYTES = 8 << 20
# The most agent names one adapter fetches rules for. Writes under any further name are
# refused, as stale, since there are no rules to check them against.
MAX_AGENTS = 64

# Fetches the rules with no proxy, whatever the app's environment says.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class MemoryLocked(PermissionError):
    """A memory write the brake's quarantine refused."""

    def __init__(self, quarantine: str, store: str, entry: str) -> None:
        super().__init__(f"memory writes are locked by quarantine {quarantine}: {store}/{entry} not written")
        self.quarantine = quarantine


class MemoryQuarantined(LookupError):
    """A memory entry the brake's quarantine stopped serving."""

    def __init__(self, quarantine: str, store: str, entry: str) -> None:
        super().__init__(f"{store}/{entry} is quarantined by {quarantine}")
        self.quarantine = quarantine


@dataclass(frozen=True)
class QuarantineHealth:
    """Whether the rules are fresh: age is seconds since the last fetch that succeeded,
    None if none has."""

    ok: bool
    age: float | None
    last_error: str | None


def _process_id() -> str:
    return f"{os.getpid()}.{uuid.uuid4().hex[:12]}"


def _valid(rules: Any) -> bool:
    if not isinstance(rules, dict):
        return False
    locked, hidden = rules.get("locked_by"), rules.get("hidden_by")
    quarantines, halts = rules.get("quarantines", []), rules.get("halts", [])
    if any(v is not None and not isinstance(v, str) for v in (locked, hidden)):
        return False
    if not isinstance(quarantines, list) or not isinstance(halts, list):
        return False
    return all(
        isinstance(q, dict) and isinstance(q.get("id"), str) and q["id"] and isinstance(q.get("selection") or {}, dict)
        for q in quarantines
    ) and all(
        isinstance(h, dict) and isinstance(h.get("id"), str) and h["id"] and isinstance(h.get("fleet", False), bool)
        and isinstance(h.get("agents", []), list) and all(isinstance(a, str) for a in h.get("agents", []))
        for h in halts
    )


class _Fetched:
    """The rules last fetched for one agent name."""

    def __init__(self) -> None:
        self.rules: dict[str, Any] | None = None
        self.at: float | None = None  # time.monotonic() of the last fetch that succeeded
        self.error: str | None = None


class Quarantine:
    """Fetches the memory rules for an agent, and for each named run its process hosts,
    from wake-proxy, and fails closed when it can't."""

    def __init__(
        self,
        url: str,
        agent: str,
        *,
        interval: float = 2.0,
        timeout: float = 2.0,
        ttl: float = 30.0,
        first_timeout: float = 1.0,
        start: bool = True,
    ) -> None:
        self._url = f"{url.rstrip('/')}/v1/memory"
        self._agent = agent
        self._interval = interval
        self._timeout = timeout
        self._first_timeout = first_timeout
        self._ttl = ttl
        self._process = _process_id()
        self._lock = threading.Lock()
        self._fetched: dict[str, _Fetched] = {agent: _Fetched()}
        self._warned_stale = False
        self._warned_full = False
        self._closed = False
        self._stops = False  # whether an integration refuses halted instances' calls with these rules
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if start:
            self._fetch_within(agent, first_timeout)
            self._start()
            if hasattr(os, "register_at_fork"):
                ref = weakref.ref(self)

                def after_in_child() -> None:
                    quarantine = ref()
                    if quarantine is not None:
                        quarantine._forked()

                os.register_at_fork(after_in_child=after_in_child)

    def _start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="wake-quarantine", daemon=True)
        self._thread.start()

    def _forked(self) -> None:
        """In a forked child: the fetch thread didn't come along, and the lock may have been
        held by it. The child is a process of its own, so it names itself anew."""
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._process = _process_id()
        if not self._closed:
            self._start()

    def _run(self) -> None:
        stop = self._stop
        while not stop.is_set():
            with self._lock:
                agents = list(self._fetched)
            for agent in agents:
                self.fetch(agent=agent)
            stop.wait(self._interval)

    def _fetch_within(self, agent: str, timeout: float) -> None:
        """Fetches an agent's rules, waiting at most timeout, name lookup included, which a
        socket timeout doesn't bound. A fetch still going carries on in the background."""
        fetching = threading.Thread(
            target=self.fetch, kwargs={"timeout": timeout, "agent": agent}, name="wake-quarantine-first", daemon=True
        )
        fetching.start()
        fetching.join(timeout)

    def fetch(self, timeout: float | None = None, agent: str | None = None) -> bool:
        """Fetches an agent's rules now, by default those of the agent the quarantine was
        given. Never raises: on failure the last rules stand until they're stale."""
        agent = agent or self._agent
        try:
            params = {"agent": agent, "process": self._process, "ttl": math.ceil(self._ttl)}
            if self._stops:
                params["stops"] = 1
            query = urllib.parse.urlencode(params)
            with _opener.open(f"{self._url}?{query}", timeout=timeout or self._timeout) as response:
                body = response.read(MAX_RULES_BYTES + 1)
            if len(body) > MAX_RULES_BYTES:
                raise ValueError(f"memory rules over {MAX_RULES_BYTES} bytes")
            rules = json.loads(body)
            if not _valid(rules):
                raise TypeError("unexpected memory rules")
        except Exception as e:  # noqa: BLE001 - fetching runs beside the agent and must never raise
            error = f"couldn't fetch memory rules: {e}"
            with self._lock:
                fetched = self._fetched.get(agent)
                repeated = fetched is None or error == fetched.error
                if fetched is not None:
                    fetched.error = error
            # Each new error is a warning; the same one again, every couple of seconds, isn't.
            log.log(logging.DEBUG if repeated else logging.WARNING, "wake: %s", error)
            self._note_staleness()
            return False
        with self._lock:
            fetched = self._fetched.get(agent)
            if fetched is not None:
                fetched.rules, fetched.at, fetched.error = rules, time.monotonic(), None
        self._note_staleness()
        return fetched is not None

    def _note_staleness(self) -> None:
        stale = self._is_stale()
        with self._lock:
            changed, self._warned_stale = stale != self._warned_stale, stale
        if changed and stale:
            log.warning("wake: memory rules are stale; refusing memory writes and reads until they're fetched")
        elif changed:
            log.warning("wake: memory rules fetched again")

    def close(self) -> None:
        self._closed = True
        self._stop.set()

    def _stops_instances(self) -> None:
        """Called by an integration that refuses halted instances' calls with these rules
        (wake.stop.stops_with): from the next fetch on, each says so."""
        self._stops = True

    def health(self) -> QuarantineHealth:
        """Whether the rules for the quarantine's agent are fresh."""
        with self._lock:
            fetched = self._fetched[self._agent]
            at, error = fetched.at, fetched.error
        age = None if at is None else time.monotonic() - at
        return QuarantineHealth(ok=age is not None and age <= self._ttl, age=age, last_error=error)

    def rules(self, agent: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            fetched = self._fetched.get(agent or self._agent)
            return fetched.rules if fetched else None

    def _is_stale(self, agent: str | None = None) -> bool:
        with self._lock:
            fetched = self._fetched.get(agent or self._agent)
            at = fetched.at if fetched else None
        return at is None or time.monotonic() - at > self._ttl

    def locked_by(self, agent: str | None = None) -> str | None:
        """The quarantine refusing memory writes, if any: by the quarantine's agent, or by
        agent, a named run the same process hosts, when either is locked. STALE when the
        rules for either are. A name not seen before has its rules fetched first, waiting at
        most first_timeout."""
        names = [self._agent] if agent in (None, self._agent) else [self._agent, agent]
        for name in names:
            if not self._tracking(name):
                return STALE
            rules = self.rules(name)
            locked = rules.get("locked_by") if rules else None
            if isinstance(locked, str) and locked:
                return locked
        return STALE if any(self._is_stale(name) for name in names) else None

    def halted(self, names: "Iterable[str]") -> str | None:
        """The halt that stops whoever goes by these names, if any: an agent's name, the
        subagent type it runs as, its instance and the instances that started it. STALE when
        the rules are. None when the proxy doesn't say what's halted, being older than
        cooperative stops: there's nothing to go on, and the proxy deny still holds."""
        if self._is_stale():
            return STALE
        rules = self.rules()
        halts = rules.get("halts") if rules else None
        if not isinstance(halts, list):
            return None
        names = set(names)
        for h in halts:
            if h.get("fleet") or names.intersection(h.get("agents") or []):
                return h["id"]
        return None

    def _tracking(self, agent: str) -> bool:
        """Whether rules are fetched for agent, starting now if they weren't, unless there
        are already MAX_AGENTS."""
        with self._lock:
            if agent in self._fetched:
                return True
            full = len(self._fetched) >= MAX_AGENTS
            if not full:
                self._fetched[agent] = _Fetched()
            warn, self._warned_full = full and not self._warned_full, self._warned_full or full
        if full:
            if warn:
                log.warning("wake: memory rules are fetched for %d agents at most; refusing writes under more", MAX_AGENTS)
            return False
        self._fetch_within(agent, self._first_timeout)
        return True

    def hiding(self, store: str, entry: str, label: Mapping[str, Any] | None, vouched: bool) -> str | None:
        """The quarantine that stops this entry being served, if any: STALE when the rules
        are, and the proxy's hidden_by when it serves no entry at all, as when it can't read
        the quarantines it holds. vouched says whether the label's digest matches the
        content read."""
        # Every agent's rules carry every quarantine, so the quarantine's agent's will do.
        rules = self.rules()
        hidden = rules.get("hidden_by") if rules else None
        if isinstance(hidden, str) and hidden:
            return hidden
        key = f"{store}/{entry}"
        label = label if vouched else None
        for q in (rules or {}).get("quarantines", []):
            sel = q.get("selection") or {}
            if label is None and sel.get("unlabeled"):
                return q["id"]
            if key in (sel.get("entries") or []):
                return q["id"]
            if label is None:
                continue
            origins = label.get("origins") if isinstance(label.get("origins"), list) else []
            if (
                label.get("agent") in (sel.get("agents") or [])
                or label.get("event") in (sel.get("writes") or [])
                or set(origins) & set(sel.get("origins") or [])
            ):
                return q["id"]
        return STALE if self._is_stale() else None
