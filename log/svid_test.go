package wakelog

import (
	"crypto"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"math/big"
	"net/url"
	"strings"
	"testing"
	"time"
)

// The event below starts at 2026-10-27T00:00:00Z.
const svidEvent = `{"attributes":{"wake.capture":"proxy","wake.event.id":"p1","wake.event.kind":"tool_call"},"end_time_unix_nano":1761523201000000000,"span_id":"0102030405060708","start_time_unix_nano":1761523200000000000,"trace_id":"0102030405060708090a0b0c0d0e0f10"}`

var eventTime = time.Unix(0, 1761523200000000000)

type ca struct {
	cert *x509.Certificate
	key  crypto.Signer
}

func newCA(t *testing.T, parent *ca) *ca {
	t.Helper()
	key, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	tmpl := &x509.Certificate{
		SerialNumber:          big.NewInt(time.Now().UnixNano()),
		Subject:               pkix.Name{CommonName: "test trust bundle"},
		NotBefore:             eventTime.Add(-24 * time.Hour),
		NotAfter:              eventTime.Add(24 * time.Hour),
		IsCA:                  true,
		BasicConstraintsValid: true,
		KeyUsage:              x509.KeyUsageCertSign,
	}
	signerCert, signerKey := tmpl, crypto.Signer(key)
	if parent != nil {
		signerCert, signerKey = parent.cert, parent.key
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, signerCert, key.Public(), signerKey)
	if err != nil {
		t.Fatal(err)
	}
	cert, _ := x509.ParseCertificate(der)
	return &ca{cert: cert, key: key}
}

func (c *ca) pool() *x509.CertPool {
	p := x509.NewCertPool()
	p.AddCert(c.cert)
	return p
}

type svid struct {
	ids       []string
	notBefore time.Time
	notAfter  time.Time
	isCA      bool
	key       crypto.Signer
}

// issue returns the base64 certificate field for an SVID issued by issuer, and its key.
func issue(t *testing.T, issuer *ca, s svid, chain ...*ca) (string, crypto.Signer) {
	t.Helper()
	if s.key == nil {
		s.key, _ = ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	}
	if s.notBefore.IsZero() {
		s.notBefore, s.notAfter = eventTime.Add(-30*time.Minute), eventTime.Add(30*time.Minute)
	}
	tmpl := &x509.Certificate{
		SerialNumber:          big.NewInt(time.Now().UnixNano()),
		NotBefore:             s.notBefore,
		NotAfter:              s.notAfter,
		IsCA:                  s.isCA,
		BasicConstraintsValid: true,
		KeyUsage:              x509.KeyUsageDigitalSignature,
	}
	for _, id := range s.ids {
		u, _ := url.Parse(id)
		tmpl.URIs = append(tmpl.URIs, u)
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, issuer.cert, s.key.Public(), issuer.key)
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range chain {
		der = append(der, c.cert.Raw...)
	}
	return base64.StdEncoding.EncodeToString(der), s.key
}

func signWith(t *testing.T, key crypto.Signer, msg string) string {
	t.Helper()
	digest := sha256.Sum256([]byte(msg))
	sig, err := key.Sign(rand.Reader, digest[:], crypto.SHA256)
	if err != nil {
		t.Fatal(err)
	}
	return base64.StdEncoding.EncodeToString(sig)
}

func proxyEntry(t *testing.T, certificate, signature string) Entry {
	t.Helper()
	data, _, err := Encode([]byte(svidEvent), Fields{Signature: signature, Signer: "spiffe://wake.test/proxy", Certificate: certificate, Valid: true})
	if err != nil {
		t.Fatal(err)
	}
	e, err := ParseEntry(data)
	if err != nil {
		t.Fatal(err)
	}
	return e
}

func TestCheckAcceptsAnSVIDThatChainsToTheBundle(t *testing.T) {
	root := newCA(t, nil)
	cert, key := issue(t, root, svid{ids: []string{"spiffe://wake.test/proxy"}})
	e := proxyEntry(t, cert, signWith(t, key, svidEvent))
	if e.Certificate != cert {
		t.Fatal("certificate not kept in the entry")
	}
	if !e.Check(Trust{Bundles: root.pool()}) {
		t.Fatal("valid SVID signature rejected")
	}
	// An intermediate in the chain, as SPIRE with an upstream authority issues.
	intermediate := newCA(t, root)
	cert, key = issue(t, intermediate, svid{ids: []string{"spiffe://wake.test/proxy"}}, intermediate)
	if !proxyEntry(t, cert, signWith(t, key, svidEvent)).Check(Trust{Bundles: root.pool()}) {
		t.Fatal("SVID with an intermediate rejected")
	}
}

func TestCheckRejectsBadSVIDSignatures(t *testing.T) {
	root, other := newCA(t, nil), newCA(t, nil)
	p384, _ := ecdsa.GenerateKey(elliptic.P384(), rand.Reader)
	good, goodKey := issue(t, root, svid{ids: []string{"spiffe://wake.test/proxy"}})
	signed := func(s svid, issuer *ca) Entry {
		c, k := issue(t, issuer, s)
		return proxyEntry(t, c, signWith(t, k, svidEvent))
	}
	proxy := []string{"spiffe://wake.test/proxy"}

	cases := []struct {
		name  string
		entry Entry
		want  string // in the error, so each case fails for its own reason
	}{
		{"another bundle's SVID", signed(svid{ids: proxy}, other), "unknown authority"},
		{"signer isn't the SVID's ID", signed(svid{ids: []string{"spiffe://wake.test/agent"}}, root), "want exactly"},
		{"two SPIFFE IDs", signed(svid{ids: []string{"spiffe://wake.test/proxy", "spiffe://wake.test/other"}}, root), "want exactly"},
		{"expired before the event", signed(svid{ids: proxy, notBefore: eventTime.Add(-2 * time.Hour), notAfter: eventTime.Add(-time.Hour)}, root), "expired"},
		{"a CA signing events", signed(svid{ids: proxy, isCA: true}, root), "a CA can't sign"},
		{"a P-384 key", signed(svid{ids: proxy, key: p384}, root), "not ECDSA P-256"},
		{"tampered event", func() Entry {
			e := proxyEntry(t, good, signWith(t, goodKey, svidEvent))
			e.Event = []byte(strings.Replace(svidEvent, `"p1"`, `"p2"`, 1))
			return e
		}(), "doesn't verify"},
		{"signature by another key", func() Entry {
			_, k := issue(t, root, svid{ids: proxy})
			return proxyEntry(t, good, signWith(t, k, svidEvent))
		}(), "doesn't verify"},
		{"garbled certificate", proxyEntry(t, "not base64!", signWith(t, goodKey, svidEvent)), "illegal base64"},
		{"no certificate", proxyEntry(t, "", signWith(t, goodKey, svidEvent)), "none given"},
	}
	for _, c := range cases {
		if c.entry.Check(Trust{Bundles: root.pool()}) {
			t.Errorf("%s: accepted", c.name)
		}
		e := c.entry
		err := VerifySVIDSignature(e.Event, e.Signature, e.Signer, e.Certificate, root.pool(), eventTime)
		if err == nil || !strings.Contains(err.Error(), c.want) {
			t.Errorf("%s: err = %v, want it to mention %q", c.name, err, c.want)
		}
	}
	// No bundles configured: no SVID signature can be accepted.
	if proxyEntry(t, good, signWith(t, goodKey, svidEvent)).Check(Trust{}) {
		t.Error("accepted with no trust bundles")
	}
}

func TestCheckStillAcceptsNoteSigners(t *testing.T) {
	signer, trusted := testSigner(t, "collector")
	entry, err := ParseEntry(signedEntry(t, signer, signedEvent))
	if err != nil {
		t.Fatal(err)
	}
	if !entry.Check(Trust{Keys: trusted, Bundles: newCA(t, nil).pool()}) {
		t.Fatal("collector signature rejected")
	}
}
