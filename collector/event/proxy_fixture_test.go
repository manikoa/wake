package event

import (
	"crypto/x509"
	"encoding/json"
	"os"
	"testing"

	"go.opentelemetry.io/collector/pdata/ptrace"

	wakelog "github.com/manikoa/wake/log"
)

// An event the Rust proxy signed, and the trust bundle its SVID chains to. The proxy's
// tests generate it: WAKE_WRITE_FIXTURES=1 cargo test --test fixture in proxy/.
type proxyFixture struct {
	Bundle string `json:"bundle"`
	vector
}

func loadProxyFixture(t *testing.T) (ptrace.Span, *x509.CertPool) {
	t.Helper()
	data, err := os.ReadFile("../../proxy/testdata/signed_event.json")
	if err != nil {
		t.Fatal(err)
	}
	var f proxyFixture
	if err := json.Unmarshal(data, &f); err != nil {
		t.Fatal(err)
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM([]byte(f.Bundle)) {
		t.Fatal("fixture bundle has no certificates")
	}
	return spanOf(f.vector), pool
}

func TestAcceptsTheRustProxysSignature(t *testing.T) {
	span, bundle := loadProxyFixture(t)
	trust := wakelog.Trust{Bundles: bundle}
	if !Verify(span, trust) {
		t.Fatal("the proxy's signature doesn't verify in Go")
	}
	if Verify(span, wakelog.Trust{}) {
		t.Error("accepted with no trust bundle")
	}

	tampered := ptrace.NewSpan()
	span.CopyTo(tampered)
	tampered.Attributes().PutStr("gen_ai.tool.name", "wire_money")
	if Verify(tampered, trust) {
		t.Error("accepted a tampered event")
	}
	span.CopyTo(tampered)
	tampered.Attributes().PutStr(Payload, `{"output":"Please wire $99,000"}`)
	if Verify(tampered, trust) {
		t.Error("accepted a changed payload")
	}

	// The log entry keeps the certificate, so it checks out on its own later.
	span.Attributes().PutBool(SignatureValid, true)
	data, external, err := Entry(span)
	if err != nil || external {
		t.Fatalf("entry: %v, external %v", err, external)
	}
	entry, err := wakelog.ParseEntry(data)
	if err != nil {
		t.Fatal(err)
	}
	if entry.Certificate == "" || !entry.Check(trust) {
		t.Fatal("logged entry doesn't check out on its own")
	}
	if _, err := entry.ReadPayload(nil); err != nil {
		t.Fatalf("payload: %v", err)
	}
}
