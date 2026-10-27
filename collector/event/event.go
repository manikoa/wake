// Package event signs and verifies Wake events, as defined in docs/events.md and docs/signing.md.
package event

import (
	"bytes"
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
)

// IsEvent reports whether span is a Wake event, as opposed to a span that only groups events.
func IsEvent(span ptrace.Span) bool {
	_, ok := span.Attributes().Get(Kind)
	return ok
}

// Canonical returns the exact bytes a signer signs for span: a JSON object with
// sorted keys and no whitespace, holding the span's ids, times, and every wake.*
// and gen_ai.* attribute except the signature fields.
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
	if key == Signer || strings.HasPrefix(key, Signature) {
		return false
	}
	return strings.HasPrefix(key, "wake.") || strings.HasPrefix(key, "gen_ai.")
}

// Sign signs span and records the signature and the signer's name on it.
func Sign(span ptrace.Span, signer note.Signer) error {
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

// Verify reports whether span carries a valid signature from one of the trusted signers,
// keyed by name.
func Verify(span ptrace.Span, trusted map[string]note.Verifier) bool {
	sigValue, ok := span.Attributes().Get(Signature)
	if !ok {
		return false
	}
	signerValue, ok := span.Attributes().Get(Signer)
	if !ok {
		return false
	}
	msg, err := Canonical(span)
	if err != nil {
		return false
	}
	return wakelog.VerifySignature(msg, sigValue.Str(), signerValue.Str(), trusted)
}

// Entry returns what gets appended to the signed log for span: the exact signed bytes,
// the signature and signer, and whether the collector found the signature valid. Each
// entry can be checked on its own with the signer's public key. The entry format lives
// in the log module, so the log's readers parse it with the same code.
func Entry(span ptrace.Span) ([]byte, error) {
	canonical, err := Canonical(span)
	if err != nil {
		return nil, err
	}
	attrs := span.Attributes()
	sig, _ := attrs.Get(Signature)
	signer, _ := attrs.Get(Signer)
	valid, _ := attrs.Get(SignatureValid)
	return wakelog.NewEntry(canonical, sig.Str(), signer.Str(), valid.Bool())
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
