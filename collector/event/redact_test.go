package event

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
	"unicode/utf16"
	"unicode/utf8"

	"go.opentelemetry.io/collector/pdata/ptrace"
	"golang.org/x/mod/sumdb/note"

	wakelog "github.com/manikoa/wake/log"
)

// testdata/redaction.json holds payloads and what each signer must make of them: the
// payload with its secrets replaced, and the kinds its placeholders name. wake-proxy is held
// to the same file.
type redactionVector struct {
	Name  string   `json:"name"`
	In    string   `json:"in"`
	Out   string   `json:"out"`
	Kinds []string `json:"kinds"`
}

func redactionVectors(t *testing.T) []redactionVector {
	t.Helper()
	data, err := os.ReadFile(filepath.Join("testdata", "redaction.json"))
	if err != nil {
		t.Fatal(err)
	}
	var vs []redactionVector
	if err := json.Unmarshal(data, &vs); err != nil {
		t.Fatal(err)
	}
	if len(vs) == 0 {
		t.Fatal("no vectors")
	}
	return vs
}

func TestRedactionVectors(t *testing.T) {
	for _, v := range redactionVectors(t) {
		t.Run(v.Name, func(t *testing.T) {
			out, kinds := Redact(v.In)
			if out != v.Out {
				t.Errorf("payload:\n got %s\nwant %s", out, v.Out)
			}
			if len(kinds) == 0 {
				kinds = []string{}
			}
			if !reflect.DeepEqual(kinds, v.Kinds) {
				t.Errorf("kinds: got %v, want %v", kinds, v.Kinds)
			}
			if !json.Valid([]byte(out)) {
				t.Errorf("not JSON any more: %s", out)
			}
			// Redacting twice changes nothing more.
			again, _ := Redact(out)
			if again != out {
				t.Errorf("not idempotent:\n%s\n%s", out, again)
			}
		})
	}
}

// Anything an agent sends, inside a JSON payload, as encoding/json writes it and as the SDKs
// do, with non-ASCII escaped: it stays JSON, and so does the JSON in its string, redacting
// twice changes nothing more, and every kind listed names a placeholder. go test runs the
// seeds; go test -fuzz=FuzzRedact explores.
func FuzzRedact(f *testing.F) {
	data, err := os.ReadFile(filepath.Join("testdata", "redaction.json"))
	if err != nil {
		f.Fatal(err)
	}
	var vs []redactionVector
	if err := json.Unmarshal(data, &vs); err != nil {
		f.Fatal(err)
	}
	for _, v := range vs {
		f.Add(v.In)
	}
	f.Add("-----BEGIN PRIVATE KEY-----\n\"\\")
	// Escaped, ® and « end in e and b, which start a JWT and a bearer token.
	f.Add("®yJhbGciOiJFUzI1NiJ9.eyJqdGkiOiJ0b2stNyJ9.c2lnbmF0dXJlLW5vdC1yZWFs «earer abcdefghijklmnopqrstuvwxyz")
	f.Fuzz(func(t *testing.T, text string) {
		inner, _ := json.Marshal(map[string]string{"text": text})
		plain, _ := json.Marshal(map[string]string{"output": text, "nested": string(inner)})
		escaped, _ := json.Marshal(map[string]string{"output": text, "nested": ascii(inner)})
		for _, payload := range []string{string(plain), ascii(escaped)} {
			out, kinds := Redact(payload)
			var doc map[string]string
			if err := json.Unmarshal([]byte(out), &doc); err != nil {
				t.Fatalf("not JSON: %s", out)
			}
			if !json.Valid([]byte(doc["nested"])) {
				t.Fatalf("the JSON in it isn't JSON: %s", out)
			}
			if again, _ := Redact(out); again != out {
				t.Fatalf("not idempotent:\n%s\n%s", out, again)
			}
			for _, k := range kinds {
				if !strings.Contains(out, "[redacted "+k+" sha256:") {
					t.Fatalf("%s listed but not in %s", k, out)
				}
			}
		}
	})
}

// ascii escapes JSON's non-ASCII as \uXXXX, as Python's json.dumps does by default, and so
// the SDKs' payloads. encoding/json writes it as it is.
func ascii(text []byte) string {
	var out strings.Builder
	for _, r := range string(text) {
		if r < utf8.RuneSelf {
			out.WriteRune(r)
			continue
		}
		for _, u := range utf16.Encode([]rune{r}) {
			fmt.Fprintf(&out, `\u%04x`, u)
		}
	}
	return out.String()
}

// An agent chooses what's in a payload. Inputs built to make a backtracking engine retry
// at every position still take linear time.
func TestRedactionTakesLinearTime(t *testing.T) {
	for name, payload := range map[string]string{
		"jwt prefixes":          strings.Repeat("eyJ", 1<<20),
		"a key that never ends": "-----BEGIN PRIVATE KEY-----" + strings.Repeat("A-", 1<<20),
		"many key starts":       strings.Repeat("-----BEGIN PRIVATE KEY-----", 1<<15),
		"bearer runs":           strings.Repeat("Bearer ", 1<<19),
		// Every pattern's literals, so none is skipped.
		"every pattern": strings.Repeat(` eyJ ghp_ AIza ya29. xoxb- sk- _live_ Bearer Basic \\u00ab ://a:b secretAccessKey=`, 1<<14),
	} {
		start := time.Now()
		Redact(payload)
		if took := time.Since(start); took > 5*time.Second {
			t.Errorf("%s: %v for %d bytes", name, took, len(payload))
		}
	}
}

func TestSignRedactsThePayloadBeforeSigningIt(t *testing.T) {
	signer, verifier := newKey(t, "collector")
	trust := wakelog.Trust{Keys: map[string]note.Verifier{"collector": verifier}}
	span := ptrace.NewSpan()
	span.Attributes().PutStr(Kind, "tool_call")
	span.Attributes().PutStr(Payload, `{"input": {"token": "ghp_`+strings.Repeat("a", 36)+`"}}`)
	// A sender can't say what was redacted, or that nothing was.
	span.Attributes().PutEmptySlice(Redacted).AppendEmpty().SetStr("nothing")
	if err := Sign(span, signer); err != nil {
		t.Fatal(err)
	}
	payload, _ := PayloadOf(span)
	if strings.Contains(string(payload), "ghp_") || !strings.Contains(string(payload), "[redacted github_token sha256:") {
		t.Errorf("payload: %s", payload)
	}
	kinds, _ := span.Attributes().Get(Redacted)
	if got := kinds.Slice().AsRaw(); !reflect.DeepEqual(got, []any{"github_token"}) {
		t.Errorf("%s: %v", Redacted, got)
	}
	// The signature commits to the redacted payload, and the list of what was redacted.
	if err := Verify(span, trust); err != nil {
		t.Fatal(err)
	}
	span.Attributes().PutEmptySlice(Redacted)
	if err := Verify(span, trust); err == nil {
		t.Error("a changed redaction list still verified")
	}

	// With nothing to redact, there's no list.
	clean := ptrace.NewSpan()
	clean.Attributes().PutStr(Kind, "tool_call")
	clean.Attributes().PutStr(Payload, `{"output": "Order 1234"}`)
	clean.Attributes().PutEmptySlice(Redacted).AppendEmpty().SetStr("jwt")
	if err := Sign(clean, signer); err != nil {
		t.Fatal(err)
	}
	if _, ok := clean.Attributes().Get(Redacted); ok {
		t.Errorf("%s set with nothing redacted", Redacted)
	}
}

// A payload sent as bytes is the bytes it holds. Read as text, it was base64, so the
// credential in it was signed and logged for anyone to decode.
func TestSignRedactsAPayloadSentAsBytes(t *testing.T) {
	signer, verifier := newKey(t, "collector")
	trust := wakelog.Trust{Keys: map[string]note.Verifier{"collector": verifier}}
	token := "ghp_" + strings.Repeat("a", 36)
	span := ptrace.NewSpan()
	span.Attributes().PutStr(Kind, "tool_call")
	span.Attributes().PutEmptyBytes(Payload).FromRaw([]byte(`{"input": {"token": "` + token + `"}}`))
	if err := Sign(span, signer); err != nil {
		t.Fatal(err)
	}
	payload, _ := PayloadOf(span)
	if strings.Contains(string(payload), token) || !strings.Contains(string(payload), "[redacted github_token sha256:") {
		t.Errorf("payload: %s", payload)
	}
	if err := Verify(span, trust); err != nil {
		t.Fatal(err)
	}

	// Bytes with nothing to redact keep their type, and are signed by their own digest.
	clean := ptrace.NewSpan()
	clean.Attributes().PutStr(Kind, "tool_call")
	clean.Attributes().PutEmptyBytes(Payload).FromRaw([]byte(testPayload))
	if err := Sign(clean, signer); err != nil {
		t.Fatal(err)
	}
	if got, _ := clean.Attributes().Get(PayloadSHA256); got.Str() != sha256Hex(testPayload) {
		t.Errorf("digest %s, want the bytes' %s", got.Str(), sha256Hex(testPayload))
	}
	if err := Verify(clean, trust); err != nil {
		t.Fatal(err)
	}
}

// A map or a list renders bytes in it as base64 too, where nothing can be redacted, so a
// payload that isn't a string or bytes isn't signed.
func TestSignRefusesAPayloadThatIsntText(t *testing.T) {
	signer, _ := newKey(t, "collector")
	span := ptrace.NewSpan()
	span.Attributes().PutStr(Kind, "tool_call")
	span.Attributes().PutEmptyMap(Payload).PutEmptyBytes("token").FromRaw([]byte("ghp_" + strings.Repeat("a", 36)))
	if err := Sign(span, signer); err == nil || !strings.Contains(err.Error(), "must be a string or bytes") {
		t.Fatalf("signed a map payload: %v", err)
	}
	if _, ok := span.Attributes().Get(Signature); ok {
		t.Error("left a signature on it")
	}
	if _, ok := PayloadOf(span); ok {
		t.Error("took a map for a payload")
	}
}
