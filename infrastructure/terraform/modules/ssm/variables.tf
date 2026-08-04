variable "ssm_prefix" {
  description = "Prefix for all SSM parameter names (e.g. `/mlops-framework/prod`). Each parameter is stored as `ssm_prefix/<suffix>`."
  type        = string
}

variable "db_password" {
  description = <<-EOT
    Optional RDS master password. If non-null, the module creates a
    `db/password` SSM parameter with this value. Set null to skip (e.g.
    when using Secrets Manager instead).
  EOT
  type        = string
  default     = null
  sensitive   = true
}

variable "db_password_description" {
  description = "Description for the `db/password` SSM parameter."
  type        = string
  default     = "RDS master password (initial DB)."
}

variable "db_password_name_suffix" {
  description = "Suffix appended to the SSM prefix for the db password parameter."
  type        = string
  default     = "db/password"
}

variable "generated_secrets" {
  description = <<-EOT
    Map of secrets to generate and store in SSM Parameter Store. Each
    suffix is appended to the ssm_prefix. The actual key in the map is
    used as the suffix.

    `base64_encode` stores base64(value) instead of the raw string. It
    exists for consumers that require a fixed number of *bytes* rather
    than characters — notably Airflow's Fernet key, which must decode to
    exactly 32 bytes. Set `length = 32` alongside it: 32 ASCII characters
    base64-encode to 44 characters that decode back to 32 bytes.
    Generating 44 raw characters instead yields 33 bytes, which
    `cryptography.fernet.Fernet` rejects outright.
  EOT
  type = map(object({
    length        = number
    special       = optional(bool, false)
    base64_encode = optional(bool, false)
    description   = string
  }))
  default = {}
}
