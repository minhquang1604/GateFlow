variable "aws_region" {
  description = <<-EOT
    AWS region to deploy resources into. Note that available RDS
    engine versions differ by region — `db_engine_version` must name
    a version offered in this region (e.g. Postgres 15.7 exists in
    us-east-1 but not ap-southeast-1, which offers 15.13+).
  EOT
  type        = string
  default     = "ap-southeast-1"
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
  description = <<-EOT
    EC2 instance type for ECS container instances. NOT Free-Tier
    (t3.small in ap-southeast-1 is ~$0.0264/hr ~= $19/month each;
    the default 2-instance fleet is ~$39/month).

    Why two t3.small rather than one larger instance: CPU is this
    stack's binding constraint, not memory. On a single
    m7i-flex.large (2 vCPU / 8 GiB) the services reserved 1984 of
    2048 CPU units — 97% committed — while 65% of the RAM sat idle,
    because general-purpose families lock a 1:4 vCPU:RAM ratio. Two
    t3.small give 4 vCPU total (4096 units) for less than half the
    cost, and ~3826 MiB of schedulable memory still comfortably
    covers the ~2696 MiB reserved.

    KNOWN RISK — t3 is burstable. Each instance accrues CPU credits
    at a fixed rate and is throttled to a 20% baseline per vCPU once
    they run out. The airflow-scheduler previously measured 185%
    sustained CPU before its DAG-parsing was tuned down; if
    sustained load returns, these instances will throttle and end up
    slower than a non-burstable type. Watch the CloudWatch
    CPUCreditBalance metric — if it trends to zero, move to a
    non-burstable type (c7i-flex.xlarge, 4 vCPU non-burstable) or
    t3.medium (same 4 vCPU across two instances, but credits accrue
    twice as fast).

    Earlier in this stack's history t3.small instances also
    repeatedly became network-wedged with dead ECS/SSM agents; that
    was under severe memory pressure which no longer applies at
    these reservations, but it is worth knowing if hosts go dark.
  EOT
  type        = string
  default     = "t3.small"
}

variable "instance_count" {
  description = <<-EOT
    Number of ECS container instances to run. 2 is required on the
    default t3.small: the full stack reserves ~2696 MiB and 1984 CPU
    units, which exceeds a single t3.small's ~1913 MiB / 2048 units.
    Setting this to 1 will leave tasks stuck PENDING forever. See
    the resource-budget comment above `services` in main.tf.

    Two instances also mean a spare host. Twice during bring-up an
    instance became network-wedged (ECS/SSM agents unreachable while
    EC2's own health checks still reported "ok"), and the second
    instance kept the stack serving until the bad one was replaced.

    Cost note: t3.small is NOT Free-Tier-eligible — roughly
    2 x 730 hrs x $0.0264/hr ~= $39/month total in ap-southeast-1.
  EOT
  type        = number
  default     = 2
}

variable "ec2_ebs_size_gb" {
  description = <<-EOT
    Root EBS volume size in GB, per instance. The ECS-optimized
    AL2023 AMI's root snapshot requires >= 30 GB; a smaller value
    fails at apply time with a "Volume of size ... is smaller than
    snapshot" error. Free Tier includes 30 GB/month of gp2/gp3
    storage total across all volumes, so the single-instance
    default sits right at that limit.
  EOT
  type        = number
  default     = 30
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

variable "db_engine_version" {
  description = <<-EOT
    PostgreSQL engine version for RDS. Must be a version offered in
    `aws_region` — this varies by region. ap-southeast-1 (Singapore)
    does not offer 15.7 (the module default); its oldest 15.x with
    db.t3.micro support is 15.13. Check with:

      aws rds describe-db-engine-versions --engine postgres \
        --region <region> --query 'DBEngineVersions[].EngineVersion'
  EOT
  type        = string
  default     = "15.13"
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
    Tag of the framework image (built from
    infrastructure/app/Dockerfile; used by app and serving) to
    deploy from ECR. Same build/push contract as `mlflow_image_tag`.
  EOT
  type        = string
  default     = "latest"
}

variable "airflow_image_tag" {
  description = <<-EOT
    Tag of the Airflow image (built from
    infrastructure/airflow/Dockerfile; used by airflow-webserver and
    airflow-scheduler) to deploy from ECR. This is a separate image
    from `app_image_tag` — Airflow 2.10.4 pins SQLAlchemy 1.4.x
    internally and cannot tolerate the framework's sqlalchemy>=2.0.0
    requirement in the same environment (see
    infrastructure/airflow/Dockerfile's header comment). Same
    build/push contract as `mlflow_image_tag`.
  EOT
  type        = string
  default     = "latest"
}
