# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# The deployer scans this directory's *.tf only; a variable a task names and
# this file does not declare raises ConfigError, and an injected one this
# file does not declare is dropped silently — the matched-pair rule
# prebuilt/autoops-incident/variables.tf states.

variable "project_id" {
  type        = string
  description = "GCP Project ID the planted VPC and cluster are created in"
}

# The runner's per-run task-cluster name (TF_VAR_cluster_name, derived from
# the Prow run identity by hack/ci-eval-pr.sh). Not used as a resource name:
# every stack in a run is handed the same value, and prebuilt/gpu-stress-test
# creates a cluster under exactly that name. main.tf hashes it into this
# stack's own names instead, so the two never share one.
variable "cluster_name" {
  type        = string
  description = "Per-run identity the stack's resource names are derived from"
}

# A zone, not a region: a regional cluster puts one node in each of three
# zones, and three nodes need three Pod blocks in a range that holds one, so
# the create would fail rather than plant the defect.
variable "location" {
  type        = string
  description = "Zone of the planted cluster; its region holds the planted subnet"

  validation {
    condition     = can(regex("^[a-z]+-[a-z]+[0-9]+-[a-z]$", var.location))
    error_message = "location must be a zone such as us-west4-a: a regional cluster needs a Pod block per zone and the planted range holds one."
  }
}

# ---------------------------------------------------------------------------
# The planted range. Its name is also written into
# bench/tasks/networking-audit-pod-range-exhaustion/task.yaml — the prompt
# does not name it (the audit has to find it), but the objective asserts on
# it. Change one and change both.
# ---------------------------------------------------------------------------

# The planted noun the ledger objective requires: the helper files the
# finding on object SecondaryRange/<this>, so a filed finding's derived id
# ends in secondaryrange-<this>. Fixed rather than derived from the run, which
# is safe because a secondary range name is unique only within its subnet,
# and the subnet is this stack's own.
variable "pod_range_name" {
  type        = string
  description = "Name of the subnet's Pod secondary range, the one the cluster fills"
  default     = "bench-pods-exhausted"
}

variable "prow_build_id" {
  type        = string
  description = "Prow BUILD_ID of the run creating this infra"
  default     = ""
}

variable "prow_pull_number" {
  type        = string
  description = "Pull request number the run belongs to"
  default     = ""
}
