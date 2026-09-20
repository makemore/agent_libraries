# Offline plans only: use the provider schema, never Google credentials or APIs.
# Shared inputs supply only required variables; optional inputs inherit defaults.
mock_provider "google" {
  mock_resource "google_compute_network" {
    defaults = {
      self_link = "https://www.googleapis.com/compute/v1/projects/makemoredigital2025/global/networks/ai-gateway"
    }
  }

  mock_resource "google_compute_subnetwork" {
    defaults = {
      self_link = "https://www.googleapis.com/compute/v1/projects/makemoredigital2025/regions/europe-west2/subnetworks/ai-gateway-subnet"
    }
  }

  mock_resource "google_compute_disk" {
    defaults = {
      self_link = "https://www.googleapis.com/compute/v1/projects/makemoredigital2025/zones/europe-west2-b/disks/ai-gateway-data"
    }
  }

  mock_resource "google_compute_address" {
    defaults = {
      address = "203.0.113.10"
    }
  }

  mock_resource "google_service_account" {
    defaults = {
      email = "ai-gateway@makemoredigital2025.iam.gserviceaccount.com"
      name  = "projects/makemoredigital2025/serviceAccounts/ai-gateway@makemoredigital2025.iam.gserviceaccount.com"
    }
  }
}

variables {
  project_id    = "makemoredigital2025"
  bifrost_image = "maximhq/bifrost@sha256:a8942692af7b4b89196cd8fc33653b7353488dfd58b24078fe793b8574a8084b"
  caddy_image   = "caddy@sha256:834468128c7696cec0ceea6172f7d692daf645ae51983ca76e39da54a97c570d"
}

run "default_deployment" {
  command = plan

  assert {
    condition = (
      google_compute_instance.gateway.project == "makemoredigital2025" &&
      google_compute_instance.gateway.machine_type == "e2-medium" &&
      google_compute_instance.gateway.zone == "europe-west2-b" &&
      google_compute_subnetwork.gateway.region == "europe-west2" &&
      output.domain == "llms.makemoredigital.com"
    )
    error_message = "The default deployment must retain its project, e2-medium VM, London location and hostname."
  }

  assert {
    condition = (
      length(google_dns_record_set.gateway) == 0 &&
      length(google_iap_tunnel_instance_iam_member.operators) == 0 &&
      length(google_compute_instance_iam_member.operators) == 0 &&
      length(google_service_account_iam_member.operators) == 0
    )
    error_message = "DNS management and all operator IAM grants must remain opt-in."
  }

  assert {
    condition = (
      google_compute_instance.gateway.boot_disk[0].initialize_params[0].size == 20 &&
      google_compute_instance.gateway.boot_disk[0].initialize_params[0].type == "pd-balanced" &&
      google_compute_disk.data.size == 30 &&
      google_compute_disk.data.type == "pd-balanced" &&
      google_compute_disk.data.zone == google_compute_instance.gateway.zone &&
      length(google_compute_instance.gateway.attached_disk) == 1 &&
      google_compute_instance.gateway.attached_disk[0].device_name == "ai-gateway-data" &&
      google_compute_instance.gateway.attached_disk[0].mode == "READ_WRITE"
    )
    error_message = "Defaults must use a 20 GB boot disk and a separate 30 GB balanced data disk in the VM zone."
  }

  assert {
    condition = (
      google_compute_resource_policy.data_snapshots.snapshot_schedule_policy[0].schedule[0].daily_schedule[0].days_in_cycle == 1 &&
      google_compute_resource_policy.data_snapshots.snapshot_schedule_policy[0].schedule[0].daily_schedule[0].start_time == "04:00" &&
      google_compute_resource_policy.data_snapshots.snapshot_schedule_policy[0].retention_policy[0].max_retention_days == 14 &&
      google_compute_resource_policy.data_snapshots.snapshot_schedule_policy[0].retention_policy[0].on_source_disk_delete == "KEEP_AUTO_SNAPSHOTS" &&
      # The API omits default-only snapshot_properties; avoid perpetual drift.
      length(google_compute_resource_policy.data_snapshots.snapshot_schedule_policy[0].snapshot_properties) == 0 &&
      google_compute_disk_resource_policy_attachment.data_snapshots.disk == google_compute_disk.data.name &&
      google_compute_disk_resource_policy_attachment.data_snapshots.name == google_compute_resource_policy.data_snapshots.name &&
      google_compute_disk_resource_policy_attachment.data_snapshots.zone == google_compute_disk.data.zone
    )
    error_message = "The data disk must have daily 04:00 snapshots retained for 14 days and kept after source disk deletion."
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
    error_message = "Only TCP 80/443 may be public; TCP 22 must be IAP-only, with both rules scoped to the VM identity."
  }

  assert {
    condition = (
      google_compute_instance.gateway.metadata["enable-oslogin"] == "TRUE" &&
      google_compute_instance.gateway.metadata["block-project-ssh-keys"] == "TRUE" &&
      google_compute_instance.gateway.metadata["serial-port-enable"] == "FALSE" &&
      google_compute_instance.gateway.deletion_protection &&
      google_compute_instance.gateway.shielded_instance_config[0].enable_secure_boot &&
      google_compute_instance.gateway.shielded_instance_config[0].enable_vtpm &&
      google_compute_instance.gateway.shielded_instance_config[0].enable_integrity_monitoring
    )
    error_message = "OS Login, blocked project SSH keys, disabled serial access, deletion protection and all Shielded VM protections must remain enabled."
  }

  assert {
    condition = (
      local.deployment_files["model-catalog.py"] == file("${path.module}/runtime/model-catalog.py") &&
      local.deployment_files["bootstrap.sh"] == file("${path.module}/runtime/bootstrap.sh") &&
      strcontains(google_compute_instance.gateway.metadata["startup-script"], "umask 077\ninstall -d -o root -g root -m 0750 /opt/ai-gateway") &&
      alltrue([for name, content in local.deployment_files :
        strcontains(google_compute_instance.gateway.metadata["startup-script"], "printf '%s' '${base64encode(content)}' | base64 --decode > '/opt/ai-gateway/${name}'")
      ]) &&
      endswith(google_compute_instance.gateway.metadata["startup-script"], "bash /opt/ai-gateway/bootstrap.sh\n")
    )
    error_message = "VM startup must privately stage every original deployment asset and the catalog guard byte-for-byte as base64 before running the owning bootstrap."
  }

  assert {
    condition = (
      google_secret_manager_secret.bootstrap.secret_id == "ai-gateway-bootstrap-json" &&
      google_secret_manager_secret_iam_member.bootstrap_reader.secret_id == google_secret_manager_secret.bootstrap.secret_id &&
      google_secret_manager_secret_iam_member.bootstrap_reader.role == "roles/secretmanager.secretAccessor" &&
      output.bootstrap_secret_id == google_secret_manager_secret.bootstrap.secret_id
    )
    error_message = "Terraform must provision the bootstrap secret container and its secret-scoped reader."
  }

  # Test expressions cannot enumerate absent resource types. This narrow guard
  # forbids secret-version resources/data reads without referencing an undeclared
  # resource or introducing payloads into the plan. Other assertions use the plan.
  assert {
    condition = alltrue([for filename in fileset(path.module, "*.tf") :
      length(regexall("(?m)^\\s*(resource|data)\\s+\"google_secret_manager_secret_version(_access)?\"", file("${path.module}/${filename}"))) == 0
    ])
    error_message = "Secret versions and payload reads must remain out of Terraform; only the secret container is managed."
  }
}

run "explicit_zone_dns_and_operator_overrides" {
  command = plan

  # Named opt-in scenario: move within the default region, manage one DNS record
  # and grant explicit operators resource-scoped access. No shared default changes.
  variables {
    zone             = "europe-west2-c"
    dns_managed_zone = "gateway-test-zone"
    operator_members = ["user:operator@example.com", "group:gateway-operators@example.com"]
  }

  override_resource {
    target = google_compute_disk.data
    values = {
      self_link = "https://www.googleapis.com/compute/v1/projects/makemoredigital2025/zones/europe-west2-c/disks/ai-gateway-data"
    }
  }

  assert {
    condition = (
      google_compute_instance.gateway.zone == "europe-west2-c" &&
      google_compute_disk.data.zone == "europe-west2-c" &&
      google_compute_disk_resource_policy_attachment.data_snapshots.zone == "europe-west2-c" &&
      output.zone == "europe-west2-c"
    )
    error_message = "An explicit same-region zone must propagate to the VM, data disk, snapshot attachment and output."
  }

  assert {
    condition = (
      length(google_dns_record_set.gateway) == 1 &&
      google_dns_record_set.gateway[0].managed_zone == "gateway-test-zone" &&
      google_dns_record_set.gateway[0].name == "llms.makemoredigital.com." &&
      google_dns_record_set.gateway[0].type == "A" &&
      google_dns_record_set.gateway[0].ttl == 300 &&
      length(google_dns_record_set.gateway[0].rrdatas) == 1
    )
    error_message = "An explicit DNS zone must create exactly one IPv4 record for the gateway hostname."
  }

  assert {
    condition = (
      toset(keys(google_iap_tunnel_instance_iam_member.operators)) == toset(var.operator_members) &&
      alltrue([for member, grant in google_iap_tunnel_instance_iam_member.operators :
        grant.member == member && grant.role == "roles/iap.tunnelResourceAccessor" &&
        grant.instance == google_compute_instance.gateway.name && grant.zone == "europe-west2-c"
      ]) &&
      toset(keys(google_compute_instance_iam_member.operators)) == toset(var.operator_members) &&
      alltrue([for member, grant in google_compute_instance_iam_member.operators :
        grant.member == member && grant.role == "roles/compute.osAdminLogin" &&
        grant.instance_name == google_compute_instance.gateway.name && grant.zone == "europe-west2-c"
      ]) &&
      toset(keys(google_service_account_iam_member.operators)) == toset(var.operator_members) &&
      alltrue([for member, grant in google_service_account_iam_member.operators :
        grant.member == member && grant.role == "roles/iam.serviceAccountUser"
      ])
    )
    error_message = "Only explicitly selected operators may receive the three resource-scoped administration roles."
  }
}

# Failure-mode overrides are local to each named run; valid defaults stay intact.
run "reject_invalid_domain" {
  command = plan
  variables {
    domain = "https://llms.makemoredigital.com/path"
  }
  expect_failures = [var.domain]
}

run "reject_mutable_bifrost_image" {
  command = plan
  variables {
    bifrost_image = "maximhq/bifrost:latest"
  }
  expect_failures = [var.bifrost_image]
}

run "reject_mutable_caddy_image" {
  command = plan
  variables {
    caddy_image = "caddy:2"
  }
  expect_failures = [var.caddy_image]
}

run "reject_zone_outside_region" {
  command = plan
  variables {
    zone = "us-central1-a"
  }
  expect_failures = [var.zone]
}

run "reject_all_users_operator" {
  command = plan
  variables {
    operator_members = ["allUsers"]
  }
  expect_failures = [var.operator_members]
}

run "reject_all_authenticated_users_operator" {
  command = plan
  variables {
    operator_members = ["allAuthenticatedUsers"]
  }
  expect_failures = [var.operator_members]
}