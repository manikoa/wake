package wakelog

import (
	"bytes"
	"crypto/rand"
	"encoding/base64"
	"testing"

	"golang.org/x/mod/sumdb/note"
)

const signedEvent = `{"attributes":{"wake.event.id":"e2","wake.event.kind":"tool_call","wake.parents":["e1"],"wake.payload":"{\"output\":\"<b>wire</b> & pay — now\"}","wake.payload.size":12},"end_time_unix_nano":2,"span_id":"0102030405060708","start_time_unix_nano":1,"trace_id":"0102030405060708090a0b0c0d0e0f10"}`

func testSigner(t *testing.T, name string) (note.Signer, map[string]note.Verifier) {
	t.Helper()
	privateKey, publicKey, err := note.GenerateKey(rand.Reader, name)
	if err != nil {
		t.Fatal(err)
	}
	signer, err := note.NewSigner(privateKey)
	if err != nil {
		t.Fatal(err)
	}
	verifier, err := note.NewVerifier(publicKey)
	if err != nil {
		t.Fatal(err)
	}
	return signer, map[string]note.Verifier{name: verifier}
}

func signedEntry(t *testing.T, signer note.Signer, event string) []byte {
	t.Helper()
	sig, err := signer.Sign([]byte(event))
	if err != nil {
		t.Fatal(err)
	}
	entry, err := NewEntry([]byte(event), base64.StdEncoding.EncodeToString(sig), signer.Name(), true)
	if err != nil {
		t.Fatal(err)
	}
	return entry
}

func TestEntryKeepsSignedBytesExactly(t *testing.T) {
	signer, trusted := testSigner(t, "collector")
	entry, err := ParseEntry(signedEntry(t, signer, signedEvent))
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(entry.Event, []byte(signedEvent)) {
		t.Fatalf("event bytes changed:\n got %s\nwant %s", entry.Event, signedEvent)
	}
	if !entry.Valid || entry.Signer != "collector" {
		t.Fatalf("entry = %+v", entry)
	}
	if !entry.Verify(trusted) {
		t.Fatal("signature doesn't verify")
	}
}

func TestVerifyRejectsTamperingAndUnknownSigners(t *testing.T) {
	signer, trusted := testSigner(t, "collector")
	entry, err := ParseEntry(signedEntry(t, signer, signedEvent))
	if err != nil {
		t.Fatal(err)
	}

	tampered := entry
	tampered.Event = bytes.Replace(entry.Event, []byte(`"e1"`), []byte(`"e0"`), 1)
	if tampered.Verify(trusted) {
		t.Error("tampered event verified")
	}

	_, other := testSigner(t, "collector")
	if entry.Verify(map[string]note.Verifier{"collector": other["collector"]}) {
		t.Error("verified with another key of the same name")
	}

	renamed := entry
	renamed.Signer = "proxy"
	if renamed.Verify(trusted) {
		t.Error("verified with an unknown signer")
	}

	garbled := entry
	garbled.Signature = "not base64!"
	if garbled.Verify(trusted) {
		t.Error("verified a garbled signature")
	}
}

func TestDecodeReadsEventFields(t *testing.T) {
	signer, _ := testSigner(t, "collector")
	entry, err := ParseEntry(signedEntry(t, signer, signedEvent))
	if err != nil {
		t.Fatal(err)
	}
	ev, err := entry.Decode()
	if err != nil {
		t.Fatal(err)
	}
	if ev.String("wake.event.id") != "e2" || ev.String("wake.event.kind") != "tool_call" {
		t.Errorf("attributes = %v", ev.Attributes)
	}
	if parents := ev.Strings("wake.parents"); len(parents) != 1 || parents[0] != "e1" {
		t.Errorf("parents = %v", parents)
	}
	if ev.String("wake.payload.size") != "" || ev.String("missing") != "" {
		t.Error("non-string attributes should read as empty")
	}
	if ev.TraceID != "0102030405060708090a0b0c0d0e0f10" || ev.StartTimeUnixNano != 1 || ev.EndTimeUnixNano != 2 {
		t.Errorf("event = %+v", ev)
	}
}

func TestParseEntryRejectsMalformedEntries(t *testing.T) {
	for _, data := range []string{
		``,
		`not json`,
		`{"signature":"x","signer":"collector","valid":true}`,
		`{"event":"a string","signature":"x","signer":"collector","valid":true}`,
		`{"event":null,"signature":"x","signer":"collector","valid":true}`,
	} {
		if _, err := ParseEntry([]byte(data)); err == nil {
			t.Errorf("ParseEntry(%q) succeeded", data)
		}
	}
}

func TestNewEntryRefusesBytesThatWouldChange(t *testing.T) {
	for _, event := range []string{`{"a": 1}`, `{"a":1}` + "\n", `not json`} {
		if _, err := NewEntry([]byte(event), "", "collector", false); err == nil {
			t.Errorf("NewEntry(%q) succeeded", event)
		}
	}
}
