package main

import (
	"bytes"
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
	"math/big"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	wakelog "github.com/manikoa/wake/log"
	"golang.org/x/mod/sumdb/note"
)

const payload = `{"amount":500,"account":"999-1234"}`

func event(id string) []byte {
	sum := sha256.Sum256([]byte(payload))
	return []byte(`{"attributes":{"wake.capture":"sdk","wake.event.id":"` + id + `","wake.payload.sha256":"` + hex.EncodeToString(sum[:]) +
		`"},"end_time_unix_nano":2,"span_id":"0102030405060708","start_time_unix_nano":1,"trace_id":"0102030405060708090a0b0c0d0e0f10"}`)
}

// testLog is a log being written for a test, with the collector's key.
type testLog struct {
	t      *testing.T
	dir    string
	key    note.Signer
	logKey string // the log's key file, once it's been written
	args   []string
}

func newTestLog(t *testing.T) *testLog {
	t.Helper()
	l := &testLog{t: t, dir: t.TempDir()}
	private, public, err := note.GenerateKey(rand.Reader, "collector")
	if err != nil {
		t.Fatal(err)
	}
	if l.key, err = note.NewSigner(private); err != nil {
		t.Fatal(err)
	}
	l.args = []string{"verify", "-log", filepath.Join(l.dir, "log"), "-key", l.write("collector.pub", public)}
	return l
}

func (l *testLog) write(name, data string) string {
	path := filepath.Join(l.dir, name)
	if err := os.WriteFile(path, []byte(data+"\n"), 0o600); err != nil {
		l.t.Fatal(err)
	}
	return path
}

// sign signs ev with the collector's key.
func (l *testLog) sign(ev []byte) string {
	sig, err := l.key.Sign(ev)
	if err != nil {
		l.t.Fatal(err)
	}
	return base64.StdEncoding.EncodeToString(sig)
}

// entry encodes an entry for ev, signed by the collector unless f says otherwise.
func (l *testLog) entry(ev []byte, f wakelog.Fields) []byte {
	if f.Signer == "" {
		f.Signature, f.Signer = l.sign(ev), "collector"
	}
	data, _, err := wakelog.Encode(ev, f)
	if err != nil {
		l.t.Fatal(err)
	}
	return data
}

// append writes the log with these entries, and payload stored next to the tiles.
func (l *testLog) append(entries ...[]byte) {
	l.t.Helper()
	if l.logKey == "" {
		logPrivate, logPublic, err := wakelog.GenerateKey("test-log")
		if err != nil {
			l.t.Fatal(err)
		}
		l.logKey = l.write("log.key", logPrivate)
		l.args = append(l.args, "-log-key", l.write("log.pub", logPublic))
	}
	w, err := wakelog.Open(wakelog.Config{Dir: filepath.Join(l.dir, "log"), KeyFile: l.logKey})
	if err != nil {
		l.t.Fatal(err)
	}
	ctx := context.Background()
	if err := w.PutPayload(ctx, []byte(payload)); err != nil {
		l.t.Fatal(err)
	}
	if _, err := w.Add(ctx, entries); err != nil {
		l.t.Fatal(err)
	}
	if err := w.Close(ctx); err != nil {
		l.t.Fatal(err)
	}
}

func (l *testLog) verify(extra ...string) (code int, stdout, stderr string) {
	var out, errOut bytes.Buffer
	code = run(append(append([]string{}, l.args...), extra...), &out, &errOut)
	return code, out.String(), errOut.String()
}

// A log with two signed events carrying the same payload: one in its entry, one stored
// next to the tiles.
func writeLog(t *testing.T) *testLog {
	l := newTestLog(t)
	p := payload
	l.append(l.entry(event("e1"), wakelog.Fields{Payload: &p, Valid: true}), l.entry(event("e2"), wakelog.Fields{Valid: true}))
	return l
}

func payloadFile(t *testing.T, l *testLog) string {
	sum := sha256.Sum256([]byte(payload))
	rel, err := wakelog.PayloadPath(hex.EncodeToString(sum[:]))
	if err != nil {
		t.Fatal(err)
	}
	return filepath.Join(l.dir, "log", filepath.FromSlash(rel))
}

func TestVerifyPrintsEntriesWithTheirPayloads(t *testing.T) {
	code, stdout, stderr := writeLog(t).verify("-entries")
	if code != 0 {
		t.Fatalf("exit %d: %s", code, stderr)
	}
	lines := strings.Split(strings.TrimSpace(stdout), "\n")
	if len(lines) != 2 {
		t.Fatalf("%d lines: %s", len(lines), stdout)
	}
	for _, line := range lines {
		var entry struct {
			Signed  bool            `json:"signed"`
			Valid   bool            `json:"valid"`
			Payload json.RawMessage `json:"payload"`
		}
		if err := json.Unmarshal([]byte(line), &entry); err != nil {
			t.Fatal(err)
		}
		if !entry.Signed || !entry.Valid || string(entry.Payload) != payload {
			t.Errorf("entry = %s", line)
		}
	}
}

func TestVerifyFailsOnATamperedPayload(t *testing.T) {
	l := writeLog(t)
	if err := os.WriteFile(payloadFile(t, l), []byte(`{"amount":5,"account":"999-1234"}`), 0o644); err != nil {
		t.Fatal(err)
	}
	code, stdout, stderr := l.verify("-entries")
	if code != 1 {
		t.Fatalf("exit %d for a tampered payload: %s", code, stderr)
	}
	if !strings.Contains(stdout, `"payload_mismatch":true`) {
		t.Fatalf("no mismatch reported: %s", stdout)
	}
}

// Payloads are checked with or without -entries, and one that's gone fails the log.
func TestVerifyFailsWhenAPayloadIsMissing(t *testing.T) {
	l := writeLog(t)
	if err := os.Remove(payloadFile(t, l)); err != nil {
		t.Fatal(err)
	}
	code, _, stderr := l.verify()
	if code != 1 {
		t.Fatalf("exit %d with a payload missing: %s", code, stderr)
	}
	if !strings.Contains(stderr, "entry 1: payload") || !strings.Contains(stderr, "logged valid but don't check") {
		t.Fatalf("missing payload not reported: %s", stderr)
	}
}

// The collector logs events whose signatures fail, as evidence. Those are expected; what
// fails a log is an entry whose check now differs from what was logged.
func TestVerifyComparesEachEntryWithWhatWasLogged(t *testing.T) {
	forged := func(l *testLog, valid bool) []byte {
		// The right name, another key.
		sig, err := newTestLog(t).key.Sign(event("forged"))
		if err != nil {
			t.Fatal(err)
		}
		return l.entry(event("forged"), wakelog.Fields{Signature: base64.StdEncoding.EncodeToString(sig), Signer: "collector", Valid: valid})
	}

	l := newTestLog(t)
	l.append(l.entry(event("e1"), wakelog.Fields{Valid: true}), forged(l, false))
	if code, _, stderr := l.verify(); code != 0 || !strings.Contains(stderr, "1 entries were logged invalid and still don't check") {
		t.Errorf("a forged event the collector logged invalid: exit %d: %s", code, stderr)
	}

	l = newTestLog(t)
	l.append(forged(l, true))
	if code, _, stderr := l.verify(); code != 1 || !strings.Contains(stderr, "logged valid but don't check now") {
		t.Errorf("an entry logged valid that doesn't check: exit %d: %s", code, stderr)
	}

	l = newTestLog(t)
	l.append(l.entry(event("e1"), wakelog.Fields{Valid: false}))
	if code, _, stderr := l.verify(); code != 1 || !strings.Contains(stderr, "logged invalid but check now") {
		t.Errorf("an entry logged invalid that checks: exit %d: %s", code, stderr)
	}
}

// A run keeps the checkpoint it checked, and a later run holds the log to it.
func TestVerifyHoldsALogToTheCheckpointItKept(t *testing.T) {
	l := newTestLog(t)
	l.append(l.entry(event("e1"), wakelog.Fields{Valid: true}))
	current := filepath.Join(l.dir, "log", "checkpoint")
	older, err := os.ReadFile(current)
	if err != nil {
		t.Fatal(err)
	}
	l.append(l.entry(event("e2"), wakelog.Fields{Valid: true}))

	kept := filepath.Join(l.dir, "kept.checkpoint")
	if code, _, stderr := l.verify("-save", kept); code != 0 || !strings.Contains(stderr, "no earlier checkpoint to compare with") {
		t.Fatalf("first run: exit %d: %s", code, stderr)
	}
	whole, err := os.ReadFile(kept)
	if err != nil {
		t.Fatal(err)
	}
	if lines := strings.Split(string(whole), "\n"); len(lines) < 2 || lines[1] != "2" {
		t.Fatalf("kept %q", whole)
	}
	code, _, stderr := l.verify("-earlier", kept, "-save", kept)
	if code != 0 || !strings.Contains(stderr, "it holds what 1 earlier checkpoints signed") {
		t.Fatalf("the log as it was: exit %d: %s", code, stderr)
	}

	// Whoever can write the log's storage puts the older checkpoint back. Alone, the log
	// looks whole, one entry shorter.
	if err := os.WriteFile(current, older, 0o644); err != nil {
		t.Fatal(err)
	}
	if code, _, stderr := l.verify(); code != 0 || !strings.Contains(stderr, "1 entries, all in the signed tree") {
		t.Fatalf("with nothing to compare: exit %d: %s", code, stderr)
	}
	code, _, stderr = l.verify("-earlier", kept, "-save", kept)
	if code != 1 || !strings.Contains(stderr, "the log was cut back") {
		t.Fatalf("a log cut back: exit %d: %s", code, stderr)
	}
	// What it kept is still there to compare with next time.
	saved, err := os.ReadFile(kept)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(saved, whole) {
		t.Fatal("a run that failed moved the kept checkpoint")
	}
}

func TestAnEarlierCheckpointThatsGoneFailsTheRun(t *testing.T) {
	l := writeLog(t)
	code, _, stderr := l.verify("-earlier", filepath.Join(l.dir, "gone.checkpoint"))
	if code != 2 || !strings.Contains(stderr, "-earlier") {
		t.Fatalf("exit %d: %s", code, stderr)
	}
}

func TestAnEarlierCheckpointMustBeTheLogsOwn(t *testing.T) {
	l := writeLog(t)
	other := writeLog(t)
	code, _, stderr := l.verify("-earlier", filepath.Join(other.dir, "log", "checkpoint"))
	if code != 1 || !strings.Contains(stderr, "earlier checkpoint") {
		t.Fatalf("exit %d: %s", code, stderr)
	}
}

func TestVerifyChecksWhoMaySignEachCapture(t *testing.T) {
	now := time.Now()
	caKey, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	caTmpl := &x509.Certificate{SerialNumber: big.NewInt(1), NotBefore: now.Add(-time.Hour), NotAfter: now.Add(time.Hour),
		IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign}
	caDER, err := x509.CreateCertificate(rand.Reader, caTmpl, caTmpl, caKey.Public(), caKey)
	if err != nil {
		t.Fatal(err)
	}
	ca, _ := x509.ParseCertificate(caDER)
	// signedBy returns a proxy event signed with an SVID for id, from the same bundle.
	signedBy := func(l *testLog, id string) []byte {
		key, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
		u, _ := url.Parse(id)
		tmpl := &x509.Certificate{SerialNumber: big.NewInt(2), NotBefore: now.Add(-time.Hour), NotAfter: now.Add(time.Hour),
			URIs: []*url.URL{u}, KeyUsage: x509.KeyUsageDigitalSignature}
		der, err := x509.CreateCertificate(rand.Reader, tmpl, ca, key.Public(), caKey)
		if err != nil {
			t.Fatal(err)
		}
		ev := []byte(strings.Replace(string(event("p1")), `"wake.capture":"sdk"`, `"wake.capture":"proxy"`, 1))
		ev = []byte(strings.Replace(string(ev), `"start_time_unix_nano":1`, `"start_time_unix_nano":`+strconv.FormatInt(now.UnixNano(), 10), 1))
		digest := sha256.Sum256(ev)
		sig, err := key.Sign(rand.Reader, digest[:], crypto.SHA256)
		if err != nil {
			t.Fatal(err)
		}
		return l.entry(ev, wakelog.Fields{Signature: base64.StdEncoding.EncodeToString(sig), Signer: id,
			Certificate: base64.StdEncoding.EncodeToString(der), Received: uint64(now.UnixNano()), Valid: true})
	}

	l := newTestLog(t)
	bundle := l.write("bundle.pem", string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: caDER})))
	l.append(signedBy(l, "spiffe://wake.test/proxy"))
	if code, _, stderr := l.verify("-bundle", bundle, "-signer", "proxy=spiffe://wake.test/proxy"); code != 0 {
		t.Errorf("the proxy's event: exit %d: %s", code, stderr)
	}
	if code, _, stderr := l.verify("-bundle", bundle, "-signer", "proxy=spiffe://wake.test/other"); code != 1 || !strings.Contains(stderr, "may not sign") {
		t.Errorf("the proxy's event with another proxy trusted: exit %d: %s", code, stderr)
	}
	if code, _, stderr := l.verify("-bundle", bundle); code != 2 || !strings.Contains(stderr, "-bundle needs -signer") {
		t.Errorf("-bundle without -signer: exit %d: %s", code, stderr)
	}
	for _, bad := range []string{"spiffe://wake.test/proxy", "proxy=", "proxy=spiffe://wake.test/"} {
		if code, _, stderr := l.verify("-bundle", bundle, "-signer", bad); code != 2 {
			t.Errorf("-signer %s: exit %d: %s", bad, code, stderr)
		}
	}

	// An agent's SVID chains to the same bundle. An event it signed as the proxy, logged
	// valid by a collector that didn't check signers, fails now.
	l = newTestLog(t)
	bundle = l.write("bundle.pem", string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: caDER})))
	l.append(signedBy(l, "spiffe://wake.test/agent/email"))
	if code, _, stderr := l.verify("-bundle", bundle, "-signer", "proxy=spiffe://wake.test/proxy"); code != 1 || !strings.Contains(stderr, "may not sign") {
		t.Errorf("an agent signing as the proxy: exit %d: %s", code, stderr)
	}
}
