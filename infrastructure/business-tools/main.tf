# Project APIs are prerequisites owned outside this stack. No API lifecycle,
# secret-version resources or secret payload data sources belong in Terraform.
locals {
  app_folders = ["agentic-social", "mautic", "actual-budget", "invoice-ninja", "observability"]

  # Explicit non-secret allowlists: a stray env/credential file must NEVER enter
  # Terraform state or VM metadata through recursive directory discovery.
  # Runtime files are staged at /opt/business-tools, not in a runtime subfolder.
  # Store bytes as base64 so neither file contents nor shell syntax are expanded.
  runtime_files = {
    for name in [
      "bootstrap.sh", "prepare-disk.py", "compose.sh", "backup.sh", "fetch-secrets.py",
      "prepare-mautic.py", "stop.py",
      "business-tools.service", "business-tools-backup.service", "business-tools-backup.timer"
    ] :
    name => filebase64("${path.module}/runtime/${name}")
  }
  app_assets = {
    for name in [
      "invoice-ninja/nginx.conf", "invoice-ninja/logrotate.conf",
      "observability/prometheus.yml", "observability/provisioning/datasources/prometheus.yaml"
    ] : name => filebase64("${path.module}/${name}")
  }
  app_compose_files = {
    for folder in local.app_folders : "${folder}/compose.yaml" => base64encode(templatefile(
      "${path.module}/${folder}/compose.yaml.tftpl", { images = var.images, domains = var.domains }
    ))
  }

  deployment_files = merge(local.runtime_files, local.app_assets, local.app_compose_files, {
    "compose.yaml" = base64encode(templatefile("${path.module}/templates/compose.yaml.tftpl", {
      images         = var.images
      domains        = var.domains
      public_enabled = var.public_enabled
    }))
    "Caddyfile" = base64encode(templatefile("${path.module}/templates/Caddyfile.tftpl", {
      domains        = var.domains
      public_enabled = var.public_enabled
    }))
    "deployment.json" = base64encode(jsonencode({
      project_id               = var.project_id
      bootstrap_secret_id      = google_secret_manager_secret.bootstrap.secret_id
      bootstrap_secret_version = "latest"
      data_device              = "/dev/disk/by-id/google-business-tools-data"
      data_mount               = "/srv/business-tools"
    }))
  })

  # Install every ancestor explicitly: install -d otherwise gives implicit parent
  # directories default permissions rather than the requested root-owned 0750.
  deployment_directories = sort(distinct(concat(["/opt/business-tools"], flatten([
    for name in keys(local.deployment_files) : [
      for depth in range(1, length(split("/", name))) :
      "/opt/business-tools/${join("/", slice(split("/", name), 0, depth))}"
    ]
  ]))))

  # metadata["startup-script"] updates neither replace the VM nor execute now.
  # After an approved update explicitly rerun GCE startup (or deliberately reboot).
  # Never switch to metadata_startup_script, which forces instance replacement.
  startup_script = join("\n", concat(
    [
      "#!/bin/bash", "set -euo pipefail", "umask 077",
      "exec 7>/run/business-tools-stage.lock", "flock --exclusive 7",
      "exec 9>/run/business-tools-maintenance.lock", "flock --exclusive 9",
      "if [[ $(systemctl show --property=LoadState --value business-tools.service) == loaded ]]; then",
      "  systemctl stop business-tools.service",
      "  [[ $(systemctl show --property=ActiveState --value business-tools.service) == inactive ]] || exit 1",
      "  [[ $(systemctl show --property=Result --value business-tools.service) == success ]] || exit 1",
      "fi",
    ],
    [for directory in local.deployment_directories :
      "install -d -o root -g root -m 0750 '${directory}'"
    ],
    flatten([for name, encoded in local.deployment_files : [
      "printf '%s' '${encoded}' | base64 --decode > '/opt/business-tools/${name}'",
      "chown root:root '/opt/business-tools/${name}'",
      # Non-secret config files are bind-mounted by non-root app containers.
      # Host traversal remains root-only via the 0750 ancestors installed above.
      "chmod ${contains(keys(local.app_assets), name) ? "0644" : "0600"} '/opt/business-tools/${name}'",
    ]]),
    [
      "chmod 0700 /opt/business-tools/bootstrap.sh /opt/business-tools/fetch-secrets.py",
      "flock --unlock 9", "exec 9>&-",
      "bash /opt/business-tools/bootstrap.sh",
      "",
    ]
  ))
}

resource "google_compute_network" "business_tools" {
  project                 = var.project_id
  name                    = "business-tools"
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "business_tools" {
  project       = var.project_id
  name          = "business-tools-subnet"
  region        = var.region
  network       = google_compute_network.business_tools.self_link
  ip_cidr_range = "10.43.0.0/24"
}

resource "google_compute_address" "business_tools" {
  project      = var.project_id
  name         = "business-tools-ip"
  region       = var.region
  address_type = "EXTERNAL"
  network_tier = "PREMIUM"
}

resource "google_service_account" "business_tools" {
  project      = var.project_id
  account_id   = "business-tools"
  display_name = "Business tools VM"
}

resource "google_compute_firewall" "public_web" {
  project                 = var.project_id
  name                    = "business-tools-public-web"
  network                 = google_compute_network.business_tools.self_link
  direction               = "INGRESS"
  source_ranges           = ["0.0.0.0/0"]
  target_service_accounts = [google_service_account.business_tools.email]

  # The runtime proxy gates application routes with public_enabled, initially
  # false. Opening transport ports is not permission to expose setup endpoints.
  allow {
    protocol = "tcp"
    ports    = ["80", "443"]
  }
}

resource "google_compute_firewall" "iap_ssh" {
  project                 = var.project_id
  name                    = "business-tools-iap-ssh"
  network                 = google_compute_network.business_tools.self_link
  direction               = "INGRESS"
  source_ranges           = ["35.235.240.0/20"]
  target_service_accounts = [google_service_account.business_tools.email]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

# Container only: the operator supplies a JSON version out of band. Runtime
# fetch-secrets.py retrieves it with the VM identity and writes per-service env.
resource "google_secret_manager_secret" "bootstrap" {
  project   = var.project_id
  secret_id = "business-tools-bootstrap-json"

  replication {
    auto {}
  }

  lifecycle {
    prevent_destroy = true
  }
}

# No project-wide roles for the dedicated VM identity.
resource "google_secret_manager_secret_iam_member" "bootstrap_reader" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.bootstrap.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.business_tools.email}"
}

resource "google_compute_disk" "data" {
  project = var.project_id
  name    = "business-tools-data"
  zone    = var.zone
  type    = "pd-balanced"
  size    = var.data_disk_size_gb

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_compute_resource_policy" "data_snapshots" {
  project = var.project_id
  name    = "business-tools-data-daily"
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

    # Crash-consistent GCE snapshots; application backups are runtime-owned.
    # Omit default-only snapshot_properties: the API drops it, causing drift.
  }
}

resource "google_compute_disk_resource_policy_attachment" "data_snapshots" {
  project = var.project_id
  name    = google_compute_resource_policy.data_snapshots.name
  disk    = google_compute_disk.data.name
  zone    = var.zone
}

resource "google_compute_instance" "business_tools" {
  project             = var.project_id
  name                = "business-tools"
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
    device_name = "business-tools-data"
    mode        = "READ_WRITE"
  }

  network_interface {
    subnetwork = google_compute_subnetwork.business_tools.self_link

    access_config {
      nat_ip       = google_compute_address.business_tools.address
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
    email  = google_service_account.business_tools.email
    scopes = ["cloud-platform"]
  }

  lifecycle {
    precondition {
      condition     = alltrue([for name in ["bootstrap.sh", "fetch-secrets.py"] : contains(keys(local.runtime_files), name)])
      error_message = "runtime/bootstrap.sh and runtime/fetch-secrets.py must exist before deployment."
    }

    precondition {
      condition = alltrue([for name in keys(local.deployment_files) :
        can(regex("^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$", name)) &&
        alltrue([for part in split("/", name) : part != "." && part != ".."])
      ])
      error_message = "Deployment asset paths must be safe relative ASCII paths without traversal or shell metacharacters."
    }

    precondition {
      # All script text is ASCII (safe paths and base64), so characters = bytes.
      condition     = length(local.startup_script) <= 256 * 1024
      error_message = "The staged startup script exceeds GCE's 256 KiB per-metadata-value limit; reduce non-secret assets before deployment."
    }
  }

  depends_on = [
    google_secret_manager_secret_iam_member.bootstrap_reader,
    google_compute_disk_resource_policy_attachment.data_snapshots,
    google_compute_firewall.public_web,
    google_compute_firewall.iap_ssh,
  ]
}

resource "google_dns_record_set" "business_tools" {
  for_each = var.dns_managed_zone == null ? {} : var.domains

  project      = var.project_id
  managed_zone = var.dns_managed_zone
  name         = "${each.value}."
  type         = "A"
  ttl          = 300
  rrdatas      = [google_compute_address.business_tools.address]
}

resource "google_iap_tunnel_instance_iam_member" "operators" {
  for_each = var.operator_members

  project  = var.project_id
  zone     = google_compute_instance.business_tools.zone
  instance = google_compute_instance.business_tools.name
  role     = "roles/iap.tunnelResourceAccessor"
  member   = each.value

  condition {
    title      = "business-tools-ssh-only"
    expression = "destination.port == 22"
  }
}

resource "google_compute_instance_iam_member" "operators" {
  for_each = var.operator_members

  project       = var.project_id
  zone          = google_compute_instance.business_tools.zone
  instance_name = google_compute_instance.business_tools.name
  role          = "roles/compute.osAdminLogin"
  member        = each.value
}

resource "google_service_account_iam_member" "operators" {
  for_each = var.operator_members

  service_account_id = google_service_account.business_tools.name
  role               = "roles/iam.serviceAccountUser"
  member             = each.value
}