package hook

import "encoding/json"

// The OTLP/JSON encoding of a span, as the collector's OTLP/HTTP receiver reads it: ids
// in hex, 64-bit integers as strings.

const spanKindInternal = 1

type span struct {
	TraceID    string     `json:"traceId"`
	SpanID     string     `json:"spanId"`
	Name       string     `json:"name"`
	Kind       int        `json:"kind"`
	Start      string     `json:"startTimeUnixNano"`
	End        string     `json:"endTimeUnixNano"`
	Attributes []keyValue `json:"attributes"`
}

type keyValue struct {
	Key   string   `json:"key"`
	Value anyValue `json:"value"`
}

type anyValue struct {
	String *string     `json:"stringValue,omitempty"`
	Array  *arrayValue `json:"arrayValue,omitempty"`
}

type arrayValue struct {
	Values []anyValue `json:"values"`
}

func (s *span) str(key, value string) {
	s.Attributes = append(s.Attributes, keyValue{Key: key, Value: anyValue{String: &value}})
}

func (s *span) strs(key string, values []string) {
	array := &arrayValue{Values: []anyValue{}}
	for _, v := range values {
		array.Values = append(array.Values, anyValue{String: &v})
	}
	s.Attributes = append(s.Attributes, keyValue{Key: key, Value: anyValue{Array: array}})
}

// exportRequest wraps spooled spans in an OTLP/HTTP trace export request.
func exportRequest(spans []json.RawMessage, version string) []byte {
	name := "wake-hook"
	return encode(map[string]any{
		"resourceSpans": []any{map[string]any{
			"resource": map[string]any{"attributes": []keyValue{
				{Key: "service.name", Value: anyValue{String: &name}},
			}},
			"scopeSpans": []any{map[string]any{
				"scope": map[string]any{"name": name, "version": version},
				"spans": spans,
			}},
		}},
	})
}
