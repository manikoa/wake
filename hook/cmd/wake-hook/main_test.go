package main

import (
	"bytes"
	"strings"
	"testing"

	"github.com/usewake/wake/hook"
)

// Exit code 2 is how the hosts hear a refusal they can't be told in their own words: an
// event the hook can't read, with --stop. Anything else exits 0.
func TestExitsTwoOnlyToRefuseAnEventItCantRead(t *testing.T) {
	defer func(n int64) { maxEvent = n }(maxEvent)
	maxEvent = 256
	prompt := `{"hook_event_name":"UserPromptSubmit","session_id":"s1","prompt":"hi"}`
	for _, c := range []struct {
		name, in string
		stop     bool
		code     int
		stdout   string // part of what's printed
		stderr   string
	}{
		{"not json", "{", true, 2, "", hook.Unreadable},
		{"not json, not checked", "{", false, 0, "", ""},
		{"cut at the limit", prompt + strings.Repeat(" ", 256), true, 2, "", hook.Unreadable},
		{"cut at the limit, not checked", prompt + strings.Repeat(" ", 256), false, 0, "", ""},
		{"at the limit", prompt + strings.Repeat(" ", 256-len(prompt)), true, 0, `"decision":"block"`, ""},
		{"refused in its own words", prompt, true, 0, `"decision":"block"`, ""},
		{"let through", prompt, false, 0, "", ""},
	} {
		t.Run(c.name, func(t *testing.T) {
			args := []string{"claude-code", "--dir", t.TempDir()}
			if c.stop {
				args = append(args, "--stop")
			}
			var stdout, stderr bytes.Buffer
			code := run(args, strings.NewReader(c.in), &stdout, &stderr)
			if code != c.code || !strings.Contains(stdout.String(), c.stdout) || c.stdout == "" && stdout.Len() != 0 ||
				strings.TrimSpace(stderr.String()) != c.stderr {
				t.Errorf("exited %d, printed %q and %q", code, stdout.String(), stderr.String())
			}
		})
	}
}
