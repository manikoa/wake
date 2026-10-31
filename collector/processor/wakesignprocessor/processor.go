// Package wakesignprocessor signs Wake events at the first hop outside the agent's
// process, and checks the signatures of events that arrive already signed, such as
// the proxy's. See docs/signing.md.
package wakesignprocessor

import (
	"context"
	"crypto/x509"
	"errors"
	"fmt"
	"os"
	"strings"
	"time"

	"go.opentelemetry.io/collector/component"
	"go.opentelemetry.io/collector/consumer"
	"go.opentelemetry.io/collector/pdata/ptrace"
	"go.opentelemetry.io/collector/processor"
	"go.opentelemetry.io/collector/processor/processorhelper"
	"golang.org/x/mod/sumdb/note"

	"github.com/usewake/wake/collector/event"
	wakelog "github.com/usewake/wake/log"
)

// Config for the wakesign processor.
type Config struct {
	// SigningKeyFile holds the note format private key the collector signs SDK and
	// memory adapter events with.
	SigningKeyFile string `mapstructure:"signing_key_file"`
	// TrustedKeys lists note format public key files for other components that sign
	// their own events with one.
	TrustedKeys []string `mapstructure:"trusted_keys"`
	// TrustBundles lists PEM files of SPIRE trust bundles. Components that sign with an
	// X.509-SVID, such as the proxy, must chain to one of them.
	TrustBundles []string `mapstructure:"trust_bundles"`
	// TrustedSigners lists, by wake.capture, who may sign events of that capture: SPIFFE
	// IDs, or path prefixes ending in "/" that match every ID below them. A trust bundle
	// vouches for every workload in its trust domain, agents included, so an event signed
	// with an X.509-SVID only counts when its signer is listed for its capture. Once a
	// capture is listed, only its listed signers count for it, whatever they sign with.
	//
	//	trusted_signers:
	//	  proxy: [spiffe://wake.local/proxy]
	TrustedSigners map[string][]string `mapstructure:"trusted_signers"`
}

// Validate checks the config.
func (c *Config) Validate() error {
	if c.SigningKeyFile == "" {
		return errors.New("signing_key_file is required")
	}
	if len(c.TrustBundles) > 0 && len(c.TrustedSigners) == 0 {
		return errors.New("trust_bundles needs trusted_signers: a bundle vouches for every workload in its trust domain, so list who may sign each capture, such as proxy: [spiffe://wake.local/proxy]")
	}
	if err := wakelog.Signers(c.TrustedSigners).Validate(); err != nil {
		return fmt.Errorf("trusted_signers: %w", err)
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
	key   note.Signer
	trust wakelog.Trust
	now   func() time.Time
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
	trust := wakelog.Trust{Keys: trusted, Signers: wakelog.Signers(cfg.TrustedSigners)}
	if len(cfg.TrustBundles) > 0 {
		trust.Bundles = x509.NewCertPool()
		for _, path := range cfg.TrustBundles {
			pem, err := os.ReadFile(path)
			if err != nil {
				return nil, fmt.Errorf("read trust bundle: %w", err)
			}
			if !trust.Bundles.AppendCertsFromPEM(pem) {
				return nil, fmt.Errorf("trust bundle %s has no certificates", path)
			}
		}
	}
	return &signer{key: key, trust: trust, now: time.Now}, nil
}

func (s *signer) processTraces(_ context.Context, td ptrace.Traces) (ptrace.Traces, error) {
	for _, rs := range td.ResourceSpans().All() {
		for _, ss := range rs.ScopeSpans().All() {
			for _, span := range ss.Spans().All() {
				s.process(span)
			}
		}
	}
	return td, nil
}

// process marks span valid or invalid, with the reason, and never fails: one bad event
// must not cost the rest of its request. It always overwrites wake.signature.valid,
// wake.signature.error and wake.received_unix_nano, so a sender can never claim them.
func (s *signer) process(span ptrace.Span) {
	if !event.IsEvent(span) {
		return
	}
	attrs := span.Attributes()
	repeated, isRepeated := event.DropRepeatedKeys(span)
	attrs.Remove(event.SignatureError)
	attrs.PutInt(event.Received, s.now().UnixNano())
	_, signed := attrs.Get(event.Signature)
	capture, _ := attrs.Get(event.Capture)
	var err error
	switch {
	case isRepeated:
		// What it said under the repeated key is ambiguous, so it's neither signed nor
		// trusted. It's kept, as the first copy of each key, as evidence.
		err = fmt.Errorf("attribute %s appears more than once", repeated)
	case signed:
		// Keep events whose signatures fail: they're evidence too.
		err = event.Verify(span, s.trust)
	case capture.Str() == "sdk" || capture.Str() == "memory_adapter" || capture.Str() == "hook":
		// Captured where the agent runs, so they arrive unsigned, and this is the first hop
		// outside the agent's reach.
		err = event.Sign(span, s.key)
	default:
		// Proxies sign their own events, so an unsigned one can't be trusted.
		err = fmt.Errorf("unsigned, and the collector only signs sdk, memory_adapter and hook events, not %q", capture.Str())
	}
	if err != nil {
		// Invalid, it still goes to the log, so its payload's credentials are replaced
		// first, as signing replaces them.
		event.RedactInvalid(span)
	}
	mark(span, err)
}

func mark(span ptrace.Span, err error) {
	span.Attributes().PutBool(event.SignatureValid, err == nil)
	if err != nil {
		span.Attributes().PutStr(event.SignatureError, err.Error())
	}
}
