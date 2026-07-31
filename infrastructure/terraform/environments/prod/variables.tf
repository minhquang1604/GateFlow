variable "aws_region" {
  description = "AWS region to deploy resources into. Free-Tier eligible regions include us-east-1, us-east-2, us-west-2."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project name used for resource naming and tagging."
  type        = string
  default     = "mlops-framework"
}

variable "env" {
  description = "Environment label (e.g. dev, staging, prod). Applied as a default tag."
  type        = string
  default     = "prod"
}

variable "owner" {
  description = "Owner tag value (e.g. team or person responsible)."
  type        = string
  default     = "mlops"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC. /16 leaves room for 4 /24 subnets across 2 AZs."
  type        = string
  default     = "10.0.0.0/16"
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

variable "ec2_instance_type" {
  description = "EC2 instance type. Free Tier covers t3.micro for the first 12 months."
  type        = string
  default     = "t3.micro"
}

variable "ec2_ebs_size_gb" {
  description = "Root EBS volume size in GB. Free Tier includes 30 GB/month of gp2/gp3 storage."
  type        = number
  default     = 20
}

variable "ssh_public_key" {
  description = <<-EOT
    SSH public key (single line, e.g. contents of ~/.ssh/id_ed25519.pub)
    used to create the EC2 key pair. Pass via TF_VAR_ssh_public_key
    or terraform.tfvars (do NOT commit the private key).
  EOT
  type        = string
  default     = ""
}

variable "db_username" {
  description = "Master username for the RDS PostgreSQL instance."
  type        = string
  default     = "mlops_admin"
}

variable "db_password" {
  description = <<-EOT
    Master password for the RDS PostgreSQL instance. Must be at least
    8 characters. This value is also written to SSM Parameter Store as
    a SecureString so EC2 containers can read it. Pass via
    TF_VAR_db_password (do NOT commit).
  EOT
  type        = string
  sensitive   = true
}

variable "db_name" {
  description = "Initial database created by RDS. The other two databases (mlflow, airflow) are created manually after first boot (see README)."
  type        = string
  default     = "mlops_framework"
}

variable "db_instance_class" {
  description = "RDS instance class. Free Tier covers db.t3.micro for the first 12 months."
  type        = string
  default     = "db.t3.micro"
}

variable "db_allocated_storage_gb" {
  description = "RDS allocated storage in GB. Free Tier includes 20 GB; do not exceed without expecting charges."
  type        = number
  default     = 20
}

variable "auto_deploy" {
  description = <<-EOT
    If true, the EC2 user data script will pull the public Apache Airflow
    image from Docker Hub, run `docker compose up -d`, and execute the
    one-time Airflow DB migrate + admin user create. The operator only
    needs to SSH in to verify — no manual deploy steps required.
    Set false to keep the stack stopped until an operator runs docker
    compose manually (useful for debugging boot problems).
  EOT
  type        = bool
  default     = true
}
