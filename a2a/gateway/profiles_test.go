package gateway

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

func targetParams(project, location, cluster string) map[string]string {
	return map[string]string{"project": project, "location": location, "cluster": cluster}
}

var clusterParams = []lib.ProfileParameter{{Name: "project", Required: true}, {Name: "location", Required: true}, {Name: "cluster", Required: true}}

func testDirectory() *profileDirectory {
	d := newProfileDirectory()
	d.put("cluster", &lib.AgentCard{Name: "cluster", Parameters: clusterParams})
	d.put("prod-east", &lib.AgentCard{Name: "prod-east", Parameters: clusterParams,
		ClusterRef: &lib.ClusterRef{ProjectID: "p1", Location: "us-east1", Cluster: "prod"}})
	d.put("chat", &lib.AgentCard{Name: "chat"})
	return d
}

func refusalOf(t *testing.T, err error) string {
	t.Helper()
	var r errRefused
	if !errors.As(err, &r) {
		t.Fatalf("err = %v, want a refusal", err)
	}
	return r.notice
}

func TestResolveProfile(t *testing.T) {
	d := testDirectory()
	cases := []struct {
		name, addressee string
		params          map[string]string
		wantProfile     string
		wantTarget      string
		wantRefusal     string
	}{
		{name: "generic with an unpinned target", addressee: "cluster", params: targetParams("p1", "us-east1", "dev"),
			wantProfile: "cluster", wantTarget: "p1/us-east1/dev"},
		{name: "generic with a pinned target goes to the pinned profile", addressee: "cluster", params: targetParams("p1", "us-east1", "prod"),
			wantProfile: "prod-east", wantTarget: "p1/us-east1/prod"},
		{name: "pinned with its own target", addressee: "prod-east", params: targetParams("p1", "us-east1", "prod"),
			wantProfile: "prod-east", wantTarget: "p1/us-east1/prod"},
		{name: "pinned with no target takes its own cluster", addressee: "prod-east",
			wantProfile: "prod-east", wantTarget: "p1/us-east1/prod"},
		{name: "pinned addressed for another cluster", addressee: "prod-east", params: targetParams("p1", "us-east1", "dev"),
			wantRefusal: "works only on cluster p1/us-east1/prod, not p1/us-east1/dev"},
		{name: "generic with no target", addressee: "cluster", wantRefusal: "needs a target cluster"},
		{name: "generic with half a target", addressee: "cluster", params: map[string]string{"project": "p1"},
			wantRefusal: "needs a target cluster"},
		{name: "a profile that takes no target, given one", addressee: "chat", params: targetParams("p1", "us-east1", "dev"),
			wantRefusal: "doesn't take a target cluster"},
		{name: "a profile that takes no target", addressee: "chat", wantProfile: "chat"},
		{name: "unknown profile", addressee: "nope", wantRefusal: `no agent profile named "nope"`},
		{name: "a parameter value that would split the resource path", addressee: "cluster", params: targetParams("p1", "us-east1", "a/b"),
			wantRefusal: "parameters aren't valid"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			profile, target, err := resolveProfile(d, tc.addressee, tc.params)
			if tc.wantRefusal != "" {
				if got := refusalOf(t, err); !strings.Contains(got, tc.wantRefusal) {
					t.Fatalf("refusal = %q, want it to contain %q", got, tc.wantRefusal)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if profile != tc.wantProfile {
				t.Fatalf("profile = %q, want %q", profile, tc.wantProfile)
			}
			got := ""
			if target != nil {
				got = target.String()
			}
			if got != tc.wantTarget {
				t.Fatalf("target = %q, want %q", got, tc.wantTarget)
			}
		})
	}
}

func TestResolveProfile_TwoProfilesPinnedToOneClusterIsRefused(t *testing.T) {
	d := testDirectory()
	d.put("prod-east-2", &lib.AgentCard{Name: "prod-east-2", Parameters: clusterParams,
		ClusterRef: &lib.ClusterRef{ProjectID: "p1", Location: "us-east1", Cluster: "prod"}})
	_, _, err := resolveProfile(d, "cluster", targetParams("p1", "us-east1", "prod"))
	if got := refusalOf(t, err); !strings.Contains(got, "more than one agent profile is pinned") {
		t.Fatalf("refusal = %q", got)
	}
}

func TestResolveProfile_ADeletedCardIsForgotten(t *testing.T) {
	d := testDirectory()
	d.put("prod-east", nil)
	profile, _, err := resolveProfile(d, "cluster", targetParams("p1", "us-east1", "prod"))
	if err != nil || profile != "cluster" {
		t.Fatalf("profile, err = %q, %v; want the generic profile once the pinned one is gone", profile, err)
	}
}

func TestTargetScopeIsTheVerifiersResourceForm(t *testing.T) {
	got := clusterTarget{Project: "p1", Location: "us-east1", Cluster: "prod"}.scope()
	if got != "project/p1/location/us-east1/cluster/prod" {
		t.Fatalf("scope = %q", got)
	}
	if err := got.Validate(); err != nil {
		t.Fatal(err)
	}
}

func scopeFrom(t *testing.T, body string) *scopeDeclaration {
	t.Helper()
	path := filepath.Join(t.TempDir(), "scope.json")
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	d, err := loadScope(path)
	if err != nil {
		t.Fatal(err)
	}
	return d
}

func TestCheckTargetScope(t *testing.T) {
	target := clusterTarget{Project: "p1", Location: "us-east1", Cluster: "prod"}
	cases := []struct {
		name, scope string
		wantRefusal string
	}{
		{name: "a listed project", scope: `{"present":true,"projects":["p1"]}`},
		{name: "an unlisted project", scope: `{"present":true,"projects":["p2"]}`, wantRefusal: "outside this install's scope"},
		{name: "an excluded cluster in a listed project",
			scope:       `{"present":true,"projects":["p1"],"exclude":{"clusters":[{"projectId":"p1","location":"us-east1","clusterName":"prod"}]}}`,
			wantRefusal: "excluded"},
		{name: "an excluded project", scope: `{"present":true,"projects":["p1"],"exclude":{"projects":["p1"]}}`, wantRefusal: "excluded"},
		{name: "a folder scope can't confirm an unlisted project", scope: `{"present":true,"folders":["123"]}`,
			wantRefusal: "can't confirm p1/us-east1/prod is in this install's scope yet"},
		{name: "an organization scope can't confirm either", scope: `{"present":true,"organizations":["456"]}`,
			wantRefusal: "can't confirm"},
		{name: "a folder scope that also lists the project", scope: `{"present":true,"folders":["123"],"projects":["p1"]}`},
		{name: "no declared scope draws no boundary", scope: `{"present":false}`},
		{name: "no declared scope still honours an exclusion",
			scope:       `{"present":false,"exclude":{"clusters":[{"projectId":"p1","location":"us-east1","clusterName":"prod"}]}}`,
			wantRefusal: "excluded"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := checkTargetScope(scopeFrom(t, tc.scope), target)
			if tc.wantRefusal == "" {
				if err != nil {
					t.Fatalf("err = %v, want in scope", err)
				}
				return
			}
			if got := refusalOf(t, err); !strings.Contains(got, tc.wantRefusal) {
				t.Fatalf("refusal = %q, want it to contain %q", got, tc.wantRefusal)
			}
		})
	}
}

func TestCheckTargetScope_NoDeclarationRefusesEveryTarget(t *testing.T) {
	err := checkTargetScope(nil, clusterTarget{Project: "p1", Location: "l", Cluster: "c"})
	if got := refusalOf(t, err); !strings.Contains(got, "can't confirm") {
		t.Fatalf("refusal = %q", got)
	}
	if _, err := loadScope(filepath.Join(t.TempDir(), "absent.json")); !errors.Is(err, errNoScopeFile) {
		t.Fatalf("loadScope(absent) = %v, want errNoScopeFile", err)
	}
}
