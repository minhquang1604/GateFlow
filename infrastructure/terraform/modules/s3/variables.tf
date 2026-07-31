variable "project_name" {
  description = "Logical project name used to build bucket names. The bucket final name is `project_name-key-name_suffix`."
  type        = string
}

variable "name_suffix" {
  description = <<-EOT
    Optional explicit suffix for bucket names. If empty, the module
    generates a 4-byte random hex suffix — useful to avoid global name
    collisions when re-deploying the same configuration.
  EOT
  type        = string
  default     = ""
}

variable "buckets" {
  description = <<-EOT
    Map of buckets to create. The key is the bucket's logical name
    (used in outputs). The actual AWS bucket name is
    `project_name-key-name_suffix` (the suffix is auto-generated when
    `name_suffix` is empty).
  EOT
  type = map(object({
    purpose                    = string
    force_destroy              = optional(bool, false)
    versioning                 = optional(bool, true)
    noncurrent_expiration_days = optional(number)
    multipart_abort_days       = optional(number)
  }))
  default = {}
}
