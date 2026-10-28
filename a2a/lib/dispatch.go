package lib

import (
	"encoding/json"
	"errors"
	"fmt"
	"time"
)

// The profile dispatcher's hand-off to warm workers and the per-task
// liveness lease (#3655; #3660, "Admission and holding"). Workers have no
// fetch grant on the shared durables: the dispatcher holds each submission
// and hands it to one worker on that worker's own subject, and the worker
// answers on a subject only it may publish, so the dispatcher can take the
// subject's pod token as the answer's identity and never a name in the
// payload. A worker renews the liveness lease for each task it runs by
// publishing on its own lease subject (DispatchLeaseSubject), and the
// dispatcher, the bucket's only writer, persists it under a key that starts
// with that (profile, tier, pod). Workers hold no grant on the bucket at all:
// a KV bucket allows rollup headers, so a put grant on any key would let a
// worker purge every lease in it. After a restart the dispatcher lists a
// (profile, tier)'s keys to rebuild which workers are busy.

// ProfileLeaseBucket is the KV bucket the leases live in.
const ProfileLeaseBucket = "PROFILE_LEASES"

// DispatchSubject is where the dispatcher hands a task to one worker pod of
// a (profile, tier) pool.
func DispatchSubject(profile, tier, pod string) string {
	return fmt.Sprintf("a2a.dispatch.%s.%s.%s", profile, tier, pod)
}

// DispatchAcceptSubject is where that worker answers.
func DispatchAcceptSubject(profile, tier, pod string) string {
	return DispatchSubject(profile, tier, pod) + ".accept"
}

// DispatchLeaseSubject is where that worker renews the leases of the tasks
// it runs, task id in the payload.
func DispatchLeaseSubject(profile, tier, pod string) string {
	return DispatchSubject(profile, tier, pod) + ".lease"
}

// ProfileLeaseKey is a task's liveness lease as the dispatcher persists it:
// <profile>.<tier>.<pod>.<taskId>.
func ProfileLeaseKey(profile, tier, pod, taskID string) string {
	return fmt.Sprintf("%s.%s.%s.%s", profile, tier, pod, taskID)
}

// ProfileLeaseSubject is the KV subject a lease key is written on.
func ProfileLeaseSubject(key string) string {
	return "$KV." + ProfileLeaseBucket + "." + key
}

// ProfileLeaseKeyFilter matches a task's lease whichever pod holds it:
// <profile>.<tier>.*.<taskId>. After a restart a redelivered submission
// names the task but not the pod, and its subject gives the profile and tier.
func ProfileLeaseKeyFilter(profile, tier, taskID string) string {
	return fmt.Sprintf("%s.%s.*.%s", profile, tier, taskID)
}

// ProfileDurableName is the shared durable on TASKS for one (profile, tier,
// kind): profile-<profile>-<tier>-<kind>. The operator creates it and the
// dispatcher is its only reader.
func ProfileDurableName(profile, tier, kind string) string {
	return fmt.Sprintf("profile-%s-%s-%s", profile, tier, kind)
}

// ProfileDurableFilter is that durable's filter: the submissions for one
// (profile, tier, kind), whatever the task.
func ProfileDurableFilter(profile, tier, kind string) string {
	return fmt.Sprintf("a2a.tasks.%s.%s.%s.*.submit", profile, tier, kind)
}

// The kinds of DispatchMessage.
const (
	DispatchOffer   = "offer"
	DispatchAbandon = "abandon"
)

// DispatchReasonCap bounds the free-text reasons in dispatch messages, in
// bytes.
const DispatchReasonCap = 1024

// DispatchMessage is what the dispatcher sends a worker on DispatchSubject.
// It is one subject, the only one the worker may read there, so the message
// says what it is.
type DispatchMessage struct {
	Type   string `json:"type"`
	TaskID string `json:"taskId"`
	// Kind is the submission's kind (SubmitKindTop or SubmitKindChild); an
	// offer only.
	Kind string `json:"kind,omitempty"`
	// Envelope is the submission, verbatim: the worker has no grant on
	// .submit, so this is how it reads the task. An offer only.
	Envelope json.RawMessage `json:"envelope,omitempty"`
	// Deadline is when the task must have ended (the profile's
	// lifecycle.activeDeadlineSeconds from acceptance). An offer only.
	Deadline time.Time `json:"deadline,omitzero"`
	// LeaseSeconds and RenewSeconds are the dispatcher's intervals, sent
	// with each offer so the worker renews on the number the dispatcher
	// enforces and the intervals are set in one place.
	LeaseSeconds int `json:"leaseSeconds,omitempty"`
	RenewSeconds int `json:"renewSeconds,omitempty"`
	// Reason says why a task is abandoned. An abandon only.
	Reason string `json:"reason,omitempty"`
}

// Validate reports the first way m is malformed, or nil.
func (m DispatchMessage) Validate() error {
	if !ValidSubjectToken(m.TaskID) {
		return fmt.Errorf("dispatch %s: task id %q is not one subject token", m.Type, m.TaskID)
	}
	switch m.Type {
	case DispatchOffer:
		switch {
		case m.Kind != SubmitKindTop && m.Kind != SubmitKindChild:
			return fmt.Errorf("dispatch offer: kind %q is not %s or %s", m.Kind, SubmitKindTop, SubmitKindChild)
		case len(m.Envelope) == 0:
			return errors.New("dispatch offer: no envelope")
		case m.Deadline.IsZero():
			return errors.New("dispatch offer: no deadline")
		case m.RenewSeconds <= 0 || m.LeaseSeconds < 3*m.RenewSeconds:
			return fmt.Errorf("dispatch offer: lease %ds must be at least three renewals of %ds", m.LeaseSeconds, m.RenewSeconds)
		}
	case DispatchAbandon:
		if m.Reason == "" || len(m.Reason) > DispatchReasonCap {
			return fmt.Errorf("dispatch abandon: needs a reason of at most %d bytes", DispatchReasonCap)
		}
	default:
		return fmt.Errorf("dispatch message type %q is not %s or %s", m.Type, DispatchOffer, DispatchAbandon)
	}
	return nil
}

// DispatchAccept is a worker's answer to an offer, on
// DispatchAcceptSubject. The pod is the subject's, never a field.
type DispatchAccept struct {
	TaskID   string `json:"taskId"`
	Accepted bool   `json:"accepted"`
	// Reason says why an offer is declined.
	Reason string `json:"reason,omitempty"`
}

// Validate reports the first way a is malformed, or nil.
func (a DispatchAccept) Validate() error {
	switch {
	case !ValidSubjectToken(a.TaskID):
		return fmt.Errorf("dispatch accept: task id %q is not one subject token", a.TaskID)
	case !a.Accepted && (a.Reason == "" || len(a.Reason) > DispatchReasonCap):
		return fmt.Errorf("dispatch accept: a decline needs a reason of at most %d bytes", DispatchReasonCap)
	}
	return nil
}

// LeaseRenewal is a worker renewing one task's lease, on
// DispatchLeaseSubject, every DispatchMessage.RenewSeconds from acceptance
// until it publishes the task's terminal, and then once more with Final.
type LeaseRenewal struct {
	TaskID string `json:"taskId"`
	// Final says the worker has published the task's terminal on .events.
	// The dispatcher has no read on .events, so this is how it learns the
	// task ended: it releases the slot, acks the submission and deletes the
	// lease at once, rather than holding the slot for a lease and then
	// failing a task that finished. (A worker that dies between the two
	// still gets a lease-expired terminal, which the fold drops as a write
	// after the final event.)
	Final bool `json:"final,omitempty"`
}

// Validate reports whether r names a task.
func (r LeaseRenewal) Validate() error {
	if !ValidSubjectToken(r.TaskID) {
		return fmt.Errorf("lease renewal: task id %q is not one subject token", r.TaskID)
	}
	return nil
}

// LeaseRecord is a lease as the dispatcher persists it under
// ProfileLeaseKey. Expiry is read from ExpiresAt, not from a bucket TTL, so
// a dispatcher that restarts can read every record and give a lease that
// went quiet only while it was down the restart grace.
type LeaseRecord struct {
	TaskID     string    `json:"taskId"`
	Pod        string    `json:"pod"`
	Kind       string    `json:"kind"`
	AcceptedAt time.Time `json:"acceptedAt"`
	RenewedAt  time.Time `json:"renewedAt"`
	ExpiresAt  time.Time `json:"expiresAt"`
}

// Expired reports whether the lease has run out at now, given grace on
// top of ExpiresAt (zero in steady state; the restart grace while a
// restarted dispatcher re-adopts).
func (l LeaseRecord) Expired(now time.Time, grace time.Duration) bool {
	return now.After(l.ExpiresAt.Add(grace))
}
