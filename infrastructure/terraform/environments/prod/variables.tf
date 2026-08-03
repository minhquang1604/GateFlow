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

variable "instance_count" {
  description = <<-EOT
    Number of ECS container instances (t3.micro each) to run. Default
    2 so the full stack (MLflow, Airflow webserver + scheduler, app,
    serving) fits across the fleet's combined ~2 GB RAM rather than
    competing for one instance's 1 GB.

    Cost note: 750 EC2 instance-hours/month are Free Tier, shared
    across ALL running instances. Two t3.micro instances running 24/7
    for a full month is ~1460 instance-hours — about 710 hours over
    the Free Tier pool, or roughly $7-8/month at on-demand pricing.
    Set this to 1 to stay strictly inside the Free Tier; not every
    service may be schedulable on a single 1 GB instance in that case.
  EOT
  type        = number
  default     = 2
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
  description = "Initial database created by RDS for the framework app/serving tasks. The mlflow and airflow databases are created automatically on first container start by their own entrypoints (idempotent CREATE DATABASE IF NOT EXISTS)."
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

variable "mlflow_image_tag" {
  description = <<-EOT
    Tag of the mlflow image (built from infrastructure/mlflow/Dockerfile)
    to deploy from ECR. Terraform never builds or pushes this image —
    .github/workflows/deploy.yml does, tagging both `:latest` and
    `:<git-sha>`. `terraform apply` creates the ECS service pointed at
    this tag; if the tag doesn't exist in ECR yet, the task shows
    CannotPullContainerError until CI runs (see the root README for
    the expected bootstrap order).
  EOT
  type        = string
  default     = "latest"
}

variable "app_image_tag" {
  description = <<-EOT
    Tag of the shared framework image (built from
    infrastructure/airflow/Dockerfile; used by airflow-webserver,
    airflow-scheduler, app, and serving) to deploy from ECR. Same
    build/push contract as `mlflow_image_tag`.
  EOT
  type        = string
  default     = "latest"
}
