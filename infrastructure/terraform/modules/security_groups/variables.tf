variable "name_prefix" {
  description = "Prefix used to name all security groups in this module."
  type        = string
}

variable "vpc_id" {
  description = "ID of the VPC where the security groups live."
  type        = string
}

variable "admin_cidr" {
  description = <<-EOT
    CIDR allowed to reach the EC2 instance over SSH (port 22).
    Override to your own IP (e.g. "203.0.113.7/32") to avoid exposing
    SSH to the entire internet.
  EOT
  type        = string
  default     = "0.0.0.0/0"
}

variable "ingress_cidr_internet" {
  description = "CIDR block for ingress rules opened to the public internet (0.0.0.0/0 by default)."
  type        = string
  default     = "0.0.0.0/0"
}

variable "egress_cidr" {
  description = "CIDR block used for egress rules (typically 0.0.0.0/0 = allow all)."
  type        = string
  default     = "0.0.0.0/0"
}

variable "ssh_port" {
  description = "SSH port exposed by the app sg."
  type        = number
  default     = 22
}

variable "mlflow_port" {
  description = "MLflow UI port exposed by the app sg."
  type        = number
  default     = 5000
}

variable "airflow_port" {
  description = "Airflow UI port exposed by the app sg."
  type        = number
  default     = 8080
}

variable "app_port" {
  description = "Framework FastAPI app port exposed by the app sg."
  type        = number
  default     = 8000
}

variable "serving_port" {
  description = "ServingBridge port exposed by the app sg."
  type        = number
  default     = 8001
}

variable "rds_port" {
  description = "PostgreSQL port exposed by the RDS sg."
  type        = number
  default     = 5432
}
