# Entirely offline plans: every Google resource/data source is mocked.
# Never mock apply retained resources: prevent_destroy also protects test cleanup.
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

  mock_resource "google_compute_instance" {
    defaults = {
      self_link = "https://www.googleapis.com/compute/v1/projects/makemoredigital2025/zones/europe-west2-b/instances/ai-gateway"
    }
  }

  mock_data "google_project" {
    defaults = {
      number = "123456789012"
    }
  }

  mock_resource "google_compute_global_address" {
    defaults = {
      address = "203.0.113.20"
    }
  }

  mock_resource "google_compute_instance_group" {
    defaults = {
      self_link = "https://www.googleapis.com/compute/v1/projects/makemoredigital2025/zones/europe-west2-b/instanceGroups/ai-gateway-admin"
    }
  }

  mock_resource "google_compute_health_check" {
    defaults = {
      self_link = "https://www.googleapis.com/compute/v1/projects/makemoredigital2025/global/healthChecks/ai-gateway-admin"
    }
  }

  mock_resource "google_compute_backend_service" {
    defaults = {
      generated_id = "9876543210987654321"
      self_link    = "https://www.googleapis.com/compute/v1/projects/makemoredigital2025/global/backendServices/ai-gateway-admin"
    }
  }

  mock_resource "google_compute_managed_ssl_certificate" {
    defaults = {
      self_link = "https://www.googleapis.com/compute/v1/projects/makemoredigital2025/global/sslCertificates/ai-gateway-admin"
    }
  }

  mock_resource "google_compute_ssl_policy" {
    defaults = {
      self_link = "https://www.googleapis.com/compute/v1/projects/makemoredigital2025/global/sslPolicies/ai-gateway-admin"
    }
  }

  mock_resource "google_compute_url_map" {
    defaults = {
      self_link = "https://www.googleapis.com/compute/v1/projects/makemoredigital2025/global/urlMaps/ai-gateway-admin"
    }
  }

  mock_resource "google_compute_target_https_proxy" {
    defaults = {
      self_link = "https://www.googleapis.com/compute/v1/projects/makemoredigital2025/global/targetHttpsProxies/ai-gateway-admin"
    }
  }
}

# Required inputs only: shared fixtures must not opt into browser administration.
variables {
  project_id    = "makemoredigital2025"
  bifrost_image = "maximhq/bifrost@sha256:a8942692af7b4b89196cd8fc33653b7353488dfd58b24078fe793b8574a8084b"
  caddy_image   = "caddy@sha256:834468128c7696cec0ceea6172f7d692daf645ae51983ca76e39da54a97c570d"
}

run "browser_admin_absent_by_default" {
  command = plan

  assert {
    condition = (
      var.admin_domain == null && length(var.admin_members) == 0 &&
      length(data.google_project.admin) == 0 &&
      length(google_compute_global_address.admin) == 0 &&
      length(google_compute_instance_group.admin) == 0 &&
      length(google_compute_instance_group_membership.admin) == 0 &&
      length(google_compute_health_check.admin) == 0 &&
      length(google_compute_backend_service.admin) == 0 &&
      length(google_compute_firewall.admin) == 0 &&
      length(google_compute_managed_ssl_certificate.admin) == 0 &&
      length(google_compute_ssl_policy.admin) == 0 &&
      length(google_compute_url_map.admin) == 0 &&
      length(google_compute_target_https_proxy.admin) == 0 &&
      length(google_compute_global_forwarding_rule.admin) == 0 &&
      length(google_iap_web_backend_service_iam_member.admin) == 0 &&
      length(google_dns_record_set.admin) == 0
    )
    error_message = "Browser admin must add no resources, IAM grants or project reads by default."
  }

  assert {
    condition = (
      output.admin_url == null && output.admin_static_ip == null &&
      output.admin_iap_audience == null && length(output.admin_members) == 0 &&
      output.domain == "llms.makemoredigital.com" &&
      output.private_url == "http://127.0.0.1:8080" &&
      google_compute_instance.gateway.machine_type == "e2-medium" &&
      google_compute_instance.gateway.zone == "europe-west2-b" &&
      length(google_iap_tunnel_instance_iam_member.operators) == 0
    )
    error_message = "Admin outputs must be absent/empty without changing the existing gateway defaults or SSH administration."
  }
}

run "dns_zone_alone_does_not_enable_admin" {
  command = plan
  variables {
    dns_managed_zone = "gateway-test-zone"
  }
  assert {
    condition     = length(google_dns_record_set.admin) == 0 && length(google_compute_backend_service.admin) == 0
    error_message = "An existing public DNS opt-in must not implicitly enable browser administration."
  }
}

run "explicit_single_user_iap_admin" {
  command = plan

  # Named, local opt-in: a synthetic single user and separate hostname verify
  # browser-only access without changing shared defaults or granting SSH roles.
  variables {
    admin_domain  = "admin.example.com"
    admin_members = ["user:chris@example.com"]
  }

  assert {
    condition = (
      length(google_compute_global_address.admin) == 1 &&
      google_compute_global_address.admin[0].name == "ai-gateway-admin-ip" &&
      google_compute_global_address.admin[0].address_type == "EXTERNAL" &&
      google_compute_global_address.admin[0].ip_version == "IPV4" &&
      google_compute_global_address.admin[0].project == var.project_id &&
      length(google_compute_global_forwarding_rule.admin) == 1 &&
      google_compute_global_forwarding_rule.admin[0].project == var.project_id &&
      google_compute_global_forwarding_rule.admin[0].load_balancing_scheme == "EXTERNAL_MANAGED" &&
      google_compute_global_forwarding_rule.admin[0].network_tier == "PREMIUM" &&
      google_compute_global_forwarding_rule.admin[0].ip_address == google_compute_global_address.admin[0].address &&
      google_compute_global_forwarding_rule.admin[0].ip_protocol == "TCP" &&
      google_compute_global_forwarding_rule.admin[0].port_range == "443" &&
      google_compute_global_forwarding_rule.admin[0].target == google_compute_target_https_proxy.admin[0].self_link
    )
    error_message = "Admin must have a dedicated PREMIUM global IPv4 HTTPS-only frontend in the gateway project."
  }

  assert {
    condition = (
      length(google_compute_instance_group.admin) == 1 &&
      google_compute_instance_group.admin[0].project == var.project_id &&
      google_compute_instance_group.admin[0].zone == var.zone &&
      google_compute_instance_group.admin[0].network == google_compute_network.gateway.self_link &&
      length(google_compute_instance_group.admin[0].instances) == 0 &&
      length(google_compute_instance_group.admin[0].named_port) == 1 &&
      google_compute_instance_group.admin[0].named_port[0].name == "admin" &&
      google_compute_instance_group.admin[0].named_port[0].port == 8081 &&
      length(google_compute_instance_group_membership.admin) == 1 &&
      google_compute_instance_group_membership.admin[0].project == var.project_id &&
      google_compute_instance_group_membership.admin[0].zone == var.zone &&
      google_compute_instance_group_membership.admin[0].instance_group == google_compute_instance_group.admin[0].name &&
      google_compute_instance_group_membership.admin[0].instance == google_compute_instance.gateway.self_link
    )
    error_message = "The group must start empty with admin:8081; a separate same-zone membership must attach the gateway VM."
  }

  # Lifecycle and graph references are not exposed as test expression values.
  # Guard their source narrowly so the audience -> VM -> membership graph stays
  # acyclic and group refresh cannot remove independently owned membership.
  assert {
    condition = alltrue([for block in [regex(
      "(?ms)^resource \"google_compute_instance_group\" \"admin\" \\{.*?^\\}",
      file("${path.module}/admin.tf")
      )] : (
      length(regexall("google_compute_instance\\.|google_compute_instance_group_membership\\.", block)) == 0 &&
      can(regex("ignore_changes\\s*=\\s*\\[instances\\]", block))
    )])
    error_message = "The empty group must not depend on the VM/membership and must ignore only instances."
  }

  assert {
    condition = (
      length(google_compute_backend_service.admin) == 1 &&
      google_compute_backend_service.admin[0].project == var.project_id &&
      google_compute_backend_service.admin[0].load_balancing_scheme == "EXTERNAL_MANAGED" &&
      google_compute_backend_service.admin[0].protocol == "HTTP" &&
      google_compute_backend_service.admin[0].port_name == "admin" &&
      google_compute_backend_service.admin[0].timeout_sec == 300 &&
      length(google_compute_backend_service.admin[0].backend) == 1 &&
      alltrue([for backend in google_compute_backend_service.admin[0].backend :
        backend.group == google_compute_instance_group.admin[0].self_link
      ]) &&
      length(google_compute_backend_service.admin[0].iap) == 1 &&
      google_compute_backend_service.admin[0].iap[0].enabled &&
      try(length(google_compute_backend_service.admin[0].iap[0].oauth2_client_id), 0) == 0 &&
      try(length(google_compute_backend_service.admin[0].iap[0].oauth2_client_secret), 0) == 0 &&
      !google_compute_backend_service.admin[0].enable_cdn &&
      length(google_compute_backend_service.admin[0].log_config) == 1 &&
      !google_compute_backend_service.admin[0].log_config[0].enable
    )
    error_message = "The only backend must require IAP with Google-managed OAuth, no CDN/access logs, and HTTP admin traffic with a 300s timeout."
  }

  assert {
    condition = (
      length(google_compute_health_check.admin) == 1 &&
      google_compute_health_check.admin[0].project == var.project_id &&
      length(google_compute_health_check.admin[0].http_health_check) == 1 &&
      google_compute_health_check.admin[0].http_health_check[0].port == 8081 &&
      google_compute_health_check.admin[0].http_health_check[0].request_path == "/_iap_health" &&
      google_compute_health_check.admin[0].http_health_check[0].host == var.admin_domain &&
      toset(google_compute_backend_service.admin[0].health_checks) == toset([google_compute_health_check.admin[0].self_link])
    )
    error_message = "Admin health checks must use the dedicated HTTP 8081 endpoint and correct admin Host header."
  }

  assert {
    condition = (
      length(google_compute_firewall.admin) == 1 &&
      google_compute_firewall.admin[0].project == var.project_id &&
      google_compute_firewall.admin[0].network == google_compute_network.gateway.self_link &&
      google_compute_firewall.admin[0].direction == "INGRESS" &&
      toset(google_compute_firewall.admin[0].source_ranges) == toset(["130.211.0.0/22", "35.191.0.0/16"]) &&
      toset(google_compute_firewall.admin[0].target_service_accounts) == toset([google_service_account.gateway.email]) &&
      try(length(google_compute_firewall.admin[0].source_tags), 0) == 0 &&
      try(length(google_compute_firewall.admin[0].source_service_accounts), 0) == 0 &&
      try(length(google_compute_firewall.admin[0].target_tags), 0) == 0 &&
      length(google_compute_firewall.admin[0].allow) == 1 &&
      alltrue([for rule in google_compute_firewall.admin[0].allow :
        rule.protocol == "tcp" && toset(rule.ports) == toset(["8081"])
      ]) &&
      alltrue([for rule in google_compute_firewall.public_web.allow :
        rule.protocol == "tcp" && toset(rule.ports) == toset(["80", "443"])
      ])
    )
    error_message = "Only the two GFE/health-check ranges may reach TCP 8081, only on the gateway identity; public ingress must remain 80/443."
  }

  assert {
    condition = (
      length(google_compute_managed_ssl_certificate.admin) == 1 &&
      google_compute_managed_ssl_certificate.admin[0].project == var.project_id &&
      toset(google_compute_managed_ssl_certificate.admin[0].managed[0].domains) == toset([var.admin_domain]) &&
      length(google_compute_ssl_policy.admin) == 1 &&
      google_compute_ssl_policy.admin[0].project == var.project_id &&
      google_compute_ssl_policy.admin[0].min_tls_version == "TLS_1_2" &&
      google_compute_ssl_policy.admin[0].profile == "MODERN" &&
      length(google_compute_target_https_proxy.admin) == 1 &&
      google_compute_target_https_proxy.admin[0].project == var.project_id &&
      google_compute_target_https_proxy.admin[0].ssl_policy == google_compute_ssl_policy.admin[0].self_link &&
      toset(google_compute_target_https_proxy.admin[0].ssl_certificates) == toset([google_compute_managed_ssl_certificate.admin[0].self_link]) &&
      google_compute_target_https_proxy.admin[0].url_map == google_compute_url_map.admin[0].self_link &&
      length(google_compute_url_map.admin) == 1 &&
      google_compute_url_map.admin[0].project == var.project_id &&
      google_compute_url_map.admin[0].default_service == google_compute_backend_service.admin[0].self_link &&
      length(google_compute_url_map.admin[0].host_rule) == 0 &&
      length(google_compute_url_map.admin[0].path_matcher) == 0 &&
      length(google_compute_url_map.admin[0].default_url_redirect) == 0 &&
      length(google_compute_url_map.admin[0].default_route_action) == 0
    )
    error_message = "TLS must use a Google-managed admin certificate and MODERN TLS 1.2 policy; all hosts and paths must hit the same IAP backend."
  }

  assert {
    condition = (
      toset(keys(google_iap_web_backend_service_iam_member.admin)) == toset(["user:chris@example.com"]) &&
      alltrue([for member, grant in google_iap_web_backend_service_iam_member.admin :
        grant.project == var.project_id && grant.member == member &&
        grant.role == "roles/iap.httpsResourceAccessor" &&
        grant.web_backend_service == google_compute_backend_service.admin[0].name
      ]) &&
      length(google_iap_tunnel_instance_iam_member.operators) == 0 &&
      length(google_compute_instance_iam_member.operators) == 0 &&
      length(google_service_account_iam_member.operators) == 0 &&
      output.admin_members == toset(["user:chris@example.com"])
    )
    error_message = "Only the explicit user may receive the backend-scoped browser role, without implicit SSH or service-account grants."
  }

  assert {
    condition = (
      length(data.google_project.admin) == 1 &&
      data.google_project.admin[0].project_id == var.project_id &&
      output.admin_url == "https://admin.example.com" &&
      output.admin_static_ip == "203.0.113.20" &&
      output.admin_static_ip != output.static_ip &&
      output.admin_iap_audience == "/projects/123456789012/global/backendServices/9876543210987654321" &&
      length(google_dns_record_set.admin) == 0
    )
    error_message = "Safe admin outputs must use the separate address and numeric IAP audience; DNS must stay external unless explicitly selected."
  }

  # Test expressions cannot enumerate undeclared resource types. Keep the
  # source guard limited to forbidden bypasses/credentials/project-wide grants.
  assert {
    condition = alltrue([for filename in fileset(path.module, "*.tf") :
      length(regexall("(?m)^\\s*resource\\s+\"(google_compute_target_http_proxy|google_iap_client|google_iap_brand|google_project_iam_[^\"]+|google_project_service)\"", file("${path.module}/${filename}"))) == 0 &&
      length(regexall("(?m)^\\s*oauth2_client_(id|secret)[a-z0-9_]*\\s*=", file("${path.module}/${filename}"))) == 0
    ])
    error_message = "Admin must not add an HTTP proxy, OAuth credentials/client, project IAM grants or project API lifecycle management."
  }
}

run "explicit_admin_cloud_dns" {
  command = plan
  # Named DNS opt-in: add only the admin A record in the selected existing zone.
  variables {
    admin_domain     = "admin.example.com"
    admin_members    = ["user:chris@example.com"]
    dns_managed_zone = "gateway-test-zone"
  }
  assert {
    condition = (
      length(google_dns_record_set.admin) == 1 &&
      google_dns_record_set.admin[0].project == var.project_id &&
      google_dns_record_set.admin[0].managed_zone == "gateway-test-zone" &&
      google_dns_record_set.admin[0].name == "admin.example.com." &&
      google_dns_record_set.admin[0].type == "A" &&
      google_dns_record_set.admin[0].ttl == 300 &&
      toset(google_dns_record_set.admin[0].rrdatas) == toset(["203.0.113.20"]) &&
      google_dns_record_set.gateway[0].name == "llms.makemoredigital.com."
    )
    error_message = "Opt-in DNS must point the separate admin hostname to its global IPv4 address without replacing the public hostname."
  }
}

# Local failure-mode inputs: these must never become shared fixture defaults.
run "reject_admin_all_users" {
  command = plan
  variables {
    admin_domain  = "admin.example.com"
    admin_members = ["allUsers"]
  }
  expect_failures = [var.admin_members]
}

run "reject_admin_all_authenticated_users" {
  command = plan
  variables {
    admin_domain  = "admin.example.com"
    admin_members = ["allAuthenticatedUsers"]
  }
  expect_failures = [var.admin_members]
}

run "reject_admin_group" {
  command = plan
  variables {
    admin_domain  = "admin.example.com"
    admin_members = ["group:admins@example.com"]
  }
  expect_failures = [var.admin_members]
}

run "reject_admin_service_account" {
  command = plan
  variables {
    admin_domain  = "admin.example.com"
    admin_members = ["serviceAccount:admin@example.iam.gserviceaccount.com"]
  }
  expect_failures = [var.admin_members]
}

run "reject_admin_domain_principal" {
  command = plan
  variables {
    admin_domain  = "admin.example.com"
    admin_members = ["domain:example.com"]
  }
  expect_failures = [var.admin_members]
}

run "reject_admin_invalid_email" {
  command = plan
  variables {
    admin_domain  = "admin.example.com"
    admin_members = ["user:not-an-email"]
  }
  expect_failures = [var.admin_members]
}

run "reject_admin_without_members" {
  command = plan
  variables {
    admin_domain = "admin.example.com"
  }
  expect_failures = [var.admin_members]
}

run "reject_members_when_admin_disabled" {
  command = plan
  variables {
    admin_members = ["user:chris@example.com"]
  }
  expect_failures = [var.admin_members]
}

run "reject_admin_public_api_hostname" {
  command = plan
  variables {
    domain        = "same.example.com"
    admin_domain  = "same.example.com"
    admin_members = ["user:chris@example.com"]
  }
  expect_failures = [var.admin_domain]
}

run "reject_admin_invalid_hostname" {
  command = plan
  variables {
    admin_domain  = "https://admin.example.com/path"
    admin_members = ["user:chris@example.com"]
  }
  expect_failures = [var.admin_domain]
}

run "reject_admin_empty_hostname" {
  command = plan
  variables {
    admin_domain  = ""
    admin_members = ["user:chris@example.com"]
  }
  expect_failures = [var.admin_domain]
}

run "reject_admin_uppercase_hostname" {
  command = plan
  variables {
    admin_domain  = "Admin.example.com"
    admin_members = ["user:chris@example.com"]
  }
  expect_failures = [var.admin_domain]
}

run "reject_admin_oversized_label" {
  command = plan
  variables {
    admin_domain  = "${join("", [for i in range(64) : "a"])}.example.com"
    admin_members = ["user:chris@example.com"]
  }
  expect_failures = [var.admin_domain]
}

run "reject_admin_oversized_hostname" {
  command = plan
  variables {
    admin_domain  = join(".", [for i in range(4) : join("", [for j in range(63) : "a"])])
    admin_members = ["user:chris@example.com"]
  }
  expect_failures = [var.admin_domain]
}