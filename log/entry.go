package wakelog

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"unicode/utf8"

	"github.com/transparency-dev/tessera"
	"golang.org/x/mod/sumdb/note"
)

// MaxEntrySize is the largest entry the log holds, in bytes. tlog-tiles stores each
// entry's length in two bytes.
const MaxEntrySize = tessera.DefaultEntrySizeLimit

// PayloadSHA256 is the signed attribute that commits an event to its payload.
const PayloadSHA256 = "wake.payload.sha256"

var (
	// ErrEntryTooLarge means an entry is over MaxEntrySize even without its payload.
	ErrEntryTooLarge = errors.New("entry too large for the log")
	// ErrNoPayload means an event carries no payload digest, so it has no payload.
	ErrNoPayload = errors.New("event has no payload")
	// ErrPayloadMismatch means a payload doesn't match its event's signed digest.
	ErrPayloadMismatch = errors.New("payload doesn't match the signed wake.payload.sha256")
)

// Entry is one log entry: an event's exact signed bytes, its signature and signer,
// whether the collector found the signature valid when it appended the entry, and the
// event's payload when it fits. The format is defined in docs/events.md.
type Entry struct {
	// Event holds the signed bytes exactly as they were signed.
	Event json.RawMessage `json:"event"`
	// Payload is the event's payload when it fits in the entry. It isn't signed; the
	// signed wake.payload.sha256 commits to it. When it's nil and the event has a
	// digest, the payload is stored next to the log at PayloadPath. Read it with
	// ReadPayload, which checks it either way.
	Payload   *string `json:"payload,omitempty"`
	Signature string  `json:"signature"`
	Signer    string  `json:"signer"`
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

// NewEntry encodes a log entry for an event with no payload. event must be the exact
// bytes that were signed.
func NewEntry(event []byte, signature, signer string, valid bool) ([]byte, error) {
	if err := checkCompact(event); err != nil {
		return nil, err
	}
	return encode(Entry{Event: event, Signature: signature, Signer: signer, Valid: valid})
}

// NewEntryWithPayload encodes a log entry for an event with a payload. event must be the
// exact bytes that were signed, with the payload's digest in them and the payload left
// out. The payload goes in the entry when the entry still fits in the log and the
// payload is valid UTF-8, which a JSON string needs to carry it byte for byte. Otherwise
// it's left out and inline is false: store it with Log.PutPayload before appending the
// entry.
func NewEntryWithPayload(event []byte, payload string, signature, signer string, valid bool) (entry []byte, inline bool, err error) {
	if err := checkCompact(event); err != nil {
		return nil, false, err
	}
	e := Entry{Event: event, Payload: &payload, Signature: signature, Signer: signer, Valid: valid}
	if utf8.ValidString(payload) {
		data, err := marshal(e)
		if err != nil {
			return nil, false, err
		}
		if len(data) <= MaxEntrySize {
			return data, true, nil
		}
	}
	e.Payload = nil
	data, err := encode(e)
	return data, false, err
}

// checkCompact refuses event bytes that encoding would change. Encoding compacts the
// event, so bytes that aren't already compact would no longer match their signature.
func checkCompact(event []byte) error {
	var compact bytes.Buffer
	if err := json.Compact(&compact, event); err != nil {
		return fmt.Errorf("event is not valid JSON: %w", err)
	}
	if !bytes.Equal(compact.Bytes(), event) {
		return errors.New("event is not compact JSON")
	}
	return nil
}

// encode marshals an entry and refuses one the log can't hold.
func encode(e Entry) ([]byte, error) {
	data, err := marshal(e)
	if err != nil {
		return nil, err
	}
	if len(data) > MaxEntrySize {
		return nil, fmt.Errorf("%w: %d bytes, the limit is %d", ErrEntryTooLarge, len(data), MaxEntrySize)
	}
	return data, nil
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

// ReadPayload returns the event's payload, checked against the signed
// wake.payload.sha256. It takes the payload from the entry when it's there, and
// otherwise calls fetch with PayloadPath, a path relative to the log's root, for
// example the tiles' directory or bucket. It returns ErrNoPayload if the event has no
// digest and ErrPayloadMismatch if the payload doesn't match it.
//
// ReadPayload doesn't check the signature. Check it with Verify, which covers the digest.
func (e Entry) ReadPayload(fetch func(path string) ([]byte, error)) ([]byte, error) {
	ev, err := e.Decode()
	if err != nil {
		return nil, err
	}
	digest := ev.String(PayloadSHA256)
	if digest == "" {
		return nil, ErrNoPayload
	}
	var payload []byte
	if e.Payload != nil {
		payload = []byte(*e.Payload)
	} else {
		path, err := PayloadPath(digest)
		if err != nil {
			return nil, err
		}
		if payload, err = fetch(path); err != nil {
			return nil, fmt.Errorf("fetch payload: %w", err)
		}
	}
	if sum := sha256.Sum256(payload); hex.EncodeToString(sum[:]) != digest {
		return nil, ErrPayloadMismatch
	}
	return payload, nil
}

// PayloadPath returns where a payload with this hex SHA-256 is stored, relative to the
// log's root: payload/sha256/<digest>. It refuses anything but 64 lowercase hex digits,
// since the digest comes from an event and must never name another path.
func PayloadPath(digest string) (string, error) {
	if len(digest) != sha256.Size*2 {
		return "", fmt.Errorf("payload digest %q is not a hex SHA-256", digest)
	}
	for _, c := range digest {
		if (c < '0' || c > '9') && (c < 'a' || c > 'f') {
			return "", fmt.Errorf("payload digest %q is not lowercase hex", digest)
		}
	}
	return payloadDir + "/" + digest, nil
}

// payloadDir holds payloads too large for their entries, relative to the log's root.
const payloadDir = "payload/sha256"

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
