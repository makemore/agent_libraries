# Browser administration is opt-in and separate from the public API hostname.
# Project API lifecycle remains externally owned, including iap.googleapis.com.
locals {
  admin_enabled  = var.admin_domain != null
  admin_audience = local.admin_enabled ? "/projects/${data.google_project.admin[0].number}/global/backendServices/${google_compute_backend_service.admin[0].generated_id}" : null
}

data "google_project" "admin" {
  count = local.admin_enabled ? 1 : 0

  project_id = var.project_id
}

resource "google_compute_global_address" "admin" {
  count = local.admin_enabled ? 1 : 0

  project      = var.project_id
  name         = "ai-gateway-admin-ip"
  address_type = "EXTERNAL"
  ip_version   = "IPV4"
  # Global external addresses are PREMIUM; the provider exposes network_tier
  # on the global forwarding rule, not on this resource.
}

# The backend must exist before VM startup can use its generated JWT audience.
# Never reference the VM here: group -> backend -> VM -> membership is the
# creation order. Only the independent membership resource owns group members.
resource "google_compute_instance_group" "admin" {
  count = local.admin_enabled ? 1 : 0

  project   = var.project_id
  name      = "ai-gateway-admin"
  zone      = var.zone
  network   = google_compute_network.gateway.self_link
  instances = []

  named_port {
    name = "admin"
    port = 8081
  }

  lifecycle {
    ignore_changes = [instances]
  }
}

resource "google_compute_instance_group_membership" "admin" {
  count = local.admin_enabled ? 1 : 0

  project        = var.project_id
  zone           = var.zone
  instance_group = google_compute_instance_group.admin[0].name
  instance       = google_compute_instance.gateway.self_link
}

resource "google_compute_health_check" "admin" {
  count = local.admin_enabled ? 1 : 0

  project = var.project_id
  name    = "ai-gateway-admin"

  http_health_check {
    port         = 8081
    request_path = "/_iap_health"
    host         = var.admin_domain
  }
}

resource "google_compute_backend_service" "admin" {
  count = local.admin_enabled ? 1 : 0

  project               = var.project_id
  name                  = "ai-gateway-admin"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  protocol              = "HTTP"
  port_name             = "admin"
  timeout_sec           = 300
  enable_cdn            = false
  health_checks         = [google_compute_health_check.admin[0].self_link]

  backend {
    group = google_compute_instance_group.admin[0].self_link
  }

  # Google-managed OAuth: no custom client or secret is managed by Terraform.
  iap {
    enabled = true
  }

  # Admin request URLs and other request data must not enter LB access logs.
  log_config {
    enable = false
  }
}

resource "google_compute_firewall" "admin" {
  count = local.admin_enabled ? 1 : 0

  project                 = var.project_id
  name                    = "ai-gateway-admin-gfe"
  network                 = google_compute_network.gateway.self_link
  direction               = "INGRESS"
  source_ranges           = ["130.211.0.0/22", "35.191.0.0/16"]
  target_service_accounts = [google_service_account.gateway.email]

  allow {
    protocol = "tcp"
    ports    = ["8081"]
  }
}

resource "google_compute_managed_ssl_certificate" "admin" {
  count = local.admin_enabled ? 1 : 0

  project = var.project_id
  name    = "ai-gateway-admin"

  managed {
    domains = [var.admin_domain]
  }
}

resource "google_compute_ssl_policy" "admin" {
  count = local.admin_enabled ? 1 : 0

  project         = var.project_id
  name            = "ai-gateway-admin"
  min_tls_version = "TLS_1_2"
  profile         = "MODERN"
}

# Every host/path goes through IAP; the runtime rejects the wrong Host header.
resource "google_compute_url_map" "admin" {
  count = local.admin_enabled ? 1 : 0

  project         = var.project_id
  name            = "ai-gateway-admin"
  default_service = google_compute_backend_service.admin[0].self_link
}

resource "google_compute_target_https_proxy" "admin" {
  count = local.admin_enabled ? 1 : 0

  project          = var.project_id
  name             = "ai-gateway-admin"
  url_map          = google_compute_url_map.admin[0].self_link
  ssl_certificates = [google_compute_managed_ssl_certificate.admin[0].self_link]
  ssl_policy       = google_compute_ssl_policy.admin[0].self_link
}

# HTTPS only: no port 80 forwarding rule or unprotected redirect backend.
resource "google_compute_global_forwarding_rule" "admin" {
  count = local.admin_enabled ? 1 : 0

  project               = var.project_id
  name                  = "ai-gateway-admin-https"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  network_tier          = "PREMIUM"
  ip_address            = google_compute_global_address.admin[0].address
  ip_protocol           = "TCP"
  port_range            = "443"
  target                = google_compute_target_https_proxy.admin[0].self_link
}

resource "google_iap_web_backend_service_iam_member" "admin" {
  for_each = local.admin_enabled ? var.admin_members : toset([])

  project             = var.project_id
  web_backend_service = google_compute_backend_service.admin[0].name
  role                = "roles/iap.httpsResourceAccessor"
  member              = each.value
}

resource "google_dns_record_set" "admin" {
  count = local.admin_enabled && var.dns_managed_zone != null ? 1 : 0

  project      = var.project_id
  managed_zone = var.dns_managed_zone
  name         = "${var.admin_domain}."
  type         = "A"
  ttl          = 300
  rrdatas      = [google_compute_global_address.admin[0].address]
}