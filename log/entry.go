package wakelog

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"

	"golang.org/x/mod/sumdb/note"
)

// Entry is one log entry: an event's exact signed bytes, its signature and signer, and
// whether the collector found the signature valid when it appended the entry. The
// format is defined in docs/events.md.
type Entry struct {
	// Event holds the signed bytes exactly as they were signed.
	Event     json.RawMessage `json:"event"`
	Signature string          `json:"signature"`
	Signer    string          `json:"signer"`
	// Valid is the collector's check at append time. Check again with Verify; don't trust it.
	Valid bool `json:"valid"`
}

// Event is the content of an entry's signed bytes.
type Event struct {
	TraceID           string         `json:"trace_id"`
	SpanID            string         `json:"span_id"`
	StartTimeUnixNano uint64         `json:"start_time_unix_nano"`
	EndTimeUnixNano   uint64         `json:"end_time_unix_nano"`
	Attributes        map[string]any `json:"attributes"`
}

// NewEntry encodes a log entry. event must be the exact bytes that were signed.
func NewEntry(event []byte, signature, signer string, valid bool) ([]byte, error) {
	// Encoding compacts the event, so bytes that aren't already compact would change
	// and no longer match their signature.
	var compact bytes.Buffer
	if err := json.Compact(&compact, event); err != nil {
		return nil, fmt.Errorf("event is not valid JSON: %w", err)
	}
	if !bytes.Equal(compact.Bytes(), event) {
		return nil, errors.New("event is not compact JSON")
	}
	return marshal(Entry{Event: event, Signature: signature, Signer: signer, Valid: valid})
}

// ParseEntry decodes a log entry, keeping the event's signed bytes exactly as they are.
func ParseEntry(data []byte) (Entry, error) {
	var e Entry
	if err := json.Unmarshal(data, &e); err != nil {
		return Entry{}, fmt.Errorf("parse entry: %w", err)
	}
	if len(e.Event) == 0 || e.Event[0] != '{' {
		return Entry{}, errors.New("parse entry: event is not a JSON object")
	}
	return e, nil
}

// Verify reports whether the entry's signature is valid for its event bytes, from one of
// the trusted signers, keyed by name.
func (e Entry) Verify(trusted map[string]note.Verifier) bool {
	return VerifySignature(e.Event, e.Signature, e.Signer, trusted)
}

// Decode returns the event inside the entry. Numbers in attributes decode as json.Number.
func (e Entry) Decode() (Event, error) {
	var ev Event
	dec := json.NewDecoder(bytes.NewReader(e.Event))
	dec.UseNumber()
	if err := dec.Decode(&ev); err != nil {
		return Event{}, fmt.Errorf("decode event: %w", err)
	}
	return ev, nil
}

// String returns a string attribute, or "" if it's missing or not a string.
func (ev Event) String(key string) string {
	s, _ := ev.Attributes[key].(string)
	return s
}

// Strings returns a string array attribute, skipping any element that isn't a string.
func (ev Event) Strings(key string) []string {
	values, _ := ev.Attributes[key].([]any)
	out := make([]string, 0, len(values))
	for _, v := range values {
		if s, ok := v.(string); ok {
			out = append(out, s)
		}
	}
	return out
}

// VerifySignature reports whether signature, base64 encoded, is valid for msg from the
// trusted signer with this name.
func VerifySignature(msg []byte, signature, signer string, trusted map[string]note.Verifier) bool {
	verifier, ok := trusted[signer]
	if !ok {
		return false
	}
	sig, err := base64.StdEncoding.DecodeString(signature)
	if err != nil {
		return false
	}
	return verifier.Verify(msg, sig)
}

// marshal encodes v without HTML escaping, which would otherwise rewrite <, > and &
// inside payloads and change the signed bytes.
func marshal(v any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(buf.Bytes(), []byte("\n")), nil
}
