terraform {
  required_version = ">= 1.9.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.0"
    }
  }

  # Supply the existing bucket through backend.hcl; never share Studio's prefix.
  backend "gcs" {
    prefix = "ai-gateway/state"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}