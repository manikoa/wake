// Command wake-hook records what a coding agent does through its hooks, and refuses what
// Wake's brake halts. See hook/README.md.
package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"sort"
	"strings"
	"syscall"
	"time"

	"github.com/usewake/wake/hook"
)

// version is set when a release is built.
var version = "dev"

// The largest hook event read. Cursor sends a file's content with beforeReadFile. One
// larger can't be read whole, so it isn't read at all.
var maxEvent int64 = 64 << 20

func main() {
	os.Exit(run(os.Args[1:], os.Stdin, os.Stdout, os.Stderr))
}

func usage(w io.Writer) {
	hosts := make([]string, 0, len(hook.Hosts))
	for h := range hook.Hosts {
		hosts = append(hosts, h)
	}
	sort.Strings(hosts)
	fmt.Fprintf(w, `Usage:
  wake-hook <host> [--agent name] [--dir dir] [--stop] [--event name]
      Answer one hook event, read from stdin. host is %s.
      Antigravity's hooks name their event with --event.
  wake-hook serve --collector url [--rules url] [--agent name]... [--dir dir] [--ca file] [--once]
      Send what the hooks spool to the collector, and fetch the brake's rules for them.
  wake-hook version
`, strings.Join(hosts, ", "))
}

func run(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	if len(args) == 0 {
		usage(stderr)
		return 2
	}
	switch args[0] {
	case "serve":
		return serve(args[1:], stderr)
	case "version":
		fmt.Fprintln(stdout, version)
		return 0
	case "-h", "--help", "help":
		usage(stdout)
		return 0
	}
	host, ok := hook.Hosts[args[0]]
	if !ok {
		// Never 2: every host reads exit code 2 as a refusal.
		fmt.Fprintf(stderr, "wake-hook: no host %q\n", args[0])
		usage(stderr)
		return 1
	}
	flags := flag.NewFlagSet(args[0], flag.ContinueOnError)
	flags.SetOutput(stderr)
	o := hook.Options{}
	flags.StringVar(&o.Agent, "agent", os.Getenv("WAKE_AGENT"), "the agent's name, as the brake halts it (default: the host's name)")
	flags.StringVar(&o.Dir, "dir", defaultDir(), "where events wait for wake-hook serve")
	flags.BoolVar(&o.Stop, "stop", false, "refuse what the brake's halts and flow policy cover, failing closed without fresh rules from wake-hook serve")
	flags.StringVar(&o.Event, "event", "", "the hook event, for a host whose input doesn't name it (antigravity)")
	if err := flags.Parse(args[1:]); err != nil {
		return 1
	}
	in, err := io.ReadAll(io.LimitReader(stdin, maxEvent+1))
	if err != nil || int64(len(in)) > maxEvent {
		in = nil
	}
	out, refused := hook.Answer(host, in, o)
	if refused != "" {
		// An event it can't read could be any, so with --stop it's refused the one way
		// the hosts share.
		fmt.Fprintln(stderr, refused)
		return 2
	}
	stdout.Write(out)
	return 0
}

func serve(args []string, stderr io.Writer) int {
	flags := flag.NewFlagSet("serve", flag.ContinueOnError)
	flags.SetOutput(stderr)
	s := &hook.Server{Version: version}
	var ca string
	var agents list
	var once bool
	interval := flags.Duration("interval", 2*time.Second, "how often to send events and fetch the rules")
	flags.StringVar(&s.Dir, "dir", defaultDir(), "where the hooks spool events")
	flags.StringVar(&s.Collector, "collector", os.Getenv("WAKE_ENDPOINT"), "the collector's OTLP/HTTP endpoint, such as https://collector:4318")
	flags.StringVar(&ca, "ca", os.Getenv("WAKE_CA_FILE"), "a PEM file of the roots to trust for the collector and wake-proxy")
	flags.StringVar(&s.Rules, "rules", "", "wake-proxy's address for the brake's rules, such as http://wake-proxy:9003, for hooks run with --stop")
	flags.Var(&agents, "agent", "an agent to fetch rules for from the start (repeatable)")
	flags.BoolVar(&once, "once", false, "make one pass and exit")
	if err := flags.Parse(args); err != nil {
		return 2
	}
	if s.Collector == "" && s.Rules == "" {
		fmt.Fprintln(stderr, "wake-hook serve: give --collector, --rules or both")
		return 2
	}
	client, err := hook.NewClient(ca)
	if err != nil {
		fmt.Fprintf(stderr, "wake-hook serve: %v\n", err)
		return 1
	}
	s.Client, s.Agents = client, agents
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if once {
		s.Pass(ctx)
		return 0
	}
	s.Run(ctx, *interval)
	return 0
}

// defaultDir is $WAKE_HOOK_DIR, or wake-hook in the user's cache directory.
func defaultDir() string {
	if dir := os.Getenv("WAKE_HOOK_DIR"); dir != "" {
		return dir
	}
	if dir, err := os.UserCacheDir(); err == nil {
		return filepath.Join(dir, "wake-hook")
	}
	return filepath.Join(os.TempDir(), "wake-hook")
}

type list []string

func (l *list) String() string     { return strings.Join(*l, ",") }
func (l *list) Set(v string) error { *l = append(*l, v); return nil }
