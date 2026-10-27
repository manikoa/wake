"""The memory adapter: records every memory write and read, and labels each entry with
where its content came from before it lands.

    memory = Memory(handler)
    label = memory.write("facts", "fact-17", text)   # save the label with the entry
    ...
    memory.read("facts", "fact-17", text, label)     # links the read to the write

A write is a memory_write event. Its parents are the tool call making it and the earlier
events in the trace whose output it carries. The label it returns holds the write's event
id, the origins upstream of it (user, tool:..., agent:...), and a digest of the content.
Store the label with the entry, so a later read, by any agent in any session, knows where
the entry came from without asking the graph.

A read is a memory_read event with origin memory:<store>/<entry>. When the entry carries
a label whose digest matches what was read, the read's parent is the write. An entry with
no label, such as one planted or written outside Wake, or one changed since it was
written, gets no parent, so the completeness metric flags it rather than trusting it.

Wrappers: WakeStore in wake.langgraph wraps LangGraph's BaseStore (and so langmem), and
Memory.write_file / read_file handle memory files. Recording never blocks or breaks the
agent: if it fails, the write or read still happens, unlabeled.

With a Quarantine (wake.quarantine), the adapter also enforces the brake's memory
quarantine: a locked agent's writes raise MemoryLocked and don't happen, and a quarantined
entry isn't served (read returns the quarantine's id, the store wrapper drops the entry,
read_file raises MemoryQuarantined). Each refusal is recorded as a `denied` event.
"""

import hashlib
import json
import logging
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from opentelemetry import trace

from wake import events, links
from wake.quarantine import MemoryLocked, MemoryQuarantined, Quarantine

log = logging.getLogger("wake")

# Where a label lives: a key in a store's entry, or a file next to a memory file.
LABEL_KEY = "__wake__"
LABEL_SUFFIX = ".wake.json"


def digest(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def content_of(value: Any) -> str:
    """The text of an entry's content, as the label's digest covers it."""
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)


class Memory:
    """Records memory writes and reads alongside the events an SDK integration records, such as a
    WakeCallbackHandler or a WakeClaudeAgent."""

    def __init__(self, handler, quarantine: Quarantine | None = None) -> None:
        self._handler = handler
        self._quarantine = quarantine

    def write(self, store: str, entry: str, content: Any) -> dict[str, Any] | None:
        """Records a write and returns the label to store with the entry, or None if
        recording failed. Raises MemoryLocked, before anything is written, when the brake's
        quarantine locks this agent's writes."""
        locked = self._locked()
        if locked:
            self._refused("write", store, entry, locked, parents=self._linked())
            raise MemoryLocked(locked, store, entry)
        try:
            return self._write(store, entry, content_of(content))
        except Exception:
            log.warning("wake: failed to record a memory write", exc_info=True)
            return None

    def read(self, store: str, entry: str, content: Any, label: Mapping[str, Any] | None) -> str | None:
        """Records a read of an entry, with the label it was stored with, if any. Returns the
        id of the quarantine that stops the entry being served, or None to serve it."""
        text = content_of(content)
        hidden = self._hidden(store, entry, text, label)
        if hidden:
            write = label.get("event") if isinstance(label, Mapping) else None
            self._refused("read", store, entry, hidden, parents=[write] if isinstance(write, str) and write else [])
            return hidden
        try:
            self._read(store, entry, text, label)
        except Exception:
            log.warning("wake: failed to record a memory read", exc_info=True)
        return None

    def write_file(self, path: str | Path, text: str) -> None:
        """Writes a memory file and its label, next to it as <file>.wake.json."""
        path = Path(path)
        label = self.write("file", str(path.resolve()), text)  # raises MemoryLocked when locked
        path.write_text(text)
        if label is not None:
            try:
                Path(str(path) + LABEL_SUFFIX).write_text(json.dumps(label))
            except OSError:
                log.warning("wake: failed to write a memory file's label", exc_info=True)

    def read_file(self, path: str | Path) -> str:
        """Reads a memory file and records the read, with its label if it has one."""
        path = Path(path)
        text = path.read_text()
        label = None
        try:
            label_path = Path(str(path) + LABEL_SUFFIX)
            if label_path.exists():
                label = json.loads(label_path.read_text())
        except (OSError, ValueError):
            log.warning("wake: failed to read a memory file's label", exc_info=True)
        entry = str(path.resolve())
        hidden = self.read("file", entry, text, label if isinstance(label, Mapping) else None)
        if hidden:
            raise MemoryQuarantined(hidden, "file", entry)
        return text

    def _locked(self) -> str | None:
        try:
            return self._quarantine.locked_by() if self._quarantine else None
        except Exception:
            log.warning("wake: failed to check the memory quarantine", exc_info=True)
            return None

    def _hidden(self, store: str, entry: str, text: str, label: Mapping[str, Any] | None) -> str | None:
        if not self._quarantine:
            return None
        try:
            vouched = isinstance(label, Mapping) and label.get("sha256") == digest(text)
            return self._quarantine.hiding(store, entry, label if isinstance(label, Mapping) else None, vouched)
        except Exception:
            log.warning("wake: failed to check the memory quarantine", exc_info=True)
            return None

    def _linked(self) -> list[str]:
        link = links.current()
        return list(link.parents) if link is not None else []

    def _refused(self, action: str, store: str, entry: str, quarantine: str, *, parents: list[str]) -> None:
        """Records what the quarantine refused, as a denied event."""
        try:
            self._emit(
                str(uuid.uuid4()),
                events.DENIED,
                self._handler._event_context(),
                parents=parents,
                origins=[],
                attributes={
                    "wake.memory.store": store,
                    "wake.memory.entry": entry,
                    "wake.memory.refused": action,
                    "wake.brake.halt_id": quarantine,
                },
                payload={"store": store, "entry": entry, "refused": action, "quarantine": quarantine},
            )
        except Exception:
            log.warning("wake: failed to record a refused memory %s", action, exc_info=True)

    def _write(self, store: str, entry: str, content: str) -> dict[str, Any]:
        h = self._handler
        context = h._event_context()
        trace_id = trace.get_current_span(context).get_span_context().trace_id if context else 0
        parents: list[str] = []
        link = links.current()
        if link is not None:
            parents += link.parents
        if trace_id:
            parents += h._matching_outputs(trace_id, content)
        parents = list(dict.fromkeys(parents))
        origins = h._origins_upstream(parents)
        event_id = str(uuid.uuid4())
        label = {"event": event_id, "origins": origins, "sha256": digest(content), "agent": h._current_agent()}
        self._emit(
            event_id,
            events.MEMORY_WRITE,
            context,
            parents=parents,
            origins=[],
            attributes={"wake.memory.store": store, "wake.memory.entry": entry, "wake.memory.origins": origins},
            payload={"store": store, "entry": entry, "content": content},
        )
        return label

    def _read(self, store: str, entry: str, content: str, label: Mapping[str, Any] | None) -> None:
        h = self._handler
        context = h._event_context()
        parents: list[str] = []
        attributes: dict[str, Any] = {"wake.memory.store": store, "wake.memory.entry": entry}
        if label is None:
            attributes["wake.memory.labeled"] = False
        else:
            attributes["wake.memory.labeled"] = True
            write_id = label.get("event")
            if label.get("sha256") == digest(content) and isinstance(write_id, str) and write_id:
                parents.append(write_id)
                origins = label.get("origins")
                if isinstance(origins, list):
                    attributes["wake.memory.origins"] = [o for o in origins if isinstance(o, str)][:64]
            else:
                # The label doesn't vouch for this content: it changed since it was written.
                attributes["wake.memory.changed"] = True
        event_id = str(uuid.uuid4())
        span = self._emit(
            event_id,
            events.MEMORY_READ,
            context,
            parents=parents,
            origins=[f"memory:{store}/{entry}"],
            attributes=attributes,
            payload={"store": store, "entry": entry, "content": content, "label": dict(label) if label else None},
        )
        # What was read can feed later calls in the trace, like a tool's output.
        h._remember_output(span.get_span_context().trace_id, event_id, content)

    def _emit(self, event_id, kind, context, *, parents, origins, attributes, payload):
        h = self._handler
        h._remember_lineage(event_id, parents, origins)
        span = h._recorder.tracer.start_span(
            f"{kind} {payload['store']}",
            context=context,
            attributes={
                events.EVENT_ID: event_id,
                events.EVENT_KIND: kind,
                events.AGENT_ID: h._current_agent(),
                events.CAPTURE: "memory_adapter",
                events.PARENTS: parents,
                events.ORIGINS: origins,
                **attributes,
                **events.payload_attributes(payload),
            },
        )
        span.end()
        return span
