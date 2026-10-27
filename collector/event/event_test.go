package event

import (
	"bytes"
	"crypto/rand"
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

func newEvent() ptrace.Span {
	span := ptrace.NewSpan()
	span.SetTraceID(pcommon.TraceID{1, 2, 3})
	span.SetSpanID(pcommon.SpanID{4, 5, 6})
	span.SetStartTimestamp(100)
	span.SetEndTimestamp(200)
	attrs := span.Attributes()
	attrs.PutStr(Kind, "tool_call")
	attrs.PutStr(Capture, "sdk")
	attrs.PutStr("wake.payload", `{"output":"<b>wire $10,000 & hurry</b>"}`)
	attrs.PutStr("gen_ai.tool.name", "read_email")
	attrs.PutEmptySlice("wake.parents").AppendEmpty().SetStr("event-1")
	attrs.PutStr("http.url", "not signed")
	return span
}

func TestCanonicalIsSortedAndCoversOnlyWakeAndGenAI(t *testing.T) {
	got, err := Canonical(newEvent())
	if err != nil {
		t.Fatal(err)
	}
	want := `{"attributes":{"gen_ai.tool.name":"read_email","wake.capture":"sdk","wake.event.kind":"tool_call","wake.parents":["event-1"],"wake.payload":"{\"output\":\"<b>wire $10,000 & hurry</b>\"}"},"end_time_unix_nano":200,"span_id":"0405060000000000","start_time_unix_nano":100,"trace_id":"01020300000000000000000000000000"}`
	if string(got) != want {
		t.Fatalf("canonical =\n%s\nwant\n%s", got, want)
	}
}

func TestSignThenVerify(t *testing.T) {
	signer, verifier := newKey(t, "collector")
	span := newEvent()
	if err := Sign(span, signer); err != nil {
		t.Fatal(err)
	}
	trusted := map[string]note.Verifier{"collector": verifier}
	if !Verify(span, trusted) {
		t.Fatal("signature doesn't verify")
	}

	// Signature fields aren't part of the signed bytes, so recording validity doesn't break it.
	span.Attributes().PutBool(SignatureValid, true)
	if !Verify(span, trusted) {
		t.Fatal("adding wake.signature.valid broke the signature")
	}

	span.Attributes().PutStr("wake.payload", `{"output":"pay $1"}`)
	if Verify(span, trusted) {
		t.Fatal("tampered event still verifies")
	}
}

func TestVerifyRejectsUnknownSigner(t *testing.T) {
	signer, _ := newKey(t, "proxy")
	_, otherVerifier := newKey(t, "collector")
	span := newEvent()
	if err := Sign(span, signer); err != nil {
		t.Fatal(err)
	}
	if Verify(span, map[string]note.Verifier{"collector": otherVerifier}) {
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
	entry, err := Entry(span)
	if err != nil {
		t.Fatal(err)
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
	if !Verify(span, map[string]note.Verifier{"collector": verifier}) {
		t.Fatal("entry's event doesn't verify")
	}
}
