terraform {
  # Terraform >= 1.9 or OpenTofu >= 1.11 (including native mock-provider tests).
  required_version = ">= 1.9.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.0"
    }
  }

  # Existing bucket supplied externally; never reuse another stack's prefix.
  backend "gcs" {
    prefix = "business-tools/state"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}