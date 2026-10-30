package wakesignprocessor

import (
	"context"
	"crypto"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"math"
	"math/big"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"go.opentelemetry.io/collector/pdata/pcommon"
	"go.opentelemetry.io/collector/pdata/ptrace"
	"golang.org/x/mod/sumdb/note"

	"github.com/usewake/wake/collector/event"

	wakelog "github.com/usewake/wake/log"
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

	if !valid(sdk) || event.Verify(sdk, wakelog.Trust{Keys: collectorTrust}) != nil {
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

// What wake-hook serve sends for a short session (hook/testdata/otlp.json): a prompt, a
// tool call and a refusal, captured by a coding agent's hooks.
func TestSignsHookEvents(t *testing.T) {
	s, _, collectorTrust := setup(t)
	data, err := os.ReadFile("../../../hook/testdata/otlp.json")
	if err != nil {
		t.Fatal(err)
	}
	td, err := (&ptrace.JSONUnmarshaler{}).UnmarshalTraces(data)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := s.processTraces(context.Background(), td); err != nil {
		t.Fatal(err)
	}
	spans := td.ResourceSpans().At(0).ScopeSpans().At(0).Spans()
	if spans.Len() != 3 {
		t.Fatalf("read %d spans, want 3", spans.Len())
	}
	for i := 0; i < spans.Len(); i++ {
		span := spans.At(i)
		if capture, _ := span.Attributes().Get(event.Capture); capture.Str() != "hook" {
			t.Errorf("%s captured by %q", span.Name(), capture.Str())
		}
		if !valid(span) || event.Verify(span, wakelog.Trust{Keys: collectorTrust}) != nil {
			t.Errorf("%s wasn't signed by the collector: %s", span.Name(), reason(span))
		}
	}
}

// spanFromWire decodes one span the way the receiver does, keeping whatever attributes
// the sender put on it, repeated keys included.
func spanFromWire(t *testing.T, attributes string) (ptrace.Traces, ptrace.Span) {
	t.Helper()
	td, err := (&ptrace.JSONUnmarshaler{}).UnmarshalTraces([]byte(`{"resourceSpans":[{"scopeSpans":[{"spans":[{
		"traceId":"0102030405060708090a0b0c0d0e0f10","spanId":"0102030405060708","name":"x",
		"attributes":[` + attributes + `]}]}]}]}`))
	if err != nil {
		t.Fatal(err)
	}
	return td, td.ResourceSpans().At(0).ScopeSpans().At(0).Spans().At(0)
}

// A key sent twice reads one way to a lookup and another in the signed bytes. Such an
// event is kept, but neither signed nor trusted.
func TestRefusesRepeatedAttributeKeys(t *testing.T) {
	s, _, _ := setup(t)
	digest := sha256.Sum256([]byte("another event's payload"))
	for name, attributes := range map[string]string{
		"capture": `{"key":"wake.event.kind","value":{"stringValue":"tool_call"}},
			{"key":"wake.capture","value":{"stringValue":"sdk"}},
			{"key":"wake.capture","value":{"stringValue":"proxy"}}`,
		"payload digest": `{"key":"wake.event.kind","value":{"stringValue":"tool_call"}},
			{"key":"wake.capture","value":{"stringValue":"sdk"}},
			{"key":"wake.payload","value":{"stringValue":"what was sent"}},
			{"key":"wake.payload.sha256","value":{"stringValue":"0000"}},
			{"key":"wake.payload.sha256","value":{"stringValue":"` + hex.EncodeToString(digest[:]) + `"}}`,
	} {
		t.Run(name, func(t *testing.T) {
			td, span := spanFromWire(t, attributes)
			if _, err := s.processTraces(context.Background(), td); err != nil {
				t.Fatal(err)
			}
			if valid(span) {
				t.Fatal("an event with a repeated key was marked valid")
			}
			if !strings.Contains(reason(span), "more than once") {
				t.Errorf("reason %q doesn't name the repeated key", reason(span))
			}
			if _, signed := span.Attributes().Get(event.Signature); signed {
				t.Error("an event with a repeated key was signed")
			}
			seen := map[string]bool{}
			for key := range span.Attributes().All() {
				if seen[key] {
					t.Errorf("%s is still repeated", key)
				}
				seen[key] = true
			}
		})
	}
}

// Every event the collector logs has its payload redacted first: those it signs, and those
// it keeps as evidence without trusting them, which reach the log all the same.
func TestRedactsEveryPayloadItLogs(t *testing.T) {
	s, _, _ := setup(t)
	token := "ghp_" + strings.Repeat("a", 36)
	payload := `{"input": {"token": "` + token + `"}}`
	type arrival struct {
		td    ptrace.Traces
		span  ptrace.Span
		valid bool
	}
	arrive := func(valid bool, change func(ptrace.Span)) arrival {
		td := ptrace.NewTraces()
		td.ResourceSpans().AppendEmpty().ScopeSpans().AppendEmpty()
		span := addSpan(td, "sdk")
		span.Attributes().PutStr(event.Payload, payload)
		change(span)
		return arrival{td, span, valid}
	}
	repeatedTD, repeated := spanFromWire(t, `{"key":"wake.event.kind","value":{"stringValue":"tool_call"}},
		{"key":"wake.capture","value":{"stringValue":"sdk"}},
		{"key":"wake.payload","value":{"stringValue":"{\"token\": \"`+token+`\"}"}},
		{"key":"wake.capture","value":{"stringValue":"proxy"}}`)
	for name, a := range map[string]arrival{
		"signed by the collector": arrive(true, func(ptrace.Span) {}),
		"sent as bytes": arrive(true, func(span ptrace.Span) {
			span.Attributes().PutEmptyBytes(event.Payload).FromRaw([]byte(payload))
		}),
		"with a signature that fails": arrive(false, func(span ptrace.Span) {
			span.Attributes().PutStr(event.Signature, "AAAA")
		}),
		"unsigned, from the proxy": arrive(false, func(span ptrace.Span) {
			span.Attributes().PutStr(event.Capture, "proxy")
		}),
		"with a repeated key": {repeatedTD, repeated, false},
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := s.processTraces(context.Background(), a.td); err != nil {
				t.Fatal(err)
			}
			if valid(a.span) != a.valid {
				t.Fatalf("valid %v: %s", valid(a.span), reason(a.span))
			}
			data, _, err := event.Entry(a.span)
			if err != nil {
				t.Fatal(err)
			}
			if strings.Contains(string(data), token) {
				t.Fatalf("the log entry holds the token: %s", data)
			}
			// What's kept can still be read from the log, and checked against its digest.
			entry, err := wakelog.ParseEntry(data)
			if err != nil {
				t.Fatal(err)
			}
			logged, err := entry.ReadPayload(nil)
			if err != nil || !strings.Contains(string(logged), "[redacted github_token sha256:") {
				t.Errorf("payload %s, %v", logged, err)
			}
			ev, _ := entry.Decode()
			if kinds := ev.Strings(event.Redacted); len(kinds) != 1 || kinds[0] != "github_token" {
				t.Errorf("%s: %v", event.Redacted, kinds)
			}
		})
	}

	// A map shows bytes in it as base64, where nothing can be redacted, so a payload that
	// isn't a string or bytes isn't logged at all.
	td := ptrace.NewTraces()
	td.ResourceSpans().AppendEmpty().ScopeSpans().AppendEmpty()
	span := addSpan(td, "sdk")
	span.Attributes().PutEmptyMap(event.Payload).PutEmptyBytes("token").FromRaw([]byte(token))
	if _, err := s.processTraces(context.Background(), td); err != nil {
		t.Fatal(err)
	}
	if valid(span) || !strings.Contains(reason(span), "must be a string or bytes") {
		t.Errorf("a map payload: valid %v, reason %q", valid(span), reason(span))
	}
	if _, ok := span.Attributes().Get(event.Payload); ok {
		t.Error("kept a map payload")
	}
}

// The collector can't change an event the proxy validly signed without breaking its
// signature, so its payload is logged as the proxy signed it: the proxy redacts its own.
func TestLeavesAnEventValidlySignedElsewhereAsItIs(t *testing.T) {
	s, proxySigner, _ := setup(t)
	td := ptrace.NewTraces()
	td.ResourceSpans().AppendEmpty().ScopeSpans().AppendEmpty()
	span := addSpan(td, "proxy")
	payload := `{"input": {"token": "ghp_` + strings.Repeat("a", 36) + `"}}`
	span.Attributes().PutStr(event.Payload, payload)
	// Signed as a proxy from before redaction signed it.
	sum := sha256.Sum256([]byte(payload))
	span.Attributes().PutStr(event.PayloadSHA256, hex.EncodeToString(sum[:]))
	msg, err := event.Canonical(span)
	if err != nil {
		t.Fatal(err)
	}
	sig, err := proxySigner.Sign(msg)
	if err != nil {
		t.Fatal(err)
	}
	span.Attributes().PutStr(event.Signature, base64.StdEncoding.EncodeToString(sig))
	span.Attributes().PutStr(event.Signer, proxySigner.Name())

	if _, err := s.processTraces(context.Background(), td); err != nil {
		t.Fatal(err)
	}
	if got, _ := event.PayloadOf(span); !valid(span) || string(got) != payload {
		t.Errorf("valid %v (%s), payload %s", valid(span), reason(span), got)
	}
}

// The collector signs an SDK event with its own key, so a certificate the sender attached
// mustn't ride along into the entry.
func TestDropsSignatureFieldsTheSenderSet(t *testing.T) {
	s, _, _ := setup(t)
	td := ptrace.NewTraces()
	td.ResourceSpans().AppendEmpty().ScopeSpans().AppendEmpty()
	sdk := addSpan(td, "sdk")
	sdk.Attributes().PutStr(event.Certificate, "c29tZSBjZXJ0aWZpY2F0ZQ==")
	if _, err := s.processTraces(context.Background(), td); err != nil {
		t.Fatal(err)
	}
	if !valid(sdk) {
		t.Fatalf("SDK event not signed: %s", reason(sdk))
	}
	if _, ok := sdk.Attributes().Get(event.Certificate); ok {
		t.Error("the sender's certificate was kept on a collector-signed event")
	}
}

func TestConfigRequiresSigningKey(t *testing.T) {
	if err := (&Config{}).Validate(); err == nil {
		t.Fatal("empty config validated")
	}
}

func TestConfigRequiresSignersForTrustBundles(t *testing.T) {
	for _, c := range []Config{
		{SigningKeyFile: "k", TrustBundles: []string{"b.pem"}},
		{SigningKeyFile: "k", TrustBundles: []string{"b.pem"}, TrustedSigners: map[string][]string{"proxy": {"spiffe://wake.local/"}}},
		{SigningKeyFile: "k", TrustedSigners: map[string][]string{"proxy": {}}},
	} {
		if err := c.Validate(); err == nil {
			t.Errorf("%+v validated", c)
		}
	}
	ok := Config{SigningKeyFile: "k", TrustBundles: []string{"b.pem"}, TrustedSigners: map[string][]string{"proxy": {"spiffe://wake.local/proxy"}}}
	if err := ok.Validate(); err != nil {
		t.Error(err)
	}
}

// identity is a stand-in SPIRE: one trust bundle, with an SVID for each workload.
type identity struct {
	t      *testing.T
	ca     *x509.Certificate
	key    *ecdsa.PrivateKey
	bundle string
}

func newIdentity(t *testing.T, dir string) *identity {
	t.Helper()
	key, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	now := time.Now()
	tmpl := &x509.Certificate{SerialNumber: big.NewInt(1), NotBefore: now.Add(-48 * time.Hour), NotAfter: now.Add(48 * time.Hour),
		IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, key.Public(), key)
	if err != nil {
		t.Fatal(err)
	}
	ca, _ := x509.ParseCertificate(der)
	bundle := filepath.Join(dir, "bundle.pem")
	if err := os.WriteFile(bundle, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), 0o644); err != nil {
		t.Fatal(err)
	}
	return &identity{t: t, ca: ca, key: key, bundle: bundle}
}

// sign signs span as id would, with an SVID valid from notBefore to notAfter.
func (i *identity) sign(span ptrace.Span, id string, notBefore, notAfter time.Time) {
	i.t.Helper()
	key, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	u, _ := url.Parse(id)
	tmpl := &x509.Certificate{SerialNumber: big.NewInt(time.Now().UnixNano()), NotBefore: notBefore, NotAfter: notAfter,
		URIs: []*url.URL{u}, KeyUsage: x509.KeyUsageDigitalSignature}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, i.ca, key.Public(), i.key)
	if err != nil {
		i.t.Fatal(err)
	}
	msg, err := event.Canonical(span)
	if err != nil {
		i.t.Fatal(err)
	}
	digest := sha256.Sum256(msg)
	sig, err := key.Sign(rand.Reader, digest[:], crypto.SHA256)
	if err != nil {
		i.t.Fatal(err)
	}
	attrs := span.Attributes()
	attrs.PutStr(event.Signature, base64.StdEncoding.EncodeToString(sig))
	attrs.PutStr(event.Signer, id)
	attrs.PutStr(event.Certificate, base64.StdEncoding.EncodeToString(der))
}

func reason(span ptrace.Span) string {
	v, _ := span.Attributes().Get(event.SignatureError)
	return v.Str()
}

// Agents get SVIDs from the same SPIRE as the proxy. Before trusted_signers, any SVID in
// the bundle could sign an event as the proxy, so an agent could forge its own record.
func TestOnlyTrustedSignersMaySignACapture(t *testing.T) {
	dir := t.TempDir()
	id := newIdentity(t, dir)
	collectorKey, _ := writeKey(t, dir, "collector")
	s, err := newSigner(&Config{SigningKeyFile: collectorKey, TrustBundles: []string{id.bundle},
		TrustedSigners: map[string][]string{"proxy": {"spiffe://wake.test/proxy"}}})
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	td := ptrace.NewTraces()
	td.ResourceSpans().AppendEmpty().ScopeSpans().AppendEmpty()
	signed := func(capture, signer string) ptrace.Span {
		span := addSpan(td, capture)
		span.SetStartTimestamp(pcommon.NewTimestampFromTime(now.Add(-time.Second)))
		id.sign(span, signer, now.Add(-time.Hour), now.Add(time.Hour))
		return span
	}
	proxy := signed("proxy", "spiffe://wake.test/proxy")
	forged := signed("proxy", "spiffe://wake.test/agent/email")
	forgedSDK := signed("sdk", "spiffe://wake.test/agent/email")
	proxyAsSDK := signed("sdk", "spiffe://wake.test/proxy")

	if _, err := s.processTraces(context.Background(), td); err != nil {
		t.Fatal(err)
	}
	if !valid(proxy) {
		t.Errorf("the proxy's event marked invalid: %s", reason(proxy))
	}
	for name, span := range map[string]ptrace.Span{"an agent signing as the proxy": forged, "an agent signing an SDK event": forgedSDK, "the proxy signing an SDK event": proxyAsSDK} {
		if valid(span) || !strings.Contains(reason(span), "may not sign") {
			t.Errorf("%s: valid %v, reason %q", name, valid(span), reason(span))
		}
	}
}

// The signer chooses an event's start time, so the collector checks the SVID as of when it
// receives the event, and records that time for checks of the log later.
func TestChecksTheSVIDWhenTheEventArrives(t *testing.T) {
	dir := t.TempDir()
	id := newIdentity(t, dir)
	collectorKey, _ := writeKey(t, dir, "collector")
	s, err := newSigner(&Config{SigningKeyFile: collectorKey, TrustBundles: []string{id.bundle},
		TrustedSigners: map[string][]string{"proxy": {"spiffe://wake.test/proxy"}}})
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	s.now = func() time.Time { return now }
	td := ptrace.NewTraces()
	td.ResourceSpans().AppendEmpty().ScopeSpans().AppendEmpty()
	// A key from an SVID that expired two hours ago, signing an event dated while it was valid.
	stale := addSpan(td, "proxy")
	stale.SetStartTimestamp(pcommon.NewTimestampFromTime(now.Add(-3 * time.Hour)))
	stale.Attributes().PutInt(event.Received, now.Add(-3*time.Hour).UnixNano()) // a sender's claim, ignored
	id.sign(stale, "spiffe://wake.test/proxy", now.Add(-4*time.Hour), now.Add(-2*time.Hour))
	fresh := addSpan(td, "proxy")
	fresh.SetStartTimestamp(pcommon.NewTimestampFromTime(now.Add(-time.Second)))
	id.sign(fresh, "spiffe://wake.test/proxy", now.Add(-time.Hour), now.Add(time.Hour))

	if _, err := s.processTraces(context.Background(), td); err != nil {
		t.Fatal(err)
	}
	if valid(stale) || !strings.Contains(reason(stale), "expired") {
		t.Errorf("an event signed with an expired SVID: valid %v, reason %q", valid(stale), reason(stale))
	}
	if !valid(fresh) {
		t.Errorf("a fresh event marked invalid: %s", reason(fresh))
	}
	for _, span := range []ptrace.Span{stale, fresh} {
		if got, _ := span.Attributes().Get(event.Received); got.Int() != now.UnixNano() {
			t.Errorf("wake.received_unix_nano = %d, want the time it arrived", got.Int())
		}
	}
	// The log entry carries the time, so a check of the log later reaches the same verdict.
	data, _, err := event.Entry(fresh)
	if err != nil {
		t.Fatal(err)
	}
	entry, err := wakelog.ParseEntry(data)
	if err != nil {
		t.Fatal(err)
	}
	if err := entry.CheckSignature(s.trust); err != nil {
		t.Errorf("the logged entry doesn't check: %v", err)
	}
}

// A double that isn't finite has no canonical bytes. That event is marked invalid, and
// the rest of its request goes on, where it used to fail the whole OTLP request.
func TestOneEventThatCantBeSignedDoesntFailTheRequest(t *testing.T) {
	s, _, _ := setup(t)
	td := ptrace.NewTraces()
	td.ResourceSpans().AppendEmpty().ScopeSpans().AppendEmpty()
	before := addSpan(td, "sdk")
	poison := addSpan(td, "sdk")
	poison.Attributes().PutDouble("gen_ai.request.temperature", math.NaN())
	poison.Attributes().PutStr(event.SignatureError, "a sender's claim, replaced")
	after := addSpan(td, "memory_adapter")

	if _, err := s.processTraces(context.Background(), td); err != nil {
		t.Fatalf("one bad event failed the request: %v", err)
	}
	if !valid(before) || !valid(after) {
		t.Error("the good events weren't signed")
	}
	if valid(poison) || !strings.Contains(reason(poison), "NaN") {
		t.Errorf("the bad event: valid %v, reason %q", valid(poison), reason(poison))
	}
	if r := reason(before); r != "" {
		t.Errorf("a valid event has a reason: %q", r)
	}
}

// The Rust proxy's signed fixture (proxy/testdata/signed_event.json) arriving at the
// processor, checked against the trust bundle in its config.
func TestChecksSVIDSignedEventsAgainstTrustBundles(t *testing.T) {
	data, err := os.ReadFile("../../../proxy/testdata/signed_event.json")
	if err != nil {
		t.Fatal(err)
	}
	var fixture struct {
		Bundle     string `json:"bundle"`
		TraceID    string `json:"trace_id"`
		SpanID     string `json:"span_id"`
		Start      uint64 `json:"start_time_unix_nano"`
		End        uint64 `json:"end_time_unix_nano"`
		Attributes []struct {
			Key     string   `json:"key"`
			String  *string  `json:"string"`
			Strings []string `json:"strings"`
		} `json:"attributes"`
	}
	if err := json.Unmarshal(data, &fixture); err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	bundle := filepath.Join(dir, "bundle.pem")
	if err := os.WriteFile(bundle, []byte(fixture.Bundle), 0o644); err != nil {
		t.Fatal(err)
	}
	collectorKey, _ := writeKey(t, dir, "collector")

	arrive := func(s *signer) ptrace.Span {
		td := ptrace.NewTraces()
		span := td.ResourceSpans().AppendEmpty().ScopeSpans().AppendEmpty().Spans().AppendEmpty()
		var traceID pcommon.TraceID
		var spanID pcommon.SpanID
		hex.Decode(traceID[:], []byte(fixture.TraceID))
		hex.Decode(spanID[:], []byte(fixture.SpanID))
		span.SetTraceID(traceID)
		span.SetSpanID(spanID)
		span.SetStartTimestamp(pcommon.Timestamp(fixture.Start))
		span.SetEndTimestamp(pcommon.Timestamp(fixture.End))
		for _, a := range fixture.Attributes {
			if a.String != nil {
				span.Attributes().PutStr(a.Key, *a.String)
				continue
			}
			list := span.Attributes().PutEmptySlice(a.Key)
			for _, v := range a.Strings {
				list.AppendEmpty().SetStr(v)
			}
		}
		// A sender can't claim its own event is valid.
		span.Attributes().PutBool(event.SignatureValid, true)
		if _, err := s.processTraces(context.Background(), td); err != nil {
			t.Fatal(err)
		}
		return span
	}

	// The fixture was signed long ago; the collector receives it a second after it started.
	received := func() time.Time { return time.Unix(0, int64(fixture.Start)).Add(time.Second) }
	proxy := map[string][]string{"proxy": {"spiffe://wake.local/proxy"}}
	with, err := newSigner(&Config{SigningKeyFile: collectorKey, TrustBundles: []string{bundle}, TrustedSigners: proxy})
	if err != nil {
		t.Fatal(err)
	}
	with.now = received
	if span := arrive(with); !valid(span) {
		t.Errorf("the proxy's event wasn't valid with its trust bundle configured: %s", reason(span))
	}
	without, err := newSigner(&Config{SigningKeyFile: collectorKey, TrustedSigners: proxy})
	if err != nil {
		t.Fatal(err)
	}
	without.now = received
	if valid(arrive(without)) {
		t.Error("the proxy's event was valid with no trust bundle")
	}
	// Received today, a year after it started, the proxy's event is too old to accept.
	late, err := newSigner(&Config{SigningKeyFile: collectorKey, TrustBundles: []string{bundle}, TrustedSigners: proxy})
	if err != nil {
		t.Fatal(err)
	}
	if span := arrive(late); valid(span) || !strings.Contains(reason(span), "before the collector received it") {
		t.Errorf("an event received long after it started: valid %v, reason %q", valid(span), reason(span))
	}

	empty := filepath.Join(dir, "empty.pem")
	os.WriteFile(empty, []byte("no certificates here"), 0o644)
	if _, err := newSigner(&Config{SigningKeyFile: collectorKey, TrustBundles: []string{empty}}); err == nil {
		t.Error("accepted a trust bundle with no certificates")
	}
	if _, err := newSigner(&Config{SigningKeyFile: collectorKey, TrustBundles: []string{filepath.Join(dir, "missing.pem")}}); err == nil {
		t.Error("accepted a missing trust bundle")
	}
}
