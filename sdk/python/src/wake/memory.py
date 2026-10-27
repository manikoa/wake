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

    def __init__(self, handler) -> None:
        self._handler = handler

    def write(self, store: str, entry: str, content: Any) -> dict[str, Any] | None:
        """Records a write and returns the label to store with the entry, or None if
        recording failed."""
        try:
            return self._write(store, entry, content_of(content))
        except Exception:
            log.warning("wake: failed to record a memory write", exc_info=True)
            return None

    def read(self, store: str, entry: str, content: Any, label: Mapping[str, Any] | None) -> None:
        """Records a read of an entry, with the label it was stored with, if any."""
        try:
            self._read(store, entry, content_of(content), label)
        except Exception:
            log.warning("wake: failed to record a memory read", exc_info=True)

    def write_file(self, path: str | Path, text: str) -> None:
        """Writes a memory file and its label, next to it as <file>.wake.json."""
        path = Path(path)
        label = self.write("file", str(path.resolve()), text)
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
        self.read("file", str(path.resolve()), text, label if isinstance(label, Mapping) else None)
        return text

    def _write(self, store: str, entry: str, content: str) -> dict[str, Any]:
        h = self._handler
        context = h._event_context()
        trace_id = trace.get_current_span(context).get_span_context().trace_id if context else 0
        parents: list[str] = []
        link = links.current()
        if link is not None:
            parents.append(link.event_id)
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
