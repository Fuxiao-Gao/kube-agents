package controller

import (
	"context"
	"encoding/json"
	"slices"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	dispatcherMapFixturePath  = "../../../a2a/authcallout/testdata/rendered-identity-map-dispatcher.json"
	dispatchConfigFixturePath = "../../../a2a/lib/testdata/rendered-dispatch-config.json"
	testDispatcherImage       = "registry.example/a2a-dispatcher:test"
)

func withDispatcher(t *testing.T) {
	t.Helper()
	t.Setenv(a2aDispatcherImageEnvVar, testDispatcherImage)
}

// Without an image the operator renders no dispatcher, not even its bus
// identity: the image isn't in the release set yet.
func TestNoDispatcherImageRendersNoDispatcher(t *testing.T) {
	hasDispatcher := func() bool {
		doc, err := renderA2AAuthMap(a2aTestAgent(), nil)
		if err != nil {
			t.Fatal(err)
		}
		for _, id := range doc.Identities {
			if id.User == a2aDispatcherUser {
				return true
			}
		}
		return false
	}
	t.Setenv(a2aDispatcherImageEnvVar, "")
	if hasDispatcher() {
		t.Fatal("the identity map carries the dispatcher with no image to run it")
	}
	withDispatcher(t)
	if !hasDispatcher() {
		t.Error("with an image, the identity map has no dispatcher")
	}
}

// The dispatcher's grants are exactly the contract's, and in particular it
// reads no task subject and creates no consumer on TASKS.
func TestTheDispatcherIdentityHoldsOnlyItsContractGrants(t *testing.T) {
	agent := a2aTestAgent()
	id := dispatcherIdentity(agent, agent.Namespace, []string{"tse"}, []string{"profile-tse-read-top", "profile-tse-read-child"})
	if id.serviceAccount != "system:serviceaccount:test-ns:test-agent-a2a-dispatcher" || id.auth != a2aAuthCallout {
		t.Errorf("serviceAccount %q, auth %v", id.serviceAccount, id.auth)
	}
	for _, s := range id.subscribe {
		if s == ">" || slices.Contains([]string{"a2a.tasks.>", "a2a.>", "a2a.tasks.*.*.in", "a2a.tasks.*.*.events"}, s) {
			t.Errorf("the dispatcher subscribes to %s; it reads submissions only through its durables", s)
		}
	}
	for _, p := range id.publish {
		switch p {
		case "$JS.API.CONSUMER.CREATE.TASKS.>", "$JS.API.CONSUMER.DURABLE.CREATE.TASKS.>", "$KV.cap.>", "a2a.tasks.>", ">":
			t.Errorf("the dispatcher publishes %s", p)
		}
	}
	for _, p := range id.publish {
		if strings.Contains(p, "TASKS.*") || strings.Contains(p, "profile-*") {
			t.Errorf("the dispatcher's grant %s is a wildcard over consumers: a token wildcard reaches every session's consumer", p)
		}
	}
	if slices.Contains(id.publish, "a2a.tasks.*.*.events") {
		t.Error("the dispatcher may write any task's events; it should name the profiles it dispatches for")
	}
	for _, want := range []string{"$KV.PROFILE_LEASES.>", "a2a.dispatch.*.*.*", "a2a.tasks.tse.*.events", "a2a.cap.verify.dispatcher",
		"$JS.API.CONSUMER.MSG.NEXT.TASKS.profile-tse-read-top", "$JS.ACK.TASKS.profile-tse-read-child.>"} {
		if !slices.Contains(id.publish, want) {
			t.Errorf("the dispatcher can't publish %s", want)
		}
	}
	for _, want := range []string{"a2a.dispatch.*.*.*.accept", "a2a.dispatch.*.*.*.lease", "a2a.cap.reply.dispatcher.>"} {
		if !slices.Contains(id.subscribe, want) {
			t.Errorf("the dispatcher can't subscribe %s", want)
		}
	}
}

// One dispatcher, replaced with Recreate, running the operator's image, as
// its own ServiceAccount, told where its config is.
func TestTheDispatcherDeploymentRunsOneDispatcher(t *testing.T) {
	withDispatcher(t)
	agent := a2aTestAgent()
	dep := buildA2ADispatcherDeployment(agent)
	if *dep.Spec.Replicas != 1 || dep.Spec.Strategy.Type != appsv1.RecreateDeploymentStrategyType {
		t.Errorf("replicas %d, strategy %s; two dispatchers would each enforce the ceiling on half the picture", *dep.Spec.Replicas, dep.Spec.Strategy.Type)
	}
	pod := dep.Spec.Template.Spec
	if pod.ServiceAccountName != a2aDispatcherName(agent) || pod.Containers[0].Image != testDispatcherImage {
		t.Errorf("serviceAccount %q, image %q", pod.ServiceAccountName, pod.Containers[0].Image)
	}
	env := map[string]string{}
	for _, e := range pod.Containers[0].Env {
		env[e.Name] = e.Value
	}
	if env["DISPATCH_CONFIGMAP"] != a2aDispatchConfigName(agent) {
		t.Errorf("DISPATCH_CONFIGMAP = %q", env["DISPATCH_CONFIGMAP"])
	}
}

// The config the dispatcher reads: every value resolved, refused profiles
// left out, and a durable per (tier, kind) for the rest.
func TestTheDispatchConfigIsResolvedAndListsTheDurables(t *testing.T) {
	agent := a2aTestAgent()
	both := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead, agentv1alpha1.AgentProfileTierUnattended}
		p.Spec.Concurrency = 3
		p.Spec.Parameters = []agentv1alpha1.AgentProfileParameter{{Name: "cluster", Required: true}}
	})
	refused := testAgentProfile(agent.Namespace, "platform") // the bridge's addressee
	cfg, err := desiredDispatchConfig(agent, []agentv1alpha1.AgentProfile{both, refused})
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Ceiling != defaultKanbanMaxInProgress || cfg.ChildReserve != 2 || cfg.InteractiveReserve != 2 || cfg.LeaseSeconds != 90 || cfg.Namespace != "test-ns" || cfg.ProfileNamespace != "test-ns-profiles" {
		t.Errorf("config = %+v", cfg)
	}
	if len(cfg.Profiles) != 1 || cfg.Profiles[0].Name != "tse" {
		t.Fatalf("profiles = %+v, want tse alone (platform is refused)", cfg.Profiles)
	}
	var names []string
	for _, d := range cfg.Profiles[0].Durables {
		names = append(names, d.Name)
	}
	want := []string{"profile-tse-read-top", "profile-tse-read-child", "profile-tse-unattended-top", "profile-tse-unattended-child"}
	if !slices.Equal(names, want) {
		t.Errorf("durables = %v, want %v", names, want)
	}
	again, _ := desiredDispatchConfig(agent, []agentv1alpha1.AgentProfile{both, refused})
	if again.Version != cfg.Version || cfg.Version == "" {
		t.Errorf("version %q then %q: the version must follow the content", cfg.Version, again.Version)
	}
	tuned := agent.DeepCopy()
	tuned.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{Profiles: &agentv1alpha1.ProfileTuning{LeaseSeconds: ptr.To(120)}}}
	changed, _ := desiredDispatchConfig(tuned, []agentv1alpha1.AgentProfile{both})
	if changed.Version == cfg.Version {
		t.Error("a tuning change kept the version")
	}
}

// The cross-module fixtures: the identity map with the dispatcher in it,
// which the callout suite runs a real server against, and the dispatch
// config, which lib decodes and validates.
func TestRenderedDispatcherFixtures(t *testing.T) {
	withDispatcher(t)
	// With the operator, whose durable grants come with the dispatcher.
	withOperatorBusPrincipal(t)
	agent := authMapTestAgent()
	tse := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.Parameters = []agentv1alpha1.AgentProfileParameter{{Name: "project", Required: true}, {Name: "location", Required: true}, {Name: "cluster", Required: true}}
	})
	cm, _, err := buildA2AAuthMapConfigMap(agent, []agentv1alpha1.AgentProfile{tse})
	if err != nil {
		t.Fatalf("buildA2AAuthMapConfigMap: %v", err)
	}
	checkFixture(t, dispatcherMapFixturePath, cm.Data[a2aAuthMapKey])
	cfgMap, err := buildA2ADispatchConfigMap(agent, []agentv1alpha1.AgentProfile{tse})
	if err != nil {
		t.Fatal(err)
	}
	checkFixture(t, dispatchConfigFixturePath, cfgMap.Data[a2aDispatchConfigKey])
	var decoded map[string]any
	if err := json.Unmarshal([]byte(cfgMap.Data[a2aDispatchConfigKey]), &decoded); err != nil {
		t.Fatalf("the dispatch config is not JSON: %v", err)
	}
}

// Turning the image off removes what the dispatcher had: the owned objects
// in the agent's namespace, and the pod-read Role and binding in the profile
// namespace that carry this agent's labels, and nothing someone else made.
func TestRemovingTheDispatcherImageRemovesTheDispatcher(t *testing.T) {
	withDispatcher(t)
	agent := a2aTestAgent()
	scheme := setupScheme()
	c := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, chartProfileNamespace(agent)).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).Build()
	r := &PlatformAgentReconciler{Client: c, Scheme: scheme}
	ctx := context.Background()
	if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, nil, true); err != nil || profileErr != nil {
		t.Fatalf("render: %v / %v", err, profileErr)
	}
	pns := profileNamespaceName(agent)
	name := a2aDispatcherName(agent)
	for _, check := range []struct {
		obj client.Object
		key types.NamespacedName
	}{
		{&appsv1.Deployment{}, types.NamespacedName{Namespace: agent.Namespace, Name: name}},
		{&corev1.ConfigMap{}, types.NamespacedName{Namespace: agent.Namespace, Name: a2aDispatchConfigName(agent)}},
		{&rbacv1.Role{}, types.NamespacedName{Namespace: pns, Name: name}},
		{&rbacv1.RoleBinding{}, types.NamespacedName{Namespace: pns, Name: name}},
	} {
		if err := c.Get(ctx, check.key, check.obj); err != nil {
			t.Fatalf("precondition: %T %s not rendered: %v", check.obj, check.key, err)
		}
	}
	t.Setenv(a2aDispatcherImageEnvVar, "")
	if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, nil, true); err != nil || profileErr != nil {
		t.Fatalf("remove: %v / %v", err, profileErr)
	}
	for _, check := range []struct {
		obj client.Object
		key types.NamespacedName
	}{
		{&appsv1.Deployment{}, types.NamespacedName{Namespace: agent.Namespace, Name: name}},
		{&corev1.ServiceAccount{}, types.NamespacedName{Namespace: agent.Namespace, Name: name}},
		{&corev1.ConfigMap{}, types.NamespacedName{Namespace: agent.Namespace, Name: a2aDispatchConfigName(agent)}},
		{&rbacv1.Role{}, types.NamespacedName{Namespace: agent.Namespace, Name: name}},
		{&rbacv1.Role{}, types.NamespacedName{Namespace: pns, Name: name}},
		{&rbacv1.RoleBinding{}, types.NamespacedName{Namespace: pns, Name: name}},
	} {
		if err := c.Get(ctx, check.key, check.obj); !apierrors.IsNotFound(err) {
			t.Errorf("%T %s survived the image going away: %v", check.obj, check.key, err)
		}
	}
	// A Role under the dispatcher's name in the profile namespace that
	// doesn't carry this agent's labels is someone else's, and stays.
	foreign := &rbacv1.Role{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: pns, Labels: map[string]string{"x": "y"}}}
	if err := c.Create(ctx, foreign); err != nil {
		t.Fatal(err)
	}
	if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, nil, true); err != nil || profileErr != nil {
		t.Fatalf("remove again: %v / %v", err, profileErr)
	}
	if err := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: name}, &rbacv1.Role{}); err != nil {
		t.Errorf("an unlabelled Role under the dispatcher's name was deleted: %v", err)
	}
}
