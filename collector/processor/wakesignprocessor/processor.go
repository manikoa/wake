// Package wakesignprocessor signs Wake events at the first hop outside the agent's
// process, and checks the signatures of events that arrive already signed, such as
// the proxy's. See docs/signing.md.
package wakesignprocessor

import (
	"context"
	"errors"
	"fmt"
	"os"
	"strings"

	"go.opentelemetry.io/collector/component"
	"go.opentelemetry.io/collector/consumer"
	"go.opentelemetry.io/collector/pdata/ptrace"
	"go.opentelemetry.io/collector/processor"
	"go.opentelemetry.io/collector/processor/processorhelper"
	"golang.org/x/mod/sumdb/note"

	"github.com/manikoa/wake/collector/event"
)

// Config for the wakesign processor.
type Config struct {
	// SigningKeyFile holds the note format private key the collector signs SDK and
	// memory adapter events with.
	SigningKeyFile string `mapstructure:"signing_key_file"`
	// TrustedKeys lists note format public key files for components that sign their
	// own events, such as proxies.
	TrustedKeys []string `mapstructure:"trusted_keys"`
}

// Validate checks the config.
func (c *Config) Validate() error {
	if c.SigningKeyFile == "" {
		return errors.New("signing_key_file is required")
	}
	return nil
}

// NewFactory returns the factory for the wakesign processor.
func NewFactory() processor.Factory {
	return processor.NewFactory(
		component.MustNewType("wakesign"),
		func() component.Config { return &Config{} },
		processor.WithTraces(createTraces, component.StabilityLevelDevelopment),
	)
}

func createTraces(ctx context.Context, set processor.Settings, cfg component.Config, next consumer.Traces) (processor.Traces, error) {
	s, err := newSigner(cfg.(*Config))
	if err != nil {
		return nil, err
	}
	return processorhelper.NewTraces(ctx, set, cfg, next, s.processTraces,
		processorhelper.WithCapabilities(consumer.Capabilities{MutatesData: true}))
}

type signer struct {
	key     note.Signer
	trusted map[string]note.Verifier
}

func newSigner(cfg *Config) (*signer, error) {
	private, err := os.ReadFile(cfg.SigningKeyFile)
	if err != nil {
		return nil, fmt.Errorf("read signing key: %w", err)
	}
	key, err := note.NewSigner(strings.TrimSpace(string(private)))
	if err != nil {
		return nil, fmt.Errorf("parse signing key: %w", err)
	}
	trusted := map[string]note.Verifier{}
	for _, path := range cfg.TrustedKeys {
		public, err := os.ReadFile(path)
		if err != nil {
			return nil, fmt.Errorf("read trusted key: %w", err)
		}
		verifier, err := note.NewVerifier(strings.TrimSpace(string(public)))
		if err != nil {
			return nil, fmt.Errorf("parse trusted key %s: %w", path, err)
		}
		trusted[verifier.Name()] = verifier
	}
	return &signer{key: key, trusted: trusted}, nil
}

func (s *signer) processTraces(_ context.Context, td ptrace.Traces) (ptrace.Traces, error) {
	for _, rs := range td.ResourceSpans().All() {
		for _, ss := range rs.ScopeSpans().All() {
			for _, span := range ss.Spans().All() {
				if err := s.process(span); err != nil {
					return td, err
				}
			}
		}
	}
	return td, nil
}

// process always overwrites wake.signature.valid, so a sender can never claim it.
func (s *signer) process(span ptrace.Span) error {
	if !event.IsEvent(span) {
		return nil
	}
	attrs := span.Attributes()
	if _, signed := attrs.Get(event.Signature); signed {
		// Keep events whose signatures fail: they're evidence too.
		attrs.PutBool(event.SignatureValid, event.Verify(span, s.trusted))
		return nil
	}
	capture, _ := attrs.Get(event.Capture)
	switch capture.Str() {
	case "sdk", "memory_adapter":
		if err := event.Sign(span, s.key); err != nil {
			return err
		}
		attrs.PutBool(event.SignatureValid, true)
	default:
		// Proxies sign their own events, so an unsigned one can't be trusted.
		attrs.PutBool(event.SignatureValid, false)
	}
	return nil
}
