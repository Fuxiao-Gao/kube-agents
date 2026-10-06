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

# The scenario driver for bench/tasks/networking-audit-pod-range-exhaustion.
#
# It plants ONE exhausted Pod range: a dedicated VPC whose one subnet carries
# a /24 Pod secondary range, and a one-node zonal Standard cluster on it at
# max_pods_per_node = 110. GKE gives each node a /24 block at 110 Pods, so the
# single node takes the whole range and GKE's own
# ipAllocationPolicy.defaultPodIpv4RangeUtilization reads 1.0 — the cluster
# cannot add a node. That is the state the networking SOP's check 2.1
# (subnet-ip-exhaustion, below 15% available) exists to flag, and it is
# planted for real because the number the check reads is GKE's, computed
# from the live allocation; nothing short of a cluster produces it.
#
# The primary range is a /28 with one node NIC in it (1 + 4 reserved of 16),
# so it is NOT exhausted: the only range this stack fills is the Pod range.
#
# A cluster of its own rather than the seeded fleet or the host cluster: both
# sit on the default network, where re-ranging a subnet to exhaustion would
# break every other case in the project. The dedicated VPC keeps the plant
# away from everything else, and teardown removes all of it.
#
# Names are derived from the run's cluster_name (see variables.tf) so two runs
# never collide on a leftover; the planted range name is fixed, and graded.

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0.0"
    }
  }
}

locals {
  region = regex("^(.*)-[a-z]$", var.location)[0]

  # 23 characters: inside GKE's 40-character cluster name limit whatever the
  # runner's cluster_name is, and distinct from it.
  name = "bench-podrange-${substr(md5(var.cluster_name), 0, 8)}"

  # The account id scheme modules/cluster/gke uses, over this stack's cluster
  # name, so that module's orphan sweep (it reaps managed-by=kube-agents-bench
  # clusters older than its cutoff, then the gke-nodes-<slug>-<hash> account
  # derived from each one's name) also reaps what a killed run of this stack
  # left behind. The VPC and subnet are not labelable and cost nothing idle.
  node_account_id = "gke-nodes-${trim(substr(local.name, 0, 9), "-")}-${substr(md5(local.name), 0, 6)}"

  # GCP label values accept lowercase letters, digits, '-' and '_' only, so
  # the Prow ids go in verbatim.
  ci_labels = {
    "managed-by"  = "kube-agents-bench"
    "build-id"    = var.prow_build_id != "" ? var.prow_build_id : "local"
    "pull-number" = var.prow_pull_number != "" ? var.prow_pull_number : "none"
  }
}

provider "google" {
  project = var.project_id
  region  = local.region
}

resource "google_compute_network" "plant" {
  name                    = local.name
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "plant" {
  name          = local.name
  region        = local.region
  network       = google_compute_network.plant.id
  ip_cidr_range = "10.10.0.0/28" # sanitizer: allow a private range inside this stack's own VPC

  # The defect. One /24 block at max_pods_per_node = 110 is this whole range.
  secondary_ip_range {
    range_name    = var.pod_range_name
    ip_cidr_range = "10.20.0.0/24" # sanitizer: allow a private range inside this stack's own VPC
  }

  # GKE reports no utilization for a Services range, so the audit measures
  # nothing here; sized well clear of any minimum.
  secondary_ip_range {
    range_name    = "${local.name}-svc"
    ip_cidr_range = "10.30.0.0/24" # sanitizer: allow a private range inside this stack's own VPC
  }
}

# A dedicated minimal node account, as modules/cluster/gke and the fleet do,
# rather than the Compute Engine default one.
resource "google_service_account" "nodes" {
  account_id   = local.node_account_id
  display_name = "GKE Node Service Account for ${local.name}"
}

resource "google_project_iam_member" "nodes_default_node_role" {
  project = var.project_id
  role    = "roles/container.defaultNodeServiceAccount"
  member  = "serviceAccount:${google_service_account.nodes.email}"
}

resource "google_container_cluster" "plant" {
  name     = local.name
  location = var.location

  network         = google_compute_network.plant.id
  subnetwork      = google_compute_subnetwork.plant.id
  networking_mode = "VPC_NATIVE"

  ip_allocation_policy {
    cluster_secondary_range_name  = var.pod_range_name
    services_secondary_range_name = "${local.name}-svc"
  }

  # 110 Pods is a /24 block per node: the one node fills the /24 range.
  default_max_pods_per_node = 110

  # The default pool is the planted pool: one node, created with the cluster,
  # so the apply pays for one pool rather than a throwaway one and its
  # replacement. Its Pod range is the cluster's default one above.
  initial_node_count  = 1
  deletion_protection = false
  resource_labels     = local.ci_labels

  node_config {
    machine_type    = "e2-small"
    disk_size_gb    = 20
    resource_labels = local.ci_labels
    service_account = google_service_account.nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]

    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    metadata = {
      disable-legacy-endpoints = "true"
    }
  }

  # Workload Identity on with GKE_METADATA above, as on the fleet: a cluster
  # standing in the leased project during a run is one the other audits can
  # sweep, and the metadata escalation is not what this case plants.
  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  depends_on = [google_project_iam_member.nodes_default_node_role]
}

# devops-bench reads these outputs unconditionally after up() and fetches
# credentials for cluster_name; nothing the task checks reads the cluster.
output "cluster_name" {
  value = google_container_cluster.plant.name
}

output "cluster_location" {
  value = google_container_cluster.plant.location
}
