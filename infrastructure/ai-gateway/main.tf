# Project APIs are prerequisites owned outside this deployment. Do not manage
# google_project_service here: API lifecycle belongs to the shared Studio project.
locals {
  # These are non-secret source files. Credential payloads must never be added.
  runtime_files = {
    "bootstrap.sh"     = file("${path.module}/runtime/bootstrap.sh")
    "fetch-secrets.py" = file("${path.module}/runtime/fetch-secrets.py")
    "config.json"      = file("${path.module}/runtime/config.json")
    "Caddyfile"        = file("${path.module}/runtime/Caddyfile")
    "backup.sh"        = file("${path.module}/runtime/backup.sh")
    "iap-auth.py"      = file("${path.module}/runtime/iap-auth.py")
    "iap-auth.service" = file("${path.module}/runtime/iap-auth.service")
    "model-catalog.py" = file("${path.module}/runtime/model-catalog.py")
  }

  deployment_files = merge(local.runtime_files, {
    "admin.caddy" = local.admin_enabled ? templatefile("${path.module}/templates/admin.caddy.tftpl", {
      admin_domain = var.admin_domain
    }) : "# Browser administration disabled.\n"
    "iap-auth.json" = jsonencode({
      enabled        = local.admin_enabled
      audience       = local.admin_audience
      allowed_emails = [for member in sort(tolist(var.admin_members)) : trimprefix(member, "user:")]
    })
    "compose.yaml" = templatefile("${path.module}/templates/compose.yaml.tftpl", {
      bifrost_image = var.bifrost_image
      caddy_image   = var.caddy_image
      domain        = var.domain
    })
    "deployment.json" = jsonencode({
      project_id               = var.project_id
      bootstrap_secret_id      = google_secret_manager_secret.bootstrap.secret_id
      bootstrap_secret_version = "latest"
      data_device              = "/dev/disk/by-id/google-ai-gateway-data"
      data_mount               = "/srv/ai-gateway"
    })
  })

  # Metadata changes do not replace the VM or execute immediately. Explicitly
  # rerun the GCE startup-script after apply, or reboot; it runs on every boot.
  # Basenames are fixed above and contents are base64 encoded, not shell-expanded.
  startup_script = join("\n", concat(
    [
      "#!/bin/bash",
      "set -euo pipefail",
      "umask 077",
      "install -d -o root -g root -m 0750 /opt/ai-gateway",
    ],
    [for name, content in local.deployment_files :
      "printf '%s' '${base64encode(content)}' | base64 --decode > '/opt/ai-gateway/${name}'"
    ],
    [
      "chmod 0700 /opt/ai-gateway/bootstrap.sh /opt/ai-gateway/fetch-secrets.py /opt/ai-gateway/backup.sh",
      "bash /opt/ai-gateway/bootstrap.sh",
      "",
    ]
  ))
}

resource "google_compute_network" "gateway" {
  project                 = var.project_id
  name                    = "ai-gateway"
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "gateway" {
  project       = var.project_id
  name          = "ai-gateway-subnet"
  region        = var.region
  network       = google_compute_network.gateway.self_link
  ip_cidr_range = "10.42.0.0/24"
}

resource "google_compute_address" "gateway" {
  project      = var.project_id
  name         = "ai-gateway-ip"
  region       = var.region
  address_type = "EXTERNAL"
  network_tier = "PREMIUM"
}

resource "google_service_account" "gateway" {
  project      = var.project_id
  account_id   = "ai-gateway"
  display_name = "AI gateway VM"
}

resource "google_compute_firewall" "public_web" {
  project                 = var.project_id
  name                    = "ai-gateway-public-web"
  network                 = google_compute_network.gateway.self_link
  direction               = "INGRESS"
  source_ranges           = ["0.0.0.0/0"]
  target_service_accounts = [google_service_account.gateway.email]

  allow {
    protocol = "tcp"
    ports    = ["80", "443"]
  }
}

resource "google_compute_firewall" "iap_ssh" {
  project                 = var.project_id
  name                    = "ai-gateway-iap-ssh"
  network                 = google_compute_network.gateway.self_link
  direction               = "INGRESS"
  source_ranges           = ["35.235.240.0/20"]
  target_service_accounts = [google_service_account.gateway.email]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

# Create only the secret container. Versions and payloads are supplied out of
# band and fetched by the VM; Terraform must never read or store their contents.
resource "google_secret_manager_secret" "bootstrap" {
  project   = var.project_id
  secret_id = "ai-gateway-bootstrap-json"

  replication {
    auto {}
  }

  # The encryption key is needed to restore every retained database backup.
  lifecycle {
    prevent_destroy = true
  }
}

# The dedicated VM identity receives no project-wide roles.
resource "google_secret_manager_secret_iam_member" "bootstrap_reader" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.bootstrap.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.gateway.email}"
}

resource "google_compute_disk" "data" {
  project = var.project_id
  name    = "ai-gateway-data"
  zone    = var.zone
  type    = "pd-balanced"
  size    = var.data_disk_size_gb

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_compute_resource_policy" "data_snapshots" {
  project = var.project_id
  name    = "ai-gateway-data-daily"
  region  = var.region

  snapshot_schedule_policy {
    schedule {
      daily_schedule {
        days_in_cycle = 1
        start_time    = "04:00"
      }
    }

    retention_policy {
      max_retention_days    = 14
      on_source_disk_delete = "KEEP_AUTO_SNAPSHOTS"
    }

    # GCE defaults to crash-consistent snapshots (guest_flush=false).
    # Omit the otherwise-empty snapshot_properties block: the API drops that
    # explicit false-only block on read, causing perpetual plan drift.
    # Application-consistent SQLite backups are runtime-owned.
  }
}

resource "google_compute_disk_resource_policy_attachment" "data_snapshots" {
  project = var.project_id
  name    = google_compute_resource_policy.data_snapshots.name
  disk    = google_compute_disk.data.name
  zone    = var.zone
}

resource "google_compute_instance" "gateway" {
  project             = var.project_id
  name                = "ai-gateway"
  zone                = var.zone
  machine_type        = var.machine_type
  deletion_protection = true

  boot_disk {
    initialize_params {
      image = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
      size  = var.boot_disk_size_gb
      type  = "pd-balanced"
    }
  }

  # Separately managed attached disks are not auto-deleted with the VM.
  attached_disk {
    source      = google_compute_disk.data.self_link
    device_name = "ai-gateway-data"
    mode        = "READ_WRITE"
  }

  network_interface {
    subnetwork = google_compute_subnetwork.gateway.self_link

    access_config {
      nat_ip       = google_compute_address.gateway.address
      network_tier = "PREMIUM"
    }
  }

  metadata = {
    "enable-oslogin"         = "TRUE"
    "block-project-ssh-keys" = "TRUE"
    "serial-port-enable"     = "FALSE"
    "startup-script"         = local.startup_script
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  service_account {
    email  = google_service_account.gateway.email
    scopes = ["cloud-platform"]
  }

  depends_on = [
    google_secret_manager_secret_iam_member.bootstrap_reader,
    google_compute_disk_resource_policy_attachment.data_snapshots,
    google_compute_firewall.public_web,
    google_compute_firewall.iap_ssh,
  ]
}

resource "google_dns_record_set" "gateway" {
  count = var.dns_managed_zone == null ? 0 : 1

  project      = var.project_id
  managed_zone = var.dns_managed_zone
  name         = "${var.domain}."
  type         = "A"
  ttl          = 300
  rrdatas      = [google_compute_address.gateway.address]
}

resource "google_iap_tunnel_instance_iam_member" "operators" {
  for_each = var.operator_members

  project  = var.project_id
  zone     = google_compute_instance.gateway.zone
  instance = google_compute_instance.gateway.name
  role     = "roles/iap.tunnelResourceAccessor"
  member   = each.value
}

resource "google_compute_instance_iam_member" "operators" {
  for_each = var.operator_members

  project       = var.project_id
  zone          = google_compute_instance.gateway.zone
  instance_name = google_compute_instance.gateway.name
  role          = "roles/compute.osAdminLogin"
  member        = each.value
}

resource "google_service_account_iam_member" "operators" {
  for_each = var.operator_members

  service_account_id = google_service_account.gateway.name
  role               = "roles/iam.serviceAccountUser"
  member             = each.value
}