//! Keeps the credentials it recognizes out of the log. A payload is kept in an append only
//! log that nothing can erase, so a credential an agent passed in a message or a tool call
//! would be readable there for as long as the log is kept. Before the proxy signs an event,
//! `redact` replaces every credential it recognizes in the payload with a placeholder naming
//! its kind and a digest:
//!
//! ```text
//! [redacted jwt sha256:56aec5e5c6aa jti:tok-7]
//! ```
//!
//! Only formats that identify themselves are recognized, so ordinary text is never taken
//! for a secret: private key blocks, JWTs, GitHub, Google, Slack, LLM provider and Stripe
//! keys, bearer tokens, Basic credentials, passwords in URLs, and AWS secret keys right
//! after a name that says so. A credential in any other form is signed and logged as it
//! came. A JWT's placeholder keeps its jti, when that's a plain id, so the token can be
//! matched to `wake.token.id` (token.rs). A password in a URL and Basic credentials get no
//! digest (see `placeholder`). Where a payload was cut short, a credential the cut went
//! through is replaced too (see CUT_SHORT). What's replaced never holds a quote or part of
//! an escape, so a JSON payload stays JSON, however deep its strings nest JSON. The
//! collector does the same for the events it signs, and
//! collector/event/testdata/redaction.json holds both to the same output. See
//! docs/events.md.
//!
//! Each pattern captures the secret it replaces; the rest of a match is context, kept as it
//! is. The regex crate runs in time linear in the payload, whatever an agent sends.

use std::sync::LazyLock;

use base64::Engine;
use base64::engine::general_purpose::{STANDARD, URL_SAFE_NO_PAD};
use regex::{Regex, RegexSet};
use sha2::{Digest, Sha256};

/// The kinds of secret the signer replaced in a payload, sorted. The signer sets it,
/// replacing any value the sender gave, and it's signed with the rest of the event.
pub const REDACTED: &str = "wake.payload.redacted";

/// A private key block, PEM or OpenPGP, to its END line, or, cut short, as far as its body
/// runs. The body is base64, header lines, spaces and line breaks, raw or escaped for JSON at
/// any depth, so a key in a string inside strings is found whole and never runs past a
/// quote. So are the escapes some encoders write for base64's + / and =: .NET writes + as
/// \u002B, PHP / as \/.
const KEY_BODY: &str = r"(?:[A-Za-z0-9+/= \t:,.-]|\\+(?:[rn/]|u00(?:2[BbFf]|3[Dd]))|\r|\n)";
const KEY_BEGIN: &str = r"-----BEGIN [A-Z ]{0,20}PRIVATE KEY(?: BLOCK)?-----";
const KEY_END: &str = r"-----END [A-Z ]{0,20}PRIVATE KEY(?: BLOCK)?-----";

/// In front of a secret that starts a word: the start of the payload, a character that can't
/// be part of one, or a whole JSON escape such as \n or \u00e9, at any depth. Never inside a
/// word, so task- or disk- doesn't start an sk- key. Never inside an escape: SDKs escape
/// non-ASCII, so ® then yJ… arrives as \u00aeyJ…, which holds no JWT, and replacing from its
/// e would leave \u00a, which isn't JSON. Never right after a backslash, which starts one.
/// And never after the ] that ends a placeholder, so a secret right after one a later
/// pattern leaves isn't found by redacting again.
const WORD: &str = r"(?:^|[^0-9A-Za-z_\\\]]|\\+[bfnrt]|\\+u[0-9A-Fa-f]{4})";

/// An AWS secret access key is 40 characters of random base64, so it's only taken for one
/// right after a name that says so, then =, : or > (STS's XML), with any quotes and spaces
/// between, and with no more base64 after it.
const AWS_SECRET_KEY: &str = concat!(
    r"(?:secret_access_key|SECRET_ACCESS_KEY|SecretAccessKey|secretAccessKey)",
    r#"(?:\\*["'])?[ \t]*[:=>][ \t]*(?:\\*["'])?([A-Za-z0-9/+]{40})(?:[^A-Za-z0-9/+=]|$)"#,
);

/// The names AWS_SECRET_KEY starts with.
const AWS_NAMES: [&str; 4] = [
    "secret_access_key",
    "SECRET_ACCESS_KEY",
    "SecretAccessKey",
    "secretAccessKey",
];

static REDACTIONS: LazyLock<Vec<(&'static str, Regex)>> = LazyLock::new(|| {
    let word = |secret: &str| format!("{WORD}({secret})");
    [
        (
            "private_key",
            format!("({KEY_BEGIN}{KEY_BODY}*?{KEY_END}|{KEY_BEGIN}{KEY_BODY}*)"),
        ),
        (
            "jwt",
            word(r"eyJ[A-Za-z0-9_-]{4,}\.eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{16,}"),
        ),
        (
            "github_token",
            word(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,}"),
        ),
        ("google_api_key", word(r"AIza[0-9A-Za-z_-]{35}")),
        ("google_oauth_token", word(r"ya29\.[0-9A-Za-z_-]{20,}")),
        (
            "slack_token",
            word(r"xox[abposr]-[0-9A-Za-z-]{10,}|xapp-[0-9A-Za-z-]{10,}"),
        ),
        (
            "llm_api_key",
            word(r"sk-(?:ant|proj|svcacct|admin)-[0-9A-Za-z_-]{20,}|sk-[0-9A-Za-z]{32,}"),
        ),
        ("stripe_key", word(r"(?:sk|rk)_live_[0-9A-Za-z]{24,}")),
        (
            "bearer_token",
            word(r"(?:Bearer|bearer|BEARER) +[A-Za-z0-9._~+/-]{20,}=*"),
        ),
        // Only when basic_credentials finds user:password in it.
        ("basic_auth", word(r"(?:Basic|basic|BASIC) +[A-Za-z0-9+/]+={0,2}")),
        // The password in scheme://user:password@host.
        (
            "url_password",
            r#"://[^ \t\r\n/?#@:"\\]*:([^ \t\r\n/?#@"\\]+)@"#.to_string(),
        ),
        ("aws_secret_key", AWS_SECRET_KEY.to_string()),
    ]
    .into_iter()
    .map(|(kind, pattern)| (kind, Regex::new(&pattern).expect("a redaction pattern")))
    .collect()
});

/// Every pattern at once. Most payloads hold no secret, and one pass of the set finds which
/// patterns can match, for about what a pass of one pattern costs, so only those run. One
/// that finds nothing in the payload as it came finds nothing once others have redacted it
/// either: no pattern matches in or across a placeholder, or starts a word after one.
static PATTERNS: LazyLock<RegexSet> = LazyLock::new(|| {
    RegexSet::new(REDACTIONS.iter().map(|(_, pattern)| pattern.as_str())).expect("the redaction patterns")
});

static PLAIN_ID: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Za-z0-9._:-]{1,128}$").expect("the plain id pattern"));

/// The payload with every secret it recognizes replaced, and the kinds of secret it
/// replaced, sorted. A placeholder already in the payload isn't counted: whatever put it
/// there, this didn't.
pub fn redact(payload: &str) -> (String, Vec<String>) {
    let mut text = payload.to_string();
    let mut kinds = Vec::new();
    for i in PATTERNS.matches(payload).iter() {
        let (kind, pattern) = &REDACTIONS[i];
        if let Some(redacted) = replace(kind, pattern, &text) {
            text = redacted;
            kinds.push(kind.to_string());
        }
    }
    if let Some(redacted) = replace_cut_short(&text, &mut kinds) {
        text = redacted;
    }
    kinds.sort();
    (text, kinds)
}

/// The text with every secret the pattern finds replaced, or None when it replaces none.
fn replace(kind: &str, pattern: &Regex, text: &str) -> Option<String> {
    let mut out = String::new();
    let mut last = 0;
    for captures in pattern.captures_iter(text) {
        let secret = captures.get(1).expect("every pattern captures its secret");
        if kind == "basic_auth" && !basic_credentials(secret.as_str()) {
            continue;
        }
        out.push_str(&text[last..secret.start()]);
        out.push_str(&placeholder(kind, secret.as_str()));
        last = secret.end();
    }
    if last == 0 {
        return None;
    }
    out.push_str(&text[last..]);
    Some(out)
}

/// What a secret of a kind becomes: its kind, the first 12 hex digits of its SHA-256, and a
/// JWT's jti when it has one fit to keep. A password in a URL and Basic credentials get no
/// digest: a person often chooses them, and an unsalted digest of one could be checked
/// against a list of likely passwords.
fn placeholder(kind: &str, secret: &str) -> String {
    if matches!(kind, "url_password" | "basic_auth") {
        return format!("[redacted {kind}]");
    }
    let digest = hex::encode(Sha256::digest(secret.as_bytes()));
    let mut out = format!("[redacted {kind} sha256:{}", &digest[..12]);
    if kind == "jwt"
        && let Some(jti) = jwt_id(secret)
    {
        out.push_str(" jti:");
        out.push_str(&jti);
    }
    out.push(']');
    out
}

/// Whether a Basic authorization holds base64 of user:password, as UTF-8 text, so a word
/// after "Basic" isn't taken for credentials.
fn basic_credentials(secret: &str) -> bool {
    STANDARD
        .decode(secret["Basic".len()..].trim_start_matches(' '))
        .is_ok_and(|decoded| std::str::from_utf8(&decoded).is_ok() && decoded.contains(&b':'))
}

/// A JWT's jti when it's a plain id, so it can't break the placeholder or the JSON around
/// it, and doesn't itself look like a secret, which redacting again would replace.
fn jwt_id(token: &str) -> Option<String> {
    let claims = URL_SAFE_NO_PAD.decode(token.split('.').nth(1)?).ok()?;
    let claims: serde_json::Value = serde_json::from_slice(&claims).ok()?;
    let jti = claims.get("jti")?.as_str()?;
    let secret = REDACTIONS.iter().any(|(_, pattern)| pattern.is_match(jti));
    (PLAIN_ID.is_match(jti) && !secret).then(|| jti.to_string())
}

/// A payload too large to send whole is cut short and marked `"truncated": true`. The SDKs
/// send its start as a string, `prefix`, and the proxy keeps the start of a body, as a
/// string, in `input`, `output` or `content` (mcp.rs, extproc.rs). A credential the cut goes
/// through keeps its start, which can be too short for its pattern: sk_live_ and 20
/// characters isn't a Stripe key. So at the end of each such string, a credential cut short
/// is replaced too: the start of one, followed only by the characters its kind is made of,
/// up to the cut. These are the starts by kind, in the order the patterns run; the first
/// found at a cut is replaced. A private key cut short is its own pattern's.
const CUT_SHORT: [(&str, CutShort); 11] = [
    ("jwt", CutShort::StartsWith("_.-", &["eyJ"])),
    (
        "github_token",
        CutShort::StartsWith("_", &["ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_"]),
    ),
    ("google_api_key", CutShort::StartsWith("_-", &["AIza"])),
    ("google_oauth_token", CutShort::StartsWith("_-", &["ya29."])),
    (
        "slack_token",
        CutShort::StartsWith("-", &["xoxa-", "xoxb-", "xoxp-", "xoxo-", "xoxs-", "xoxr-", "xapp-"]),
    ),
    ("llm_api_key", CutShort::StartsWith("_-", &["sk-"])),
    ("stripe_key", CutShort::StartsWith("", &["sk_live_", "rk_live_"])),
    (
        "bearer_token",
        CutShort::AfterSpaces("._~+/=-", &["Bearer", "bearer", "BEARER"]),
    ),
    // Not checked for user:password, since what's left of it may not decode.
    ("basic_auth", CutShort::AfterSpaces("+/=", &["Basic", "basic", "BASIC"])),
    ("url_password", CutShort::UrlPassword),
    ("aws_secret_key", CutShort::AwsSecretKey),
];

/// How a credential cut short is found, back from the cut. A class is the ASCII letters and
/// digits and the bytes of its string.
enum CutShort {
    /// One of the literals, starting a word, then only bytes of the class up to the cut. The
    /// first such start is taken, so all of what may be the credential is replaced.
    StartsWith(&'static str, &'static [&'static str]),
    /// One of the literals, starting a word, and spaces, then only bytes of the class up to
    /// the cut, as a Bearer token or Basic credentials.
    AfterSpaces(&'static str, &'static [&'static str]),
    /// A password in scheme://user:password@ cut short before its @. As with the whole
    /// pattern, only the password is replaced.
    UrlPassword,
    /// An AWS secret access key, fewer than its 40 characters, after its name and what
    /// AWS_SECRET_KEY allows between, read back from the key. As with the whole pattern,
    /// only the key is replaced.
    AwsSecretKey,
}

impl CutShort {
    /// Where a credential cut short at cut in text starts, if one is.
    fn start(&self, text: &str, cut: usize) -> Option<usize> {
        let bytes = text.as_bytes();
        let class = |extra: &'static str| move |b: u8| b.is_ascii_alphanumeric() || extra.as_bytes().contains(&b);
        match *self {
            CutShort::StartsWith(extra, literals) => {
                let run = run_start(bytes, cut, class(extra));
                literals
                    .iter()
                    .filter_map(|literal| {
                        // The literal ends in the run of the class, or where it starts. It's
                        // ASCII, so it starts on a character.
                        let mut from = run.saturating_sub(literal.len());
                        while !text.is_char_boundary(from) {
                            from += 1;
                        }
                        text[from..cut]
                            .match_indices(*literal)
                            .map(|(at, _)| from + at)
                            .find(|&at| word_start(bytes, at))
                    })
                    .min()
            }
            CutShort::AfterSpaces(extra, literals) => {
                let run = run_start(bytes, cut, class(extra));
                let spaces = run_start(bytes, run, |b| b == b' ');
                if spaces == run {
                    return None;
                }
                literals.iter().find_map(|literal| {
                    let start = spaces.checked_sub(literal.len())?;
                    (bytes[..spaces].ends_with(literal.as_bytes()) && word_start(bytes, start)).then_some(start)
                })
            }
            CutShort::UrlPassword => {
                let run = run_start(bytes, cut, |b| !b" \t\r\n/?#@\"\\".contains(&b));
                if !bytes[..run].ends_with(b"://") {
                    return None;
                }
                let colon = run + bytes[run..cut].iter().position(|&b| b == b':')?;
                (colon + 1 < cut).then_some(colon + 1)
            }
            CutShort::AwsSecretKey => {
                let blank = |b: u8| b == b' ' || b == b'\t';
                let key = run_start(bytes, cut, |b| b.is_ascii_alphanumeric() || b == b'/' || b == b'+');
                if key == cut || cut - key >= 40 {
                    return None;
                }
                let i = run_start(bytes, quote_start(bytes, key), blank);
                if i == 0 || !b":=>".contains(&bytes[i - 1]) {
                    return None;
                }
                let i = quote_start(bytes, run_start(bytes, i - 1, blank));
                AWS_NAMES
                    .iter()
                    .any(|name| bytes[..i].ends_with(name.as_bytes()))
                    .then_some(key)
            }
        }
    }
}

/// The text with a credential cut short at each of its cuts replaced, adding the kinds it
/// replaced to kinds, or None when it replaces none.
fn replace_cut_short(text: &str, kinds: &mut Vec<String>) -> Option<String> {
    let mut out = String::new();
    let mut last = 0;
    for cut in cuts(text) {
        let found = CUT_SHORT
            .iter()
            .find_map(|(kind, shape)| shape.start(text, cut).map(|start| (*kind, start)));
        let Some((kind, start)) = found else { continue };
        out.push_str(&text[last..start]);
        out.push_str(&placeholder(kind, &text[start..cut]));
        last = cut;
        if !kinds.iter().any(|k| k == kind) {
            kinds.push(kind.to_string());
        }
    }
    if last == 0 {
        return None;
    }
    out.push_str(&text[last..]);
    Some(out)
}

/// Where the strings a payload was cut short in end, at their closing quotes: in a JSON
/// object whose "truncated" is true, its "prefix", "input", "output" and "content" that are
/// strings. The proxy says a payload was truncated, not which part it cut, so a part it kept
/// whole as a string, because it isn't JSON, is taken for cut too. The object is read only
/// as far as finding those strings; what it holds isn't checked.
fn cuts(payload: &str) -> Vec<usize> {
    if !payload.contains(r#""truncated""#) {
        return Vec::new();
    }
    let text = payload.as_bytes();
    let mut truncated = false;
    let mut ends = Vec::new();
    let mut i = json_space(text, 0);
    if text.get(i) != Some(&b'{') {
        return Vec::new();
    }
    loop {
        i = json_space(text, i + 1);
        let Some(key_end) = string_end(text, i) else {
            return Vec::new();
        };
        let key = &text[i + 1..key_end];
        i = json_space(text, key_end + 1);
        if text.get(i) != Some(&b':') {
            return Vec::new();
        }
        i = json_space(text, i + 1);
        let Some(end) = value_end(text, i) else {
            return Vec::new();
        };
        match key {
            b"truncated" => truncated = text[i..end] == *b"true",
            b"prefix" | b"input" | b"output" | b"content" if text[i] == b'"' => ends.push(end - 1),
            _ => {}
        }
        i = json_space(text, end);
        match text.get(i) {
            Some(b'}') => break,
            Some(b',') => {}
            _ => return Vec::new(),
        }
    }
    if truncated { ends } else { Vec::new() }
}

/// Where the JSON whitespace from i in text ends.
fn json_space(text: &[u8], mut i: usize) -> usize {
    while i < text.len() && b" \t\r\n".contains(&text[i]) {
        i += 1;
    }
    i
}

/// Where the JSON string at i in text ends, at its closing quote.
fn string_end(text: &[u8], i: usize) -> Option<usize> {
    if text.get(i) != Some(&b'"') {
        return None;
    }
    let mut j = i + 1;
    while j < text.len() {
        match text[j] {
            b'\\' => j += 1,
            b'"' => return Some(j),
            _ => {}
        }
        j += 1;
    }
    None
}

/// Where the JSON value at i in text ends: past a string's closing quote, the bracket that
/// closes what opens at i, or a number or a literal. Only its end is found.
fn value_end(text: &[u8], i: usize) -> Option<usize> {
    match *text.get(i)? {
        b'"' => string_end(text, i).map(|end| end + 1),
        b'{' | b'[' => {
            let mut depth = 0usize;
            let mut j = i;
            while j < text.len() {
                match text[j] {
                    b'"' => j = string_end(text, j)?,
                    b'{' | b'[' => depth += 1,
                    b'}' | b']' => {
                        depth -= 1;
                        if depth == 0 {
                            return Some(j + 1);
                        }
                    }
                    _ => {}
                }
                j += 1;
            }
            None
        }
        _ => {
            let end = text[i..]
                .iter()
                .position(|b| b",}] \t\r\n".contains(b))
                .map_or(text.len(), |n| i + n);
            (end > i).then_some(end)
        }
    }
}

/// Whether a secret can start at i in text, as WORD says: at the start, after a byte that
/// can't be part of a word, or after a whole JSON escape.
fn word_start(text: &[u8], i: usize) -> bool {
    if i == 0 {
        return true;
    }
    let c = text[i - 1];
    if !(c.is_ascii_alphanumeric() || matches!(c, b'_' | b'\\' | b']')) {
        return true;
    }
    if i >= 2 && text[i - 2] == b'\\' && b"bfnrt".contains(&c) {
        return true;
    }
    i >= 6 && text[i - 6] == b'\\' && text[i - 5] == b'u' && text[i - 4..i].iter().all(u8::is_ascii_hexdigit)
}

/// Where the run of bytes of a class that ends at end in text starts.
fn run_start(text: &[u8], mut end: usize, class: impl Fn(u8) -> bool) -> usize {
    while end > 0 && class(text[end - 1]) {
        end -= 1;
    }
    end
}

/// Where a quote that ends at i in text starts, with the backslashes escaping it, or i when
/// there's none.
fn quote_start(text: &[u8], i: usize) -> usize {
    if i > 0 && matches!(text[i - 1], b'"' | b'\'') {
        run_start(text, i - 1, |b| b == b'\\')
    } else {
        i
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;
    use std::time::{Duration, Instant};

    /// Pieces of secrets and of what's around them, for the properties to mix into JSON.
    const PIECES: &[&str] = &[
        "eyJhbGciOiJFUzI1NiJ9.eyJqdGkiOiJ0b2stNyJ9.c2lnbmF0dXJlLW5vdC1yZWFs",
        "-----BEGIN PRIVATE KEY-----\nMIGH\n",
        "-----END PRIVATE KEY-----",
        "Bearer ",
        "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "Basic dXNlcjpwYXNz",
        "postgres://app:hunter2@db",
        "SecretAccessKey=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "\"\\",
        "\n",
        // Escaped, ® and « end in e and b, which start a JWT and a bearer token.
        "®",
        "«",
        "yJhbGciOiJFUzI1NiJ9.eyJqdGkiOiJ0b2stNyJ9.c2lnbmF0dXJlLW5vdC1yZWFs",
        "earer abcdefghijklmnopqrstuvwxyz",
        // Credentials cut short, which a payload marked truncated may end with.
        "sk-proj-AbCd",
        "://app:hunt",
        "SecretAccessKey=je7Mt",
    ];

    /// JSON's non-ASCII escaped as \uXXXX, as Python's json.dumps writes it by default, and
    /// so the SDKs' payloads. serde_json writes it as it is.
    fn ascii(json: &str) -> String {
        let mut out = String::with_capacity(json.len());
        for c in json.chars() {
            if c.is_ascii() {
                out.push(c);
            } else {
                for unit in c.encode_utf16(&mut [0; 2]) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
        }
        out
    }

    // The fuzz target's properties (fuzz/fuzz_targets/redact.rs), on stable Rust, with
    // inputs that mix secret-shaped pieces into JSON so the patterns are exercised, written
    // as serde_json writes it and as the SDKs do, and marked truncated or not.
    proptest! {
        #[test]
        fn json_stays_json_and_redacting_twice_changes_nothing(
            pieces in proptest::collection::vec(
                prop_oneof![
                    1 => any::<String>(),
                    4 => proptest::sample::select(PIECES).prop_map(str::to_string),
                ],
                0..12,
            )
        ) {
            let text: String = pieces.concat();
            let nested = serde_json::json!({"text": text}).to_string();
            for truncated in [false, true] {
                let doc = |nested: String| {
                    let mut doc = serde_json::json!({"output": text, "nested": nested});
                    if truncated {
                        doc["truncated"] = true.into(); // so the output is taken for cut short
                    }
                    doc.to_string()
                };
                let plain = doc(nested.clone());
                let escaped = ascii(&doc(ascii(&nested)));
                for payload in [plain, escaped] {
                    let (out, kinds) = redact(&payload);
                    let doc: serde_json::Value = serde_json::from_str(&out)
                        .map_err(|e| TestCaseError::fail(format!("not JSON, {e}: {out}")))?;
                    let nested = doc["nested"].as_str().unwrap_or_default();
                    prop_assert!(
                        serde_json::from_str::<serde_json::Value>(nested).is_ok(),
                        "the JSON in it isn't JSON: {out}"
                    );
                    prop_assert_eq!(&redact(&out).0, &out);
                    for kind in &kinds {
                        let digested = format!("[redacted {kind} sha256:");
                        let bare = format!("[redacted {kind}]");
                        prop_assert!(out.contains(&digested) || out.contains(&bare));
                    }
                    // Running only the patterns the set finds in the payload as it came misses
                    // nothing that running every pattern would replace.
                    let mut every = payload.clone();
                    for (kind, pattern) in REDACTIONS.iter() {
                        if let Some(redacted) = replace(kind, pattern, &every) {
                            every = redacted;
                        }
                    }
                    if let Some(redacted) = replace_cut_short(&every, &mut Vec::new()) {
                        every = redacted;
                    }
                    prop_assert_eq!(&every, &out);
                }
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
            (
                "every pattern",
                r" eyJ ghp_ AIza ya29. xoxb- sk- _live_ Bearer Basic \\u00ab ://a:b secretAccessKey=".repeat(1 << 14),
            ),
            // A cut at the end of a run that holds a literal everywhere, but never where a
            // word starts, and a cut, with a credential cut short at it, many times over.
            (
                "a cut through a run",
                format!(r#"{{"truncated":true,"prefix":"{}"}}"#, "aeyJ".repeat(1 << 20)),
            ),
            (
                "many cuts",
                format!(r#"{{"truncated":true{}}}"#, r#","input":"sk-""#.repeat(1 << 18)),
            ),
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
