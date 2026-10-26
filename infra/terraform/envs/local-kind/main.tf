# Local Kubernetes cluster for Sentinel: kind (Kubernetes-in-Docker), defined as code.
# 1 control plane + 2 workers, so scheduling, spreading and node-failure scenarios behave like a
# real cluster while fitting in ~10 GB of WSL memory.

terraform {
  required_version = ">= 1.6"
  required_providers {
    kind = {
      source  = "tehcyx/kind"
      version = ">= 0.7.0"
    }
  }
}

provider "kind" {}

variable "cluster_name" {
  type    = string
  default = "sentinel"
}

resource "kind_cluster" "this" {
  name           = var.cluster_name
  wait_for_ready = true

  kind_config {
    kind        = "Cluster"
    api_version = "kind.x-k8s.io/v1alpha4"

    node {
      role = "control-plane"
      # Label used later by ingress-nginx; ports 8080/8443 on the laptop map to 80/443 in the cluster.
      kubeadm_config_patches = [
        "kind: InitConfiguration\nnodeRegistration:\n  kubeletExtraArgs:\n    node-labels: \"ingress-ready=true\"\n"
      ]
      extra_port_mappings {
        container_port = 80
        host_port      = 8080
      }
      extra_port_mappings {
        container_port = 443
        host_port      = 8443
      }
    }

    node {
      role = "worker"
    }

    node {
      role = "worker"
    }
  }
}

output "cluster_name" {
  value = kind_cluster.this.name
}

output "endpoint" {
  value = kind_cluster.this.endpoint
}
