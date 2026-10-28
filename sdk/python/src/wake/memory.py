"""The memory adapter: records every memory write and read, and labels each entry with
where its content came from before it lands.

    memory = Memory(handler)
    label = memory.write("facts", "fact-17", text)   # save the label with the entry
    ...
    memory.read("facts", "fact-17", text, label)     # links the read to the write

A write is a memory_write event. Its parents are the tool call making it and the earlier
events in the trace whose output it carries. The label it returns holds the write's event
id, the origins upstream of it (user, tool:..., agent:...), and a digest of the content,
which the event carries too, as wake.memory.sha256. Store the label with the entry, so a
later read, by any agent in any session, knows where the entry came from without asking
the graph.

A delete is a memory_write event too, with wake.memory.deleted set and no label to keep.

A read is a memory_read event with origin memory:<store>/<entry>, and the digest of what
was read. When the entry carries a label whose digest matches what was read, the read's
parent is the write. An entry with no label, such as one planted or written outside Wake,
or one changed since it was written, gets no parent, so the completeness metric flags it
rather than trusting it.

Wrappers: WakeStore in wake.langgraph wraps LangGraph's BaseStore (and so langmem), and
Memory.write_file / read_file handle memory files. Both keep the versions an entry held in
its label's history, and roll back to one (WakeStore.rollback, Memory.rollback_file).
Recording never blocks or breaks the agent: if it fails, the write or read still happens,
unlabeled, and the failure is counted in the recorder's health.

A gate, given to Memory, sees each write, delete and rollback before it happens, and
refuses it by answering False. A gate that raises, or that can't be asked because the
write's origins can't be worked out, refuses it too. A refused write raises MemoryLocked
under GATE_DENIED.

With a Quarantine (wake.quarantine), the adapter also enforces the brake's memory
quarantine: a locked agent's writes and deletes raise MemoryLocked and don't happen, a
quarantined entry isn't served (read returns the quarantine's id, the store wrapper drops
the entry, read_file raises MemoryQuarantined), and overwriting or deleting one raises
MemoryLocked: it's kept, as evidence. Each refusal is recorded as a `denied` event. If
checking the quarantine fails, the write or delete is refused and the entry isn't served.
In a process that names its agents' runs, each run's writes are locked by its own name.
"""

import hashlib
import json
import logging
import os
import shutil
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from opentelemetry import trace

from wake import events, links
from wake._graph import failed
from wake.quarantine import MemoryLocked, MemoryQuarantined, Quarantine

log = logging.getLogger("wake")

# Where a label lives: a key in a store's entry, or a file next to a memory file.
LABEL_KEY = "__wake__"
LABEL_SUFFIX = ".wake.json"
# The id a write is refused under, and an entry not served, when checking the quarantine fails.
UNCHECKED = "quarantine-check-failed"
GATE_DENIED = "write-gate-denied"
# The most versions a label's history keeps.
MAX_HISTORY = 16
# What a read's payload keeps of the label: not its history, which can be far larger than the entry.
_LABEL_FIELDS = ("event", "origins", "sha256", "agent")

# gate(store, entry, content, origins): content is None for a delete.
Gate = Callable[[str, str, str | None, list[str]], bool]


def digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", "surrogatepass")).hexdigest()


def content_of(value: Any) -> str:
    """The text of an entry's content, as the label's digest covers it. Never raises.

    A value is its JSON, keys sorted, with each whole number in it an integer: 500.0 is 500.
    JavaScript keeps no difference between the two, so that's how the TypeScript SDK writes
    it, and an entry's digest is the same whichever SDK reads it."""
    if isinstance(value, str):
        return value
    try:
        value = _whole(value, set())
    except Exception:  # nested too deep to walk, say: written as it is
        pass
    return events.dumps(value, ensure_ascii=False)


def _whole(value: Any, inside: set[int]) -> Any:
    """The value with each whole float in it an int, keys included."""
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    if not isinstance(value, (dict, list, tuple)) or id(value) in inside:
        return value
    inside.add(id(value))
    try:
        if isinstance(value, dict):
            return {_whole(k, inside) if isinstance(k, float) else k: _whole(v, inside) for k, v in value.items()}
        return [_whole(v, inside) for v in value]
    finally:
        inside.discard(id(value))


def _event_id(value: Any) -> str | None:
    """An event id from a label, which anything that can write the store can forge, if it
    looks like one."""
    return value if isinstance(value, str) and links._EVENT_ID.match(value) else None


def _origins(label: Mapping[str, Any]) -> list[str]:
    origins = label.get("origins")
    return [o for o in origins if isinstance(o, str)] if isinstance(origins, list) else []


def _without_history(label: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """A label as a read's payload records it."""
    return None if label is None else {k: label[k] for k in _LABEL_FIELDS if k in label}


def _history(label: Any, current: Any) -> list[dict[str, Any]]:
    """The history a new version of an entry keeps in its label: the version it replaces,
    when that version's label vouches for it, then that label's own history, newest first."""
    if not isinstance(label, Mapping):
        return []
    prior = label.get("previous")
    versions = [v for v in prior if isinstance(v, dict)] if isinstance(prior, list) else []
    event = _event_id(label.get("event"))
    # Content changed since its label was written isn't kept: no label vouches for it.
    if current is not None and event and label.get("sha256") == digest(content_of(current)):
        agent = label.get("agent")
        versions.insert(
            0,
            {
                "event": event,
                "origins": _origins(label),
                "sha256": label["sha256"],
                "agent": agent if isinstance(agent, str) else "",
                "content": current,
            },
        )
    return versions[:MAX_HISTORY]


def _version(label: Any, target: str | None, restorable: Callable[[Any], bool], where: str) -> dict[str, Any]:
    """The version a rollback restores, from a label's history: the newest, or the one target
    names. Raises ValueError when there's none, or it names no write or doesn't match its
    own digest: anything that can write the store can write its history."""
    history = label.get("previous") if isinstance(label, Mapping) else None
    versions = [v for v in history if isinstance(v, dict)] if isinstance(history, list) else []
    if not versions:
        raise ValueError(f"no rollback history in {where}")
    version = versions[0] if target is None else next((v for v in versions if v.get("event") == target), None)
    if version is None:
        raise ValueError(f"rollback target {target} not found in {where}")
    event, content, sha256 = _event_id(version.get("event")), version.get("content"), version.get("sha256")
    if not event or not isinstance(sha256, str) or not restorable(content) or digest(content_of(content)) != sha256:
        raise ValueError(f"rollback version in {where} doesn't check out: it must name a write and match its digest")
    agent = version.get("agent")
    return {
        "event": event,
        "origins": _origins(version),
        "sha256": sha256,
        "agent": agent if isinstance(agent, str) else "",
        "content": content,
    }


def _describe(error: BaseException) -> str:
    try:
        return f"{type(error).__name__}: {error}"
    except Exception:
        return type(error).__name__


def _read_text(path: Path) -> str:
    """A memory file's text, whatever the locale: UTF-8, with its newlines as they are, so
    the label's digest covers the file's bytes. Bytes that aren't UTF-8 read as U+FFFD, as
    the TypeScript SDK reads them, rather than raising into the agent."""
    data = path.read_bytes()
    try:
        return data.decode("utf-8", "surrogatepass")
    except UnicodeDecodeError:
        return data.decode("utf-8", "replace")


def _write_atomically(path: Path, text: str) -> None:
    """Replaces the file whole, keeping its mode: a reader finds the old file or the new one.
    The bytes are the text's UTF-8, newlines as they are, which is what the digest covers."""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        temporary.write_bytes(text.encode("utf-8", "surrogatepass"))
        if path.exists():
            shutil.copymode(path, temporary)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


class Memory:
    """Records memory writes and reads alongside the events an SDK integration records, such as a
    WakeCallbackHandler or a WakeClaudeAgent.

    gate, if given, is called as gate(store, entry, content, origins) before each write, with
    the content's text and the origins upstream of it; before each delete, with content None;
    and before each rollback, with the content and origins of the version it restores. A
    falsy answer refuses it, and so does a gate that raises."""

    def __init__(
        self,
        handler,
        quarantine: Quarantine | None = None,
        *,
        gate: Gate | None = None,
    ) -> None:
        self._handler = handler
        self._quarantine = quarantine
        self._gate = gate

    @property
    def quarantine(self) -> Quarantine | None:
        return self._quarantine

    def write(
        self, store: str, entry: str, content: Any, current: Any = None, label: Mapping[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """Records a write and returns the label to store with the entry, or None if
        recording failed. current and label are what the entry holds now (None if nothing)
        and the label stored with it. Raises MemoryLocked, before anything is written, when
        the brake's quarantine locks this agent's writes, or hides the entry: a quarantined
        entry is kept, as evidence, not overwritten. And when the gate refuses it."""
        return self._admit_write(store, entry, content, current, label)()

    def _admit_write(
        self, store: str, entry: str, content: Any, current: Any, label: Mapping[str, Any] | None, *, history: bool = False
    ) -> Callable[[], dict[str, Any] | None]:
        """Checks a write as write() does, and returns what records it, so a batch can be
        checked whole before any of it is recorded. With history, the label it records keeps
        the version the write replaces."""
        self._check("write", store, entry, current, label)
        text = content_of(content)
        lineage = self._gated("write", store, entry, text, lambda: self._parents_and_origins(text))
        previous = _history(label, current) if history else []

        def record() -> dict[str, Any] | None:
            try:
                written = self._write(store, entry, text, lineage)
            except Exception:
                self._failed("a memory write")
                return None
            return {**written, "previous": previous} if previous else written

        return record

    def delete(self, store: str, entry: str, current: Any = None, label: Mapping[str, Any] | None = None) -> None:
        """Records the delete of an entry, given what it holds now (None if nothing) and the
        label stored with it. Raises MemoryLocked, before anything is deleted, when the
        brake's quarantine locks this agent's writes, or hides the entry: a quarantined
        entry is kept, as evidence. And when the gate refuses it."""
        self._admit_delete(store, entry, current, label)()

    def _admit_delete(
        self, store: str, entry: str, current: Any, label: Mapping[str, Any] | None
    ) -> Callable[[], None]:
        """Checks a delete as delete() does, and returns what records it."""
        self._check("delete", store, entry, current, label)
        lineage = self._gated("delete", store, entry, None, self._delete_lineage)

        def record() -> None:
            try:
                self._delete(store, entry, lineage)
            except Exception:
                self._failed("a memory delete")

        return record

    def _admit_rollback(
        self,
        store: str,
        entry: str,
        current: Any,
        label: Mapping[str, Any] | None,
        target: str | None,
        restorable: Callable[[Any], bool],
        where: str,
    ) -> tuple[Any, Callable[[], dict[str, Any] | None]]:
        """Checks a rollback of an entry to a version its label's history keeps, and returns
        the version's content and what records the rollback, which gives the label to keep
        with it. A rollback is a write, so it's refused as one, and also when the quarantine
        wouldn't serve the version: restoring it would give it a label the quarantine no
        longer matches. Raises MemoryLocked when refused, and ValueError when there's no such
        version or it doesn't match its digest."""
        self._check("write", store, entry, current, label)
        version = _version(label, target, restorable, where)
        event, origins, content = version["event"], version["origins"], version["content"]
        text = content_of(content)
        hidden = self._hidden(store, entry, text, version)
        if hidden:
            self._refused("write", store, entry, hidden, parents=self._linked() + [event])
            raise MemoryLocked(hidden, store, entry)
        self._gated("write", store, entry, text, lambda: ([event], origins))
        # The version it replaces is kept too, so a rollback can be rolled back.
        previous = _history(label, current)

        def record() -> dict[str, Any] | None:
            try:
                restored = self._rollback(store, entry, text, event, origins)
            except Exception:
                self._failed("a memory rollback")
                return None
            return {**restored, "previous": previous} if previous else restored

        return content, record

    def _check(self, action: str, store: str, entry: str, current: Any, label: Mapping[str, Any] | None) -> None:
        """Raises MemoryLocked, and records the refusal, when the quarantine locks this
        agent's writes or hides the entry as it is now."""
        refused = self._locked()
        if not refused and current is not None:
            refused = self._hidden(store, entry, content_of(current), label)
        if refused:
            write = _event_id(label.get("event")) if isinstance(label, Mapping) else None
            self._refused(action, store, entry, refused, parents=self._linked() + ([write] if write else []))
            raise MemoryLocked(refused, store, entry)

    def _gated(
        self,
        action: str,
        store: str,
        entry: str,
        content: str | None,
        lineage: Callable[[], tuple[list[str], list[str]]],
    ) -> tuple[list[str], list[str]] | None:
        """Asks the gate, if there is one, and returns the parents and origins it judged by.
        Raises MemoryLocked, and records the refusal, when it refuses, when it raises, and
        when the origins it needs can't be worked out: it fails closed."""
        if self._gate is None:
            return None
        try:
            parents, origins = lineage()
        except Exception:
            self._failed(f"a memory {action}'s origins")
            self._refused(action, store, entry, GATE_DENIED, parents=self._linked(), error="origins unavailable")
            raise MemoryLocked(GATE_DENIED, store, entry) from None
        try:
            allowed, error = bool(self._gate(store, entry, content, list(origins))), None
        except Exception as e:
            log.warning("wake: the memory gate raised; refusing the %s", action, exc_info=True)
            allowed, error = False, _describe(e)
        if not allowed:
            self._refused(action, store, entry, GATE_DENIED, parents=parents, error=error)
            raise MemoryLocked(GATE_DENIED, store, entry)
        return parents, origins

    def read(self, store: str, entry: str, content: Any, label: Mapping[str, Any] | None) -> str | None:
        """Records a read of an entry, with the label it was stored with, if any. Returns the
        id of the quarantine that stops the entry being served, or None to serve it."""
        text = content_of(content)
        hidden = self._hidden(store, entry, text, label)
        if hidden:
            write = _event_id(label.get("event")) if isinstance(label, Mapping) else None
            self._refused("read", store, entry, hidden, parents=[write] if write else [])
            return hidden
        try:
            self._read(store, entry, text, label, content)
        except Exception:
            self._failed("a memory read")
        return None

    def write_file(self, path: str | Path, text: str) -> None:
        """Writes a memory file and its label, next to it as <file>.wake.json. The label
        lands first and each file is replaced whole, so the new content is never found
        with a label that doesn't vouch for it, only the other way round, which reads as
        changed. The label keeps the versions the file held, at most MAX_HISTORY, for
        rollback_file. Raises MemoryLocked when the quarantine locks this agent's writes or
        hides the file as it is now, or the gate refuses the write."""
        path = Path(path)
        current = current_label = None
        if path.exists():
            try:
                current = _read_text(path)
            except OSError:
                # What it holds can't be checked, so a quarantine refuses the write.
                if self._quarantine is not None:
                    raise
            else:
                current_label = self._label_of(path)
        label = self._admit_write("file", str(path.resolve()), text, current, current_label, history=True)()
        if label is not None:
            try:
                _write_atomically(Path(str(path) + LABEL_SUFFIX), json.dumps(label))
            except OSError:
                self._failed("a memory file's label")
        _write_atomically(path.resolve(), text)

    def rollback_file(self, path: str | Path, target_event_id: str | None = None) -> dict[str, Any] | None:
        """Rolls a memory file back to a version its label keeps: the newest, or the one
        target_event_id names. A rollback is a write: it raises MemoryLocked when write_file
        would, when the quarantine wouldn't serve the version, or when the gate refuses its
        content, and ValueError, restoring nothing, when there's no such version or it
        doesn't match its digest. It's recorded as a memory_write naming the write it
        restores. Returns the new label, which names the rollback and keeps the version it
        replaced, so it can be rolled back too; None if recording failed."""
        path = Path(path)
        label_path = Path(str(path) + LABEL_SUFFIX)
        current = _read_text(path) if path.exists() else None
        content, record = self._admit_rollback(
            "file", str(path.resolve()), current, self._label_of(path), target_event_id,
            lambda c: isinstance(c, str), str(label_path),
        )
        label = record()
        if label is not None:
            try:
                _write_atomically(label_path, json.dumps(label))
            except OSError:
                self._failed("a memory file's label")
        _write_atomically(path.resolve(), content)
        return label

    def read_file(self, path: str | Path) -> str:
        """Reads a memory file and records the read, with its label if it has one."""
        path = Path(path)
        text = _read_text(path)
        entry = str(path.resolve())
        hidden = self.read("file", entry, text, self._label_of(path))
        if hidden:
            raise MemoryQuarantined(hidden, "file", entry)
        return text

    def _label_of(self, path: Path) -> Mapping[str, Any] | None:
        """The label next to a memory file, if it has one that reads."""
        try:
            label_path = Path(str(path) + LABEL_SUFFIX)
            if label_path.exists():
                label = json.loads(label_path.read_bytes())
                return label if isinstance(label, Mapping) else None
        except (OSError, ValueError):
            log.warning("wake: failed to read a memory file's label", exc_info=True)
        return None

    def _locked(self) -> str | None:
        try:
            return self._quarantine.locked_by(self._run_agent()) if self._quarantine else None
        except Exception:
            log.warning("wake: failed to check the memory quarantine", exc_info=True)
            return UNCHECKED

    def _run_agent(self) -> str | None:
        """The named run writing, such as handler.agent("email"), when it isn't the agent the
        quarantine was given: one process can host several agents, and the brake locks each
        by its own name."""
        agent = self._handler._current_agent()
        return None if agent == self._handler._recorder.agent else agent

    def _hidden(self, store: str, entry: str, text: str, label: Mapping[str, Any] | None) -> str | None:
        if not self._quarantine:
            return None
        try:
            vouched = isinstance(label, Mapping) and label.get("sha256") == digest(text)
            return self._quarantine.hiding(store, entry, label if isinstance(label, Mapping) else None, vouched)
        except Exception:
            log.warning("wake: failed to check the memory quarantine", exc_info=True)
            return UNCHECKED

    def _linked(self) -> list[str]:
        link = links.current()
        return list(link.parents) if link is not None else []

    def _refused(
        self, action: str, store: str, entry: str, quarantine: str, *, parents: list[str], error: str | None = None
    ) -> None:
        """Records what the quarantine or the gate refused, as a denied event, with why the gate
        couldn't judge it, if it couldn't."""
        payload = {"store": store, "entry": entry, "refused": action, "quarantine": quarantine}
        if error is not None:
            payload["error"] = error
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
                payload=payload,
            )
        except Exception:
            self._failed(f"a refused memory {action}")

    def _failed(self, what: str) -> None:
        log.warning("wake: failed to record %s", what, exc_info=True)
        failed(self._handler._recorder, what)

    def _parents_and_origins(self, content: str) -> tuple[list[str], list[str]]:
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
        return parents, origins

    def _delete_lineage(self) -> tuple[list[str], list[str]]:
        parents = self._linked()
        return parents, self._handler._origins_upstream(parents)

    def _write(
        self, store: str, entry: str, content: str, lineage: tuple[list[str], list[str]] | None = None
    ) -> dict[str, Any]:
        h = self._handler
        context = h._event_context()
        parents, origins = lineage if lineage is not None else self._parents_and_origins(content)
        event_id = str(uuid.uuid4())
        sha256 = digest(content)
        label = {"event": event_id, "origins": origins, "sha256": sha256, "agent": h._current_agent()}
        self._emit(
            event_id,
            events.MEMORY_WRITE,
            context,
            parents=parents,
            origins=[],
            attributes={
                "wake.memory.store": store,
                "wake.memory.entry": entry,
                "wake.memory.origins": origins,
                "wake.memory.sha256": sha256,
            },
            payload={"store": store, "entry": entry, "content": content},
        )
        return label

    def _rollback(self, store: str, entry: str, content: str, version: str, origins: list[str]) -> dict[str, Any]:
        """Records a rollback, and returns the label to keep with what it restores. Its parent
        is the write it restores, whose origins it carries."""
        h = self._handler
        event_id = str(uuid.uuid4())
        sha256 = digest(content)
        self._emit(
            event_id,
            events.MEMORY_WRITE,
            h._event_context(),
            parents=[version],
            origins=[],
            attributes={
                "wake.memory.store": store,
                "wake.memory.entry": entry,
                "wake.memory.rollback_to": version,
                "wake.memory.origins": origins,
                "wake.memory.sha256": sha256,
            },
            payload={"store": store, "entry": entry, "content": content, "rollback_to": version},
        )
        return {"event": event_id, "origins": origins, "sha256": sha256, "agent": h._current_agent()}

    def _delete(self, store: str, entry: str, lineage: tuple[list[str], list[str]] | None = None) -> None:
        h = self._handler
        parents, origins = lineage if lineage is not None else self._delete_lineage()
        self._emit(
            str(uuid.uuid4()),
            events.MEMORY_WRITE,
            h._event_context(),
            parents=parents,
            origins=[],
            attributes={
                "wake.memory.store": store,
                "wake.memory.entry": entry,
                "wake.memory.deleted": True,
                "wake.memory.origins": origins,
            },
            payload={"store": store, "entry": entry, "deleted": True},
        )

    def _read(self, store: str, entry: str, content: str, label: Mapping[str, Any] | None, value: Any) -> None:
        h = self._handler
        context = h._event_context()
        parents: list[str] = []
        sha256 = digest(content)
        attributes: dict[str, Any] = {"wake.memory.store": store, "wake.memory.entry": entry, "wake.memory.sha256": sha256}
        if label is None:
            attributes["wake.memory.labeled"] = False
        else:
            attributes["wake.memory.labeled"] = True
            write_id = _event_id(label.get("event"))
            if label.get("sha256") == sha256 and write_id:
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
            payload={"store": store, "entry": entry, "content": content, "label": _without_history(label)},
        )
        # What was read can feed later calls in the trace, like a tool's output: as the label's
        # digest covers it, and with its whole numbers as Python writes them, 500.0 as 500.0.
        trace_id = span.get_span_context().trace_id
        h._remember_output(trace_id, event_id, content)
        if not isinstance(value, str):
            plain = events.dumps(value, ensure_ascii=False)
            if plain != content:
                h._remember_output(trace_id, event_id, plain)

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
