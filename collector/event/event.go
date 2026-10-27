// Package event signs and verifies Wake events, as defined in docs/events.md and docs/signing.md.
package event

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"strings"

	"go.opentelemetry.io/collector/pdata/ptrace"
	"golang.org/x/mod/sumdb/note"

	wakelog "github.com/manikoa/wake/log"
)

// Attribute names used here. See docs/events.md for the full list.
const (
	Kind           = "wake.event.kind"
	Capture        = "wake.capture"
	Signature      = "wake.signature"
	Signer         = "wake.signer"
	SignatureValid = "wake.signature.valid"
	Certificate    = "wake.signature.certificate"
	Payload        = "wake.payload"
	PayloadSHA256  = wakelog.PayloadSHA256
)

// IsEvent reports whether span is a Wake event, as opposed to a span that only groups events.
func IsEvent(span ptrace.Span) bool {
	_, ok := span.Attributes().Get(Kind)
	return ok
}

// Canonical returns the exact bytes a signer signs for span: a JSON object with
// sorted keys and no whitespace, holding the span's ids, times, and every wake.*
// and gen_ai.* attribute except the signature fields and the payload. The payload's
// signed digest commits to it.
func Canonical(span ptrace.Span) ([]byte, error) {
	attributes := map[string]any{}
	for key, value := range span.Attributes().All() {
		if signed(key) {
			attributes[key] = value.AsRaw()
		}
	}
	traceID, spanID := span.TraceID(), span.SpanID()
	// Fields are declared in sorted order; encoding/json sorts map keys.
	doc := struct {
		Attributes map[string]any `json:"attributes"`
		EndTime    uint64         `json:"end_time_unix_nano"`
		SpanID     string         `json:"span_id"`
		StartTime  uint64         `json:"start_time_unix_nano"`
		TraceID    string         `json:"trace_id"`
	}{
		Attributes: attributes,
		EndTime:    uint64(span.EndTimestamp()),
		SpanID:     hex.EncodeToString(spanID[:]),
		StartTime:  uint64(span.StartTimestamp()),
		TraceID:    hex.EncodeToString(traceID[:]),
	}
	return marshal(doc)
}

func signed(key string) bool {
	if key == Signer || key == Payload || strings.HasPrefix(key, Signature) {
		return false
	}
	return strings.HasPrefix(key, "wake.") || strings.HasPrefix(key, "gen_ai.")
}

// PayloadOf returns span's payload, the bytes its digest is taken over, and whether it
// has one.
func PayloadOf(span ptrace.Span) ([]byte, bool) {
	value, ok := span.Attributes().Get(Payload)
	if !ok {
		return nil, false
	}
	return []byte(value.AsString()), true
}

func digest(payload []byte) string {
	sum := sha256.Sum256(payload)
	return hex.EncodeToString(sum[:])
}

// Sign signs span and records the signature and the signer's name on it. It sets the
// payload's digest first, from the bytes received, so the signature commits to the
// payload that arrived and not to a digest the sender claimed.
func Sign(span ptrace.Span, signer note.Signer) error {
	if payload, ok := PayloadOf(span); ok {
		span.Attributes().PutStr(PayloadSHA256, digest(payload))
	}
	msg, err := Canonical(span)
	if err != nil {
		return err
	}
	sig, err := signer.Sign(msg)
	if err != nil {
		return err
	}
	span.Attributes().PutStr(Signature, base64.StdEncoding.EncodeToString(sig))
	span.Attributes().PutStr(Signer, signer.Name())
	return nil
}

// Verify reports whether span carries a valid signature under trust, and a payload, if
// it has one, that matches the signed digest. A signer with a note format key, such as
// another collector, is checked against trust.Keys. A signer that signs with an
// X.509-SVID, such as the proxy, sends its certificate in wake.signature.certificate,
// and it's checked against trust.Bundles as of the event's start.
func Verify(span ptrace.Span, trust wakelog.Trust) bool {
	if payload, ok := PayloadOf(span); ok {
		signed, _ := span.Attributes().Get(PayloadSHA256)
		if signed.Str() != digest(payload) {
			return false
		}
	}
	attrs := span.Attributes()
	sigValue, ok := attrs.Get(Signature)
	if !ok {
		return false
	}
	signerValue, ok := attrs.Get(Signer)
	if !ok {
		return false
	}
	msg, err := Canonical(span)
	if err != nil {
		return false
	}
	if wakelog.VerifySignature(msg, sigValue.Str(), signerValue.Str(), trust.Keys) {
		return true
	}
	cert, ok := attrs.Get(Certificate)
	if !ok || trust.Bundles == nil {
		return false
	}
	at := span.StartTimestamp().AsTime()
	return wakelog.VerifySVIDSignature(msg, sigValue.Str(), signerValue.Str(), cert.Str(), trust.Bundles, at) == nil
}

// Entry returns what gets appended to the signed log for span: the exact signed bytes,
// the signature and signer, the signer's certificate if it signs with one, whether the
// collector found the signature valid, and the payload when it fits. Each entry can be
// checked on its own. When the payload doesn't fit, external is true: store
// PayloadOf(span) with the log's PutPayload before appending the entry. The entry format
// lives in the log module, so the log's readers parse it with the same code.
func Entry(span ptrace.Span) (entry []byte, external bool, err error) {
	canonical, err := Canonical(span)
	if err != nil {
		return nil, false, err
	}
	attrs := span.Attributes()
	sig, _ := attrs.Get(Signature)
	signer, _ := attrs.Get(Signer)
	cert, _ := attrs.Get(Certificate)
	valid, _ := attrs.Get(SignatureValid)
	fields := wakelog.Fields{Signature: sig.Str(), Signer: signer.Str(), Certificate: cert.Str(), Valid: valid.Bool()}
	if payload, ok := PayloadOf(span); ok {
		p := string(payload)
		fields.Payload = &p
	}
	entry, inline, err := wakelog.Encode(canonical, fields)
	return entry, !inline, err
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
