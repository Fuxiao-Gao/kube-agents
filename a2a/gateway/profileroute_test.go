package gateway

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

type subjectEnvelope struct {
	subject string
	env     *lib.Envelope
}

// streamEnvelopes is every envelope on TASKS under filter, oldest first.
func streamEnvelopes(t *testing.T, url, filter string) []subjectEnvelope {
	t.Helper()
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	cons, err := js.OrderedConsumer(ctx, lib.TasksStream, jetstream.OrderedConsumerConfig{FilterSubjects: []string{filter}})
	if err != nil {
		t.Fatal(err)
	}
	it, err := cons.Messages()
	if err != nil {
		t.Fatal(err)
	}
	defer it.Stop()
	var out []subjectEnvelope
	for {
		fctx, fcancel := context.WithTimeout(ctx, 300*time.Millisecond)
		msg, err := fetchNext(fctx, it)
		fcancel()
		if err != nil {
			break
		}
		if env, err := lib.ParseEnvelope(msg.Data()); err == nil {
			out = append(out, subjectEnvelope{subject: msg.Subject(), env: env})
		}
	}
	return out
}

// mintedEntry reads the grant ref names from the cap bucket.
func mintedEntry(t *testing.T, url string, ref *capability.Ref) capability.Entry {
	t.Helper()
	if ref == nil {
		t.Fatal("no capability on the task")
	}
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	kv, err := js.KeyValue(ctx, capability.Bucket)
	if err != nil {
		t.Fatal(err)
	}
	e, err := kv.GetRevision(ctx, ref.Key, ref.Revision)
	if err != nil {
		t.Fatal(err)
	}
	var entry capability.Entry
	if err := json.Unmarshal(e.Value(), &entry); err != nil {
		t.Fatal(err)
	}
	return entry
}

// startProfileRig is a rig whose gateway knows a generic cluster profile and
// one pinned to p1/us-east1/prod, under a scope that lists project p1.
func startProfileRig(t *testing.T) *rig {
	t.Helper()
	scope := filepath.Join(t.TempDir(), "scope.json")
	if err := os.WriteFile(scope, []byte(`{"present":true,"projects":["p1"]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	r := startRigWith(t, func(c *Config) { c.ScopeFile = scope; c.Namespace = "kubeagents-system" })
	r.g.profiles.put("cluster", &lib.AgentCard{Name: "cluster", Parameters: clusterParams})
	r.g.profiles.put("prod-east", &lib.AgentCard{Name: "prod-east", Parameters: clusterParams,
		ClusterRef: &lib.ClusterRef{ProjectID: "p1", Location: "us-east1", Cluster: "prod"}})
	return r
}

func profileTurn(conversation, profile string, params map[string]string) InboundMessage {
	return InboundMessage{Conversation: conversation, Kind: "dm", AuthorID: "1001", MessageID: "m-" + conversation,
		Text: "what's wrong with it", Profile: profile, Parameters: params}
}

func TestProfileTurn_IsMintedForTheResolvedProfileAndSubmittedOnSubmit(t *testing.T) {
	r := startProfileRig(t)
	r.adapter.inbox <- profileTurn("discord:1/1", "cluster", targetParams("p1", "us-east1", "prod"))

	var got []subjectEnvelope
	waitFor(t, "a submission on the pinned profile's .submit", func() bool {
		got = streamEnvelopes(t, r.url, "a2a.tasks.prod-east.>")
		return len(got) == 1
	})
	profile, tier, kind, taskID, ok := lib.ParseSubmitSubject(got[0].subject)
	if !ok || profile != "prod-east" || tier != "read" || kind != lib.SubmitKindTop {
		t.Fatalf("subject = %q, want prod-east's read top .submit", got[0].subject)
	}
	params, found, err := lib.TaskParametersOf(messageParts(t, got[0].env))
	if err != nil || !found {
		t.Fatalf("taskParameters part: found=%v err=%v", found, err)
	}
	if want := targetParams("p1", "us-east1", "prod"); !mapsEqual(params.Parameters, want) {
		t.Fatalf("parameters = %v, want %v", params.Parameters, want)
	}

	rec := waitForRecord(t, r, "discord:1/1", "the task on record", func(rec *SessionRecord) bool {
		ref, ok := rec.TaskRefFor(taskID)
		return ok && ref.Tier == "read"
	})
	if rec.Addressee != "prod-east" {
		t.Fatalf("addressee = %q, want the resolved profile", rec.Addressee)
	}
	ref, _ := rec.TaskRefFor(taskID)
	entry := mintedEntry(t, r.url, ref.Capability)
	if entry.Tier != capability.TierRead || entry.Profile != "prod-east" || entry.Delegate != profileGrantDelegate ||
		entry.Scope != "project/p1/location/us-east1/cluster/prod" {
		t.Fatalf("grant = %+v, want read for prod-east at its cluster, delegated to the dispatcher", entry)
	}
	// The grant answers the dispatcher's question for this task and no other.
	if err := capability.PermitsProfile(entry, "prod-east", capability.TierRead, "project/p1/location/us-east1/cluster/prod"); err != nil {
		t.Fatalf("PermitsProfile for the minted target: %v", err)
	}
	if err := capability.PermitsProfile(entry, "prod-east", capability.TierRead, "project/p1/location/us-east1/cluster/dev"); err == nil {
		t.Fatal("the grant permits a cluster it wasn't minted for")
	}
	if err := capability.PermitsProfile(entry, "cluster", capability.TierRead, "project/p1/location/us-east1/cluster/prod"); err == nil {
		t.Fatal("the grant permits a profile it wasn't minted for")
	}
}

func TestProfileTurn_ARefusalPostsWhyAndSubmitsNothing(t *testing.T) {
	cases := []struct {
		name, profile string
		params        map[string]string
		want          string
	}{
		{"out of scope", "cluster", targetParams("p9", "us-east1", "prod"), "outside this install's scope"},
		{"pinned for another cluster", "prod-east", targetParams("p1", "us-east1", "dev"), "works only on cluster"},
		{"unknown profile", "nope", nil, `no agent profile named "nope"`},
	}
	for i, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r := startProfileRig(t)
			conv := "discord:2/" + string(rune('a'+i))
			r.adapter.inbox <- profileTurn(conv, tc.profile, tc.params)
			waitFor(t, "the refusal", func() bool {
				for _, p := range r.adapter.postTexts() {
					if strings.Contains(p, tc.want) {
						return true
					}
				}
				return false
			})
			if got := streamEnvelopes(t, r.url, "a2a.tasks.>"); len(got) != 0 {
				t.Fatalf("published %d message(s) for a refused turn: %s", len(got), got[0].subject)
			}
		})
	}
}

func TestDeliverChildResult_ReachesAProfileParentOnItsIn(t *testing.T) {
	r := startProfileRig(t)
	rec := &SessionRecord{Key: "discord:3/3", ContextID: "ctx-3", Tasks: []TaskRef{
		{ID: "task-parent", Addressee: "cluster", Tier: "read", CorrelationID: "corr-p",
			Attribution: json.RawMessage(`{"requester":{"backend":"discord","subject":"hmac:00"}}`)},
		{ID: "task-child", Addressee: "prod-east", Role: taskRoleChild, ParentTaskID: "task-parent"},
	}}
	child, _ := rec.TaskRefFor("task-child")
	if !r.g.deliverChildResult(context.Background(), rec, child, lib.StateCompleted, "the pod is OOMKilled", "") {
		t.Fatal("not delivered")
	}
	var got []subjectEnvelope
	waitFor(t, "the child result on the parent's .in", func() bool {
		got = streamEnvelopes(t, r.url, lib.TaskInSubject("cluster", "task-parent"))
		return len(got) == 1
	})
	c, ok := lib.ChildResultOf(messageParts(t, got[0].env))
	if !ok || c.ChildTaskID != "task-child" || c.State != lib.StateCompleted || c.Text != "the pod is OOMKilled" || c.Addressee != "prod-east" {
		t.Fatalf("child result = %+v, %v", c, ok)
	}
}

func TestDeliverChildResult_LeavesASessionParentToTheWake(t *testing.T) {
	r := startProfileRig(t)
	rec := &SessionRecord{Key: "discord:4/4", Tasks: []TaskRef{
		{ID: "task-parent", Addressee: "chat-otter-1a2b"},
		{ID: "task-child", Addressee: "prod-east", Role: taskRoleChild, ParentTaskID: "task-parent"},
	}}
	child, _ := rec.TaskRefFor("task-child")
	if r.g.deliverChildResult(context.Background(), rec, child, lib.StateCompleted, "done", "") {
		t.Fatal("a session parent's child was delivered as a child result")
	}
}

func messageParts(t *testing.T, env *lib.Envelope) []lib.Part {
	t.Helper()
	var m lib.Message
	if err := json.Unmarshal(env.Payload, &m); err != nil {
		t.Fatal(err)
	}
	return m.Parts
}

func mapsEqual(a, b map[string]string) bool {
	if len(a) != len(b) {
		return false
	}
	for k, v := range a {
		if b[k] != v {
			return false
		}
	}
	return true
}
