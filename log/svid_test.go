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
		NotBefore:             eventTime.Add(-48 * time.Hour),
		NotAfter:              eventTime.Add(48 * time.Hour),
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

const proxyID = "spiffe://wake.test/proxy"

// trustProxy trusts SVIDs from root, and lets only the proxy sign proxy events.
func trustProxy(root *ca) Trust {
	return Trust{Bundles: root.pool(), Signers: Signers{"proxy": {proxyID}}}
}

// svidEntry is an entry for event signed by signer, which the collector received at
// received, or at no recorded time if it's zero.
func svidEntry(t *testing.T, event, signer, certificate, signature string, received time.Time) Entry {
	t.Helper()
	f := Fields{Signature: signature, Signer: signer, Certificate: certificate, Valid: true}
	if !received.IsZero() {
		f.Received = uint64(received.UnixNano())
	}
	data, _, err := Encode([]byte(event), f)
	if err != nil {
		t.Fatal(err)
	}
	e, err := ParseEntry(data)
	if err != nil {
		t.Fatal(err)
	}
	return e
}

// proxyEntry is svidEvent signed by the proxy, received a second after it started.
func proxyEntry(t *testing.T, certificate, signature string) Entry {
	t.Helper()
	return svidEntry(t, svidEvent, proxyID, certificate, signature, eventTime.Add(time.Second))
}

func TestCheckAcceptsAnSVIDThatChainsToTheBundle(t *testing.T) {
	root := newCA(t, nil)
	cert, key := issue(t, root, svid{ids: []string{proxyID}})
	e := proxyEntry(t, cert, signWith(t, key, svidEvent))
	if e.Certificate != cert {
		t.Fatal("certificate not kept in the entry")
	}
	if err := e.CheckSignature(trustProxy(root)); err != nil {
		t.Fatalf("valid SVID signature rejected: %v", err)
	}
	// An intermediate in the chain, as SPIRE with an upstream authority issues.
	intermediate := newCA(t, root)
	cert, key = issue(t, intermediate, svid{ids: []string{proxyID}}, intermediate)
	if err := proxyEntry(t, cert, signWith(t, key, svidEvent)).CheckSignature(trustProxy(root)); err != nil {
		t.Fatalf("SVID with an intermediate rejected: %v", err)
	}
}

func TestCheckRejectsBadSVIDSignatures(t *testing.T) {
	root, other := newCA(t, nil), newCA(t, nil)
	p384, _ := ecdsa.GenerateKey(elliptic.P384(), rand.Reader)
	good, goodKey := issue(t, root, svid{ids: []string{proxyID}})
	signed := func(s svid, issuer *ca) Entry {
		c, k := issue(t, issuer, s)
		return proxyEntry(t, c, signWith(t, k, svidEvent))
	}
	proxy := []string{proxyID}

	cases := []struct {
		name  string
		entry Entry
		want  string // in the error, so each case fails for its own reason
	}{
		{"another bundle's SVID", signed(svid{ids: proxy}, other), "unknown authority"},
		{"signer isn't the SVID's ID", signed(svid{ids: []string{"spiffe://wake.test/agent"}}, root), "want exactly"},
		{"two SPIFFE IDs", signed(svid{ids: []string{proxyID, "spiffe://wake.test/other"}}, root), "want exactly"},
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
		{"no time it was received", svidEntry(t, svidEvent, proxyID, good, signWith(t, goodKey, svidEvent), time.Time{}), "no time"},
	}
	for _, c := range cases {
		if c.entry.Check(trustProxy(root)) {
			t.Errorf("%s: accepted", c.name)
		}
		err := c.entry.CheckSignature(trustProxy(root))
		if err == nil || !strings.Contains(err.Error(), c.want) {
			t.Errorf("%s: err = %v, want it to mention %q", c.name, err, c.want)
		}
	}
	// No certificate at all.
	e := proxyEntry(t, "", signWith(t, goodKey, svidEvent))
	if err := VerifySVIDSignature(e.Event, e.Signature, e.Signer, e.Certificate, root.pool(), eventTime); err == nil || !strings.Contains(err.Error(), "none given") {
		t.Errorf("no certificate: err = %v", err)
	}
	if e.Check(trustProxy(root)) {
		t.Error("no certificate: accepted")
	}
	// No bundles configured: no SVID signature can be accepted.
	if proxyEntry(t, good, signWith(t, goodKey, svidEvent)).Check(Trust{Signers: Signers{"proxy": proxy}}) {
		t.Error("accepted with no trust bundles")
	}
}

// Every workload in a trust domain has an SVID from the same bundle, agents included. An
// agent with its own SVID must not be able to sign its record as the proxy.
func TestCheckOnlyAcceptsSignersListedForTheCapture(t *testing.T) {
	root := newCA(t, nil)
	sdkEvent := strings.Replace(svidEvent, `"wake.capture":"proxy"`, `"wake.capture":"sdk"`, 1)
	noCapture := strings.Replace(svidEvent, `"wake.capture":"proxy",`, "", 1)
	received := eventTime.Add(time.Second)
	signedBy := func(id, event string) Entry {
		cert, key := issue(t, root, svid{ids: []string{id}})
		return svidEntry(t, event, id, cert, signWith(t, key, event), received)
	}
	collector, collectorKeys := testSigner(t, "collector")
	noteSigned := func(event string) Entry {
		sig, err := collector.Sign([]byte(event))
		if err != nil {
			t.Fatal(err)
		}
		return svidEntry(t, event, "collector", "", base64.StdEncoding.EncodeToString(sig), received)
	}
	prefix := Signers{"proxy": {"spiffe://wake.test/proxy/"}}

	cases := []struct {
		name    string
		entry   Entry
		signers Signers
		keys    bool
		want    string // empty when it's accepted
	}{
		{"the proxy signs a proxy event", signedBy(proxyID, svidEvent), nil, false, ""},
		{"an agent signs a proxy event", signedBy("spiffe://wake.test/agent/email", svidEvent), nil, false, "may not sign"},
		{"an agent signs an SDK event", signedBy("spiffe://wake.test/agent/email", sdkEvent), nil, false, "may not sign"},
		{"an agent signs an event with no capture", signedBy("spiffe://wake.test/agent/email", noCapture), nil, false, "may not sign"},
		{"the proxy signs an SDK event", signedBy(proxyID, sdkEvent), nil, false, "may not sign"},
		{"the brake signs a proxy event", signedBy("spiffe://wake.test/brake", svidEvent), nil, false, "may not sign"},
		{"a proxy under a listed prefix", signedBy("spiffe://wake.test/proxy/eu-1", svidEvent), prefix, false, ""},
		{"the prefix itself isn't under it", signedBy(proxyID, svidEvent), prefix, false, "may not sign"},
		{"a name that only starts the same", signedBy("spiffe://wake.test/proxyish", svidEvent), nil, false, "may not sign"},
		{"the collector signs an SDK event", noteSigned(sdkEvent), nil, true, ""},
		{"the collector signs a proxy event", noteSigned(svidEvent), nil, true, "may not sign"},
		{"the collector's key, untrusted", noteSigned(sdkEvent), nil, false, "isn't a trusted signer"},
	}
	for _, c := range cases {
		trust := trustProxy(root)
		if c.signers != nil {
			trust.Signers = c.signers
		}
		if c.keys {
			trust.Keys = collectorKeys
		}
		err := c.entry.CheckSignature(trust)
		switch {
		case c.want == "" && err != nil:
			t.Errorf("%s: rejected: %v", c.name, err)
		case c.want != "" && (err == nil || !strings.Contains(err.Error(), c.want)):
			t.Errorf("%s: err = %v, want it to mention %q", c.name, err, c.want)
		}
	}
	// Trusting the bundle alone trusts no one to sign anything.
	if signedBy(proxyID, svidEvent).Check(Trust{Bundles: root.pool()}) {
		t.Error("an SVID signature counted with no signers listed")
	}
}

// The signer chooses an event's start time, so the SVID is checked as of when the
// collector received the event. Otherwise a key taken from an expired SVID could still
// sign events dated back to when it was valid.
func TestCheckTakesTheSVIDsTimeFromTheCollector(t *testing.T) {
	root := newCA(t, nil)
	cert, key := issue(t, root, svid{ids: []string{proxyID}, notBefore: eventTime.Add(-30 * time.Minute), notAfter: eventTime.Add(30 * time.Minute)})
	at := func(received time.Time) error {
		return svidEntry(t, svidEvent, proxyID, cert, signWith(t, key, svidEvent), received).CheckSignature(trustProxy(root))
	}
	if err := at(eventTime.Add(time.Second)); err != nil {
		t.Fatalf("received while the SVID was valid: %v", err)
	}
	if err := at(eventTime.Add(30*time.Minute + MaxClockSkew - time.Second)); err != nil {
		t.Errorf("received within the clock skew of the SVID expiring: %v", err)
	}
	if err := at(eventTime.Add(2 * time.Hour)); err == nil || !strings.Contains(err.Error(), "expired") {
		t.Errorf("received after the SVID expired: err = %v", err)
	}
	if err := at(eventTime.Add(-MaxClockSkew - time.Minute)); err == nil || !strings.Contains(err.Error(), "after the collector received it") {
		t.Errorf("started after it was received: err = %v", err)
	}

	// A signer with a valid SVID can't date an event far back either.
	later, laterKey := issue(t, root, svid{ids: []string{proxyID}, notBefore: eventTime.Add(MaxEventAge), notAfter: eventTime.Add(MaxEventAge + time.Hour)})
	old := svidEntry(t, svidEvent, proxyID, later, signWith(t, laterKey, svidEvent), eventTime.Add(MaxEventAge+time.Minute))
	if err := old.CheckSignature(trustProxy(root)); err == nil || !strings.Contains(err.Error(), "more than") {
		t.Errorf("started more than MaxEventAge before it was received: err = %v", err)
	}
}

func TestSignersRefuseNamesThatTrustTooMuch(t *testing.T) {
	for _, s := range []Signers{
		{"proxy": {"spiffe://wake.test/"}},
		{"proxy": {""}},
		{"proxy": {}},
	} {
		if err := s.Validate(); err == nil {
			t.Errorf("%v validated", s)
		}
	}
	if err := (Signers{"proxy": {proxyID, "spiffe://wake.test/proxy/"}, "sdk": {"wake-collector"}}).Validate(); err != nil {
		t.Error(err)
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
