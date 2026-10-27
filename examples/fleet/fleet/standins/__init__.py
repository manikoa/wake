"""Stand-ins for the services the brake pulls levers on, for the reference fleet and its game
day only: an identity provider, as a design partner's Okta would be (oauth.py), and E2B
(e2b.py). Each implements what the fleet's agents and the brake call, and nothing more. Real
ones come later, from a design partner, or from an Okta developer org and an E2B account.

Each starts as root, hands out the credentials it issues by writing each into the volume of
whoever holds it (OUT/<holder>/<name>), then drops to nobody and serves. Its state, with the
revocations and pauses the brake made, is kept in its own volume, so a restart doesn't undo
a lever. It keeps digests of the credentials, not the credentials.
"""

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Any

NOBODY = 65534
# One directory per holder, each its own volume: brake, tools, support, research, billing.
OUT = Path(os.environ.get("STANDIN_OUT", "/out"))
STATE = Path(os.environ.get("STANDIN_STATE", "/var/lib/standin"))


def new_secret(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def matches(value: str, want: str) -> bool:
    return hmac.compare_digest(digest(value), want)


def hand_out(holder: str, name: str, value: str) -> None:
    """Writes a credential into its holder's volume, readable by the holder's container
    whatever user it runs as. Only the holder mounts that volume."""
    path = OUT / holder / name
    for d in reversed([p for p in path.parents if p.is_relative_to(OUT / holder)]):
        d.mkdir(mode=0o755, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(value + "\n")
    tmp.chmod(0o444)
    tmp.replace(path)


def load(name: str) -> dict[str, Any] | None:
    try:
        return json.loads((STATE / name).read_text())
    except FileNotFoundError:
        return None


def save(name: str, state: dict[str, Any]) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = STATE / (name + ".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE / name)


def drop_privileges() -> None:
    """Gives the state to nobody and becomes nobody, once the credentials are handed out."""
    STATE.mkdir(parents=True, exist_ok=True)
    if os.getuid() != 0:
        return
    for path in [STATE, *STATE.iterdir()]:
        os.chown(path, NOBODY, NOBODY)
    os.setgroups([])
    os.setgid(NOBODY)
    os.setuid(NOBODY)
