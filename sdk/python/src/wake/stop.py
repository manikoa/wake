"""The in-process stop every integration shares: whether a halt in the brake's rules covers
whoever is about to act, and what they're told when one does.

Each integration knows who is acting by its own means, and asks here, so every one refuses
the same things the same way: a call by an agent, or by an instance of it, that a halt
covers; a run it would start of an agent a halt covers, such as a Claude subagent type, an
OpenAI Agents handoff's target or a LangChain named run; every call while the rules are
stale (wake.quarantine); and while an integration can't yet tell which instance is acting,
any call a halt on one it could be would cover. A check that fails refuses too: the brake
fails closed.

Once no halt refuses a call, each integration checks the flow policy served with the halts
at the same point (wake.flow), and refuses what it holds back the same way.
"""

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from wake.quarantine import Quarantine

log = logging.getLogger("wake")

# The id a call is refused under when its halts couldn't be checked.
UNCHECKED = "halt-check-failed"


class Refusal(NamedTuple):
    """A call a halt stops: the halt, as the denied event names it, and what the agent is told."""

    halt: str
    reason: str


class AgentHalted(PermissionError):
    """A run the brake's halt stopped before it started."""

    def __init__(self, refusal: Refusal) -> None:
        super().__init__(refusal.reason)
        self.halt = refusal.halt


def refusal(
    quarantine: "Quarantine | None",
    who: str,
    names: Iterable[str],
    *,
    unplaced: Iterable[str] = (),
    outcome: str = "this tool call wasn't run",
) -> Refusal | None:
    """The refusal of something who is about to do, if a halt covers any of names: its agent,
    the instance it is and those that started it, as far as they're known. While who can't
    be placed, unplaced names every instance it could be, and a halt on any of them refuses
    it too. None without rules. Never raises: when the check itself fails, it refuses."""
    if quarantine is None:
        return None
    try:
        names = set(names)
        halt = quarantine.halted(names)
        if halt is not None:
            return Refusal(halt, f"Wake halted {who} ({halt}), so {outcome}.")
        unplaced = set(unplaced) - names
        if unplaced:
            halt = quarantine.halted(names | unplaced)
            if halt is not None:
                return Refusal(
                    halt,
                    f"Wake can't yet tell which copy of {who} is calling, and a halt ({halt}) may cover it, so {outcome}.",
                )
        return None
    except Exception:
        log.warning("wake: couldn't check the halts covering %s; refusing", who, exc_info=True)
        return unchecked(outcome)


def unchecked(outcome: str = "this tool call wasn't run") -> Refusal:
    """The refusal when Wake couldn't work out whether a halt covers a call."""
    return Refusal(UNCHECKED, f"Wake couldn't check its halts, so {outcome}.")


def stops_with(quarantine: "Quarantine | None") -> None:
    """Notes that an integration refuses halted instances' calls with these rules, so each
    fetch says so from then on (wake.quarantine). Rules that can't say it, such as a test's,
    are left as they are."""
    note = getattr(quarantine, "_stops_instances", None)
    if callable(note):
        note()
