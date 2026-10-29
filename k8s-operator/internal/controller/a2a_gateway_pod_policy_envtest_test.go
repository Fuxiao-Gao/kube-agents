/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"bytes"
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	utilyaml "k8s.io/apimachinery/pkg/util/yaml"
	"k8s.io/client-go/rest"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// The policy source the chart's copy is generated from, relative to this package.
var gatewayPodPolicyFile = filepath.Join("..", "..", "config", "admission", "a2a-gateway-pod-policy.yaml")

const (
	gatewayPodPolicyNamespace = "a2a-pod-policy"
	gatewayPodPolicyAgent     = "pa"
	// How long the API server may take to start enforcing a policy it just stored.
	gatewayPodPolicyActivation = 30 * time.Second
)

// TestTheGatewayPodPolicyHoldsGatewayPodsToTheSessionShape runs the shipped
// policy in a real API server: a pod the A2A gateway's ServiceAccount creates
// is admitted only in the shape a2a/gateway/spawn.go builds, and every way out
// of that shape is denied by the rule that names it. Other requesters are not
// selected.
func TestTheGatewayPodPolicyHoldsGatewayPodsToTheSessionShape(t *testing.T) {
	cfg, scheme := startEnvtestConfig(t)
	admin, err := client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	applyGatewayPodPolicy(ctx, t, admin)

	ns := gatewayPodPolicyNamespace
	mustCreate(ctx, t, admin, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: ns}})
	for _, sa := range []string{gatewayPodPolicyAgent + "-a2a-gateway", gatewayPodPolicyAgent + "-a2a-session", gatewayPodPolicyAgent, "other-a2a-gateway"} {
		mustCreate(ctx, t, admin, &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: sa, Namespace: ns}})
	}
	// The gateway Role's pod verbs, for both gateway-named identities.
	mustCreate(ctx, t, admin, &rbacv1.Role{
		ObjectMeta: metav1.ObjectMeta{Name: "pod-create", Namespace: ns},
		Rules:      []rbacv1.PolicyRule{{APIGroups: []string{""}, Resources: []string{"pods"}, Verbs: []string{"create"}}},
	})
	mustCreate(ctx, t, admin, &rbacv1.RoleBinding{
		ObjectMeta: metav1.ObjectMeta{Name: "pod-create", Namespace: ns},
		RoleRef:    rbacv1.RoleRef{APIGroup: rbacv1.GroupName, Kind: "Role", Name: "pod-create"},
		Subjects: []rbacv1.Subject{
			{Kind: rbacv1.ServiceAccountKind, Name: gatewayPodPolicyAgent + "-a2a-gateway", Namespace: ns},
			{Kind: rbacv1.ServiceAccountKind, Name: "other-a2a-gateway", Namespace: ns},
		},
	})
	gateway := impersonatedClient(t, cfg, ns, gatewayPodPolicyAgent+"-a2a-gateway")

	// The policy is stored before it is enforced; wait until a pod with the
	// platform agent's identity is refused.
	asAgent := sessionPod("activation", func(p *corev1.Pod) { p.Spec.ServiceAccountName = gatewayPodPolicyAgent })
	deadline := time.Now().Add(gatewayPodPolicyActivation)
	for {
		err := gateway.Create(ctx, asAgent.DeepCopy())
		if apierrors.IsForbidden(err) || apierrors.IsInvalid(err) {
			break
		}
		if err == nil {
			_ = admin.Delete(ctx, asAgent.DeepCopy())
		}
		if time.Now().After(deadline) {
			t.Fatalf("the policy was not enforced within %s (last create: %v)", gatewayPodPolicyActivation, err)
		}
		time.Sleep(time.Second)
	}

	for name, mutate := range map[string]func(*corev1.Pod){
		"the session pod":                   func(*corev1.Pod) {},
		"the session pod with cluster view": withClusterViewToken,
	} {
		if err := gateway.Create(ctx, sessionPod(strings.ReplaceAll(name, " ", "-"), mutate)); err != nil {
			t.Errorf("%s: refused, want admitted: %v", name, err)
		}
	}

	denied := map[string]struct {
		mutate func(*corev1.Pod)
		rule   string
	}{
		"the platform agent's identity": {func(p *corev1.Pod) { p.Spec.ServiceAccountName = gatewayPodPolicyAgent }, "must run as pa-a2a-session"},
		// Without its token volume: the API server refuses a projected token on
		// a pod with no ServiceAccount before admission runs.
		"no ServiceAccount": {func(p *corev1.Pod) {
			p.Spec.ServiceAccountName = ""
			p.Spec.Volumes = p.Spec.Volumes[:2]
			p.Spec.Containers[0].VolumeMounts = p.Spec.Containers[0].VolumeMounts[:1]
		}, "must run as pa-a2a-session"},
		"automount on":    {func(p *corev1.Pod) { p.Spec.AutomountServiceAccountToken = ptr.To(true) }, "automountServiceAccountToken: false"},
		"automount unset": {func(p *corev1.Pod) { p.Spec.AutomountServiceAccountToken = nil }, "automountServiceAccountToken: false"},
		"a Secret volume": {func(p *corev1.Pod) {
			p.Spec.Volumes = append(p.Spec.Volumes, corev1.Volume{Name: "s", VolumeSource: corev1.VolumeSource{Secret: &corev1.SecretVolumeSource{SecretName: "x"}}})
		}, "may mount only"},
		"a hostPath volume": {func(p *corev1.Pod) {
			p.Spec.Volumes = append(p.Spec.Volumes, corev1.Volume{Name: "h", VolumeSource: corev1.VolumeSource{HostPath: &corev1.HostPathVolumeSource{Path: "/"}}})
		}, "may mount only"},
		"a Secret projection": {func(p *corev1.Pod) {
			p.Spec.Volumes = append(p.Spec.Volumes, corev1.Volume{Name: "ps", VolumeSource: corev1.VolumeSource{Projected: &corev1.ProjectedVolumeSource{
				Sources: []corev1.VolumeProjection{{Secret: &corev1.SecretProjection{LocalObjectReference: corev1.LocalObjectReference{Name: "x"}}}},
			}}})
		}, "may mount only"},
		"a token for the scope audience": {func(p *corev1.Pod) {
			p.Spec.Volumes = append(p.Spec.Volumes, tokenVolume("scope", "kubeagents-credential-proxy-scope"))
		}, "a2a-bus and kubeagents-credential-proxy-session"},
		"a token with no audience": {func(p *corev1.Pod) { p.Spec.Volumes = append(p.Spec.Volumes, tokenVolume("api", "")) }, "a2a-bus and kubeagents-credential-proxy-session"},
		"host network":             {func(p *corev1.Pod) { p.Spec.HostNetwork = true }, "host namespaces"},
		"an init container": {func(p *corev1.Pod) {
			p.Spec.InitContainers = []corev1.Container{{Name: "init", Image: "busybox"}}
		}, "host namespaces"},
		"a privileged container": {func(p *corev1.Pod) {
			p.Spec.Containers[0].SecurityContext = &corev1.SecurityContext{Privileged: ptr.To(true)}
		}, "host namespaces"},
		"env from a Secret": {func(p *corev1.Pod) {
			p.Spec.Containers[0].Env = append(p.Spec.Containers[0].Env, corev1.EnvVar{Name: "K", ValueFrom: &corev1.EnvVarSource{
				SecretKeyRef: &corev1.SecretKeySelector{LocalObjectReference: corev1.LocalObjectReference{Name: "x"}, Key: "k"},
			}})
		}, "host namespaces"},
		"envFrom a Secret": {func(p *corev1.Pod) {
			p.Spec.Containers[0].EnvFrom = []corev1.EnvFromSource{{SecretRef: &corev1.SecretEnvSource{LocalObjectReference: corev1.LocalObjectReference{Name: "x"}}}}
		}, "host namespaces"},
		"no session label": {func(p *corev1.Pod) { delete(p.Labels, "app.kubernetes.io/component") }, "session labels"},
	}
	for name, c := range denied {
		err := gateway.Create(ctx, sessionPod("denied", c.mutate))
		if err == nil {
			t.Errorf("%s: admitted, want refused", name)
			_ = admin.Delete(ctx, sessionPod("denied", nil))
			continue
		}
		if !strings.Contains(err.Error(), c.rule) {
			t.Errorf("%s: refused for %q, want the rule %q", name, err, c.rule)
		}
	}

	// The session account follows the requester's name: another install's
	// gateway may not run its pods as this install's session account.
	other := impersonatedClient(t, cfg, ns, "other-a2a-gateway")
	if err := other.Create(ctx, sessionPod("other", nil)); err == nil || !strings.Contains(err.Error(), "must run as other-a2a-session") {
		t.Errorf("another gateway creating a pod as pa-a2a-session: %v, want refused for other-a2a-session", err)
	}

	// Not selected: the same out-of-shape pod from an identity that is not a gateway.
	if err := admin.Create(ctx, sessionPod("admin", func(p *corev1.Pod) { p.Spec.ServiceAccountName = gatewayPodPolicyAgent })); err != nil {
		t.Errorf("a non-gateway requester's pod was refused: %v", err)
	}
}

// sessionPod is the pod a2a/gateway/spawn.go builds, reduced to the fields the
// policy reads, with mutate applied when it is not nil.
func sessionPod(name string, mutate func(*corev1.Pod)) *corev1.Pod {
	p := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: gatewayPodPolicyNamespace,
			Labels: map[string]string{
				"app.kubernetes.io/part-of":   "a2a-next",
				"app.kubernetes.io/component": "a2a-session",
			},
		},
		Spec: corev1.PodSpec{
			RestartPolicy:                corev1.RestartPolicyNever,
			ServiceAccountName:           gatewayPodPolicyAgent + "-a2a-session",
			AutomountServiceAccountToken: ptr.To(false),
			Containers: []corev1.Container{{
				Name:  "worker",
				Image: "worker",
				VolumeMounts: []corev1.VolumeMount{
					{Name: "scratch", MountPath: "/scratch"},
					{Name: "bus-token", MountPath: "/var/run/secrets/a2a", ReadOnly: true},
				},
				SecurityContext: &corev1.SecurityContext{AllowPrivilegeEscalation: ptr.To(false)},
			}},
			Volumes: []corev1.Volume{
				{Name: "scratch", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}},
				{Name: "primer", VolumeSource: corev1.VolumeSource{DownwardAPI: &corev1.DownwardAPIVolumeSource{
					Items: []corev1.DownwardAPIVolumeFile{{Path: "primer", FieldRef: &corev1.ObjectFieldSelector{FieldPath: "metadata.name"}}},
				}}},
				tokenVolume("bus-token", "a2a-bus"),
				{Name: "nats-ca", VolumeSource: corev1.VolumeSource{ConfigMap: &corev1.ConfigMapVolumeSource{
					LocalObjectReference: corev1.LocalObjectReference{Name: "nats-ca"},
				}}},
			},
		},
	}
	if mutate != nil {
		mutate(p)
	}
	return p
}

func withClusterViewToken(p *corev1.Pod) {
	p.Name = "cluster-view"
	p.Spec.Volumes = append(p.Spec.Volumes, tokenVolume("credential-proxy-token", "kubeagents-credential-proxy-session"))
}

func tokenVolume(name, audience string) corev1.Volume {
	return corev1.Volume{Name: name, VolumeSource: corev1.VolumeSource{Projected: &corev1.ProjectedVolumeSource{
		Sources: []corev1.VolumeProjection{{ServiceAccountToken: &corev1.ServiceAccountTokenProjection{
			Audience: audience, ExpirationSeconds: ptr.To(int64(3600)), Path: "token",
		}}},
	}}}
}

func impersonatedClient(t *testing.T, cfg *rest.Config, ns, sa string) client.Client {
	t.Helper()
	as := rest.CopyConfig(cfg)
	as.Impersonate = rest.ImpersonationConfig{
		UserName: "system:serviceaccount:" + ns + ":" + sa,
		Groups:   []string{"system:serviceaccounts", "system:serviceaccounts:" + ns, "system:authenticated"},
	}
	c, err := client.New(as, client.Options{})
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func applyGatewayPodPolicy(ctx context.Context, t *testing.T, c client.Client) {
	t.Helper()
	raw, err := os.ReadFile(gatewayPodPolicyFile)
	if err != nil {
		t.Fatal(err)
	}
	decoder := utilyaml.NewYAMLOrJSONDecoder(bytes.NewReader(raw), len(raw))
	for {
		u := &unstructured.Unstructured{}
		if err := decoder.Decode(&u.Object); err != nil {
			if errors.Is(err, io.EOF) {
				return
			}
			t.Fatal(err)
		}
		if len(u.Object) == 0 {
			continue
		}
		if err := c.Create(ctx, u); err != nil {
			t.Fatalf("apply %s %s: %v", u.GetKind(), u.GetName(), err)
		}
	}
}

func mustCreate(ctx context.Context, t *testing.T, c client.Client, obj client.Object) {
	t.Helper()
	if err := c.Create(ctx, obj); err != nil {
		t.Fatalf("create %T %s: %v", obj, obj.GetName(), err)
	}
}
