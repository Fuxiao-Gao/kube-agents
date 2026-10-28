package lib

import (
	"encoding/json"
	"strings"
	"testing"
	"time"
)

// The dispatcher's names line up with the subjects they read: a durable's
// filter matches its kind's submissions and nothing else, and the lease
// filter finds a task's lease whichever pod holds it.
func TestTheDispatchNamesMatchWhatTheyRead(t *testing.T) {
	if got := ProfileDurableName("tse", "read", SubmitKindTop); got != "profile-tse-read-top" {
		t.Errorf("durable name = %q", got)
	}
	filter := ProfileDurableFilter("tse", "read", SubmitKindTop)
	if !subjectMatches(filter, TaskSubmitSubject("tse", "read", SubmitKindTop, "t1")) {
		t.Errorf("%s does not match its own submissions", filter)
	}
	for _, other := range []string{
		TaskSubmitSubject("tse", "read", SubmitKindChild, "t1"),
		TaskSubmitSubject("tse", "unattended", SubmitKindTop, "t1"),
		TaskSubmitSubject("other", "read", SubmitKindTop, "t1"),
		TaskInSubject("tse", "t1"),
	} {
		if subjectMatches(filter, other) {
			t.Errorf("%s matches %s", filter, other)
		}
	}
	lease := ProfileLeaseKeyFilter("tse", "read", "t1")
	if !subjectMatches(lease, ProfileLeaseKey("tse", "read", "tse-read-7f9c", "t1")) {
		t.Errorf("%s does not find the task's lease", lease)
	}
	if subjectMatches(lease, ProfileLeaseKey("tse", "read", "tse-read-7f9c", "t2")) {
		t.Errorf("%s finds another task's lease", lease)
	}
}

// subjectMatches is NATS token matching for the * wildcard, enough for these
// names (none uses >).
func subjectMatches(pattern, subject string) bool {
	p, s := strings.Split(pattern, "."), strings.Split(subject, ".")
	if len(p) != len(s) {
		return false
	}
	for i := range p {
		if p[i] != "*" && p[i] != s[i] {
			return false
		}
	}
	return true
}

func validOffer() DispatchMessage {
	return DispatchMessage{
		Type: DispatchOffer, TaskID: "t1", Kind: SubmitKindTop,
		Envelope:     json.RawMessage(`{"taskId":"t1"}`),
		Deadline:     time.Date(2026, 10, 28, 18, 0, 0, 0, time.UTC),
		LeaseSeconds: 90, RenewSeconds: 20,
	}
}

// An offer and an abandon survive the wire, and each malformed one is
// refused with a reason that names what is wrong.
func TestADispatchMessageIsCheckedBothWays(t *testing.T) {
	for _, m := range []DispatchMessage{validOffer(), {Type: DispatchAbandon, TaskID: "t1", Reason: "stopped by the conversation"}} {
		raw, err := json.Marshal(m)
		if err != nil {
			t.Fatal(err)
		}
		var back DispatchMessage
		if err := json.Unmarshal(raw, &back); err != nil {
			t.Fatal(err)
		}
		if err := back.Validate(); err != nil {
			t.Errorf("%s did not survive the wire: %v (%s)", m.Type, err, raw)
		}
	}
	for name, tc := range map[string]struct {
		edit func(*DispatchMessage)
		want string
	}{
		"unknown type":       {func(m *DispatchMessage) { m.Type = "steal" }, "type"},
		"task id with a dot": {func(m *DispatchMessage) { m.TaskID = "a.b" }, "subject token"},
		"bad kind":           {func(m *DispatchMessage) { m.Kind = "grandchild" }, "kind"},
		"no envelope":        {func(m *DispatchMessage) { m.Envelope = nil }, "envelope"},
		"no deadline":        {func(m *DispatchMessage) { m.Deadline = time.Time{} }, "deadline"},
		"no renewal":         {func(m *DispatchMessage) { m.RenewSeconds = 0 }, "renewals"},
		"lease too short":    {func(m *DispatchMessage) { m.LeaseSeconds = 59 }, "renewals"},
		"abandon no reason":  {func(m *DispatchMessage) { *m = DispatchMessage{Type: DispatchAbandon, TaskID: "t1"} }, "reason"},
	} {
		m := validOffer()
		tc.edit(&m)
		if err := m.Validate(); err == nil || !strings.Contains(err.Error(), tc.want) {
			t.Errorf("%s: Validate = %v, want an error saying %q", name, err, tc.want)
		}
	}
}

// A decline says why; an accept and a renewal need only the task.
func TestAnAcceptAndARenewalAreChecked(t *testing.T) {
	if err := (DispatchAccept{TaskID: "t1", Accepted: true}).Validate(); err != nil {
		t.Errorf("accept: %v", err)
	}
	if err := (DispatchAccept{TaskID: "t1", Reason: "busy"}).Validate(); err != nil {
		t.Errorf("decline with a reason: %v", err)
	}
	if err := (DispatchAccept{TaskID: "t1"}).Validate(); err == nil {
		t.Error("a decline with no reason was accepted")
	}
	if err := (DispatchAccept{TaskID: "t1", Reason: strings.Repeat("x", DispatchReasonCap+1)}).Validate(); err == nil {
		t.Error("a decline with an over-long reason was accepted")
	}
	if err := (LeaseRenewal{TaskID: "t1"}).Validate(); err != nil {
		t.Errorf("renewal: %v", err)
	}
	if err := (LeaseRenewal{TaskID: ""}).Validate(); err == nil {
		t.Error("a renewal naming no task was accepted")
	}
}

// A lease is expired only once ExpiresAt plus the grace has passed.
func TestALeaseExpiresAfterItsGrace(t *testing.T) {
	at := time.Date(2026, 10, 28, 18, 0, 0, 0, time.UTC)
	l := LeaseRecord{ExpiresAt: at}
	if l.Expired(at, 0) {
		t.Error("expired at ExpiresAt itself")
	}
	if !l.Expired(at.Add(time.Second), 0) {
		t.Error("not expired a second past ExpiresAt with no grace")
	}
	if l.Expired(at.Add(30*time.Second), time.Minute) {
		t.Error("expired inside the grace")
	}
	if !l.Expired(at.Add(61*time.Second), time.Minute) {
		t.Error("not expired past the grace")
	}
}

// A card with the routing fields round-trips, and one without them (what
// the operator published before) still parses as an agent card.
func TestAProfileCardCarriesItsRoutingFields(t *testing.T) {
	full := `{"name":"tse","description":"Diagnoses GKE cluster problems.","tiers":["read"],` +
		`"parameters":[{"name":"project","required":true},{"name":"location","required":true},{"name":"cluster","required":true}]}`
	var c AgentCard
	if err := json.Unmarshal([]byte(full), &c); err != nil {
		t.Fatal(err)
	}
	if c.Name != "tse" || len(c.Tiers) != 1 || len(c.Parameters) != 3 || !c.Parameters[2].Required || c.ClusterRef != nil {
		t.Errorf("card = %+v", c)
	}
	pinned := `{"name":"prod-a","clusterRef":{"projectId":"p","location":"us-central1","cluster":"a"}}`
	if err := json.Unmarshal([]byte(pinned), &c); err != nil || c.ClusterRef == nil || c.ClusterRef.Cluster != "a" {
		t.Errorf("pinned card = %+v, %v", c, err)
	}
	for _, payload := range []string{full, pinned, `{"name":"old"}`} {
		if err := validatePayload(KindAgentCard, "", json.RawMessage(payload)); err != nil {
			t.Errorf("%s: %v", payload, err)
		}
	}
}
