terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
  # backend "gcs" { bucket = "your-tf-state"; prefix = "arc-voice-agents" }
}

provider "google" {
  project = var.project
  region  = var.region
}

locals {
  name = "arc-voice-agents-${var.env}"
}

# One Artifact Registry repo shared by all environments (created from the dev workspace)
resource "google_artifact_registry_repository" "arc" {
  count         = var.env == "dev" ? 1 : 0
  repository_id = "arc"
  location      = var.region
  format        = "DOCKER"
}

resource "google_service_account" "runtime" {
  account_id   = "${local.name}-run"
  display_name = "Runtime identity for ${local.name}"
}

# Secrets live in Secret Manager, never in tfvars or the image
resource "google_secret_manager_secret" "secret" {
  for_each  = toset(["signal-webhook-secret", "tools-api-token", "database-url", "llm-api-key"])
  secret_id = "${local.name}-${each.key}"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_iam_member" "runtime_access" {
  for_each  = google_secret_manager_secret.secret
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_cloud_run_v2_service" "api" {
  name     = local.name
  location = var.region
  # the alarm receiver and the voice platform reach us through a load balancer / IAP in front
  ingress = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.runtime.email
    scaling {
      min_instance_count = var.min_instances # >0 in prod: no cold start on a live alarm
      max_instance_count = var.max_instances
    }
    containers {
      image = var.image
      resources {
        limits = { cpu = "1", memory = "512Mi" }
      }
      env {
        name  = "SEED_DEMO"
        value = var.env == "dev" ? "true" : "false"
      }
      dynamic "env" {
        for_each = {
          SIGNAL_WEBHOOK_SECRET = "signal-webhook-secret"
          TOOLS_API_TOKEN       = "tools-api-token"
          DATABASE_URL          = "database-url"
          ARC_LLM_API_KEY       = "llm-api-key"
        }
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.secret[env.value].secret_id
              version = "latest"
            }
          }
        }
      }
      startup_probe {
        http_get { path = "/readyz" }
      }
      liveness_probe {
        http_get { path = "/healthz" }
      }
    }
  }

  lifecycle {
    ignore_changes = [template[0].containers[0].image] # CI/CD owns the image tag
  }
}

# Page someone when 5xx responses spike
resource "google_monitoring_alert_policy" "errors" {
  display_name = "${local.name} 5xx rate"
  combiner     = "OR"
  conditions {
    display_name = "5xx > 1/min for 5 min"
    condition_threshold {
      filter          = "resource.type = \"cloud_run_revision\" AND resource.labels.service_name = \"${local.name}\" AND metric.type = \"run.googleapis.com/request_count\" AND metric.labels.response_code_class = \"5xx\""
      comparison      = "COMPARISON_GT"
      threshold_value = 1
      duration        = "300s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_RATE"
      }
    }
  }
  notification_channels = var.alert_channels
}

output "url" {
  value = google_cloud_run_v2_service.api.uri
}
