package event

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"regexp"
	"sort"
	"strings"
)

// Redacted lists the kinds of secret a payload's placeholders name, sorted. The signer sets
// it, replacing any value the sender gave, and it's signed with the rest of the event.
const Redacted = "wake.payload.redacted"

// A payload is kept in an append only log that nothing can erase, so a credential an agent
// passed in a message, a tool argument or memory would be readable there for as long as the
// log is kept. Before an event is signed, Redact replaces every credential it recognizes in
// the payload with a placeholder naming its kind and a digest:
//
//	[redacted jwt sha256:56aec5e5c6aa jti:tok-7]
//
// Only formats that identify themselves are recognized, so ordinary text is never taken for
// a secret: JWTs, private key blocks, GitHub, Google, Slack, LLM provider and Stripe keys,
// and bearer tokens. A JWT's placeholder keeps its jti, when that's a plain id, so the token
// can be matched to wake.token.id on the proxy's events. No pattern matches a quote, so a
// JSON payload stays JSON. wake-proxy does the same for the events it signs, and
// testdata/redaction.json holds both to the same output. See docs/events.md.
//
// Go's regexp runs in time linear in the payload, whatever an agent sends.
var redactions = []struct {
	kind    string
	pattern *regexp.Regexp
}{
	{"private_key", regexp.MustCompile(privateKey)},
	{"jwt", regexp.MustCompile(`eyJ[A-Za-z0-9_-]{4,}\.eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{16,}`)},
	{"github_token", regexp.MustCompile(`(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,}`)},
	{"google_api_key", regexp.MustCompile(`AIza[0-9A-Za-z_-]{35}`)},
	{"google_oauth_token", regexp.MustCompile(`ya29\.[0-9A-Za-z_-]{20,}`)},
	{"slack_token", regexp.MustCompile(`xox[abposr]-[0-9A-Za-z-]{10,}`)},
	{"llm_api_key", regexp.MustCompile(`sk-ant-[0-9A-Za-z_-]{20,}|sk-proj-[0-9A-Za-z_-]{20,}|sk-[0-9A-Za-z]{32,}`)},
	{"stripe_key", regexp.MustCompile(`(?:sk|rk)_live_[0-9A-Za-z]{24,}`)},
	{"bearer_token", regexp.MustCompile(`(?:Bearer|bearer|BEARER) +[A-Za-z0-9._~+/-]{20,}=*`)},
}

// A private key block, to its END line, or, cut short, as far as its body runs. The body is
// base64, header lines, spaces and line breaks, raw or escaped once or twice for JSON, so a
// key in a string inside a string is found whole and never runs past a quote.
const (
	keyBody    = `(?:[A-Za-z0-9+/= \t:,.-]|\\r|\\n|\\\\r|\\\\n|\r|\n)`
	keyBegin   = `-----BEGIN [A-Z ]{0,20}PRIVATE KEY-----`
	keyEnd     = `-----END [A-Z ]{0,20}PRIVATE KEY-----`
	privateKey = keyBegin + keyBody + `*?` + keyEnd + `|` + keyBegin + keyBody + `*`
)

var (
	placeholder = regexp.MustCompile(`\[redacted ([a-z_]+) sha256:[0-9a-f]{12}`)
	plainID     = regexp.MustCompile(`^[A-Za-z0-9._:-]{1,128}$`)
)

// Redact returns payload with every secret it recognizes replaced, and the kinds of secret
// its placeholders name, sorted, whether replaced now or by an earlier hop.
func Redact(payload string) (string, []string) {
	for _, r := range redactions {
		kind := r.kind
		payload = r.pattern.ReplaceAllStringFunc(payload, func(secret string) string {
			sum := sha256.Sum256([]byte(secret))
			out := "[redacted " + kind + " sha256:" + hex.EncodeToString(sum[:])[:12]
			if kind == "jwt" {
				if jti := jwtID(secret); jti != "" {
					out += " jti:" + jti
				}
			}
			return out + "]"
		})
	}
	seen := map[string]bool{}
	var kinds []string
	for _, m := range placeholder.FindAllStringSubmatch(payload, -1) {
		if !seen[m[1]] {
			seen[m[1]] = true
			kinds = append(kinds, m[1])
		}
	}
	sort.Strings(kinds)
	return payload, kinds
}

// jwtID is a JWT's jti when it's a plain id, so it can't break the placeholder or the JSON
// around it; otherwise empty.
func jwtID(token string) string {
	parts := strings.Split(token, ".")
	claims, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
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
	return jti
}
