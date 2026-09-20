output "static_ip" {
  description = "Reserved IPv4 address; point all application A records here when DNS is external."
  value       = google_compute_address.business_tools.address
}

output "image_references" {
  description = "Configured immutable image references for release audits; contains no registry credentials."
  value       = var.images
}

output "public_urls" {
  description = "Intended HTTPS URLs after explicit public enablement; empty while the initialization gate is closed. This is configuration, not a readiness check."
  value       = var.public_enabled ? { for service, domain in var.domains : service => "https://${domain}" } : {}
}

output "instance_name" {
  description = "Business-tools Compute Engine instance name."
  value       = google_compute_instance.business_tools.name
}

output "zone" {
  description = "Business-tools Compute Engine zone."
  value       = google_compute_instance.business_tools.zone
}

output "project_id" {
  description = "Existing GCP project hosting this independent stack."
  value       = var.project_id
}

output "bootstrap_secret_id" {
  description = "Secret container ID to populate out of band; Terraform never reads or creates a payload version."
  value       = google_secret_manager_secret.bootstrap.secret_id
}

output "iap_ssh_command" {
  description = "Operator SSH command via IAP; add loopback-only -L forwards for the runtime's documented initialization ports."
  value       = "gcloud --project ${var.project_id} compute ssh ${google_compute_instance.business_tools.name} --zone ${google_compute_instance.business_tools.zone} --tunnel-through-iap"
}