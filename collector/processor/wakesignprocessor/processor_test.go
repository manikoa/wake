package wakesignprocessor

import (
	"context"
	"crypto/rand"
	"os"
	"path/filepath"
	"testing"

	"go.opentelemetry.io/collector/pdata/ptrace"
	"golang.org/x/mod/sumdb/note"

	"github.com/manikoa/wake/collector/event"
)

func writeKey(t *testing.T, dir, name string) (privatePath, publicPath string) {
	t.Helper()
	private, public, err := note.GenerateKey(rand.Reader, name)
	if err != nil {
		t.Fatal(err)
	}
	privatePath = filepath.Join(dir, name+".key")
	publicPath = filepath.Join(dir, name+".pub")
	if err := os.WriteFile(privatePath, []byte(private), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(publicPath, []byte(public), 0o644); err != nil {
		t.Fatal(err)
	}
	return privatePath, publicPath
}

func addSpan(td ptrace.Traces, capture string) ptrace.Span {
	span := td.ResourceSpans().At(0).ScopeSpans().At(0).Spans().AppendEmpty()
	span.Attributes().PutStr(event.Kind, "tool_call")
	span.Attributes().PutStr(event.Capture, capture)
	return span
}

func setup(t *testing.T) (*signer, note.Signer, map[string]note.Verifier) {
	t.Helper()
	dir := t.TempDir()
	collectorKey, collectorPub := writeKey(t, dir, "collector")
	proxyKey, proxyPub := writeKey(t, dir, "proxy")
	s, err := newSigner(&Config{SigningKeyFile: collectorKey, TrustedKeys: []string{proxyPub}})
	if err != nil {
		t.Fatal(err)
	}
	proxyPrivate, _ := os.ReadFile(proxyKey)
	proxySigner, err := note.NewSigner(string(proxyPrivate))
	if err != nil {
		t.Fatal(err)
	}
	collectorPublic, _ := os.ReadFile(collectorPub)
	collectorVerifier, err := note.NewVerifier(string(collectorPublic))
	if err != nil {
		t.Fatal(err)
	}
	return s, proxySigner, map[string]note.Verifier{"collector": collectorVerifier}
}

func valid(span ptrace.Span) bool {
	v, ok := span.Attributes().Get(event.SignatureValid)
	return ok && v.Bool()
}

func TestSignsSDKEventsAndChecksProxyEvents(t *testing.T) {
	s, proxySigner, collectorTrust := setup(t)

	td := ptrace.NewTraces()
	td.ResourceSpans().AppendEmpty().ScopeSpans().AppendEmpty()
	sdk := addSpan(td, "sdk")
	// The proxy signs the payload's digest, and sends the payload outside the signed bytes.
	proxyGood := addSpan(td, "proxy")
	proxyGood.Attributes().PutStr(event.Payload, `{"result":"ok"}`)
	if err := event.Sign(proxyGood, proxySigner); err != nil {
		t.Fatal(err)
	}
	proxyTampered := addSpan(td, "proxy")
	proxyTampered.Attributes().PutStr(event.Payload, `{"result":"ok"}`)
	if err := event.Sign(proxyTampered, proxySigner); err != nil {
		t.Fatal(err)
	}
	proxyTampered.Attributes().PutStr(event.Payload, "changed after signing")
	proxyUnsigned := addSpan(td, "proxy")
	proxyUnsigned.Attributes().PutBool(event.SignatureValid, true) // a sender's claim, ignored
	structure := td.ResourceSpans().At(0).ScopeSpans().At(0).Spans().AppendEmpty()

	if _, err := s.processTraces(context.Background(), td); err != nil {
		t.Fatal(err)
	}

	if !valid(sdk) || !event.Verify(sdk, collectorTrust) {
		t.Error("SDK event wasn't signed by the collector")
	}
	if !valid(proxyGood) {
		t.Error("proxy event with a good signature marked invalid")
	}
	if valid(proxyTampered) {
		t.Error("tampered proxy event marked valid")
	}
	if valid(proxyUnsigned) {
		t.Error("unsigned proxy event marked valid")
	}
	if _, ok := structure.Attributes().Get(event.Signature); ok {
		t.Error("structure span was signed")
	}
}

func TestConfigRequiresSigningKey(t *testing.T) {
	if err := (&Config{}).Validate(); err == nil {
		t.Fatal("empty config validated")
	}
}
