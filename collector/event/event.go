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
// has one. A payload is text, sent as a string or as bytes, which are read as the bytes
// they hold: as text, pcommon gives them in base64, where no credential could be found to
// redact. It does the same to bytes in a map or a list, so a value of any other type isn't
// taken for a payload: Sign refuses an event that has one, and it never reaches the log.
func PayloadOf(span ptrace.Span) ([]byte, bool) {
	value, ok := span.Attributes().Get(Payload)
	if !ok {
		return nil, false
	}
	switch value.Type() {
	case pcommon.ValueTypeStr:
		return []byte(value.Str()), true
	case pcommon.ValueTypeBytes:
		return value.Bytes().AsRaw(), true
	}
	return nil, false
}

// notText says why span's payload isn't one, when it has a wake.payload of another type.
func notText(span ptrace.Span) error {
	value, ok := span.Attributes().Get(Payload)
	if !ok || value.Type() == pcommon.ValueTypeStr || value.Type() == pcommon.ValueTypeBytes {
		return nil
	}
	return fmt.Errorf("%s must be a string or bytes, not %s", Payload, value.Type())
}

// redactPayload replaces the secrets in span's payload, given as read. When it replaces
// any, it puts the payload back on span, as a string, and lists their kinds in
// wake.payload.redacted. It returns the payload as kept, and whether it changed.
func redactPayload(span ptrace.Span, payload []byte) ([]byte, bool) {
	redacted, kinds := Redact(string(payload))
	if len(kinds) == 0 {
		return payload, false
	}
	span.Attributes().PutStr(Payload, redacted)
	list := span.Attributes().PutEmptySlice(Redacted)
	for _, k := range kinds {
		list.AppendEmpty().SetStr(k)
	}
	return []byte(redacted), true
}

// RedactInvalid redacts the payload of an event the collector neither signed nor found
// validly signed, since it's logged all the same, as evidence. As signing would, it lists
// the kinds it replaced in wake.payload.redacted and sets wake.payload.sha256 from the
// payload kept, so the payload can still be read from the log and checked against its
// digest; the event stays invalid, and a signature it came with no longer covers it. An
// event with nothing to redact is left as it came, and a payload that isn't text, which
// can't be redacted, is dropped. An event validly signed elsewhere, such as the proxy's,
// can't be changed without breaking its signature: its signer redacts it.
func RedactInvalid(span ptrace.Span) {
	if notText(span) != nil {
		span.Attributes().Remove(Payload)
		return
	}
	if payload, ok := PayloadOf(span); ok {
		if payload, changed := redactPayload(span, payload); changed {
			span.Attributes().PutStr(PayloadSHA256, digest(payload))
		}
	}
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
// lists the kinds replaced; a list the sender set is dropped. It refuses an event whose
// payload isn't text (see PayloadOf).
func Sign(span ptrace.Span, signer note.Signer) error {
	span.Attributes().RemoveIf(func(key string, _ pcommon.Value) bool {
		return key == Signer || strings.HasPrefix(key, Signature) || key == Redacted
	})
	if err := notText(span); err != nil {
		return err
	}
	if payload, ok := PayloadOf(span); ok {
		payload, _ = redactPayload(span, payload)
		span.Attributes().PutStr(PayloadSHA256, digest(payload))
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
