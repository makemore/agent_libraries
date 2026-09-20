variable "project_id" {
  description = "Existing GCP project shared with Studio; this deployment does not manage project APIs."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be a valid 6-30 character GCP project ID."
  }
}

variable "region" {
  description = "GCP region for the gateway network, address and snapshot schedule."
  type        = string
  default     = "europe-west2"
  nullable    = false

  validation {
    condition     = can(regex("^[a-z]+(-[a-z]+)+[0-9]+$", var.region))
    error_message = "region must be a GCP region name, such as europe-west2."
  }
}

variable "zone" {
  description = "GCP zone for the gateway VM and its retained data disk."
  type        = string
  default     = "europe-west2-b"
  nullable    = false

  validation {
    condition     = can(regex("^${var.region}-[a-z]$", var.zone))
    error_message = "zone must be a zone within the configured region."
  }
}

variable "machine_type" {
  description = "Gateway VM machine type."
  type        = string
  default     = "e2-medium"
  nullable    = false

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*[a-z0-9]$", var.machine_type))
    error_message = "machine_type must be a GCP machine type name."
  }
}

variable "boot_disk_size_gb" {
  description = "Size of the disposable Ubuntu 24.04 boot disk in GB."
  type        = number
  default     = 20
  nullable    = false

  validation {
    condition     = var.boot_disk_size_gb >= 10 && floor(var.boot_disk_size_gb) == var.boot_disk_size_gb
    error_message = "boot_disk_size_gb must be an integer of at least 10 GB."
  }
}

variable "data_disk_size_gb" {
  description = "Size of the retained pd-balanced data disk in GB; an existing disk can grow but cannot shrink."
  type        = number
  default     = 30
  nullable    = false

  validation {
    condition     = var.data_disk_size_gb >= 10 && floor(var.data_disk_size_gb) == var.data_disk_size_gb
    error_message = "data_disk_size_gb must be an integer of at least 10 GB."
  }
}

variable "domain" {
  description = "Public gateway DNS hostname, without a scheme, port, path or trailing dot."
  type        = string
  default     = "llms.makemoredigital.com"
  nullable    = false

  validation {
    condition = length(var.domain) <= 253 && can(regex(
      "^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$",
      var.domain
    ))
    error_message = "domain must be a lowercase DNS hostname with labels of 1-63 characters and at most 253 characters overall."
  }
}

variable "admin_domain" {
  description = "Optional separate browser admin DNS hostname protected by Google IAP. Null disables all browser admin infrastructure."
  type        = string
  default     = null

  validation {
    condition = var.admin_domain == null ? true : (
      length(var.admin_domain) <= 253 && can(regex(
        "^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$",
        var.admin_domain
      ))
    )
    error_message = "admin_domain must be null or a lowercase DNS hostname with labels of 1-63 characters and at most 253 characters overall, without a scheme, port, path or trailing dot."
  }

  validation {
    condition     = var.admin_domain == null ? true : var.admin_domain != var.domain
    error_message = "admin_domain must be distinct from the public API domain."
  }
}

variable "admin_members" {
  description = "Explicit user: email identities granted browser access only to the admin IAP backend; required when admin_domain is set and empty when disabled."
  type        = set(string)
  default     = []
  nullable    = false

  validation {
    condition = alltrue([
      for member in var.admin_members : can(regex(
        "^user:[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$",
        member
      ))
    ])
    error_message = "admin_members must contain only explicit user: email identities; public, group, domain and service-account principals are not allowed."
  }

  validation {
    condition     = var.admin_domain == null ? length(var.admin_members) == 0 : length(var.admin_members) > 0
    error_message = "admin_members must be nonempty when admin_domain is set, and empty when admin_domain is null."
  }
}

variable "bifrost_image" {
  description = "Verified Bifrost container image pinned to an immutable sha256 digest; no mutable tag-only references."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[a-z0-9][A-Za-z0-9._:/-]*@sha256:[a-f0-9]{64}$", var.bifrost_image))
    error_message = "bifrost_image must be a shell-safe container reference ending in @sha256: followed by 64 lowercase hexadecimal characters."
  }
}

variable "caddy_image" {
  description = "Verified Caddy container image pinned to an immutable sha256 digest; no mutable tag-only references."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[a-z0-9][A-Za-z0-9._:/-]*@sha256:[a-f0-9]{64}$", var.caddy_image))
    error_message = "caddy_image must be a shell-safe container reference ending in @sha256: followed by 64 lowercase hexadecimal characters."
  }
}

variable "dns_managed_zone" {
  description = "Optional existing Cloud DNS managed zone name in project_id. Null leaves all DNS management external."
  type        = string
  default     = null

  validation {
    condition     = var.dns_managed_zone == null ? true : can(regex("^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$", var.dns_managed_zone))
    error_message = "dns_managed_zone must be null or an existing Cloud DNS managed zone name, not its DNS suffix."
  }
}

variable "operator_members" {
  description = "Optional IAM user, group or serviceAccount members granted IAP access and OS Admin Login only on this VM, plus serviceAccountUser on its dedicated service account."
  type        = set(string)
  default     = []
  nullable    = false

  validation {
    condition = alltrue([
      for member in var.operator_members :
      can(regex("^(user|group|serviceAccount):[^\\s@]+@[^\\s@]+$", member))
    ])
    error_message = "operator_members must contain explicit user:, group: or serviceAccount: email identities; public principals are not allowed."
  }
}