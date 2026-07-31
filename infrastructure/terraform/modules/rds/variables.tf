variable "name_prefix" {
  description = "Prefix used to name the RDS instance and parameter group."
  type        = string
}

variable "identifier" {
  description = "Identifier of the RDS instance. Defaults to `name_prefix-postgres`."
  type        = string
  default     = null
}

variable "engine_version" {
  description = "PostgreSQL engine version."
  type        = string
  default     = "15.7"
}

variable "parameter_group_family" {
  description = "Family of the DB parameter group (must match engine_version)."
  type        = string
  default     = "postgres15"
}

variable "parameter_group_description" {
  description = "Description of the DB parameter group."
  type        = string
  default     = "Tuned parameter group for the MLOps framework RDS instance."
}

variable "parameters" {
  description = <<-EOT
    List of parameters to apply to the parameter group. Each entry is
    an object with `name`, `value`, and optional `apply_method`.
  EOT
  type = list(object({
    name         = string
    value        = string
    apply_method = optional(string)
  }))
  default = [
    {
      name         = "log_min_duration_statement"
      value        = "1000"
      apply_method = "pending-reboot"
    },
    {
      name         = "max_connections"
      value        = "100"
      apply_method = "pending-reboot"
    },
  ]
}

variable "instance_class" {
  description = "RDS instance class. Free-Tier covers db.t3.micro for the first 12 months."
  type        = string
}

variable "allocated_storage_gb" {
  description = "Allocated storage in GB. Free-Tier safe default is 20."
  type        = number
}

variable "max_allocated_storage_gb" {
  description = "Storage autoscaling upper bound. Set to 0 to disable."
  type        = number
  default     = 0
}

variable "storage_type" {
  description = "Storage type. gp2 is free-tier-eligible."
  type        = string
  default     = "gp2"
}

variable "storage_encrypted" {
  description = "Whether the underlying storage is encrypted."
  type        = bool
  default     = true
}

variable "kms_key_id" {
  description = "KMS key ID for storage encryption. `null` = use the default aws/rds key (free)."
  type        = string
  default     = null
}

variable "db_name" {
  description = "Initial database created by RDS. The other two databases (mlflow, airflow) are created manually after first boot."
  type        = string
}

variable "db_username" {
  description = "Master username for the RDS instance."
  type        = string
}

variable "db_password" {
  description = "Master password for the RDS instance (sensitive). Also written to SSM via the ssm module."
  type        = string
  sensitive   = true
}

variable "db_subnet_group_name" {
  description = "DB subnet group name. Use the network module's output."
  type        = string
}

variable "vpc_security_group_ids" {
  description = "Security group IDs to attach to the RDS instance."
  type        = list(string)
}

variable "multi_az" {
  description = "Whether to enable Multi-AZ. Free-Tier safe default is false; multi-AZ bills immediately."
  type        = bool
  default     = false
}

variable "publicly_accessible" {
  description = "Whether the RDS instance is reachable from the public internet."
  type        = bool
  default     = false
}

variable "skip_final_snapshot" {
  description = "Skip the final snapshot when destroying. Free-Tier dev default true; flip false for prod."
  type        = bool
  default     = true
}

variable "copy_tags_to_snapshot" {
  description = "Copy instance tags to snapshots."
  type        = bool
  default     = true
}

variable "deletion_protection" {
  description = "Block destroy. Free-Tier dev default false; flip true for prod."
  type        = bool
  default     = false
}

variable "backup_retention_period" {
  description = "Days to retain automated backups."
  type        = number
  default     = 1
}

variable "backup_window" {
  description = "Daily time range during which automated backups are created (UTC)."
  type        = string
  default     = "07:00-09:00"
}

variable "maintenance_window" {
  description = "Weekly time range during which system maintenance can occur (UTC)."
  type        = string
  default     = "Sun:09:30-Sun:11:00"
}

variable "enabled_cloudwatch_logs_exports" {
  description = "List of log types to export to CloudWatch."
  type        = list(string)
  default     = ["postgresql", "upgrade"]
}

variable "performance_insights_enabled" {
  description = "Whether Performance Insights is enabled (extra cost). Free-Tier default false."
  type        = bool
  default     = false
}

variable "monitoring_interval" {
  description = "Enhanced monitoring interval in seconds. 0 disables (free). 1/5/10/15/30/60 require an IAM role."
  type        = number
  default     = 0
}
