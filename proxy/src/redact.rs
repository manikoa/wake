//! Secrets never reach the log. A payload is kept in an append only log that nothing can
//! erase, so a credential an agent passed in a message or a tool call would be readable there
//! for as long as the log is kept. Before the proxy signs an event, `redact` replaces every
//! credential it recognizes in the payload with a placeholder naming its kind and a digest:
//!
//! ```text
//! [redacted jwt sha256:56aec5e5c6aa jti:tok-7]
//! ```
//!
//! Only formats that identify themselves are recognized, so ordinary text is never taken
//! for a secret: JWTs, private key blocks, GitHub, Google, Slack, LLM provider and Stripe
//! keys, and bearer tokens. A JWT's placeholder keeps its jti, when that's a plain id, so the
//! token can be matched to `wake.token.id` (token.rs). No pattern matches a quote, so a JSON
//! payload stays JSON. The collector does the same for the events it signs, and
//! collector/event/testdata/redaction.json holds both to the same output. See
//! docs/events.md.
//!
//! The regex crate runs in time linear in the payload, whatever an agent sends.

use std::sync::LazyLock;

use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use regex::Regex;
use sha2::{Digest, Sha256};

/// The kinds of secret a payload's placeholders name, sorted. The signer sets it, replacing
/// any value the sender gave, and it's signed with the rest of the event.
pub const REDACTED: &str = "wake.payload.redacted";

/// A private key block, to its END line, or, cut short, as far as its body runs. The body
/// is base64, header lines, spaces and line breaks, raw or escaped once or twice for JSON, so
/// a key in a string inside a string is found whole and never runs past a quote.
const KEY_BODY: &str = r"(?:[A-Za-z0-9+/= \t:,.-]|\\r|\\n|\\\\r|\\\\n|\r|\n)";
const KEY_BEGIN: &str = r"-----BEGIN [A-Z ]{0,20}PRIVATE KEY-----";
const KEY_END: &str = r"-----END [A-Z ]{0,20}PRIVATE KEY-----";

static REDACTIONS: LazyLock<Vec<(&'static str, Regex)>> = LazyLock::new(|| {
    let private_key = format!("{KEY_BEGIN}{KEY_BODY}*?{KEY_END}|{KEY_BEGIN}{KEY_BODY}*");
    [
        ("private_key", private_key.as_str()),
        ("jwt", r"eyJ[A-Za-z0-9_-]{4,}\.eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{16,}"),
        (
            "github_token",
            r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,}",
        ),
        ("google_api_key", r"AIza[0-9A-Za-z_-]{35}"),
        ("google_oauth_token", r"ya29\.[0-9A-Za-z_-]{20,}"),
        ("slack_token", r"xox[abposr]-[0-9A-Za-z-]{10,}"),
        (
            "llm_api_key",
            r"sk-ant-[0-9A-Za-z_-]{20,}|sk-proj-[0-9A-Za-z_-]{20,}|sk-[0-9A-Za-z]{32,}",
        ),
        ("stripe_key", r"(?:sk|rk)_live_[0-9A-Za-z]{24,}"),
        ("bearer_token", r"(?:Bearer|bearer|BEARER) +[A-Za-z0-9._~+/-]{20,}=*"),
    ]
    .into_iter()
    .map(|(kind, pattern)| (kind, Regex::new(pattern).expect("a redaction pattern")))
    .collect()
});

static PLACEHOLDER: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\[redacted ([a-z_]+) sha256:[0-9a-f]{12}").expect("the placeholder pattern"));
static PLAIN_ID: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Za-z0-9._:-]{1,128}$").expect("the plain id pattern"));

/// The payload with every secret it recognizes replaced, and the kinds of secret its
/// placeholders name, sorted, whether replaced now or by an earlier hop.
pub fn redact(payload: &str) -> (String, Vec<String>) {
    let mut text = payload.to_string();
    for (kind, pattern) in REDACTIONS.iter() {
        if !pattern.is_match(&text) {
            continue;
        }
        text = pattern
            .replace_all(&text, |m: &regex::Captures| {
                let secret = &m[0];
                let digest = hex::encode(Sha256::digest(secret.as_bytes()));
                let mut out = format!("[redacted {kind} sha256:{}", &digest[..12]);
                if *kind == "jwt"
                    && let Some(jti) = jwt_id(secret)
                {
                    out.push_str(" jti:");
                    out.push_str(&jti);
                }
                out.push(']');
                out
            })
            .into_owned();
    }
    let mut kinds: Vec<String> = PLACEHOLDER.captures_iter(&text).map(|c| c[1].to_string()).collect();
    kinds.sort();
    kinds.dedup();
    (text, kinds)
}

/// A JWT's jti when it's a plain id, so it can't break the placeholder or the JSON around it.
fn jwt_id(token: &str) -> Option<String> {
    let claims = URL_SAFE_NO_PAD.decode(token.split('.').nth(1)?).ok()?;
    let claims: serde_json::Value = serde_json::from_slice(&claims).ok()?;
    let jti = claims.get("jti")?.as_str()?;
    PLAIN_ID.is_match(jti).then(|| jti.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;
    use std::time::{Duration, Instant};

    // The fuzz target's properties (fuzz/fuzz_targets/redact.rs), on stable Rust, with
    // inputs that mix secret-shaped pieces into JSON so the patterns are exercised.
    proptest! {
        #[test]
        fn json_stays_json_and_redacting_twice_changes_nothing(
            pieces in proptest::collection::vec(
                prop_oneof![
                    any::<String>(),
                    Just("eyJhbGciOiJFUzI1NiJ9.eyJqdGkiOiJ0b2stNyJ9.c2lnbmF0dXJlLW5vdC1yZWFs".to_string()),
                    Just("-----BEGIN PRIVATE KEY-----\nMIGH\n".to_string()),
                    Just("-----END PRIVATE KEY-----".to_string()),
                    Just("Bearer ".to_string()),
                    Just(format!("ghp_{}", "a".repeat(36))),
                    Just("\"\\".to_string()),
                ],
                0..12,
            )
        ) {
            let text: String = pieces.concat();
            let payload = serde_json::json!({
                "output": text,
                "nested": serde_json::json!({"text": text}).to_string(),
            })
            .to_string();
            let (out, kinds) = redact(&payload);
            prop_assert!(serde_json::from_str::<serde_json::Value>(&out).is_ok(), "not JSON: {out}");
            prop_assert_eq!(&redact(&out).0, &out);
            for kind in &kinds {
                let placeholder = format!("[redacted {kind} sha256:");
                prop_assert!(out.contains(&placeholder));
            }
        }
    }

    #[derive(serde::Deserialize)]
    struct Vector {
        name: String,
        #[serde(rename = "in")]
        input: String,
        out: String,
        kinds: Vec<String>,
    }

    /// The collector's vectors: the proxy must make of each payload exactly what it does.
    #[test]
    fn matches_the_collectors_vectors() {
        let data = include_str!("../../collector/event/testdata/redaction.json");
        let vectors: Vec<Vector> = serde_json::from_str(data).unwrap();
        assert!(!vectors.is_empty());
        for v in vectors {
            let (out, kinds) = redact(&v.input);
            assert_eq!(out, v.out, "{}", v.name);
            assert_eq!(kinds, v.kinds, "{}", v.name);
            assert!(
                serde_json::from_str::<serde_json::Value>(&out).is_ok(),
                "{}: not JSON",
                v.name
            );
            assert_eq!(redact(&out).0, out, "{}: not idempotent", v.name);
        }
    }

    /// An agent chooses what's in a payload. Inputs built to make a backtracking engine
    /// retry at every position still take linear time.
    #[test]
    fn takes_linear_time() {
        for (name, payload) in [
            ("jwt prefixes", "eyJ".repeat(1 << 20)),
            (
                "a key that never ends",
                format!("-----BEGIN PRIVATE KEY-----{}", "A-".repeat(1 << 20)),
            ),
            ("many key starts", "-----BEGIN PRIVATE KEY-----".repeat(1 << 15)),
            ("bearer runs", "Bearer ".repeat(1 << 19)),
        ] {
            let start = Instant::now();
            redact(&payload);
            assert!(
                start.elapsed() < Duration::from_secs(10),
                "{name}: {:?}",
                start.elapsed()
            );
        }
    }
}
