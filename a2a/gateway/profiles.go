package gateway

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"slices"
	"sort"
	"sync"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The task parameters that name a cluster target. A profile that declares all
// three takes one (spec-profiles-dispatch.md, "Addressing"); the dispatcher
// joins them into the verifier's resource in this order.
const (
	paramProject  = "project"
	paramLocation = "location"
	paramCluster  = "cluster"
)

// The refusals resolveProfile and checkTargetScope give, worded for the room.
// Each names what was asked, since the asker may not know the install's
// profiles or scope.
const (
	noticeProfileUnknown      = "⚠️ not started: there is no agent profile named %q"
	noticeProfilePinnedOther  = "⚠️ not started: %s works only on cluster %s, not %s"
	noticeProfileNoTarget     = "⚠️ not started: %s needs a target cluster (project, location and cluster)"
	noticeProfileTakesNone    = "⚠️ not started: %s doesn't take a target cluster"
	noticeProfileAmbiguous    = "⚠️ not started: more than one agent profile is pinned to cluster %s"
	noticeProfileNoGeneric    = "⚠️ not started: no agent profile serves cluster %s"
	noticeTargetExcluded      = "⚠️ not started: cluster %s is excluded from this install's scope"
	noticeTargetOutOfScope    = "⚠️ not started: cluster %s is outside this install's scope"
	noticeTargetUnconfirmed   = "⚠️ not started: can't confirm %s is in this install's scope yet"
	noticeTargetParamsInvalid = "⚠️ not started: the task's parameters aren't valid: %v"
)

// profileCard is the routing half of a profile's card on DIRECTORY
// (a2a.agents.<profile>), as the operator publishes it: the contract on #3655,
// addendum 1. Tiers absent means read alone.
type profileCard struct {
	Name        string             `json:"name"`
	Description string             `json:"description,omitempty"`
	Tiers       []string           `json:"tiers,omitempty"`
	ClusterRef  *profileClusterRef `json:"clusterRef,omitempty"`
	Parameters  []profileCardParam `json:"parameters,omitempty"`
}

type profileClusterRef struct {
	ProjectID string `json:"projectId"`
	Location  string `json:"location"`
	Cluster   string `json:"cluster"`
}

type profileCardParam struct {
	Name     string `json:"name"`
	Required bool   `json:"required,omitempty"`
}

// takesTarget reports whether the profile declares the three parameters that
// name a cluster.
func (c profileCard) takesTarget() bool {
	have := map[string]bool{}
	for _, p := range c.Parameters {
		have[p.Name] = true
	}
	return have[paramProject] && have[paramLocation] && have[paramCluster]
}

// clusterTarget is a cluster a task is addressed to.
type clusterTarget struct {
	Project, Location, Cluster string
}

func (t clusterTarget) String() string {
	return t.Project + "/" + t.Location + "/" + t.Cluster
}

func (t clusterTarget) matches(r *profileClusterRef) bool {
	return r != nil && r.ProjectID == t.Project && r.Location == t.Location && r.Cluster == t.Cluster
}

// scope is the target as the verifier's resource: project/P/location/L/cluster/C.
func (t clusterTarget) scope() capability.Scope {
	return capability.Scope("project/" + t.Project + "/location/" + t.Location + "/cluster/" + t.Cluster)
}

// targetOf reads a cluster target out of task parameters. ok is false when
// none of the three is set; partial is true when some but not all are.
func targetOf(params map[string]string) (t clusterTarget, ok, partial bool) {
	t = clusterTarget{Project: params[paramProject], Location: params[paramLocation], Cluster: params[paramCluster]}
	set := 0
	for _, v := range []string{t.Project, t.Location, t.Cluster} {
		if v != "" {
			set++
		}
	}
	return t, set == 3, set > 0 && set < 3
}

// profileDirectory is the gateway's view of the profiles it can address, kept
// from DIRECTORY. Safe for concurrent use.
type profileDirectory struct {
	mu    sync.RWMutex
	cards map[string]profileCard
}

func newProfileDirectory() *profileDirectory {
	return &profileDirectory{cards: map[string]profileCard{}}
}

// put records a card, or forgets the profile when card is nil (a deleted
// profile's card).
func (d *profileDirectory) put(name string, card *profileCard) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if card == nil {
		delete(d.cards, name)
		return
	}
	d.cards[name] = *card
}

func (d *profileDirectory) lookup(name string) (profileCard, bool) {
	d.mu.RLock()
	defer d.mu.RUnlock()
	c, ok := d.cards[name]
	return c, ok
}

// pinnedTo is every profile pinned to t, in name order.
func (d *profileDirectory) pinnedTo(t clusterTarget) []string {
	d.mu.RLock()
	defer d.mu.RUnlock()
	var out []string
	for name, c := range d.cards {
		if t.matches(c.ClusterRef) {
			out = append(out, name)
		}
	}
	sort.Strings(out)
	return out
}

// profileRoute is a task addressed to an AgentProfile: what the gateway mints
// the grant against and the subject it submits on.
type profileRoute struct {
	Profile string
	Tier    capability.Tier
	Kind    string
	Params  map[string]string
	Scope   capability.Scope
}

// errRefused carries a notice for the room.
type errRefused struct{ notice string }

func (e errRefused) Error() string { return e.notice }

func refused(format string, args ...any) error {
	return errRefused{notice: fmt.Sprintf(format, args...)}
}

// resolveProfile picks the profile a task addressed to `addressee` with
// `params` goes to (spec-profiles-dispatch.md, "Addressing", step 1):
//   - addressed to a pinned profile, the target must be its cluster;
//   - addressed to a profile that takes a target, a profile pinned to that
//     target gets it instead, and otherwise the addressed profile does;
//   - addressed to a profile that takes none, the task carries no target.
//
// Two profiles pinned to one cluster is refused rather than guessed between.
func resolveProfile(dir *profileDirectory, addressee string, params map[string]string) (string, *clusterTarget, error) {
	if err := lib.ValidateTaskParameters(params); err != nil {
		return "", nil, refused(noticeTargetParamsInvalid, err)
	}
	card, ok := dir.lookup(addressee)
	if !ok {
		return "", nil, refused(noticeProfileUnknown, addressee)
	}
	target, hasTarget, partial := targetOf(params)
	switch {
	case card.ClusterRef != nil:
		if !hasTarget {
			// A pinned profile's target is its own cluster.
			t := clusterTarget{Project: card.ClusterRef.ProjectID, Location: card.ClusterRef.Location, Cluster: card.ClusterRef.Cluster}
			return addressee, &t, nil
		}
		if !target.matches(card.ClusterRef) {
			ref := clusterTarget{Project: card.ClusterRef.ProjectID, Location: card.ClusterRef.Location, Cluster: card.ClusterRef.Cluster}
			return "", nil, refused(noticeProfilePinnedOther, addressee, ref, target)
		}
		return addressee, &target, nil
	case card.takesTarget():
		if !hasTarget {
			return "", nil, refused(noticeProfileNoTarget, addressee)
		}
		switch pinned := dir.pinnedTo(target); len(pinned) {
		case 0:
			return addressee, &target, nil
		case 1:
			return pinned[0], &target, nil
		default:
			return "", nil, refused(noticeProfileAmbiguous, target)
		}
	default:
		if hasTarget || partial {
			return "", nil, refused(noticeProfileTakesNone, addressee)
		}
		return addressee, nil, nil
	}
}

// scopeDeclaration is the operator's rendered spec.scope (scope.json, the
// file the platform agent also reads; renderScopeJSON in the operator).
type scopeDeclaration struct {
	Present       bool     `json:"present"`
	Projects      []string `json:"projects"`
	Folders       []string `json:"folders"`
	Organizations []string `json:"organizations"`
	Exclude       struct {
		Projects []string `json:"projects"`
		Clusters []struct {
			ProjectID   string `json:"projectId"`
			Location    string `json:"location"`
			ClusterName string `json:"clusterName"`
		} `json:"clusters"`
	} `json:"exclude"`
}

// errNoScopeFile is loadScope's answer when the file isn't there: an operator
// that renders none into the gateway pod.
var errNoScopeFile = errors.New("no scope file")

func loadScope(path string) (*scopeDeclaration, error) {
	raw, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, errNoScopeFile
	}
	if err != nil {
		return nil, err
	}
	var d scopeDeclaration
	if err := json.Unmarshal(raw, &d); err != nil {
		return nil, fmt.Errorf("scope file %s: %w", path, err)
	}
	return &d, nil
}

// checkTargetScope refuses a target outside the install's scope
// (spec-profiles-dispatch.md, "Addressing", step 2). An exclusion always
// refuses. A declared project list admits only its projects. Past that, a
// folder or organization scope can't be confirmed without resolving
// membership, which the gateway can't do yet (#4119), so it refuses: "not
// excluded" isn't "in scope". An install that declares no scope draws no
// boundary, as the platform agent reads it, so only exclusions apply. A nil
// declaration (no file rendered, or one that didn't parse) refuses every
// target.
func checkTargetScope(decl *scopeDeclaration, t clusterTarget) error {
	if decl == nil {
		return refused(noticeTargetUnconfirmed, t)
	}
	if slices.Contains(decl.Exclude.Projects, t.Project) {
		return refused(noticeTargetExcluded, t)
	}
	for _, c := range decl.Exclude.Clusters {
		if c.ProjectID == t.Project && c.Location == t.Location && c.ClusterName == t.Cluster {
			return refused(noticeTargetExcluded, t)
		}
	}
	if !decl.Present {
		return nil
	}
	if slices.Contains(decl.Projects, t.Project) {
		return nil
	}
	if len(decl.Folders) > 0 || len(decl.Organizations) > 0 {
		return refused(noticeTargetUnconfirmed, t)
	}
	return refused(noticeTargetOutOfScope, t)
}
