// Package event signs and verifies Wake events, as defined in docs/events.md and docs/signing.md.
package event

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"go.opentelemetry.io/collector/pdata/pcommon"
	"go.opentelemetry.io/collector/pdata/ptrace"
	"golang.org/x/mod/sumdb/note"

	wakelog "github.com/manikoa/wake/log"
)

// Attribute names used here. See docs/events.md for the full list.
const (
	Kind           = "wake.event.kind"
	Capture        = wakelog.Capture
	Signature      = "wake.signature"
	Signer         = "wake.signer"
	SignatureValid = "wake.signature.valid"
	// SignatureError says why wake.signature.valid is false.
	SignatureError = "wake.signature.error"
	Certificate    = "wake.signature.certificate"
	Payload        = "wake.payload"
	PayloadSHA256  = wakelog.PayloadSHA256
	// Received is when the collector received the event, in Unix nanoseconds. It's kept
	// out of the signed bytes, because the proxy signs before the collector receives.
	Received = "wake.received_unix_nano"
)

// IsEvent reports whether span is a Wake event, as opposed to a span that only groups events.
func IsEvent(span ptrace.Span) bool {
	_, ok := span.Attributes().Get(Kind)
	return ok
}

// DropRepeatedKeys keeps only the first attribute with each key and returns the first key
// that was repeated, if any. OTLP says keys are unique, but nothing on the wire enforces
// it, and a lookup (the first wins) and Canonical (the last wins) would read two
// different events from one span: a sender could get a capture or a payload digest signed
// that the collector never checked.
func DropRepeatedKeys(span ptrace.Span) (string, bool) {
	seen := map[string]bool{}
	repeated := ""
	span.Attributes().RemoveIf(func(key string, _ pcommon.Value) bool {
		if seen[key] {
			if repeated == "" {
				repeated = key
			}
			return true
		}
		seen[key] = true
		return false
	})
	return repeated, repeated != ""
}

// Canonical returns the exact bytes a signer signs for span: a JSON object with
// sorted keys and no whitespace, holding the span's ids, times, and every wake.*
// and gen_ai.* attribute except the signature fields, the payload and the time the
// collector received it. The payload's signed digest commits to it. It fails for a
// double that isn't finite, which JSON can't hold.
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
	if key == Signer || key == Payload || key == Received || strings.HasPrefix(key, Signature) {
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
// payload that arrived and not to a digest the sender claimed. An event with no payload
// gets no digest. Any signature fields the sender set, such as a certificate, are dropped
// first, so the entry carries only what this signer adds. Before any of that, the payload's
// secrets are replaced (redact.go), so they never reach the log, and wake.payload.redacted
// lists their kinds; a list the sender set is dropped.
func Sign(span ptrace.Span, signer note.Signer) error {
	span.Attributes().RemoveIf(func(key string, _ pcommon.Value) bool {
		return key == Signer || strings.HasPrefix(key, Signature) || key == Redacted
	})
	if payload, ok := PayloadOf(span); ok {
		redacted, kinds := Redact(string(payload))
		if redacted != string(payload) {
			span.Attributes().PutStr(Payload, redacted)
		}
		if len(kinds) > 0 {
			list := span.Attributes().PutEmptySlice(Redacted)
			for _, k := range kinds {
				list.AppendEmpty().SetStr(k)
			}
		}
		span.Attributes().PutStr(PayloadSHA256, digest([]byte(redacted)))
	} else {
		span.Attributes().Remove(PayloadSHA256)
	}
	msg, err := Canonical(span)
	if err != nil {
		return fmt.Errorf("canonical bytes: %w", err)
	}
	sig, err := signer.Sign(msg)
	if err != nil {
		return err
	}
	span.Attributes().PutStr(Signature, base64.StdEncoding.EncodeToString(sig))
	span.Attributes().PutStr(Signer, signer.Name())
	return nil
}

// Verify returns nil when span carries a valid signature under trust, and a payload, if
// it has one, that matches the signed digest; otherwise it says why not. A signer with a
// note format key, such as another collector, is checked against trust.Keys. A signer
// that signs with an X.509-SVID, such as the proxy, sends its certificate in
// wake.signature.certificate, and it's checked against trust.Bundles as of
// wake.received_unix_nano. Either way trust.Signers must allow the signer for the
// event's wake.capture. The checks are wakelog's Entry.CheckSignature, so what the
// collector decides here, anyone can check again from the log.
func Verify(span ptrace.Span, trust wakelog.Trust) error {
	attrs := span.Attributes()
	if payload, ok := PayloadOf(span); ok {
		signed, _ := attrs.Get(PayloadSHA256)
		if signed.Str() != digest(payload) {
			return wakelog.ErrPayloadMismatch
		}
	} else if _, ok := attrs.Get(PayloadSHA256); ok {
		return errors.New("signed a payload digest but sent no payload")
	}
	if _, ok := attrs.Get(Signature); !ok {
		return errors.New("not signed")
	}
	msg, err := Canonical(span)
	if err != nil {
		return fmt.Errorf("canonical bytes: %w", err)
	}
	sig, _ := attrs.Get(Signature)
	signer, _ := attrs.Get(Signer)
	cert, _ := attrs.Get(Certificate)
	received, _ := attrs.Get(Received)
	e := wakelog.Entry{Event: msg, Signature: sig.Str(), Signer: signer.Str(), Certificate: cert.Str(), Received: uint64(received.Int())}
	return e.CheckSignature(trust)
}

// Entry returns what gets appended to the signed log for span: the exact signed bytes,
// the signature and signer, the signer's certificate if it signs with one, when the
// collector received it, whether the collector found the signature valid, and the
// payload when it fits. Each entry can be
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
	received, _ := attrs.Get(Received)
	valid, _ := attrs.Get(SignatureValid)
	fields := wakelog.Fields{Signature: sig.Str(), Signer: signer.Str(), Certificate: cert.Str(), Received: uint64(received.Int()), Valid: valid.Bool()}
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
