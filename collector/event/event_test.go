package event

import (
	"bytes"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"strings"
	"testing"

	"go.opentelemetry.io/collector/pdata/pcommon"
	"go.opentelemetry.io/collector/pdata/ptrace"
	"golang.org/x/mod/sumdb/note"

	wakelog "github.com/manikoa/wake/log"
)

func newKey(t *testing.T, name string) (note.Signer, note.Verifier) {
	t.Helper()
	private, public, err := note.GenerateKey(rand.Reader, name)
	if err != nil {
		t.Fatal(err)
	}
	signer, err := note.NewSigner(private)
	if err != nil {
		t.Fatal(err)
	}
	verifier, err := note.NewVerifier(public)
	if err != nil {
		t.Fatal(err)
	}
	return signer, verifier
}

const testPayload = `{"output":"<b>wire $10,000 & hurry</b>"}`

func sha256Hex(s string) string {
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:])
}

func newEvent() ptrace.Span {
	span := ptrace.NewSpan()
	span.SetTraceID(pcommon.TraceID{1, 2, 3})
	span.SetSpanID(pcommon.SpanID{4, 5, 6})
	span.SetStartTimestamp(100)
	span.SetEndTimestamp(200)
	attrs := span.Attributes()
	attrs.PutStr(Kind, "tool_call")
	attrs.PutStr(Capture, "sdk")
	attrs.PutStr(Payload, testPayload)
	attrs.PutStr("gen_ai.tool.name", "read_email")
	attrs.PutEmptySlice("wake.parents").AppendEmpty().SetStr("event-1")
	attrs.PutStr("http.url", "not signed")
	return span
}

func TestCanonicalIsSortedAndCoversOnlyWakeAndGenAI(t *testing.T) {
	span := newEvent()
	span.Attributes().PutStr(PayloadSHA256, "abc")
	got, err := Canonical(span)
	if err != nil {
		t.Fatal(err)
	}
	// The payload is left out; its digest is signed.
	want := `{"attributes":{"gen_ai.tool.name":"read_email","wake.capture":"sdk","wake.event.kind":"tool_call","wake.parents":["event-1"],"wake.payload.sha256":"abc"},"end_time_unix_nano":200,"span_id":"0405060000000000","start_time_unix_nano":100,"trace_id":"01020300000000000000000000000000"}`
	if string(got) != want {
		t.Fatalf("canonical =\n%s\nwant\n%s", got, want)
	}
}

func TestSignSetsThePayloadDigestFromTheBytes(t *testing.T) {
	signer, _ := newKey(t, "collector")
	span := newEvent()
	span.Attributes().PutStr(PayloadSHA256, sha256Hex("a payload the sender didn't send"))
	if err := Sign(span, signer); err != nil {
		t.Fatal(err)
	}
	got, _ := span.Attributes().Get(PayloadSHA256)
	if got.Str() != sha256Hex(testPayload) {
		t.Fatalf("signed digest = %s, want the digest of the payload received", got.Str())
	}
}

func TestSignThenVerify(t *testing.T) {
	signer, verifier := newKey(t, "collector")
	span := newEvent()
	if err := Sign(span, signer); err != nil {
		t.Fatal(err)
	}
	trusted := map[string]note.Verifier{"collector": verifier}
	if !Verify(span, wakelog.Trust{Keys: trusted}) {
		t.Fatal("signature doesn't verify")
	}

	// Signature fields aren't part of the signed bytes, so recording validity doesn't break it.
	span.Attributes().PutBool(SignatureValid, true)
	if !Verify(span, wakelog.Trust{Keys: trusted}) {
		t.Fatal("adding wake.signature.valid broke the signature")
	}

	tampered := ptrace.NewSpan()
	span.CopyTo(tampered)
	tampered.Attributes().PutStr(Payload, `{"output":"pay $1"}`)
	if Verify(tampered, wakelog.Trust{Keys: trusted}) {
		t.Fatal("event with a tampered payload still verifies")
	}

	span.CopyTo(tampered)
	tampered.Attributes().PutStr(PayloadSHA256, sha256Hex(`{"output":"pay $1"}`))
	tampered.Attributes().PutStr(Payload, `{"output":"pay $1"}`)
	if Verify(tampered, wakelog.Trust{Keys: trusted}) {
		t.Fatal("event with a tampered payload and digest still verifies")
	}

	span.CopyTo(tampered)
	tampered.Attributes().Remove(PayloadSHA256)
	if Verify(tampered, wakelog.Trust{Keys: trusted}) {
		t.Fatal("event with a payload but no signed digest verifies")
	}
}

func TestVerifyRejectsUnknownSigner(t *testing.T) {
	signer, _ := newKey(t, "proxy")
	_, otherVerifier := newKey(t, "collector")
	span := newEvent()
	if err := Sign(span, signer); err != nil {
		t.Fatal(err)
	}
	if Verify(span, wakelog.Trust{Keys: map[string]note.Verifier{"collector": otherVerifier}}) {
		t.Fatal("event from an untrusted signer verifies")
	}
}

func TestEntryKeepsTheExactSignedBytes(t *testing.T) {
	signer, verifier := newKey(t, "collector")
	span := newEvent()
	if err := Sign(span, signer); err != nil {
		t.Fatal(err)
	}
	span.Attributes().PutBool(SignatureValid, true)
	entry, external, err := Entry(span)
	if err != nil {
		t.Fatal(err)
	}
	if external {
		t.Fatal("a small payload didn't go in the entry")
	}
	// A reader of the log parses the entry with the log module and checks it on its own.
	decoded, err := wakelog.ParseEntry(entry)
	if err != nil {
		t.Fatal(err)
	}
	canonical, _ := Canonical(span)
	if !bytes.Equal(decoded.Event, canonical) {
		t.Fatalf("entry rewrote the signed bytes:\n%s\nwant\n%s", decoded.Event, canonical)
	}
	if decoded.Signer != "collector" || !decoded.Valid {
		t.Fatalf("entry = %+v", decoded)
	}
	if !decoded.Verify(map[string]note.Verifier{"collector": verifier}) {
		t.Fatal("entry doesn't verify on its own")
	}
	if !Verify(span, wakelog.Trust{Keys: map[string]note.Verifier{"collector": verifier}}) {
		t.Fatal("entry's event doesn't verify")
	}
	payload, err := decoded.ReadPayload(nil)
	if err != nil || string(payload) != testPayload {
		t.Fatalf("payload = %q, %v", payload, err)
	}
}

func TestEntryLeavesOutAPayloadTooLargeForIt(t *testing.T) {
	signer, verifier := newKey(t, "collector")
	span := newEvent()
	large := `{"output":"` + strings.Repeat("Quarterly numbers attached. ", 4000) + `"}`
	span.Attributes().PutStr(Payload, large)
	if err := Sign(span, signer); err != nil {
		t.Fatal(err)
	}
	entry, external, err := Entry(span)
	if err != nil {
		t.Fatal(err)
	}
	if !external || len(entry) > wakelog.MaxEntrySize {
		t.Fatalf("external = %v, entry = %d bytes", external, len(entry))
	}
	decoded, err := wakelog.ParseEntry(entry)
	if err != nil {
		t.Fatal(err)
	}
	if decoded.Payload != nil || !decoded.Verify(map[string]note.Verifier{"collector": verifier}) {
		t.Fatalf("entry = %+v", decoded)
	}
	stored, _ := PayloadOf(span)
	path, _ := wakelog.PayloadPath(sha256Hex(large))
	payload, err := decoded.ReadPayload(func(p string) ([]byte, error) {
		if p != path {
			t.Fatalf("fetched %s, want %s", p, path)
		}
		return stored, nil
	})
	if err != nil || string(payload) != large {
		t.Fatalf("ReadPayload = %d bytes, %v", len(payload), err)
	}
}
