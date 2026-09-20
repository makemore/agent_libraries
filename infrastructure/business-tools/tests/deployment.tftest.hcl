# Offline, mocked plans only. Never apply: retained resources also block cleanup.
# Only required inputs are supplied globally; optional settings inherit defaults.
mock_provider "google" {
  mock_resource "google_compute_network" {
    defaults = {
      self_link = "https://www.googleapis.com/compute/v1/projects/business-tools-test/global/networks/business-tools"
    }
  }
  mock_resource "google_compute_subnetwork" {
    defaults = {
      self_link = "https://www.googleapis.com/compute/v1/projects/business-tools-test/regions/europe-west2/subnetworks/business-tools-subnet"
    }
  }
  mock_resource "google_compute_disk" {
    defaults = {
      self_link = "https://www.googleapis.com/compute/v1/projects/business-tools-test/zones/europe-west2-b/disks/business-tools-data"
    }
  }
  mock_resource "google_compute_address" {
    defaults = {
      address = "203.0.113.20"
    }
  }
  mock_resource "google_service_account" {
    defaults = {
      email = "business-tools@business-tools-test.iam.gserviceaccount.com"
      name  = "projects/business-tools-test/serviceAccounts/business-tools@business-tools-test.iam.gserviceaccount.com"
    }
  }
}

variables {
  project_id = "business-tools-test"
  # Synthetic digests on a reserved invalid registry, exclusively for mock tests.
  # These are NOT verified/pullable deployment images and must not enter examples.
  images = { for name in [
    "postiz", "postgres", "redis", "temporal", "mautic", "mariadb",
    "actual", "invoice_ninja", "grafana", "prometheus", "node_exporter", "caddy",
    # Separate pins required by the current Postiz and Invoice Ninja templates.
    "invoice_nginx", "temporal_postgres"
  ] : name => "example.invalid/business-tools/${name}@sha256:${sha256("mock-only-${name}")}" }
}

run "default_deployment" {
  command = plan

  assert {
    condition = (
      google_compute_instance.business_tools.project == "business-tools-test" &&
      google_compute_instance.business_tools.name == "business-tools" &&
      google_compute_instance.business_tools.machine_type == "e2-standard-4" &&
      google_compute_instance.business_tools.zone == "europe-west2-b" &&
      google_compute_subnetwork.business_tools.region == "europe-west2" &&
      google_compute_instance.business_tools.boot_disk[0].initialize_params[0].size == 50 &&
      google_compute_disk.data.size == 150 && google_compute_disk.data.type == "pd-balanced" &&
      google_compute_instance.business_tools.attached_disk[0].device_name == "business-tools-data" &&
      google_compute_instance.business_tools.attached_disk[0].mode == "READ_WRITE"
    )
    error_message = "Defaults must deploy one London e2-standard-4 VM with a 50 GB boot disk and separate 150 GB balanced data disk."
  }

  assert {
    condition = (
      !var.public_enabled && length(output.public_urls) == 0 &&
      alltrue([for service, domain in var.domains : domain == "${service}.makemoredigital.com"]) &&
      length(google_dns_record_set.business_tools) == 0 &&
      length(google_iap_tunnel_instance_iam_member.operators) == 0 &&
      length(google_compute_instance_iam_member.operators) == 0 &&
      length(google_service_account_iam_member.operators) == 0
    )
    error_message = "Default hostnames must remain stable; public application access, DNS management and operator IAM must be opt-in."
  }

  assert {
    condition = (
      google_compute_instance.business_tools.deletion_protection &&
      google_compute_instance.business_tools.metadata["enable-oslogin"] == "TRUE" &&
      google_compute_instance.business_tools.metadata["block-project-ssh-keys"] == "TRUE" &&
      google_compute_instance.business_tools.metadata["serial-port-enable"] == "FALSE" &&
      google_compute_instance.business_tools.shielded_instance_config[0].enable_secure_boot &&
      google_compute_instance.business_tools.shielded_instance_config[0].enable_vtpm &&
      google_compute_instance.business_tools.shielded_instance_config[0].enable_integrity_monitoring &&
      google_compute_resource_policy.data_snapshots.snapshot_schedule_policy[0].schedule[0].daily_schedule[0].days_in_cycle == 1 &&
      google_compute_resource_policy.data_snapshots.snapshot_schedule_policy[0].retention_policy[0].max_retention_days == 14 &&
      google_compute_resource_policy.data_snapshots.snapshot_schedule_policy[0].retention_policy[0].on_source_disk_delete == "KEEP_AUTO_SNAPSHOTS" &&
      google_compute_disk_resource_policy_attachment.data_snapshots.disk == google_compute_disk.data.name &&
      google_compute_disk_resource_policy_attachment.data_snapshots.zone == google_compute_disk.data.zone
    )
    error_message = "VM security, deletion protection and the data disk's daily retained 14-day snapshot policy must remain enabled."
  }

  assert {
    condition = (
      google_compute_firewall.public_web.direction == "INGRESS" &&
      toset(google_compute_firewall.public_web.source_ranges) == toset(["0.0.0.0/0"]) &&
      length(google_compute_firewall.public_web.allow) == 1 &&
      alltrue([for rule in google_compute_firewall.public_web.allow :
        rule.protocol == "tcp" && toset(rule.ports) == toset(["80", "443"])
      ]) &&
      google_compute_firewall.iap_ssh.direction == "INGRESS" &&
      toset(google_compute_firewall.iap_ssh.source_ranges) == toset(["35.235.240.0/20"]) &&
      length(google_compute_firewall.iap_ssh.allow) == 1 &&
      alltrue([for rule in google_compute_firewall.iap_ssh.allow :
        rule.protocol == "tcp" && toset(rule.ports) == toset(["22"])
      ]) &&
      length(google_compute_firewall.public_web.target_service_accounts) == 1 &&
      length(google_compute_firewall.iap_ssh.target_service_accounts) == 1
    )
    error_message = "Only TCP 80/443 may be public; SSH must be IAP-only, with both rules scoped to the VM identity."
  }

  assert {
    condition = (
      toset(keys(local.runtime_files)) == toset([
        "bootstrap.sh", "prepare-disk.py", "compose.sh", "backup.sh", "fetch-secrets.py",
        "prepare-mautic.py",
        "business-tools.service", "business-tools-backup.service", "business-tools-backup.timer"
      ]) &&
      toset(keys(local.app_assets)) == toset([
        "invoice-ninja/nginx.conf", "invoice-ninja/logrotate.conf", "observability/prometheus.yml",
        "observability/provisioning/datasources/prometheus.yaml"
      ]) &&
      local.deployment_files["bootstrap.sh"] == filebase64("${path.module}/runtime/bootstrap.sh") &&
      local.deployment_files["fetch-secrets.py"] == filebase64("${path.module}/runtime/fetch-secrets.py") &&
      alltrue([for name, encoded in local.deployment_files :
        strcontains(local.startup_script, "printf '%s' '${encoded}' | base64 --decode > '/opt/business-tools/${name}'")
      ]) &&
      alltrue([for directory in local.deployment_directories :
        strcontains(local.startup_script, "install -d -o root -g root -m 0750 '${directory}'")
      ]) &&
      alltrue([for name in [
        "invoice-ninja/nginx.conf", "observability/prometheus.yml",
        "observability/provisioning/datasources/prometheus.yaml"
        ] : local.deployment_files[name] == filebase64("${path.module}/${name}") &&
        strcontains(local.startup_script, "chmod 0644 '/opt/business-tools/${name}'")
      ]) &&
      alltrue([for folder in local.app_folders : local.deployment_files["${folder}/compose.yaml"] == base64encode(
        templatefile("${path.module}/${folder}/compose.yaml.tftpl", { images = var.images, domains = var.domains })
      )]) &&
      local.deployment_files["compose.yaml"] == base64encode(templatefile("${path.module}/templates/compose.yaml.tftpl", {
        images = var.images, domains = var.domains, public_enabled = false
      })) &&
      local.deployment_files["Caddyfile"] == base64encode(templatefile("${path.module}/templates/Caddyfile.tftpl", {
        domains = var.domains, public_enabled = false
      })) &&
      startswith(local.startup_script, "#!/bin/bash\nset -euo pipefail\numask 077\n") &&
      endswith(local.startup_script, "bash /opt/business-tools/bootstrap.sh\n") &&
      length(local.startup_script) <= 256 * 1024
    )
    error_message = "All runtime/config bytes and closed-gate templates must be staged privately before bootstrap within GCE's metadata limit."
  }

  assert {
    condition = (
      jsondecode(base64decode(local.deployment_files["deployment.json"])) == {
        project_id               = "business-tools-test"
        bootstrap_secret_id      = "business-tools-bootstrap-json"
        bootstrap_secret_version = "latest"
        data_device              = "/dev/disk/by-id/google-business-tools-data"
        data_mount               = "/srv/business-tools"
      } &&
      google_secret_manager_secret.bootstrap.secret_id == "business-tools-bootstrap-json" &&
      google_secret_manager_secret_iam_member.bootstrap_reader.secret_id == google_secret_manager_secret.bootstrap.secret_id &&
      google_secret_manager_secret_iam_member.bootstrap_reader.role == "roles/secretmanager.secretAccessor" &&
      output.bootstrap_secret_id == "business-tools-bootstrap-json"
    )
    error_message = "The runtime must receive only deployment identifiers and fetch latest secret content itself with a secret-scoped grant."
  }

  # Absent resource types and lifecycle meta-arguments cannot be inspected in
  # plan assertions; use narrow source guards for those safety invariants only.
  assert {
    condition = (
      alltrue([for filename in fileset(path.module, "*.tf") :
        length(regexall("(?m)^\\s*(resource|data)\\s+\"(google_secret_manager_secret_version[^\"]*|google_project_service[s]?|google_project_iam_[^\"]*)\"", file("${path.module}/${filename}"))) == 0
      ]) &&
      alltrue([for target in ["google_compute_disk\" \"data", "google_secret_manager_secret\" \"bootstrap"] :
        can(regex("(?s)resource \"${target}\" \\{.*?lifecycle \\{\\s*prevent_destroy\\s*=\\s*true", file("${path.module}/main.tf")))
      ]) &&
      strcontains(file("${path.module}/versions.tf"), "prefix = \"business-tools/state\"")
    )
    error_message = "State must be isolated, data/secret destruction prevented, and secret payloads, project IAM and API lifecycle kept out of this stack."
  }
}

run "explicit_dns_and_scoped_operators" {
  command = plan
  # Opt-in scenario: manage the five records and grant one explicit operator.
  variables {
    dns_managed_zone = "business-tools-test-zone"
    operator_members = ["user:operator@example.com"]
  }
  assert {
    condition = (
      length(google_dns_record_set.business_tools) == 5 &&
      alltrue([for service, record in google_dns_record_set.business_tools :
        record.name == "${var.domains[service]}." && record.type == "A" &&
        record.managed_zone == "business-tools-test-zone" && record.ttl == 300 && length(record.rrdatas) == 1
      ]) &&
      length(google_iap_tunnel_instance_iam_member.operators) == 1 &&
      alltrue([for member, grant in google_iap_tunnel_instance_iam_member.operators :
        grant.member == member && grant.instance == "business-tools" && grant.zone == var.zone &&
        grant.role == "roles/iap.tunnelResourceAccessor" && grant.condition[0].expression == "destination.port == 22"
      ]) &&
      length(google_compute_instance_iam_member.operators) == 1 &&
      alltrue([for member, grant in google_compute_instance_iam_member.operators :
        grant.member == member && grant.instance_name == "business-tools" && grant.role == "roles/compute.osAdminLogin"
      ]) &&
      length(google_service_account_iam_member.operators) == 1 &&
      alltrue([for member, grant in google_service_account_iam_member.operators :
        grant.member == member && grant.role == "roles/iam.serviceAccountUser"
      ])
    )
    error_message = "Opt-in DNS must use each configured domain; operator roles must remain instance/identity-scoped and IAP restricted to SSH."
  }
}

run "explicit_public_enablement_after_initialization" {
  command = plan
  # Deliberate post-initialization override; never enable this in shared inputs.
  variables {
    public_enabled = true
  }
  assert {
    condition = (
      length(output.public_urls) == 5 &&
      alltrue([for service, url in output.public_urls : url == "https://${var.domains[service]}"]) &&
      local.deployment_files["Caddyfile"] == base64encode(templatefile("${path.module}/templates/Caddyfile.tftpl", {
        domains = var.domains, public_enabled = true
      })) &&
      local.deployment_files["Caddyfile"] != base64encode(templatefile("${path.module}/templates/Caddyfile.tftpl", {
        domains = var.domains, public_enabled = false
      }))
    )
    error_message = "Public enablement must be explicit and must actually change the root proxy configuration."
  }
}

run "allow_extra_pinned_service_images" {
  command = plan
  # Extra images are an explicit template extension, not new shared defaults.
  # Keep input expressions self-contained: OpenTofu does not expose preceding
  # plan-only run outputs while evaluating these run-level variable overrides.
  variables {
    images = merge({ for name in [
      "postiz", "postgres", "redis", "temporal", "temporal_postgres", "mautic", "mariadb",
      "actual", "invoice_ninja", "invoice_nginx", "grafana", "prometheus", "node_exporter", "caddy"
      ] : name => "example.invalid/business-tools/${name}@sha256:${sha256("mock-only-${name}")}" }, {
      future_sidecar = "example.invalid/sidecar@sha256:${sha256("mock-only-sidecar")}"
    })
  }
}

# Failure-mode overrides are local; production and shared test defaults stay put.
run "reject_invalid_domain" {
  command = plan
  variables {
    domains = {
      social    = "https://social.example.com/path"
      marketing = "marketing.example.com"
      budget    = "budget.example.com"
      invoices  = "invoices.example.com"
      metrics   = "metrics.example.com"
    }
  }
  expect_failures = [var.domains]
}

run "reject_uppercase_domain" {
  command = plan
  variables {
    domains = {
      social    = "Social.example.com"
      marketing = "marketing.example.com"
      budget    = "budget.example.com"
      invoices  = "invoices.example.com"
      metrics   = "metrics.example.com"
    }
  }
  expect_failures = [var.domains]
}

run "reject_duplicate_domains" {
  command = plan
  variables {
    domains = {
      social    = "shared.example.com"
      marketing = "shared.example.com"
      budget    = "budget.example.com"
      invoices  = "invoices.example.com"
      metrics   = "metrics.example.com"
    }
  }
  expect_failures = [var.domains]
}

run "reject_mutable_image" {
  command = plan
  variables {
    images = merge({ for name in [
      "postiz", "postgres", "redis", "temporal", "temporal_postgres", "mautic", "mariadb",
      "actual", "invoice_ninja", "invoice_nginx", "grafana", "prometheus", "node_exporter", "caddy"
    ] : name => "example.invalid/business-tools/${name}@sha256:${sha256("mock-only-${name}")}" }, { postiz = "example.invalid/postiz:latest" })
  }
  expect_failures = [var.images]
}

run "reject_placeholder_digest" {
  command = plan
  variables {
    images = merge({ for name in [
      "postiz", "postgres", "redis", "temporal", "temporal_postgres", "mautic", "mariadb",
      "actual", "invoice_ninja", "invoice_nginx", "grafana", "prometheus", "node_exporter", "caddy"
    ] : name => "example.invalid/business-tools/${name}@sha256:${sha256("mock-only-${name}")}" }, { caddy = "caddy@sha256:REPLACE_WITH_VERIFIED_DIGEST" })
  }
  expect_failures = [var.images]
}

run "reject_missing_required_image" {
  command = plan
  variables {
    images = { for name in [
      "postiz", "postgres", "redis", "temporal", "temporal_postgres", "mautic", "mariadb",
      "actual", "invoice_ninja", "invoice_nginx", "grafana", "prometheus", "node_exporter", "caddy"
    ] : name => "example.invalid/business-tools/${name}@sha256:${sha256("mock-only-${name}")}" if name != "redis" }
  }
  expect_failures = [var.images]
}

run "reject_mutable_extra_image" {
  command = plan
  variables {
    images = merge({ for name in [
      "postiz", "postgres", "redis", "temporal", "temporal_postgres", "mautic", "mariadb",
      "actual", "invoice_ninja", "invoice_nginx", "grafana", "prometheus", "node_exporter", "caddy"
    ] : name => "example.invalid/business-tools/${name}@sha256:${sha256("mock-only-${name}")}" }, { future_sidecar = "example.invalid/sidecar:latest" })
  }
  expect_failures = [var.images]
}

run "reject_zone_outside_region" {
  command = plan
  variables {
    zone = "us-central1-a"
  }
  expect_failures = [var.zone]
}

run "reject_invalid_region" {
  command = plan
  variables {
    region = "london"
    zone   = "london-b"
  }
  expect_failures = [var.region]
}

run "reject_small_boot_disk" {
  command = plan
  variables {
    boot_disk_size_gb = 1
  }
  expect_failures = [var.boot_disk_size_gb]
}

run "reject_fractional_data_disk" {
  command = plan
  variables {
    data_disk_size_gb = 150.5
  }
  expect_failures = [var.data_disk_size_gb]
}

run "reject_public_operator" {
  command = plan
  variables {
    operator_members = ["allUsers"]
  }
  expect_failures = [var.operator_members]
}

run "reject_all_authenticated_operator" {
  command = plan
  variables {
    operator_members = ["allAuthenticatedUsers"]
  }
  expect_failures = [var.operator_members]
}

run "reject_invalid_dns_zone" {
  command = plan
  variables {
    dns_managed_zone = "example.com."
  }
  expect_failures = [var.dns_managed_zone]
}

run "reject_oversized_startup_metadata" {
  command = plan
  # A shell-safe but deliberately huge mock reference exercises the byte limit
  # without adding fixture files or making any registry/cloud calls.
  variables {
    images = merge({ for name in [
      "postiz", "postgres", "redis", "temporal", "temporal_postgres", "mautic", "mariadb",
      "actual", "invoice_ninja", "invoice_nginx", "grafana", "prometheus", "node_exporter", "caddy"
      ] : name => "example.invalid/business-tools/${name}@sha256:${sha256("mock-only-${name}")}" }, {
      caddy = "example.invalid/${join("", [for i in range(800) : format("%0400d", i)])}@sha256:${sha256("mock-only-caddy")}"
    })
  }
  expect_failures = [google_compute_instance.business_tools]
}