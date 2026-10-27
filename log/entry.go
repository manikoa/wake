package wakelog

import (
	"bytes"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/transparency-dev/tessera"
	"golang.org/x/mod/sumdb/note"
)

// MaxEntrySize is the largest entry the log holds, in bytes. tlog-tiles stores each
// entry's length in two bytes.
const MaxEntrySize = tessera.DefaultEntrySizeLimit

// PayloadSHA256 is the signed attribute that commits an event to its payload.
const PayloadSHA256 = "wake.payload.sha256"

// Capture is the signed attribute naming what recorded an event: sdk, proxy or
// memory_adapter. Trust.Signers says who may sign each.
const Capture = "wake.capture"

// MaxClockSkew is how far a signer's clock may run ahead of the collector's, and how
// close to the edge of its validity an X.509-SVID may be. An SVID signature is checked as
// of when the collector received the event, give or take this.
const MaxClockSkew = 5 * time.Minute

// MaxEventAge is how long before the collector received it an event signed with an
// X.509-SVID may have started. It bounds how far back a signer can date an event. A long
// call, such as an A2A task, starts well before the proxy signs it, so this is generous.
const MaxEventAge = 24 * time.Hour

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
	// Certificate is set for a signer that signs with an X.509-SVID, such as the proxy:
	// base64 of the DER certificate, followed by any intermediates. Check verifies it
	// against SPIRE trust bundles.
	Certificate string `json:"certificate,omitempty"`
	// Received is when the collector received the event, in Unix nanoseconds. It isn't in
	// the event's signed bytes, since the proxy signs before the collector receives the
	// event; the log's signed checkpoint covers it, so read entries through Verify. Check
	// checks an SVID as of this time, as the collector did.
	Received uint64 `json:"received_unix_nano,omitempty"`
	// Valid is the collector's check at append time. Check again with Check; don't trust it.
	Valid bool `json:"valid"`
}

// Fields are what an entry holds besides the event's signed bytes.
type Fields struct {
	// Payload is the event's payload, or nil if it has none.
	Payload     *string
	Signature   string
	Signer      string
	Certificate string
	// Received is when the collector received the event, in Unix nanoseconds.
	Received uint64
	Valid    bool
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
	data, _, err := Encode(event, Fields{Signature: signature, Signer: signer, Valid: valid})
	return data, err
}

// NewEntryWithPayload encodes a log entry for an event with a payload. See Encode.
func NewEntryWithPayload(event []byte, payload string, signature, signer string, valid bool) (entry []byte, inline bool, err error) {
	return Encode(event, Fields{Payload: &payload, Signature: signature, Signer: signer, Valid: valid})
}

// Encode encodes a log entry. event must be the exact bytes that were signed, with the
// payload's digest in them and the payload left out. The payload goes in the entry when
// the entry still fits in the log and the payload is valid UTF-8, which a JSON string
// needs to carry it byte for byte. Otherwise it's left out and inline is false: store it
// with Log.PutPayload before appending the entry. With no payload, inline is true.
func Encode(event []byte, f Fields) (entry []byte, inline bool, err error) {
	if err := checkCompact(event); err != nil {
		return nil, false, err
	}
	e := Entry{Event: event, Payload: f.Payload, Signature: f.Signature, Signer: f.Signer, Certificate: f.Certificate, Received: f.Received, Valid: f.Valid}
	if f.Payload == nil {
		data, err := encode(e)
		return data, true, err
	}
	if utf8.ValidString(*f.Payload) {
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
// the trusted note format signers, keyed by name. It doesn't apply Trust.Signers; Check
// does, and also accepts X.509-SVID signers.
func (e Entry) Verify(trusted map[string]note.Verifier) bool {
	return VerifySignature(e.Event, e.Signature, e.Signer, trusted)
}

// Trust says whose signatures to accept.
type Trust struct {
	// Keys are note format public keys by signer name, such as the collector's.
	Keys map[string]note.Verifier
	// Bundles are the SPIRE trust bundles that X.509-SVID signers, such as the proxy,
	// must chain to.
	Bundles *x509.CertPool
	// Signers says who may sign each capture. A bundle vouches for every workload in its
	// trust domain, agents included, so an SVID signature only counts when its signer is
	// listed for the event's capture. A note format key is trusted by being in Keys, but
	// once a capture is listed here, only its listed signers count for it, whatever they
	// sign with.
	Signers Signers
}

// Signers lists, by wake.capture, who may sign events of that capture: signer names,
// such as SPIFFE IDs, or path prefixes ending in "/" that match every ID below them. A
// SPIFFE ID never ends in "/", so the two can't be confused.
type Signers map[string][]string

// Allows reports whether signer may sign events of this capture.
func (s Signers) Allows(capture, signer string) bool {
	for _, id := range s[capture] {
		if id == signer || strings.HasSuffix(id, "/") && len(signer) > len(id) && strings.HasPrefix(signer, id) {
			return true
		}
	}
	return false
}

// Validate refuses an empty name, and a prefix that would admit a whole trust domain.
func (s Signers) Validate() error {
	for capture, ids := range s {
		if len(ids) == 0 {
			return fmt.Errorf("signers for %q: none listed", capture)
		}
		for _, id := range ids {
			if id == "" {
				return fmt.Errorf("signers for %q: an empty name", capture)
			}
			if rest, ok := strings.CutPrefix(id, "spiffe://"); ok && strings.HasSuffix(id, "/") && !strings.Contains(strings.TrimSuffix(rest, "/"), "/") {
				return fmt.Errorf("signers for %q: %s would let every workload in the trust domain sign", capture, id)
			}
		}
	}
	return nil
}

// Check reports whether the entry's signature is valid for its event bytes under trust.
// CheckSignature says why when it isn't.
func (e Entry) Check(t Trust) bool {
	return e.CheckSignature(t) == nil
}

// CheckSignature returns nil when the entry's signature is valid for its event bytes
// under trust, and otherwise why it isn't. The signer must be a trusted note format key
// or an X.509-SVID that chains to a trusted bundle, and t.Signers must allow it to sign
// the event's capture (see Trust). An SVID is checked as of when the collector received
// the event, give or take MaxClockSkew, and the event must have started no more than
// MaxEventAge before that, so a signer can't pick the time its certificate is checked at.
// The collector runs the same check as events arrive.
func (e Entry) CheckSignature(t Trust) error {
	ev, err := e.Decode()
	if err != nil {
		return err
	}
	if e.Signature == "" {
		return errors.New("not signed")
	}
	capture := ev.String(Capture)
	if VerifySignature(e.Event, e.Signature, e.Signer, t.Keys) {
		if _, listed := t.Signers[capture]; listed && !t.Signers.Allows(capture, e.Signer) {
			return fmt.Errorf("%s may not sign events captured by %q", e.Signer, capture)
		}
		return nil
	}
	if e.Certificate == "" {
		if _, ok := t.Keys[e.Signer]; ok {
			return errors.New("signature doesn't verify")
		}
		return fmt.Errorf("%s isn't a trusted signer", e.Signer)
	}
	if t.Bundles == nil {
		return errors.New("signed with an X.509-SVID, and no trust bundle is given")
	}
	if !t.Signers.Allows(capture, e.Signer) {
		return fmt.Errorf("%s may not sign events captured by %q", e.Signer, capture)
	}
	if e.Received == 0 {
		return errors.New("no time the collector received it, to check the X.509-SVID at")
	}
	received := time.Unix(0, int64(e.Received))
	if start := ev.StartTime(); start.After(received.Add(MaxClockSkew)) {
		return fmt.Errorf("started at %s, after the collector received it at %s", start.UTC().Format(time.RFC3339), received.UTC().Format(time.RFC3339))
	} else if received.Sub(start) > MaxEventAge {
		return fmt.Errorf("started at %s, more than %s before the collector received it at %s", start.UTC().Format(time.RFC3339), MaxEventAge, received.UTC().Format(time.RFC3339))
	}
	return verifySVID(e.Event, e.Signature, e.Signer, e.Certificate, t.Bundles, received, MaxClockSkew)
}

// StartTime is when the event started.
func (ev Event) StartTime() time.Time {
	return time.Unix(0, int64(ev.StartTimeUnixNano))
}

// VerifySVIDSignature checks a signature made with an X.509-SVID's key, as the proxy
// signs (docs/proxy.md). certificate is base64 of the DER SVID followed by any
// intermediates. The SVID must chain to one of bundles as of at. It must be an
// end-entity certificate with exactly one spiffe:// URI SAN, equal to signer, and a P-256
// key. signature is base64 ASN.1 DER ECDSA over the SHA-256 of msg.
//
// It doesn't say whether signer may sign the event, or which time to check at. Use
// Entry.CheckSignature, which checks both.
func VerifySVIDSignature(msg []byte, signature, signer, certificate string, bundles *x509.CertPool, at time.Time) error {
	return verifySVID(msg, signature, signer, certificate, bundles, at, 0)
}

// verifySVID is VerifySVIDSignature, accepting a certificate that became valid or expired
// within skew of at.
func verifySVID(msg []byte, signature, signer, certificate string, bundles *x509.CertPool, at time.Time, skew time.Duration) error {
	der, err := base64.StdEncoding.DecodeString(certificate)
	if err != nil {
		return fmt.Errorf("certificate: %w", err)
	}
	certs, err := x509.ParseCertificates(der)
	if err != nil {
		return fmt.Errorf("certificate: %w", err)
	}
	if len(certs) == 0 {
		return errors.New("certificate: none given")
	}
	leaf := certs[0]
	intermediates := x509.NewCertPool()
	for _, c := range certs[1:] {
		intermediates.AddCert(c)
	}
	if leaf.IsCA {
		return errors.New("certificate: a CA can't sign events")
	}
	if at.Before(leaf.NotBefore) && leaf.NotBefore.Sub(at) <= skew {
		at = leaf.NotBefore
	} else if at.After(leaf.NotAfter) && at.Sub(leaf.NotAfter) <= skew {
		at = leaf.NotAfter
	}
	if _, err := leaf.Verify(x509.VerifyOptions{
		Roots:         bundles,
		Intermediates: intermediates,
		CurrentTime:   at,
		KeyUsages:     []x509.ExtKeyUsage{x509.ExtKeyUsageAny},
	}); err != nil {
		return fmt.Errorf("certificate: %w", err)
	}
	var ids []string
	for _, u := range leaf.URIs {
		if u.Scheme == "spiffe" {
			ids = append(ids, u.String())
		}
	}
	if len(ids) != 1 || ids[0] != signer {
		return fmt.Errorf("certificate: SPIFFE IDs %v, want exactly %q", ids, signer)
	}
	key, ok := leaf.PublicKey.(*ecdsa.PublicKey)
	if !ok || key.Curve != elliptic.P256() {
		return errors.New("certificate: key is not ECDSA P-256")
	}
	sig, err := base64.StdEncoding.DecodeString(signature)
	if err != nil {
		return fmt.Errorf("signature: %w", err)
	}
	digest := sha256.Sum256(msg)
	if !ecdsa.VerifyASN1(key, digest[:], sig) {
		return errors.New("signature doesn't verify")
	}
	return nil
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
