package event

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"iter"
	"regexp"
	"slices"
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
// name that says so. A credential in any other form is signed and logged as it came. A
// JWT's placeholder keeps its jti, when that's a plain id, so the token can be matched to
// wake.token.id on the proxy's events. A password in a URL and Basic credentials get no
// digest (see placeholder). Where a payload was cut short, a credential the cut went
// through is replaced too (see cutShort). What's replaced never holds a quote or part of an
// escape, so a JSON payload stays JSON, however deep its strings nest JSON. wake-proxy does
// the same for the events it signs, and testdata/redaction.json holds both to the same
// output. See docs/events.md.
//
// Each pattern captures the secret it replaces; the rest of a match is context, kept as it
// is.
var redactions = []redaction{
	{"private_key", nil, regexp.MustCompile(`(` + privateKey + `)`), nil},
	{"jwt", []string{"eyJ"}, word(`eyJ[A-Za-z0-9_-]{4,}\.eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{16,}`),
		secret{{lit("eyJ"), atLeast(b64url, 4), lit(".eyJ"), atLeast(b64url, 4), lit("."), atLeast(b64url, 16)}}},
	{"github_token", []string{"ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_"}, word(`(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,}`),
		secret{{lit("ghp_", "gho_", "ghu_", "ghs_", "ghr_"), atLeast(alnum, 36)}, {lit("github_pat_"), atLeast(wordChar, 22)}}},
	{"google_api_key", []string{"AIza"}, word(`AIza[0-9A-Za-z_-]{35}`),
		secret{{lit("AIza"), exactly(b64url, 35)}}},
	{"google_oauth_token", []string{"ya29."}, word(`ya29\.[0-9A-Za-z_-]{20,}`),
		secret{{lit("ya29."), atLeast(b64url, 20)}}},
	{"slack_token", []string{"xox", "xapp-"}, word(`xox[abposr]-[0-9A-Za-z-]{10,}|xapp-[0-9A-Za-z-]{10,}`),
		secret{{lit("xoxa-", "xoxb-", "xoxp-", "xoxo-", "xoxs-", "xoxr-", "xapp-"), atLeast(slackChar, 10)}}},
	{"llm_api_key", []string{"sk-"}, word(`sk-(?:ant|proj|svcacct|admin)-[0-9A-Za-z_-]{20,}|sk-[0-9A-Za-z]{32,}`),
		secret{{lit("sk-ant-", "sk-proj-", "sk-svcacct-", "sk-admin-"), atLeast(b64url, 20)}, {lit("sk-"), atLeast(alnum, 32)}}},
	{"stripe_key", []string{"sk_live_", "rk_live_"}, word(`(?:sk|rk)_live_[0-9A-Za-z]{24,}`),
		secret{{lit("sk_live_", "rk_live_"), atLeast(alnum, 24)}}},
	{"bearer_token", []string{"Bearer ", "bearer ", "BEARER "}, word(`(?:Bearer|bearer|BEARER) +[A-Za-z0-9._~+/-]{20,}=*`),
		secret{{lit("Bearer", "bearer", "BEARER"), atLeast(space, 1), atLeast(bearerChar, 20), atLeast(equals, 0)}}},
	// Only when basicCredentials finds user:password in it.
	{"basic_auth", []string{"Basic ", "basic ", "BASIC "}, word(`(?:Basic|basic|BASIC) +[A-Za-z0-9+/]+={0,2}`),
		secret{{lit("Basic", "basic", "BASIC"), atLeast(space, 1), atLeast(b64, 1), upTo(equals, 2)}}},
	// The password in scheme://user:password@host.
	{"url_password", nil, regexp.MustCompile(`://[^ \t\r\n/?#@:"\\]*:([^ \t\r\n/?#@"\\]+)@`), nil},
	{"aws_secret_key", awsNames, regexp.MustCompile(awsSecretKey), nil},
}

// A redaction finds one kind of secret. Go's regexp runs in time linear in the payload,
// whatever an agent sends, but it has no DFA: it skips ahead only to a literal a pattern
// starts with, and steps through the rest a byte at a time. A pattern behind a word start
// can't skip, so it stepped through every byte of a payload that held its literals, many
// times slower than one that can. A secret that starts a word is matched by hand instead,
// from the steps its regexp takes, and only where one of its literals is: see secrets. The
// regexp still says what's matched, as wake-proxy matches it, and the tests hold the steps
// to it.
type redaction struct {
	kind string
	// Every match holds one of these, so a pattern runs only on a payload that does, and a
	// secret matched by hand starts with one.
	literals []string
	pattern  *regexp.Regexp
	// For a pattern word made, the secret it captures, as the steps its regexp takes.
	secret secret
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

// The names awsSecretKey starts with.
var awsNames = []string{"secret_access_key", "SECRET_ACCESS_KEY", "SecretAccessKey", "secretAccessKey"}

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

// A secret is the alternatives its regexp tries, in order, each the steps a match takes.
type secret [][]step

// A step matches one of a few literals, or a run of a class of bytes from min to max long,
// with no max if it's -1.
type step struct {
	literals []string
	class    *[256]bool
	min, max int
}

func lit(literals ...string) step      { return step{literals: literals} }
func atLeast(c *[256]bool, n int) step { return step{class: c, min: n, max: -1} }
func exactly(c *[256]bool, n int) step { return step{class: c, min: n, max: n} }
func upTo(c *[256]bool, n int) step    { return step{class: c, max: n} }

// The classes of bytes that secrets, words and escapes are made of.
var (
	alnum      = class(`[0-9A-Za-z]`)
	wordChar   = class(`[0-9A-Za-z_]`)
	b64url     = class(`[0-9A-Za-z_-]`)
	slackChar  = class(`[0-9A-Za-z-]`)
	bearerChar = class(`[A-Za-z0-9._~+/-]`)
	b64        = class(`[A-Za-z0-9+/]`)
	space      = class(` `)
	equals     = class(`=`)
	hexDigit   = class(`[0-9A-Fa-f]`)
	blank      = class(`[ \t]`)
	backslash  = class(`\\`)
	// A password in a URL, which may be any character but these.
	urlChar = func() *[256]bool {
		var c [256]bool
		for b := range c {
			c[b] = strings.IndexByte(" \t\r\n/?#@\"\\", byte(b)) < 0
		}
		return &c
	}()
)

// class is the bytes a regexp class of ASCII characters matches.
func class(set string) *[256]bool {
	re := regexp.MustCompile(`^` + set + `$`)
	var c [256]bool
	for b := range utf8.RuneSelf {
		c[b] = re.MatchString(string(rune(b)))
	}
	return &c
}

var plainID = regexp.MustCompile(`^[A-Za-z0-9._:-]{1,128}$`)

// Redact returns payload with every secret it recognizes replaced, and the kinds of secret
// it replaced, sorted. A placeholder already in the payload isn't counted: whatever put it
// there, this didn't.
func Redact(payload string) (string, []string) {
	var kinds []string
	for _, r := range redactions {
		var out strings.Builder
		last := 0
		for start, end := range r.secrets(payload) {
			secret := payload[start:end]
			if r.kind == "basic_auth" && !basicCredentials(secret) {
				continue
			}
			out.WriteString(payload[last:start])
			out.WriteString(placeholder(r.kind, secret))
			last = end
		}
		if last == 0 {
			continue // nothing replaced
		}
		out.WriteString(payload[last:])
		payload = out.String()
		kinds = append(kinds, r.kind)
	}
	payload, kinds = redactCuts(payload, kinds)
	sort.Strings(kinds)
	return payload, kinds
}

// secrets yields where each secret r's pattern captures in payload starts and ends, in
// order.
func (r redaction) secrets(payload string) iter.Seq2[int, int] {
	return func(yield func(int, int) bool) {
		if r.secret == nil {
			if !holdsAny(payload, r.literals) {
				return
			}
			for _, m := range r.pattern.FindAllStringSubmatchIndex(payload, -1) {
				if !yield(m[2], m[3]) {
					return
				}
			}
			return
		}
		// As the regexp finds them: the first place a word starts and the secret matches,
		// then the first after that match, and so on.
		starts := scan(payload, r.literals)
		runs := r.secret.runs()
		after := 0 // where the last match ended
		for i := starts.next(0); i >= 0; i = starts.next(i + 1) {
			if !wordStart(payload, i, after) {
				continue
			}
			if end := r.secret.match(payload, i, runs); end >= 0 {
				if !yield(i, end) {
					return
				}
				after = end
			}
		}
	}
}

// wordStart reports whether word's prefix can end at i in text, starting no sooner than
// from, where its regexp looks for its next match: i is 0, or before it is a byte that
// can't be part of a word, or a whole escape. A byte of a character past ASCII can't be,
// and from is never inside one, since every secret is ASCII.
func wordStart(text string, i, from int) bool {
	if i == 0 {
		return true
	}
	c := text[i-1]
	if !wordChar[c] && c != '\\' && c != ']' {
		return i-1 >= from
	}
	if i-2 >= from && text[i-2] == '\\' && strings.IndexByte("bfnrt", c) >= 0 {
		return true
	}
	return i-6 >= from && text[i-6] == '\\' && text[i-5] == 'u' &&
		hexDigit[text[i-4]] && hexDigit[text[i-3]] && hexDigit[text[i-2]] && hexDigit[c]
}

// match returns where the secret that starts at i in text ends, or -1 if none does. It
// tries each alternative in turn, as the regexp does, and each step takes all it can, as
// the regexp's repetitions do. What follows a run is never of its class, so a run cut
// shorter would never let the rest match, and the regexp's other choices all fail.
func (s secret) match(text string, i int, runs [][]run) int {
alternatives:
	for a, steps := range s {
		at := i
		for k, st := range steps {
			if st.class == nil {
				n := prefixLen(text[at:], st.literals)
				if n < 0 {
					continue alternatives
				}
				at += n
				continue
			}
			n := runs[a][k].end(text, at, st.class) - at
			if n < st.min {
				continue alternatives
			}
			if st.max >= 0 {
				n = min(n, st.max)
			}
			at += n
		}
		return at
	}
	return -1
}

// runs returns a run for each step of s to keep.
func (s secret) runs() [][]run {
	runs := make([][]run, len(s))
	for a, steps := range s {
		runs[a] = make([]run, len(steps))
	}
	return runs
}

// The run of a class of bytes a step measured last: text[from:to] is all of the class, and
// text[to] isn't. A step is asked where runs end at places that only grow as the scan goes
// on, so keeping its last run, it measures each byte once, however many starts share it,
// and a payload of starts all in one run still takes linear time.
type run struct {
	from, to int
	measured bool
}

// end returns where the run of class from i in text ends.
func (r *run) end(text string, i int, class *[256]bool) int {
	if !r.measured || i < r.from || i > r.to {
		r.from, r.to, r.measured = i, i, true
		for r.to < len(text) && class[text[r.to]] {
			r.to++
		}
	}
	return r.to
}

// prefixLen returns the length of the one of literals text starts with, or -1 for none.
func prefixLen(text string, literals []string) int {
	for _, l := range literals {
		if strings.HasPrefix(text, l) {
			return len(l)
		}
	}
	return -1
}

// A literalScan finds where any of a few literals is in a text, in order. It looks for each
// again only once the scan has passed where it found it last.
type literalScan struct {
	text  string
	these []string
	at    []int // where each was found last, or -1 when there are no more
}

func scan(text string, these []string) *literalScan {
	l := &literalScan{text: text, these: these, at: make([]int, len(these))}
	for k, s := range these {
		l.at[k] = strings.Index(text, s)
	}
	return l
}

// next returns the first place at or after i where one of the literals is, or -1.
func (l *literalScan) next(i int) int {
	first := -1
	for k, s := range l.these {
		if l.at[k] >= 0 && l.at[k] < i {
			l.at[k] = -1
			if j := strings.Index(l.text[i:], s); j >= 0 {
				l.at[k] = i + j
			}
		}
		if l.at[k] >= 0 && (first < 0 || l.at[k] < first) {
			first = l.at[k]
		}
	}
	return first
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

// A payload too large to send whole is cut short and marked "truncated": true. The SDKs
// send its start as a string, "prefix", and the proxy keeps the start of a body, as a
// string, in "input", "output" or "content". A credential the cut goes through keeps its
// start, which can be too short for its pattern: sk_live_ and 20 characters isn't a Stripe
// key. So at the end of each such string, a credential cut short is replaced too: the start
// of one, followed only by the characters its kind is made of, up to the cut. These are the
// starts by kind, in the order the patterns run; the first found at a cut is replaced. A
// private key cut short is its own pattern's.
var cutShort = []struct {
	kind string
	// start returns where a credential of the kind cut short at cut in text starts, or -1.
	start func(text string, cut int) int
}{
	{"jwt", startsWith(class(`[A-Za-z0-9_.-]`), "eyJ")},
	{"github_token", startsWith(wordChar, "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_")},
	{"google_api_key", startsWith(b64url, "AIza")},
	{"google_oauth_token", startsWith(b64url, "ya29.")},
	{"slack_token", startsWith(slackChar, "xoxa-", "xoxb-", "xoxp-", "xoxo-", "xoxs-", "xoxr-", "xapp-")},
	{"llm_api_key", startsWith(b64url, "sk-")},
	{"stripe_key", startsWith(alnum, "sk_live_", "rk_live_")},
	{"bearer_token", afterSpaces(class(`[A-Za-z0-9._~+/=-]`), "Bearer", "bearer", "BEARER")},
	// Not checked for user:password, since what's left of it may not decode.
	{"basic_auth", afterSpaces(class(`[A-Za-z0-9+/=]`), "Basic", "basic", "BASIC")},
	{"url_password", urlPasswordCut},
	{"aws_secret_key", awsSecretKeyCut},
}

// redactCuts replaces a credential cut short at each of payload's cuts, and adds the kinds
// it replaced to kinds.
func redactCuts(payload string, kinds []string) (string, []string) {
	var out strings.Builder
	last := 0
	for _, cut := range cuts(payload) {
		for _, c := range cutShort {
			start := c.start(payload, cut)
			if start < 0 {
				continue
			}
			out.WriteString(payload[last:start])
			out.WriteString(placeholder(c.kind, payload[start:cut]))
			last = cut
			if !slices.Contains(kinds, c.kind) {
				kinds = append(kinds, c.kind)
			}
			break
		}
	}
	if last == 0 {
		return payload, kinds // nothing replaced
	}
	out.WriteString(payload[last:])
	return out.String(), kinds
}

// cuts returns where the strings a payload was cut short in end, at their closing quotes:
// in a JSON object whose "truncated" is true, its "prefix", "input", "output" and "content"
// that are strings. The proxy says a payload was truncated, not which part it cut, so a part
// it kept whole as a string, because it isn't JSON, is taken for cut too. The object is read
// only as far as finding those strings; what it holds isn't checked.
func cuts(payload string) []int {
	if !strings.Contains(payload, `"truncated"`) {
		return nil
	}
	truncated := false
	var ends []int
	i := jsonSpace(payload, 0)
	if i == len(payload) || payload[i] != '{' {
		return nil
	}
	for {
		i = jsonSpace(payload, i+1)
		keyEnd := stringEnd(payload, i)
		if keyEnd < 0 {
			return nil
		}
		key := payload[i+1 : keyEnd]
		i = jsonSpace(payload, keyEnd+1)
		if i == len(payload) || payload[i] != ':' {
			return nil
		}
		i = jsonSpace(payload, i+1)
		end := valueEnd(payload, i)
		if end < 0 {
			return nil
		}
		switch key {
		case "truncated":
			truncated = payload[i:end] == "true"
		case "prefix", "input", "output", "content":
			if payload[i] == '"' {
				ends = append(ends, end-1)
			}
		}
		i = jsonSpace(payload, end)
		if i < len(payload) && payload[i] == '}' {
			break
		}
		if i == len(payload) || payload[i] != ',' {
			return nil
		}
	}
	if !truncated {
		return nil
	}
	return ends
}

// jsonSpace returns where the JSON whitespace from i in text ends.
func jsonSpace(text string, i int) int {
	for i < len(text) && strings.IndexByte(" \t\r\n", text[i]) >= 0 {
		i++
	}
	return i
}

// stringEnd returns where the JSON string at i in text ends, at its closing quote, or -1.
func stringEnd(text string, i int) int {
	if i >= len(text) || text[i] != '"' {
		return -1
	}
	for j := i + 1; j < len(text); j++ {
		switch text[j] {
		case '\\':
			j++
		case '"':
			return j
		}
	}
	return -1
}

// valueEnd returns where the JSON value at i in text ends, or -1: past a string's closing
// quote, the bracket that closes what opens at i, or a number or a literal. Only its end is
// found.
func valueEnd(text string, i int) int {
	if i >= len(text) {
		return -1
	}
	switch text[i] {
	case '"':
		if end := stringEnd(text, i); end >= 0 {
			return end + 1
		}
		return -1
	case '{', '[':
		depth := 0
		for j := i; j < len(text); j++ {
			switch text[j] {
			case '"':
				if j = stringEnd(text, j); j < 0 {
					return -1
				}
			case '{', '[':
				depth++
			case '}', ']':
				if depth--; depth == 0 {
					return j + 1
				}
			}
		}
		return -1
	}
	j := i
	for j < len(text) && strings.IndexByte(",}] \t\r\n", text[j]) < 0 {
		j++
	}
	if j == i {
		return -1
	}
	return j
}

// startsWith finds a credential cut short that starts a word with one of literals, then
// holds only bytes of class up to the cut. It takes the first such start, so all of what may
// be the credential is replaced.
func startsWith(class *[256]bool, literals ...string) func(string, int) int {
	return func(text string, cut int) int {
		run := runStart(text, cut, class)
		first := -1
		for _, l := range literals {
			// The literal ends in the run of class, or where it starts.
			for from := max(run-len(l), 0); ; {
				j := strings.Index(text[from:cut], l)
				if j < 0 || (first >= 0 && from+j >= first) {
					break
				}
				if wordStart(text, from+j, 0) {
					first = from + j
					break
				}
				from += j + 1
			}
		}
		return first
	}
}

// afterSpaces finds a credential cut short that starts a word with one of literals and
// spaces, then holds only bytes of class up to the cut, as a Bearer token or Basic
// credentials do.
func afterSpaces(class *[256]bool, literals ...string) func(string, int) int {
	return func(text string, cut int) int {
		run := runStart(text, cut, class)
		spaces := runStart(text, run, space)
		if spaces == run {
			return -1
		}
		for _, l := range literals {
			if start := spaces - len(l); strings.HasSuffix(text[:spaces], l) && wordStart(text, start, 0) {
				return start
			}
		}
		return -1
	}
}

// urlPasswordCut finds a password in scheme://user:password@ cut short before its @. As
// with the whole pattern, only the password is replaced.
func urlPasswordCut(text string, cut int) int {
	run := runStart(text, cut, urlChar)
	if !strings.HasSuffix(text[:run], "://") {
		return -1
	}
	colon := strings.IndexByte(text[run:cut], ':')
	if colon < 0 || run+colon+1 == cut {
		return -1
	}
	return run + colon + 1
}

// awsSecretKeyCut finds an AWS secret access key cut short, fewer than its 40 characters,
// after its name and what awsSecretKey allows between, read back from the key. As with the
// whole pattern, only the key is replaced.
func awsSecretKeyCut(text string, cut int) int {
	key := runStart(text, cut, b64)
	if key == cut || cut-key >= 40 {
		return -1
	}
	i := runStart(text, quoteStart(text, key), blank)
	if i == 0 || strings.IndexByte(":=>", text[i-1]) < 0 {
		return -1
	}
	i = quoteStart(text, runStart(text, i-1, blank))
	for _, name := range awsNames {
		if strings.HasSuffix(text[:i], name) {
			return key
		}
	}
	return -1
}

// quoteStart returns where a quote that ends at i in text starts, with the backslashes
// escaping it, or i when there's none.
func quoteStart(text string, i int) int {
	if i > 0 && (text[i-1] == '"' || text[i-1] == '\'') {
		return runStart(text, i-1, backslash)
	}
	return i
}

// runStart returns where the run of bytes of class that ends at end in text starts.
func runStart(text string, end int, class *[256]bool) int {
	for end > 0 && class[text[end-1]] {
		end--
	}
	return end
}

// placeholder is what a secret of kind becomes: its kind, the first 12 hex digits of its
// SHA-256, and a JWT's jti when it has one fit to keep. A password in a URL and Basic
// credentials get no digest: a person often chooses them, and an unsalted digest of one
// could be checked against a list of likely passwords.
func placeholder(kind, secret string) string {
	if kind == "url_password" || kind == "basic_auth" {
		return "[redacted " + kind + "]"
	}
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
	if len(parts) < 2 {
		return "" // cut short before its claims
	}
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
