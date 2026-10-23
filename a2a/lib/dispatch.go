package lib

import "fmt"

// The profile dispatcher's hand-off to warm workers and the per-task
// liveness lease (#3655; #3660, "Admission and holding"). Workers have no
// fetch grant on the shared durables: the dispatcher holds each submission
// and hands it to one worker on that worker's own subject, and the worker
// answers on a subject only it may publish, so the dispatcher can take the
// subject's pod token as the answer's identity and never a name in the
// payload. A worker renews a lease for each task it runs under a key that
// starts with its own (profile, tier, pod), so it can only write its own
// leases, and the dispatcher rebuilds which workers are busy by listing a
// (profile, tier)'s keys after a restart.

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

// ProfileLeaseKey is a task's liveness lease, written by the worker running
// it: <profile>.<tier>.<pod>.<taskId>.
func ProfileLeaseKey(profile, tier, pod, taskID string) string {
	return fmt.Sprintf("%s.%s.%s.%s", profile, tier, pod, taskID)
}

// ProfileLeaseSubject is the KV subject a lease key is written on.
func ProfileLeaseSubject(key string) string {
	return "$KV." + ProfileLeaseBucket + "." + key
}
