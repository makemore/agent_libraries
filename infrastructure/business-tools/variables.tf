variable "project_id" {
  description = "Existing GCP project; API enablement and lifecycle are managed outside this deployment."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be a valid 6-30 character GCP project ID."
  }
}

variable "region" {
  description = "GCP region for the business-tools network, address and snapshot schedule."
  type        = string
  default     = "europe-west2"
  nullable    = false

  validation {
    condition     = can(regex("^[a-z]+(-[a-z]+)+[0-9]+$", var.region))
    error_message = "region must be a GCP region name, such as europe-west2."
  }
}

variable "zone" {
  description = "GCP zone for the VM and retained data disk; must belong to region."
  type        = string
  default     = "europe-west2-b"
  nullable    = false

  validation {
    condition     = can(regex("^${var.region}-[a-z]$", var.zone))
    error_message = "zone must be a zone within the configured region."
  }
}

variable "machine_type" {
  description = "Shared VM machine type; the default provides 4 vCPUs and 16 GB RAM."
  type        = string
  default     = "e2-standard-4"
  nullable    = false

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*[a-z0-9]$", var.machine_type))
    error_message = "machine_type must be a GCP machine type name."
  }
}

variable "boot_disk_size_gb" {
  description = "Disposable Ubuntu 24.04 boot disk size in GB."
  type        = number
  default     = 50
  nullable    = false

  validation {
    condition     = var.boot_disk_size_gb >= 10 && floor(var.boot_disk_size_gb) == var.boot_disk_size_gb
    error_message = "boot_disk_size_gb must be an integer of at least 10 GB."
  }
}

variable "data_disk_size_gb" {
  description = "Retained pd-balanced disk size in GB; an existing disk may grow but cannot shrink."
  type        = number
  default     = 150
  nullable    = false

  validation {
    condition     = var.data_disk_size_gb >= 10 && floor(var.data_disk_size_gb) == var.data_disk_size_gb
    error_message = "data_disk_size_gb must be an integer of at least 10 GB."
  }
}

variable "domains" {
  description = "Distinct lowercase DNS hostnames, without schemes, ports, paths or trailing dots."
  type = object({
    social    = string
    marketing = string
    budget    = string
    invoices  = string
    metrics   = string
  })
  default = {
    social    = "social.makemoredigital.com"
    marketing = "marketing.makemoredigital.com"
    budget    = "budget.makemoredigital.com"
    invoices  = "invoices.makemoredigital.com"
    metrics   = "metrics.makemoredigital.com"
  }
  nullable = false

  validation {
    condition = alltrue([for domain in values(var.domains) : try(
      length(domain) <= 253 && can(regex(
        "^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$",
        domain
      )), false
    )])
    error_message = "Every domain must be a non-null lowercase DNS hostname with 1-63 character labels and at most 253 characters overall."
  }

  validation {
    condition     = length(distinct(values(var.domains))) == length(values(var.domains))
    error_message = "Each business tool must have a unique domain."
  }
}

variable "images" {
  description = "Verified digest-pinned images for all required services, including invoice_nginx and temporal_postgres. Additional service keys are allowed but must also be digest-pinned."
  type        = map(string)
  nullable    = false

  validation {
    condition = alltrue([for name in [
      "postiz", "postgres", "redis", "temporal", "mautic", "mariadb",
      "actual", "invoice_ninja", "grafana", "prometheus", "node_exporter", "caddy",
      "invoice_nginx", "temporal_postgres"
    ] : contains(keys(var.images), name)])
    error_message = "images must include postiz, postgres, redis, temporal, temporal_postgres, mautic, mariadb, actual, invoice_ninja, invoice_nginx, grafana, prometheus, node_exporter and caddy."
  }

  validation {
    condition = alltrue([for name, image in var.images :
      can(regex("^[a-z][a-z0-9_]*$", name)) &&
      can(regex("^[a-z0-9][A-Za-z0-9._:/-]*@sha256:[a-f0-9]{64}$", image))
    ])
    error_message = "Every image key must be lowercase snake_case and every value a shell-safe image reference ending in @sha256: and 64 lowercase hexadecimal characters. Mutable tags and placeholder digests are not deployable."
  }
}

variable "public_enabled" {
  description = "Explicitly enable public sites only after initializing every application through IAP/loopback. False keeps the root runtime proxy fail-closed; changing metadata requires an explicit startup-script rerun."
  type        = bool
  default     = false
  nullable    = false
}

variable "dns_managed_zone" {
  description = "Optional existing Cloud DNS managed zone name in project_id; null leaves DNS external."
  type        = string
  default     = null

  validation {
    condition     = var.dns_managed_zone == null ? true : can(regex("^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$", var.dns_managed_zone))
    error_message = "dns_managed_zone must be null or an existing Cloud DNS managed zone name, not a DNS suffix."
  }
}

variable "operator_members" {
  description = "Optional explicit user, group or serviceAccount identities granted instance-scoped IAP SSH and OS Admin Login, and serviceAccountUser on this VM's identity only."
  type        = set(string)
  default     = []
  nullable    = false

  validation {
    condition = alltrue([for member in var.operator_members : can(regex(
      "^(user|group|serviceAccount):[^\\s@]+@[^\\s@]+$", member
    ))])
    error_message = "operator_members must contain explicit user:, group: or serviceAccount: email identities; public and domain-wide principals are forbidden."
  }
}