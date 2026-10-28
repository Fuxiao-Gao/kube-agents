package authcallout

import (
	"os"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The dispatcher's principal as the operator renders it when it has an image
// for the dispatcher (k8s-operator: TestRenderedDispatcherFixtures writes the
// fixture), run through the callout and a real server.
const (
	dispatcherFixturePath = "testdata/rendered-identity-map-dispatcher.json"
	renderedDispatcherSA  = "system:serviceaccount:kubeagents-system:agent-a2a-dispatcher"
	tokenDispatcher       = "token-for-the-rendered-dispatcher-serviceaccount-padded-to-len"
)

func renderedDispatcherMap(t *testing.T) string {
	t.Helper()
	raw, err := os.ReadFile(dispatcherFixturePath)
	if err != nil {
		t.Fatalf("reading %s: %v\nRegenerate with: go test ./internal/controller/ -run 'Fixture' -update (in k8s-operator)", dispatcherFixturePath, err)
	}
	return string(raw)
}

// The map parses with the dispatcher in it, as a plain callout principal.
func TestTheOperatorsDispatcherMapParses(t *testing.T) {
	m, err := ParseIdentityMap([]byte(renderedDispatcherMap(t)))
	if err != nil {
		t.Fatalf("the operator's map with the dispatcher does not parse: %v", err)
	}
	for _, id := range m.Identities {
		if id.User == "dispatcher" {
			if id.ServiceAccount != renderedDispatcherSA || id.Narrowing != "" {
				t.Errorf("dispatcher entry = %+v", id)
			}
			return
		}
	}
	t.Fatal("the operator's map has no dispatcher")
}

// What the dispatcher may do on the bus, and the nearest things it may not,
// against the rendered grants. The refusals are the point: it reads
// submissions only through its durables, never a task's subjects; it writes
// only PROFILE_LEASES among the buckets; it asks the verifier only as itself.
func TestTheRenderedDispatcherReachesItsContractAndNothingNear(t *testing.T) {
	h := startHarness(t, renderedDispatcherMap(t), map[string]Attested{
		tokenDispatcher: {ServiceAccount: renderedDispatcherSA},
	})
	nc, violations := h.connectAs(t, "dispatcher", tokenDispatcher)
	pod := "tse-read-7f9c"
	checkPublish(t, nc, violations, map[string]bool{
		// Allowed: the contract.
		lib.DispatchSubject("tse", "read", pod):                                           false,
		lib.TaskEventsSubject("tse", "t1"):                                                false,
		lib.ProfileLeaseSubject(lib.ProfileLeaseKey("tse", "read", pod, "t1")):            false,
		"$JS.API.CONSUMER.MSG.NEXT.TASKS." + lib.ProfileDurableName("tse", "read", "top"): false,
		"$JS.ACK.TASKS." + lib.ProfileDurableName("tse", "read", "top") + ".1.1.1.1.0":    false,
		"a2a.cap.verify.dispatcher":                                                       false,
		// Refused: the nearest things it must not do.
		lib.TaskInSubject("tse", "t1"):                                                  true,
		lib.TaskSubmitSubject("tse", "read", "top", "t1"):                               true,
		lib.TaskSupervisorSubject("tse", "t1"):                                          true,
		lib.DispatchAcceptSubject("tse", "read", pod):                                   true,
		"$JS.API.CONSUMER.CREATE.TASKS." + lib.ProfileDurableName("tse", "read", "top"): true,
		"$JS.API.CONSUMER.MSG.NEXT.TASKS.session-x-origin":                              true,
		// A durable the operator didn't create for it: no grant.
		"$JS.API.CONSUMER.MSG.NEXT.TASKS." + lib.ProfileDurableName("other", "read", "top"): true,
		"$KV.cap.root.x":         true,
		"$KV.session-state.x":    true,
		"a2a.cap.verify.gateway": true,
		"a2a.agents.tse":         true,
	})
	for subject, wantRefused := range map[string]bool{
		lib.DispatchAcceptSubject("tse", "read", pod): false,
		lib.DispatchLeaseSubject("tse", "read", pod):  false,
		"a2a.tasks.>":                           true,
		lib.TaskEventsSubject("tse", "t1"):      true,
		lib.DispatchSubject("tse", "read", pod): true,
		"_INBOX.gateway.>":                      true,
	} {
		if got := subscribeRefused(t, nc, violations, subject); got != wantRefused {
			t.Errorf("subscribe %s refused=%v, want %v", subject, got, wantRefused)
		}
	}
}
