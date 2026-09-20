output "static_ip" {
  description = "Reserved public IPv4 address; point the gateway's A record here if DNS is managed externally."
  value       = google_compute_address.gateway.address
}

output "domain" {
  description = "Public gateway hostname."
  value       = var.domain
}

output "instance_name" {
  description = "Gateway Compute Engine instance name."
  value       = google_compute_instance.gateway.name
}

output "zone" {
  description = "Gateway Compute Engine zone."
  value       = google_compute_instance.gateway.zone
}

output "project_id" {
  description = "Existing GCP project hosting the gateway."
  value       = var.project_id
}

output "bootstrap_secret_id" {
  description = "Secret ID to populate out of band with the runtime bootstrap JSON; no payload is managed by Terraform."
  value       = google_secret_manager_secret.bootstrap.secret_id
}

output "iap_ssh_tunnel_command" {
  description = "Run locally as an authorized operator to reach the private Bifrost UI through IAP and SSH."
  value       = "gcloud --project ${var.project_id} compute ssh ${google_compute_instance.gateway.name} --zone ${google_compute_instance.gateway.zone} --tunnel-through-iap -- -N -L 127.0.0.1:8080:127.0.0.1:8080"
}

output "private_url" {
  description = "Private Bifrost UI URL on the operator's machine while the IAP SSH tunnel is running."
  value       = "http://127.0.0.1:8080"
}

output "admin_url" {
  description = "IAP-protected browser admin URL, or null when browser administration is disabled."
  value       = local.admin_enabled ? "https://${var.admin_domain}" : null
}

output "admin_static_ip" {
  description = "Reserved admin IPv4 address for external DNS, or null when browser administration is disabled."
  value       = local.admin_enabled ? google_compute_global_address.admin[0].address : null
}

output "admin_iap_audience" {
  description = "Expected IAP JWT audience using numeric project/backend IDs, or null when browser administration is disabled."
  value       = local.admin_audience
}

output "admin_members" {
  description = "Explicit IAM user principals configured for browser admin access; contains no credentials."
  value       = toset([for grant in google_iap_web_backend_service_iam_member.admin : grant.member])
}