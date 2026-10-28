package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"sync"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

const (
	// directoryStream holds the profile cards (a2a.agents.<profile>), one
	// per subject; the operator writes them.
	directoryStream = "DIRECTORY"
	// directoryRetryInterval spaces retries of the start-up read. A gateway
	// that can't read DIRECTORY yet (the stream not provisioned, or an
	// operator that hasn't granted the read) keeps running with no profiles,
	// so a task addressed to one is refused as unknown until a read lands.
	directoryRetryInterval = 30 * time.Second
)

// agentsWildcard is every profile's card subject: profile names are one
// token (a DNS label).
var agentsWildcard = lib.AgentSubject("*")

// directoryFeed keeps a profileDirectory current: a live subscription to
// a2a.agents.* for changes, and one read of the stream's newest card per
// subject for what was there before it subscribed. A profile the live feed
// has spoken for since the subscription began is not overwritten by the
// read, which may have fetched an older card.
type directoryFeed struct {
	dir *profileDirectory
	log interface {
		Warn(msg string, args ...any)
		Info(msg string, args ...any)
	}
	mu   sync.Mutex
	live map[string]bool
}

// apply records one directory message, from either source. It reports
// whether the message was a card or tombstone it understood.
func (f *directoryFeed) apply(subject string, raw []byte, fromLive bool) bool {
	profile, ok := strings.CutPrefix(subject, lib.AgentSubject(""))
	if !ok || profile == "" || strings.Contains(profile, ".") {
		return false
	}
	env, err := lib.ParseEnvelope(raw)
	if err != nil {
		f.log.Warn("directory message unreadable", "subject", subject, "err", err)
		return false
	}
	if err := lib.CheckSubjectAgreement(subject, env, lib.AgreementPolicy{}); err != nil {
		f.log.Warn("directory message refused", "subject", subject, "err", err)
		return false
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if !fromLive && f.live[profile] {
		return true
	}
	if fromLive {
		f.live[profile] = true
	}
	if env.Kind == lib.KindAgentClosed {
		f.dir.put(profile, nil)
		return true
	}
	var card lib.AgentCard
	if err := json.Unmarshal(env.Payload, &card); err != nil || card.Name != profile {
		f.log.Warn("profile card refused", "subject", subject, "name", card.Name, "err", err)
		return false
	}
	f.dir.put(profile, &card)
	return true
}

// startProfileDirectory subscribes to the cards and reads what DIRECTORY
// already holds, retrying the read until it lands or ctx ends. The
// subscription comes first, so a card published between the two is seen.
func (g *Gateway) startProfileDirectory(ctx context.Context) {
	f := &directoryFeed{dir: g.profiles, log: g.log, live: map[string]bool{}}
	sub, err := g.client.SubscribeCore(agentsWildcard, func(m *nats.Msg) { f.apply(m.Subject, m.Data, true) })
	if err != nil {
		g.log.Warn("profile directory subscription failed; profiles are unaddressable", "err", err)
		return
	}
	go func() {
		defer sub.Stop()
		for {
			err := g.readDirectory(ctx, f)
			if err == nil {
				break
			}
			g.log.Warn("profile directory read failed; retrying", "err", err, "in", directoryRetryInterval)
			select {
			case <-ctx.Done():
				return
			case <-time.After(directoryRetryInterval):
			}
		}
		<-ctx.Done()
	}()
}

// readDirectory reads the newest card on each subject DIRECTORY holds: the
// stream's subject list, then a direct get per subject, so no consumer is
// created on the stream.
func (g *Gateway) readDirectory(ctx context.Context, f *directoryFeed) error {
	stream, err := g.client.JetStream().Stream(ctx, directoryStream)
	if err != nil {
		return err
	}
	info, err := stream.Info(ctx, jetstream.WithSubjectFilter(agentsWildcard))
	if err != nil {
		return err
	}
	n := 0
	for subject := range info.State.Subjects {
		msg, err := stream.GetLastMsgForSubject(ctx, subject)
		if errors.Is(err, jetstream.ErrMsgNotFound) {
			continue
		}
		if err != nil {
			return err
		}
		if f.apply(subject, msg.Data, false) {
			n++
		}
	}
	g.log.Info("profile directory read", "cards", n)
	return nil
}
