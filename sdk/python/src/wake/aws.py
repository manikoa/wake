"""The name a sub-agent's AWS session goes by, so the brake can deny that session alone.

    import boto3
    from wake.aws import session_name

    name = session_name("support/researcher#toolu_1")
    creds = boto3.client("sts").assume_role(RoleArn=role, RoleSessionName=name, SourceIdentity=name)

An agent whose sub-agents each assume its role in a session of their own names each
session, as its SourceIdentity or its RoleSessionName or both, by the sub-agent's identity:
the identity Wake's brake halts it by, such as its instance (wake.agent.instance) or the
act.sub its delegated tokens name it with. Halting the sub-agent then denies its sessions
of the role, and only them (aws_sub_agent_sessions in the brake's inventory). A source
identity is the better of the two: AWS never lets a session change it, and it carries over
when the session assumes another role.

IAM takes 2 to 64 of A-Z a-z 0-9 _ + = , . @ - for either. An identity IAM takes as it is
is the name. Any other has each byte of its UTF-8 outside those made "-", is cut to 47,
and ends in "-" and 16 hex digits of its SHA-256, a lone surrogate counting as U+FFFD. Two
identities can still share a name, as when one IAM takes as it is reads like another's:
support-researcher-toolu_1-0a0378fa58a21af6 is also the name of support/researcher#toolu_1.
Halting either then denies the sessions of both, so a shared name only ever denies more
than the halt named, never less. The brake (wake-platform's brake/aws.SessionName) and the
TypeScript SDK (wake-sdk/aws) name a session the same way, checked against
sdk/testdata/aws_session_names.json.
"""

import hashlib

_SAFE = frozenset(b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_+=,.@-")


def session_name(identity: str) -> str:
    """The session name for a sub-agent with this identity."""
    # UTF-8 can't hold a lone surrogate, which a str can: it's U+FFFD, as the brake and the
    # TypeScript SDK see it, and a pair of them the character they make together.
    raw = identity.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace").encode("utf-8")
    if 2 <= len(raw) <= 64 and not identity.lower().startswith("aws:") and all(b in _SAFE for b in raw):
        return identity
    clean = bytes(b if b in _SAFE else ord("-") for b in raw)[:47]
    return clean.decode("ascii") + "-" + hashlib.sha256(raw).hexdigest()[:16]
