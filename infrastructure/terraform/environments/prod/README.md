# Production environment

This directory is the **only** place where concrete resource arguments
get passed. Every other file is a module that takes inputs and returns
outputs.

The orchestration in `main.tf` wires 9 modules together:

```
network → security_groups → rds ──┐
s3 ──────────────────────────────┤
ecr (repos only) ─────────────────┤
ssm ──────────────────────────────┼──→ compute (ASG of N ECS container instances)
iam (ec2 role + ecs task roles) ──┘         │
                                             ▼
                                            ecs (cluster, capacity provider,
                                                 task defs, services)
```

`compute` creates the fleet of EC2 container instances (ECS-optimized
AMI, fixed-size ASG). `ecs` creates the cluster/capacity
provider/Cloud Map namespace/task definitions/services and pulls the
RDS endpoint, S3 bucket name, ECR image URIs, and SSM parameter ARNs
from the other modules to build each service's container definition.

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

**Then** populate the GitHub Actions repo secrets/variables (see the
root `infrastructure/terraform/README.md`'s "Bootstrap order" section)
and run `.github/workflows/deploy.yml` — ECS services are created
pointed at ECR tags that don't exist until CI pushes them.

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
| `ecr_repositories` | ECR URI map (`mlflow`, `app`) |
| `ecs_cluster_name` | ECS cluster name (for `aws ecs` commands + CI repo variable) |
| `ecs_service_names` | Map of service key -> ECS service name |
| `ec2_instance_ids` | Current container instance IDs (list) |
| `ec2_public_ips` | Current container instance public IPs (list; not stable across replacement) |
| `ssh_commands` | One SSH command per current instance (empty list without a key pair) |
| `ssm_parameters` | Map of parameter suffix → full name |
| `vpc_id` | VPC ID |
| `public_subnet_ids` | List of public subnet IDs |
| `private_subnet_ids` | List of private subnet IDs |
| `deploy_instructions` | What to do if ECS tasks are stuck pulling images |

## Key variables

| Variable | Default | Description |
|---|---|---|
| `instance_count` | `2` | Fixed ASG size; set to `1` to roughly halve the ~$30/month `t3.small` cost |
| `ec2_instance_type` | `"t3.small"` | Not Free-Tier — `t3.micro` proved too memory-constrained in practice, see root README's "Why not t3.micro" |
| `mlflow_image_tag` / `app_image_tag` | `"latest"` | ECR tags ECS services deploy; CI pushes both `:latest` and `:<git-sha>` |
| `admin_cidr` | `"0.0.0.0/0"` | Restrict to your IP to avoid exposing SSH publicly |
| `db_password` | *(required, no default)* | Pass via `TF_VAR_db_password`; never commit |

See `variables.tf` for the full list.
