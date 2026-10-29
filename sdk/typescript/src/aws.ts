/**
 * The name a sub-agent's AWS session goes by, so the brake can deny that session alone.
 *
 *     import { AssumeRoleCommand, STSClient } from "@aws-sdk/client-sts";
 *     import { sessionName } from "wake-sdk/aws";
 *
 *     const name = sessionName("support/researcher#toolu_1");
 *     await new STSClient({}).send(new AssumeRoleCommand({ RoleArn: role, RoleSessionName: name, SourceIdentity: name }));
 *
 * An agent whose sub-agents each assume its role in a session of their own names each
 * session, as its SourceIdentity or its RoleSessionName or both, by the sub-agent's identity:
 * the identity Wake's brake halts it by, such as its instance (wake.agent.instance) or the
 * act.sub its delegated tokens name it with. Halting the sub-agent then denies its sessions of
 * the role, and only them (aws_sub_agent_sessions in the brake's inventory). A source identity
 * is the better of the two: AWS never lets a session change it, and it carries over when the
 * session assumes another role.
 *
 * IAM takes 2 to 64 of A-Z a-z 0-9 _ + = , . @ - for either. An identity IAM takes as it is
 * is the name. Any other has each byte of its UTF-8 outside those made "-", is cut to 47, and
 * ends in "-" and 16 hex digits of its SHA-256, a lone surrogate counting as U+FFFD. Two
 * identities can still share a name, as when one IAM takes as it is reads like another's:
 * support-researcher-toolu_1-0a0378fa58a21af6 is also the name of support/researcher#toolu_1.
 * Halting either then denies the sessions of both, so a shared name only ever denies more than
 * the halt named, never less. The brake (wake-platform's brake/aws.SessionName) and the Python
 * SDK (wake.aws) name a session the same way, checked against sdk/testdata/aws_session_names.json.
 */

import { createHash } from "node:crypto";

const SAFE = new Set(Buffer.from("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_+=,.@-"));
const DASH = "-".charCodeAt(0);

/** The session name for a sub-agent with this identity. */
export function sessionName(identity: string): string {
  const raw = Buffer.from(identity, "utf8");
  if (raw.length >= 2 && raw.length <= 64 && !identity.toLowerCase().startsWith("aws:") && raw.every((b) => SAFE.has(b))) {
    return identity;
  }
  const clean = Buffer.from(raw.map((b) => (SAFE.has(b) ? b : DASH))).subarray(0, 47);
  return `${clean.toString("ascii")}-${createHash("sha256").update(raw).digest("hex").slice(0, 16)}`;
}
