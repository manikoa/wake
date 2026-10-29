package event

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"regexp"
	"sort"
	"strings"
	"unicode/utf8"
)

// Redacted lists the kinds of secret the signer replaced in a payload, sorted. The signer
// sets it, replacing any value the sender gave, and it's signed with the rest of the event.
const Redacted = "wake.payload.redacted"

// A payload is kept in an append only log that nothing can erase, so a credential an agent
// passed in a message, a tool argument or memory would be readable there for as long as the
// log is kept. Before an event is signed, Redact replaces every credential it recognizes in
// the payload with a placeholder naming its kind and a digest:
//
//	[redacted jwt sha256:56aec5e5c6aa jti:tok-7]
//
// Only formats that identify themselves are recognized, so ordinary text is never taken for
// a secret: private key blocks, JWTs, GitHub, Google, Slack, LLM provider and Stripe keys,
// bearer tokens, Basic credentials, passwords in URLs, and AWS secret keys right after a
// name that says so. A JWT's placeholder keeps its jti, when that's a plain id, so the token
// can be matched to wake.token.id on the proxy's events. What's replaced never holds a quote
// or part of an escape, so a JSON payload stays JSON, however deep its strings nest JSON.
// wake-proxy does the same for the events it signs, and testdata/redaction.json holds both
// to the same output. See docs/events.md.
//
// Each pattern captures the secret it replaces; the rest of a match is context, kept as it
// is. Go's regexp runs in time linear in the payload, whatever an agent sends.
var redactions = []struct {
	kind string
	// Go's regexp has no DFA, so a pattern that can't skip ahead to a literal it starts
	// with, as one behind a word boundary can't, runs many times slower. A pattern with
	// literals runs only on a payload that holds one of them, as every match does.
	literals []string
	pattern  *regexp.Regexp
}{
	{"private_key", nil, regexp.MustCompile(`(` + privateKey + `)`)},
	{"jwt", []string{"eyJ"}, word(`eyJ[A-Za-z0-9_-]{4,}\.eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{16,}`)},
	{"github_token", []string{"ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_"}, word(`(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,}`)},
	{"google_api_key", []string{"AIza"}, word(`AIza[0-9A-Za-z_-]{35}`)},
	{"google_oauth_token", []string{"ya29."}, word(`ya29\.[0-9A-Za-z_-]{20,}`)},
	{"slack_token", []string{"xox", "xapp-"}, word(`xox[abposr]-[0-9A-Za-z-]{10,}|xapp-[0-9A-Za-z-]{10,}`)},
	{"llm_api_key", []string{"sk-"}, word(`sk-(?:ant|proj|svcacct|admin)-[0-9A-Za-z_-]{20,}|sk-[0-9A-Za-z]{32,}`)},
	{"stripe_key", []string{"_live_"}, word(`(?:sk|rk)_live_[0-9A-Za-z]{24,}`)},
	{"bearer_token", []string{"Bearer ", "bearer ", "BEARER "}, word(`(?:Bearer|bearer|BEARER) +[A-Za-z0-9._~+/-]{20,}=*`)},
	// Only when basicCredentials finds user:password in it.
	{"basic_auth", []string{"Basic ", "basic ", "BASIC "}, word(`(?:Basic|basic|BASIC) +[A-Za-z0-9+/]+={0,2}`)},
	// The password in scheme://user:password@host.
	{"url_password", nil, regexp.MustCompile(`://[^ \t\r\n/?#@:"\\]*:([^ \t\r\n/?#@"\\]+)@`)},
	{"aws_secret_key", []string{"secret_access_key", "SECRET_ACCESS_KEY", "SecretAccessKey", "secretAccessKey"}, regexp.MustCompile(awsSecretKey)},
}

// A private key block, PEM or OpenPGP, to its END line, or, cut short, as far as its body
// runs. The body is base64, header lines, spaces and line breaks, raw or escaped for JSON
// at any depth, so a key in a string inside strings is found whole and never runs past a
// quote. So are the escapes some encoders write for base64's + / and =: .NET writes + as
// \u002B, PHP / as \/.
const (
	keyBody    = `(?:[A-Za-z0-9+/= \t:,.-]|\\+(?:[rn/]|u00(?:2[BbFf]|3[Dd]))|\r|\n)`
	keyBegin   = `-----BEGIN [A-Z ]{0,20}PRIVATE KEY(?: BLOCK)?-----`
	keyEnd     = `-----END [A-Z ]{0,20}PRIVATE KEY(?: BLOCK)?-----`
	privateKey = keyBegin + keyBody + `*?` + keyEnd + `|` + keyBegin + keyBody + `*`
)

// An AWS secret access key is 40 characters of random base64, so it's only taken for one
// right after a name that says so, then =, : or > (STS's XML), with any quotes and spaces
// between, and with no more base64 after it.
const awsSecretKey = `(?:secret_access_key|SECRET_ACCESS_KEY|SecretAccessKey|secretAccessKey)` +
	`(?:\\*["'])?[ \t]*[:=>][ \t]*(?:\\*["'])?([A-Za-z0-9/+]{40})(?:[^A-Za-z0-9/+=]|$)`

// word compiles a pattern for a secret that starts a word: at the start of the payload,
// after a character that can't be part of one, or after a whole JSON escape such as \n or
// \u00e9, at any depth. Never inside a word, so task- or disk- doesn't start an sk- key.
// Never inside an escape: SDKs escape non-ASCII, so ® then yJ… arrives as \u00aeyJ…, which
// holds no JWT, and replacing from its e would leave \u00a, which isn't JSON. Never right
// after a backslash, which starts one. And never after the ] that ends a placeholder, so a
// secret right after one a later pattern leaves isn't found by redacting again.
func word(secret string) *regexp.Regexp {
	return regexp.MustCompile(`(?:^|[^0-9A-Za-z_\\\]]|\\+[bfnrt]|\\+u[0-9A-Fa-f]{4})(` + secret + `)`)
}

var plainID = regexp.MustCompile(`^[A-Za-z0-9._:-]{1,128}$`)

// Redact returns payload with every secret it recognizes replaced, and the kinds of secret
// it replaced, sorted. A placeholder already in the payload isn't counted: whatever put it
// there, this didn't.
func Redact(payload string) (string, []string) {
	var kinds []string
	for _, r := range redactions {
		if !holdsAny(payload, r.literals) {
			continue
		}
		var out strings.Builder
		last := 0
		for _, m := range r.pattern.FindAllStringSubmatchIndex(payload, -1) {
			secret := payload[m[2]:m[3]]
			if r.kind == "basic_auth" && !basicCredentials(secret) {
				continue
			}
			out.WriteString(payload[last:m[2]])
			out.WriteString(placeholder(r.kind, secret))
			last = m[3]
		}
		if last == 0 {
			continue // nothing replaced
		}
		out.WriteString(payload[last:])
		payload = out.String()
		kinds = append(kinds, r.kind)
	}
	sort.Strings(kinds)
	return payload, kinds
}

// holdsAny reports whether s holds one of literals, or there are none to look for.
func holdsAny(s string, literals []string) bool {
	if literals == nil {
		return true
	}
	for _, l := range literals {
		if strings.Contains(s, l) {
			return true
		}
	}
	return false
}

// placeholder is what a secret of kind becomes: its kind, the first 12 hex digits of its
// SHA-256, and a JWT's jti when it has one fit to keep.
func placeholder(kind, secret string) string {
	sum := sha256.Sum256([]byte(secret))
	out := "[redacted " + kind + " sha256:" + hex.EncodeToString(sum[:])[:12]
	if kind == "jwt" {
		if jti := jwtID(secret); jti != "" {
			out += " jti:" + jti
		}
	}
	return out + "]"
}

// basicCredentials reports whether a Basic authorization holds base64 of user:password, as
// UTF-8 text, so a word after "Basic" isn't taken for credentials.
func basicCredentials(secret string) bool {
	decoded, err := base64.StdEncoding.Strict().DecodeString(strings.TrimLeft(secret[len("Basic"):], " "))
	return err == nil && utf8.Valid(decoded) && bytes.IndexByte(decoded, ':') >= 0
}

// jwtID is a JWT's jti when it's a plain id, so it can't break the placeholder or the JSON
// around it, and doesn't itself look like a secret, which redacting again would replace;
// otherwise empty. The claims are read as strictly as wake-proxy reads them: base64 with no
// stray bits in its last character, and UTF-8.
func jwtID(token string) string {
	parts := strings.Split(token, ".")
	claims, err := base64.RawURLEncoding.Strict().DecodeString(parts[1])
	if err != nil || !utf8.Valid(claims) {
		return ""
	}
	var c map[string]any
	if json.Unmarshal(claims, &c) != nil {
		return ""
	}
	jti, _ := c["jti"].(string)
	if !plainID.MatchString(jti) {
		return ""
	}
	for _, r := range redactions {
		if r.pattern.MatchString(jti) {
			return ""
		}
	}
	return jti
}
