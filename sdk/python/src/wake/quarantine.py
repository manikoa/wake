"""The brake's memory quarantine, as the memory adapter enforces it.

    quarantine = Quarantine("http://wake-proxy:9003", agent="billing")
    memory = Memory(handler, quarantine=quarantine)

The brake puts quarantines on wake-proxy, and this fetches the agent's rules from it in the
background, every couple of seconds, so it never holds up the agent. With them, the memory
adapter refuses the agent's writes when they're locked (MemoryLocked) and stops serving
entries the quarantine covers: by the agent that wrote them, their origins, the write that
made them, their key, or because no label vouches for them. Quarantined entries stay in the
store, as evidence; they're only not served.

Until the first fetch succeeds nothing is enforced, and when a fetch fails the last rules
stand. The proxy notes each fetch, which is how the brake confirms an adapter has a
quarantine. All of this runs in the agent's own process, so it holds only as long as the
agent's code does; the proxy deny doesn't depend on it.
"""

import json
import logging
import threading
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

log = logging.getLogger("wake")


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


class Quarantine:
    """Fetches one agent's memory rules from wake-proxy in the background."""

    def __init__(self, url: str, agent: str, *, interval: float = 2.0, timeout: float = 2.0, start: bool = True) -> None:
        query = urllib.parse.urlencode({"agent": agent})
        self._url = f"{url.rstrip('/')}/v1/memory?{query}"
        self._interval = interval
        self._timeout = timeout
        self._rules: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if start:
            self._thread = threading.Thread(target=self._run, name="wake-quarantine", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.fetch()
            self._stop.wait(self._interval)

    def fetch(self) -> bool:
        """Fetches the rules now. Never raises: on failure the last rules stand."""
        try:
            with urllib.request.urlopen(self._url, timeout=self._timeout) as response:
                rules = json.loads(response.read(1 << 20))
            if not isinstance(rules, dict) or not isinstance(rules.get("quarantines", []), list):
                raise TypeError("unexpected memory rules")
            with self._lock:
                self._rules = rules
            return True
        except Exception as e:  # noqa: BLE001 - fetching runs beside the agent and must never raise
            log.debug("wake: couldn't fetch memory rules: %s", e)
            return False

    def close(self) -> None:
        self._stop.set()

    def rules(self) -> dict[str, Any] | None:
        with self._lock:
            return self._rules

    def locked_by(self) -> str | None:
        """The quarantine refusing this agent's memory writes, if any."""
        rules = self.rules()
        locked = rules.get("locked_by") if rules else None
        return locked if isinstance(locked, str) and locked else None

    def hiding(self, store: str, entry: str, label: Mapping[str, Any] | None, vouched: bool) -> str | None:
        """The quarantine that stops this entry being served, if any. vouched says whether
        the label's digest matches the content read."""
        rules = self.rules()
        if not rules:
            return None
        key = f"{store}/{entry}"
        label = label if vouched else None
        for q in rules.get("quarantines", []):
            if not isinstance(q, dict):
                continue
            sel = q.get("selection") or {}
            if label is None and sel.get("unlabeled"):
                return q.get("id")
            if key in (sel.get("entries") or []):
                return q.get("id")
            if label is None:
                continue
            origins = label.get("origins") if isinstance(label.get("origins"), list) else []
            if (
                label.get("agent") in (sel.get("agents") or [])
                or label.get("event") in (sel.get("writes") or [])
                or set(origins) & set(sel.get("origins") or [])
            ):
                return q.get("id")
        return None
