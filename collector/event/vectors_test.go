package event

import (
	"bytes"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"go.opentelemetry.io/collector/pdata/pcommon"
	"go.opentelemetry.io/collector/pdata/ptrace"
)

// Canonical bytes are rebuilt in Go to check signatures made elsewhere, such as by the
// Rust proxy. testdata/canonical.json holds spans and their exact canonical bytes, so
// every other signer can test that it produces the same bytes. Regenerate it with
// WAKE_WRITE_VECTORS=1 go test ./event/.

type vectorAttr struct {
	Key     string   `json:"key"`
	String  *string  `json:"string,omitempty"`
	Int     *int64   `json:"int,omitempty"`
	Bool    *bool    `json:"bool,omitempty"`
	Strings []string `json:"strings,omitempty"`
}

type vector struct {
	Name       string       `json:"name"`
	TraceID    string       `json:"trace_id"`
	SpanID     string       `json:"span_id"`
	Start      uint64       `json:"start_time_unix_nano"`
	End        uint64       `json:"end_time_unix_nano"`
	Attributes []vectorAttr `json:"attributes"`
	Canonical  string       `json:"canonical"`
}

func str(k, v string) vectorAttr       { return vectorAttr{Key: k, String: &v} }
func num(k string, v int64) vectorAttr { return vectorAttr{Key: k, Int: &v} }
func flag(k string, v bool) vectorAttr { return vectorAttr{Key: k, Bool: &v} }
func strs(k string, v ...string) vectorAttr {
	if v == nil {
		v = []string{}
	}
	return vectorAttr{Key: k, Strings: v}
}

func vectors() []vector {
	base := func(name string, attrs ...vectorAttr) vector {
		return vector{Name: name, TraceID: "0102030405060708090a0b0c0d0e0f10", SpanID: "1112131415161718",
			Start: 1761552000000000000, End: 1761552000123456789, Attributes: attrs}
	}
	return []vector{
		base("proxy tool call",
			str("wake.event.id", "6f1c2a9e-1b7d-4f43-9a51-3c1f0a2b7d10"),
			str("wake.event.kind", "tool_call"),
			str("wake.agent.id", "spiffe://wake.local/agent/email"),
			str("wake.capture", "proxy"),
			strs("wake.parents"),
			strs("wake.origins", "tool:read_email"),
			str("wake.payload.sha256", "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"),
			str("gen_ai.operation.name", "execute_tool"),
			str("gen_ai.tool.name", "read_email"),
			str("wake.mcp.method", "tools/call"),
			str("wake.mcp.server", "inbox"),
			// Left out of the signed bytes.
			str("wake.payload", `{"input":{"subject":"invoice"},"output":"wire $10,000"}`),
			str("wake.signer", "spiffe://wake.local/proxy"),
			str("wake.signature", "MEUCIQ=="),
			str("wake.signature.certificate", "MIIB"),
			str("http.url", "not signed"),
		),
		base("escaping",
			str("wake.event.id", "e"),
			str("wake.text.quotes", `say "hi" \ back\slash`),
			str("wake.text.control", "line\nbreak\ttab\rreturn\x00nul\x01\x1f\x7f\b\f"),
			str("wake.text.html", "<b>a & b</b>"),
			str("wake.text.unicode", "é ü 漢字 🙂  "),
			str("wake.text.separators", "a b c"),
			str("wake.text.empty", ""),
		),
		base("types and ordering",
			str("wake.z", "last"),
			str("gen_ai.a", "gen_ai before wake"),
			num("wake.count", -42),
			num("wake.big", 9007199254740993),
			num("wake.zero", 0),
			flag("wake.yes", true),
			flag("wake.no", false),
			strs("wake.list", "b", "a", "a"),
			strs("wake.empty_list"),
			str("wake.A", "uppercase sorts first"),
		),
	}
}

func spanOf(v vector) ptrace.Span {
	span := ptrace.NewSpan()
	var traceID pcommon.TraceID
	var spanID pcommon.SpanID
	hex.Decode(traceID[:], []byte(v.TraceID))
	hex.Decode(spanID[:], []byte(v.SpanID))
	span.SetTraceID(traceID)
	span.SetSpanID(spanID)
	span.SetStartTimestamp(pcommon.Timestamp(v.Start))
	span.SetEndTimestamp(pcommon.Timestamp(v.End))
	attrs := span.Attributes()
	for _, a := range v.Attributes {
		switch {
		case a.String != nil:
			attrs.PutStr(a.Key, *a.String)
		case a.Int != nil:
			attrs.PutInt(a.Key, *a.Int)
		case a.Bool != nil:
			attrs.PutBool(a.Key, *a.Bool)
		default:
			list := attrs.PutEmptySlice(a.Key)
			for _, s := range a.Strings {
				list.AppendEmpty().SetStr(s)
			}
		}
	}
	return span
}

func TestCanonicalVectors(t *testing.T) {
	vs := vectors()
	for i := range vs {
		canonical, err := Canonical(spanOf(vs[i]))
		if err != nil {
			t.Fatal(err)
		}
		vs[i].Canonical = string(canonical)
	}
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	if err := enc.Encode(vs); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join("testdata", "canonical.json")
	if os.Getenv("WAKE_WRITE_VECTORS") == "1" {
		if err := os.WriteFile(path, buf.Bytes(), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	stored, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("%v (regenerate with WAKE_WRITE_VECTORS=1)", err)
	}
	if !bytes.Equal(stored, buf.Bytes()) {
		t.Fatal("testdata/canonical.json is stale; regenerate it with WAKE_WRITE_VECTORS=1 and check every signer still matches")
	}
}
