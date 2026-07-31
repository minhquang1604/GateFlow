# Production environment

This directory is the **only** place where concrete resource arguments
get passed. Every other file is a module that takes inputs and returns
outputs.

The orchestration in `main.tf` wires 8 modules together:

```
network → security_groups → rds ──┐
s3 ──────────────────────────────┤
ecr ──────────────────────────────┤
ssm ──────────────────────────────┼──→ compute
iam ← (s3, ecr, ssm) ─────────────┘
```

`compute` is the only module that depends on every other — it pulls
the bucket name, ECR URIs, RDS endpoint, and SSM prefix from the rest
and feeds them to the userdata script.

## Quickstart

```bash
cd environments/prod
cp terraform.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars

terraform init
terraform fmt -recursive
terraform validate
terraform plan -var-file=terraform.tfvars
terraform apply -var-file=terraform.tfvars
```

## Adding a new environment

Copy this directory:

```bash
cp -r environments/prod environments/staging
cd environments/staging

# Edit variables.tf / terraform.tfvars to point at staging
# (or override at apply time: -var=env=staging)
terraform init
terraform apply -var-file=terraform.tfvars
```

Each environment is fully isolated in its own state file (none of the
modules share state).

## Outputs

| Output | Description |
|---|---|
| `rds_endpoint` | RDS endpoint (host:port) |
| `rds_address` | RDS hostname |
| `s3_mlflow_artifacts_bucket` | MLflow S3 bucket |
| `s3_airflow_logs_bucket` | Airflow log bucket |
| `s3_app_backups_bucket` | Backup bucket |
| `ecr_repositories` | ECR URI map |
| `ec2_public_ip` | Elastic IP |
| `ec2_instance_id` | Instance ID |
| `ssh_command` | `ssh -i ~/.ssh/<keyfile> ec2-user@<ip>` |
| `ssm_parameters` | Map of parameter suffix → full name |
| `vpc_id` | VPC ID |
| `public_subnet_ids` | List of public subnet IDs |
| `private_subnet_ids` | List of private subnet IDs |

## State migration from the legacy flat layout

If you're upgrading from the previous flat layout (`*.tf` files at the
repo root), the resource addresses change because everything moved into
modules. The cleanest path is:

```bash
# 1. From the OLD root:
cd infrastructure/terraform
terraform destroy -var-file=terraform.tfvars

# 2. Copy your tfvars to the new location:
cp terraform.tfvars environments/prod/terraform.tfvars

# 3. From the NEW root:
cd environments/prod
terraform init
terraform plan -var-file=terraform.tfvars
terraform apply -var-file=terraform.tfvars
```

The old `terraform.tfstate` and `terraform.tfstate.backup` files become
invalid and can be deleted (they're already in `.gitignore`).
