package event

import (
	"crypto/x509"
	"encoding/json"
	"os"
	"testing"
	"time"

	"go.opentelemetry.io/collector/pdata/ptrace"

	wakelog "github.com/manikoa/wake/log"
)

// An event the Rust proxy signed, and the trust bundle its SVID chains to. The proxy's
// tests generate it: WAKE_WRITE_FIXTURES=1 cargo test --test fixture in proxy/.
type proxyFixture struct {
	Bundle string `json:"bundle"`
	vector
}

// loadProxyFixture returns the proxy's event as the collector received it, a second after
// it started, and the bundle its SVID chains to.
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
	span := spanOf(f.vector)
	span.Attributes().PutInt(Received, int64(f.Start)+int64(time.Second))
	return span, pool
}

func TestAcceptsTheRustProxysSignature(t *testing.T) {
	span, bundle := loadProxyFixture(t)
	trust := wakelog.Trust{Bundles: bundle, Signers: wakelog.Signers{"proxy": {"spiffe://wake.local/proxy"}}}
	if err := Verify(span, trust); err != nil {
		t.Fatalf("the proxy's signature doesn't verify in Go: %v", err)
	}
	if Verify(span, wakelog.Trust{Signers: trust.Signers}) == nil {
		t.Error("accepted with no trust bundle")
	}
	if Verify(span, wakelog.Trust{Bundles: bundle}) == nil {
		t.Error("accepted with no signer trusted to sign proxy events")
	}

	tampered := ptrace.NewSpan()
	span.CopyTo(tampered)
	tampered.Attributes().PutStr("gen_ai.tool.name", "wire_money")
	if Verify(tampered, trust) == nil {
		t.Error("accepted a tampered event")
	}
	span.CopyTo(tampered)
	tampered.Attributes().PutStr(Payload, `{"output":"Please wire $99,000"}`)
	if Verify(tampered, trust) == nil {
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
	if entry.Certificate == "" || entry.Received == 0 {
		t.Fatalf("entry = %+v", entry)
	}
	if err := entry.CheckSignature(trust); err != nil {
		t.Fatalf("logged entry doesn't check out on its own: %v", err)
	}
	if _, err := entry.ReadPayload(nil); err != nil {
		t.Fatalf("payload: %v", err)
	}
}
