package controller

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/nats-io/nats.go/jetstream"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The profiles' shared durables on TASKS (#3660, "Admission and holding";
// contract on #3655): one per (profile, tier, kind), created and owned by the
// operator, read only by the dispatcher. This is lib.EnsureHeldConsumer's
// contract restated here, because this module can't import that one: a
// durable made here must be one the dispatcher's SubscribeHeld accepts.
const (
	// heldMarkerKey/Value are lib's: the metadata SubscribeHeld requires,
	// so the dispatcher binds only a durable made for held delivery, and
	// the mark this operator deletes by, so it never removes a consumer it
	// didn't make.
	a2aHeldMarkerKey   = "kube-agents.held-delivery"
	a2aHeldMarkerValue = "v1"

	// a2aHeldAckWait is lib.DefaultHeldAckWait: the redelivery window the
	// dispatcher's in-progress acks keep open. Fixed once a durable exists;
	// changing it means re-creating the durable (see ensureDurable).
	a2aHeldAckWait = 2 * time.Minute
)

// a2aAllProfileTiers are every tier a profile can allow, so a dropped tier's
// durables can be found by name without listing consumers.
var a2aAllProfileTiers = []string{agentv1alpha1.AgentProfileTierRead, agentv1alpha1.AgentProfileTierUnattended}

// profileDurable is one durable as the operator wants it.
type profileDurable struct {
	name          string
	filter        string
	maxAckPending int
}

// profileDurableFilter is lib.ProfileDurableFilter.
func profileDurableFilter(profile, tier, kind string) string {
	return fmt.Sprintf("a2a.tasks.%s.%s.%s.*.submit", profile, tier, kind)
}

// desiredProfileDurables are the durables a profile with a rendered identity
// has: one per (allowed tier, kind), each capped at the profile's
// concurrency. The child durable has the same cap as the top one but its
// own, so children never queue behind their parents' kind.
func desiredProfileDurables(p *agentv1alpha1.AgentProfile) []profileDurable {
	var out []profileDurable
	for _, tier := range p.AllowedTierList() {
		for _, kind := range a2aSubmitKinds {
			out = append(out, profileDurable{
				name:          profileDurableName(p.Name, tier, kind),
				filter:        profileDurableFilter(p.Name, tier, kind),
				maxAckPending: int(p.Spec.Concurrency),
			})
		}
	}
	return out
}

// allProfileDurableNames is every durable name a profile could have.
func allProfileDurableNames(profile string) []string {
	var out []string
	for _, tier := range a2aAllProfileTiers {
		for _, kind := range a2aSubmitKinds {
			out = append(out, profileDurableName(profile, tier, kind))
		}
	}
	return out
}

// ensureDurable creates the durable, or updates an existing one's
// MaxAckPending, as lib.EnsureHeldConsumer does: only a durable known not to
// exist is created, and an existing one's filter and AckWait are never moved
// under a bound dispatcher, which checked the filter and took its heartbeat
// pace from AckWait when it bound. A consumer under the name that doesn't
// carry the held marker isn't this operator's, and is refused.
func ensureDurable(ctx context.Context, js jetstream.JetStream, d profileDurable) error {
	existing, err := js.Consumer(ctx, a2aTasksStream, d.name)
	switch {
	case errors.Is(err, jetstream.ErrConsumerNotFound):
	case err != nil:
		return fmt.Errorf("durable %s: look it up: %w", d.name, err)
	default:
		old := existing.CachedInfo().Config
		switch {
		case old.Metadata[a2aHeldMarkerKey] != a2aHeldMarkerValue:
			return fmt.Errorf("a consumer named %s exists on %s without the held-delivery mark; the operator doesn't adopt it", d.name, a2aTasksStream)
		case old.FilterSubject != d.filter || len(old.FilterSubjects) > 0:
			return fmt.Errorf("durable %s filters %q; refusing to move it to %q under a bound dispatcher (re-create it)", d.name, old.FilterSubject, d.filter)
		case old.AckWait != a2aHeldAckWait:
			return fmt.Errorf("durable %s has AckWait %v; refusing to change it to %v under a bound dispatcher (re-create it)", d.name, old.AckWait, a2aHeldAckWait)
		case old.MaxAckPending == d.maxAckPending:
			return nil
		}
	}
	_, err = js.CreateOrUpdateConsumer(ctx, a2aTasksStream, jetstream.ConsumerConfig{
		Durable:       d.name,
		FilterSubject: d.filter,
		AckPolicy:     jetstream.AckExplicitPolicy,
		MaxAckPending: d.maxAckPending,
		AckWait:       a2aHeldAckWait,
		Metadata:      map[string]string{a2aHeldMarkerKey: a2aHeldMarkerValue},
	})
	if err != nil {
		return fmt.Errorf("durable %s: %w", d.name, err)
	}
	return nil
}

// deleteDurable deletes the named durable if it exists and carries the held
// marker. Deleting it drops whatever submissions it held unacknowledged; a
// profile or tier that goes away takes its queue with it.
func deleteDurable(ctx context.Context, js jetstream.JetStream, name string) error {
	existing, err := js.Consumer(ctx, a2aTasksStream, name)
	switch {
	case errors.Is(err, jetstream.ErrConsumerNotFound):
		return nil
	case err != nil:
		return fmt.Errorf("durable %s: look it up: %w", name, err)
	}
	if existing.CachedInfo().Config.Metadata[a2aHeldMarkerKey] != a2aHeldMarkerValue {
		return nil
	}
	if err := js.DeleteConsumer(ctx, a2aTasksStream, name); err != nil && !errors.Is(err, jetstream.ErrConsumerNotFound) {
		return fmt.Errorf("deleting durable %s: %w", name, err)
	}
	return nil
}

// SyncDurables makes the profile's durables on the bus exactly want: each
// one created or its cap updated, and every other name the profile could
// have deleted if this operator made it. want empty deletes them all.
func (n *natsCardPublisher) SyncDurables(ctx context.Context, agent *agentv1alpha1.PlatformAgent, profile string, want []profileDurable) error {
	nc, err := n.conn(agent)
	if err != nil {
		return err
	}
	js, err := jetstream.New(nc)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(ctx, cardBusTimeout)
	defer cancel()
	keep := map[string]bool{}
	for _, d := range want {
		keep[d.name] = true
		if err := ensureDurable(ctx, js, d); err != nil {
			return err
		}
	}
	for _, name := range allProfileDurableNames(profile) {
		if keep[name] {
			continue
		}
		if err := deleteDurable(ctx, js, name); err != nil {
			return err
		}
	}
	return nil
}
