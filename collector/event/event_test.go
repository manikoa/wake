package event

import (
	"bytes"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"math"
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
	if err := Verify(span, wakelog.Trust{Keys: trusted}); err != nil {
		t.Fatalf("signature doesn't verify: %v", err)
	}

	// Signature fields and the time it was received aren't part of the signed bytes, so
	// the collector recording them doesn't break it.
	span.Attributes().PutBool(SignatureValid, true)
	span.Attributes().PutStr(SignatureError, "a reason")
	span.Attributes().PutInt(Received, 12345)
	if err := Verify(span, wakelog.Trust{Keys: trusted}); err != nil {
		t.Fatalf("recording the collector's check broke the signature: %v", err)
	}

	tampered := ptrace.NewSpan()
	span.CopyTo(tampered)
	tampered.Attributes().PutStr(Payload, `{"output":"pay $1"}`)
	if err := Verify(tampered, wakelog.Trust{Keys: trusted}); err != wakelog.ErrPayloadMismatch {
		t.Fatalf("event with a tampered payload: %v", err)
	}

	span.CopyTo(tampered)
	tampered.Attributes().PutStr(PayloadSHA256, sha256Hex(`{"output":"pay $1"}`))
	tampered.Attributes().PutStr(Payload, `{"output":"pay $1"}`)
	if Verify(tampered, wakelog.Trust{Keys: trusted}) == nil {
		t.Fatal("event with a tampered payload and digest still verifies")
	}

	span.CopyTo(tampered)
	tampered.Attributes().Remove(PayloadSHA256)
	if Verify(tampered, wakelog.Trust{Keys: trusted}) == nil {
		t.Fatal("event with a payload but no signed digest verifies")
	}

	// A signer committed to a payload it didn't send.
	span.CopyTo(tampered)
	tampered.Attributes().Remove(Payload)
	if err := Verify(tampered, wakelog.Trust{Keys: trusted}); err == nil || !strings.Contains(err.Error(), "no payload") {
		t.Fatalf("event with a signed digest but no payload: %v", err)
	}
}

// docs/events.md: the signer sets wake.payload.sha256 from the payload it received,
// replacing the sender's. With no payload received, there's nothing for a digest to
// commit to, and the collector mustn't sign one the sender made up.
func TestSignDropsADigestWithNoPayload(t *testing.T) {
	signer, verifier := newKey(t, "collector")
	span := newEvent()
	span.Attributes().Remove(Payload)
	span.Attributes().PutStr(PayloadSHA256, sha256Hex(`{"output":"a payload the agent never sent"}`))
	if err := Sign(span, signer); err != nil {
		t.Fatal(err)
	}
	if got, ok := span.Attributes().Get(PayloadSHA256); ok {
		t.Fatalf("signed the sender's digest %s for an event with no payload", got.Str())
	}
	canonical, _ := Canonical(span)
	if bytes.Contains(canonical, []byte(PayloadSHA256)) {
		t.Fatalf("signed bytes hold a digest: %s", canonical)
	}
	if err := Verify(span, wakelog.Trust{Keys: map[string]note.Verifier{"collector": verifier}}); err != nil {
		t.Fatal(err)
	}
}

func TestSignRefusesADoubleThatIsntFinite(t *testing.T) {
	signer, _ := newKey(t, "collector")
	span := newEvent()
	span.Attributes().PutDouble("gen_ai.request.temperature", math.NaN())
	if err := Sign(span, signer); err == nil {
		t.Fatal("signed an event JSON can't hold")
	}
	if _, ok := span.Attributes().Get(Signature); ok {
		t.Fatal("left a signature on it")
	}
}

func TestVerifyRejectsUnknownSigner(t *testing.T) {
	signer, _ := newKey(t, "proxy")
	_, otherVerifier := newKey(t, "collector")
	span := newEvent()
	if err := Sign(span, signer); err != nil {
		t.Fatal(err)
	}
	if err := Verify(span, wakelog.Trust{Keys: map[string]note.Verifier{"collector": otherVerifier}}); err == nil || !strings.Contains(err.Error(), "isn't a trusted signer") {
		t.Fatalf("event from an untrusted signer: %v", err)
	}
}

func TestEntryKeepsTheExactSignedBytes(t *testing.T) {
	signer, verifier := newKey(t, "collector")
	span := newEvent()
	if err := Sign(span, signer); err != nil {
		t.Fatal(err)
	}
	span.Attributes().PutBool(SignatureValid, true)
	span.Attributes().PutInt(Received, 300)
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
	if decoded.Signer != "collector" || !decoded.Valid || decoded.Received != 300 {
		t.Fatalf("entry = %+v", decoded)
	}
	if !decoded.Verify(map[string]note.Verifier{"collector": verifier}) {
		t.Fatal("entry doesn't verify on its own")
	}
	if err := Verify(span, wakelog.Trust{Keys: map[string]note.Verifier{"collector": verifier}}); err != nil {
		t.Fatalf("entry's event doesn't verify: %v", err)
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
