package gateway

import (
	"context"
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

// profileGrantDelegate is the principal a profile task's grant names as its
// delegate: the dispatcher, which resolves the grant when it asks the
// verifier whether the task may run (the contract on #3655, section 6). The
// verifier resolves an entry only for the caller it names.
const profileGrantDelegate = "dispatcher"

// mintTaskCapability mints a task's root grant: against the resolved profile
// for a profile route, and as mintCapability does for everything else.
func (g *Gateway) mintTaskCapability(ctx context.Context, taskID, addressee string, route *profileRoute) (*capability.Ref, error) {
	if route == nil {
		return g.mintCapability(ctx, taskID, addressee)
	}
	ref, err := capability.NewMinter(g.client.JetStream()).Mint(ctx, taskID, capability.Entry{
		Tier:     route.Tier,
		Scope:    route.Scope,
		Profile:  route.Profile,
		Delegate: profileGrantDelegate,
	})
	if err != nil {
		return nil, err
	}
	return &ref, nil
}

// submissionPayload is the submission's message: the text, and for a profile
// route with parameters, the taskParameters part beside it.
func submissionPayload(text, taskID, contextID string, route *profileRoute) ([]byte, error) {
	if route == nil || len(route.Params) == 0 {
		return messagePayload(text, taskID, contextID)
	}
	params, err := lib.TaskParametersPart(lib.TaskParameters{Parameters: route.Params})
	if err != nil {
		return nil, err
	}
	return messagePayloadParts([]lib.Part{{Kind: "text", Text: text}, params}, taskID, contextID)
}

// submissionSubject is where a new task is published: a profile's .submit
// subject, carrying the tier and kind its durables route on, or the
// addressee's .in subject for platform and sessions.
func submissionSubject(addressee, taskID string, route *profileRoute) string {
	if route == nil {
		return lib.TaskInSubject(addressee, taskID)
	}
	return lib.TaskSubmitSubject(route.Profile, string(route.Tier), route.Kind, taskID)
}

// takesTarget reports whether a profile's card declares the three
// parameters that name a cluster (the contract on #3655, addendum 1).
func takesTarget(c lib.AgentCard) bool {
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

func (t clusterTarget) matches(r *lib.ClusterRef) bool {
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
// from their cards on DIRECTORY (a2a.agents.<profile>). Safe for concurrent use.
type profileDirectory struct {
	mu    sync.RWMutex
	cards map[string]lib.AgentCard
}

func newProfileDirectory() *profileDirectory {
	return &profileDirectory{cards: map[string]lib.AgentCard{}}
}

// put records a card, or forgets the profile when card is nil (a deleted
// profile's card).
func (d *profileDirectory) put(name string, card *lib.AgentCard) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if card == nil {
		delete(d.cards, name)
		return
	}
	d.cards[name] = *card
}

func (d *profileDirectory) lookup(name string) (lib.AgentCard, bool) {
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
	case takesTarget(card):
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
