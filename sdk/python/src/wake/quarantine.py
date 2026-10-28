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

The proxy notes each fetch, which is how the brake confirms an adapter has a quarantine.
Each fetch names the agent and this process (a process parameter the proxy is free to
ignore), and a forked process starts fetching on its own. All of this runs in the agent's
own process, so it holds only as long as the agent's code does; the proxy deny doesn't
depend on it.
"""

import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
import uuid
import weakref
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("wake")

# The id writes and reads are refused under while the rules are stale.
STALE = "stale-rules"
# The largest rules accepted. A larger answer is a failed fetch.
MAX_RULES_BYTES = 8 << 20


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
    quarantines = rules.get("quarantines", [])
    if any(v is not None and not isinstance(v, str) for v in (locked, hidden)) or not isinstance(quarantines, list):
        return False
    return all(
        isinstance(q, dict) and isinstance(q.get("id"), str) and q["id"] and isinstance(q.get("selection") or {}, dict)
        for q in quarantines
    )


class Quarantine:
    """Fetches one agent's memory rules from wake-proxy, and fails closed when it can't."""

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
        self._ttl = ttl
        self._process = _process_id()
        self._lock = threading.Lock()
        self._rules: dict[str, Any] | None = None
        self._fetched_at: float | None = None  # time.monotonic() of the last fetch that succeeded
        self._error: str | None = None
        self._warned_stale = False
        self._closed = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if start:
            self.fetch(timeout=first_timeout)
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
            self.fetch()
            stop.wait(self._interval)

    def fetch(self, timeout: float | None = None) -> bool:
        """Fetches the rules now. Never raises: on failure the last rules stand until
        they're stale."""
        try:
            query = urllib.parse.urlencode({"agent": self._agent, "process": self._process})
            with urllib.request.urlopen(f"{self._url}?{query}", timeout=timeout or self._timeout) as response:
                body = response.read(MAX_RULES_BYTES + 1)
            if len(body) > MAX_RULES_BYTES:
                raise ValueError(f"memory rules over {MAX_RULES_BYTES} bytes")
            rules = json.loads(body)
            if not _valid(rules):
                raise TypeError("unexpected memory rules")
        except Exception as e:  # noqa: BLE001 - fetching runs beside the agent and must never raise
            error = f"couldn't fetch memory rules: {e}"
            with self._lock:
                repeated, self._error = error == self._error, error
            # Each new error is a warning; the same one again, every couple of seconds, isn't.
            log.log(logging.DEBUG if repeated else logging.WARNING, "wake: %s", error)
            self._note_staleness()
            return False
        with self._lock:
            self._rules = rules
            self._fetched_at = time.monotonic()
            self._error = None
        self._note_staleness()
        return True

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

    def health(self) -> QuarantineHealth:
        with self._lock:
            fetched, error = self._fetched_at, self._error
        age = None if fetched is None else time.monotonic() - fetched
        return QuarantineHealth(ok=age is not None and age <= self._ttl, age=age, last_error=error)

    def rules(self) -> dict[str, Any] | None:
        with self._lock:
            return self._rules

    def _is_stale(self) -> bool:
        with self._lock:
            fetched = self._fetched_at
        return fetched is None or time.monotonic() - fetched > self._ttl

    def locked_by(self) -> str | None:
        """The quarantine refusing this agent's memory writes, if any: STALE when the rules
        are."""
        rules = self.rules()
        locked = rules.get("locked_by") if rules else None
        if isinstance(locked, str) and locked:
            return locked
        return STALE if self._is_stale() else None

    def hiding(self, store: str, entry: str, label: Mapping[str, Any] | None, vouched: bool) -> str | None:
        """The quarantine that stops this entry being served, if any: STALE when the rules
        are, and the proxy's hidden_by when it serves no entry at all, as when it can't read
        the quarantines it holds. vouched says whether the label's digest matches the
        content read."""
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
