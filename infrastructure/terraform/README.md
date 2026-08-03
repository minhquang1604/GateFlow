# Terraform — MLOps Framework on AWS Free Tier (ECS EC2 launch type)

This directory contains a production-ready Terraform layout that stands up
the **MLflow + Airflow + framework API + ServingBridge** stack on Amazon
ECS (EC2 launch type), backed by RDS, S3, ECR, IAM, SSM Parameter Store,
and CloudWatch Logs.

## Layout

```
infrastructure/terraform/
├── modules/                 # Reusable Terraform modules (one per concern)
│   ├── network/              # VPC + subnets + IGW + RTs + DB subnet group
│   ├── security_groups/      # sg-app, sg-rds
│   ├── s3/                   # Buckets with versioning + lifecycle
│   ├── ecr/                  # ECR repositories (mlflow, app)
│   ├── rds/                  # RDS PostgreSQL instance + parameter group
│   ├── ssm/                  # SecureString parameters + generated secrets
│   ├── iam/                  # EC2 instance role + ECS task exec/task roles
│   ├── compute/               # ECS container instance fleet (ASG + launch template)
│   └── ecs/                  # ECS cluster, capacity provider, task defs, services
└── environments/
    └── prod/                 # Production environment root (orchestration only)
        ├── main.tf
        ├── variables.tf
        ├── outputs.tf
        ├── providers.tf
        ├── versions.tf
        ├── terraform.tfvars.example
        ├── .gitignore
        └── README.md
```

Each module has the standard `main.tf / variables.tf / outputs.tf /
versions.tf / README.md` skeleton. Adding a new environment
(staging, dev) is a matter of copying `environments/prod/` and
changing `terraform.tfvars`.

## Architecture

```
network → security_groups → rds ──┐
s3 ──────────────────────────────┤
ecr (repos only) ─────────────────┤
ssm ──────────────────────────────┼──→ compute (ASG, N ECS container instances)
iam (ec2 role + ecs task roles) ──┘         │
                                             ▼
                                            ecs (cluster, capacity provider,
                                                 task defs, services)
```

**ECS, not docker-compose-on-EC2.** Container instances run the
plain ECS agent (from the AWS-published ECS-optimized AMI); MLflow,
Airflow webserver, Airflow scheduler, the framework app, and the
ServingBridge are each an ECS **service** (EC2 launch type, `bridge`
network mode, host-port-mapped — there is no ALB in this Free-Tier
stack, so services publish directly on each container instance's
public IP). Tasks spread across the fleet via an
`ordered_placement_strategy` so the stack's combined memory footprint
doesn't pile onto one instance. Inter-service calls (e.g. `app` →
`mlflow`) resolve through an AWS Cloud Map private DNS namespace
(`<service>.<project>-<env>.local`) rather than compose-style
container DNS, since a task can land on any instance in the fleet.

**Terraform only creates the ECR repositories — it never builds or
pushes images.** `.github/workflows/deploy.yml` builds
`infrastructure/mlflow/Dockerfile` and
`infrastructure/airflow/Dockerfile` (the latter is shared by
airflow-webserver, airflow-scheduler, app, and serving — same as
today's `docker-compose.yml`) and pushes them to ECR on every push to
`main`. See **Bootstrap order** below — this is the one place
`terraform apply` alone does not produce a fully healthy stack; CI has
to run once too.

## What gets created

| Resource | Purpose |
|---|---|
| VPC + 2 public + 2 private subnets + IGW + route tables | Networking |
| DB subnet group | Hosts the RDS instance in private subnets |
| 2 security groups | `sg-app` (opened directly to the internet on the 4 app ports + SSH from `admin_cidr`), `sg-rds` |
| 3 S3 buckets | `mlflow-artifacts`, `airflow-logs`, `app-backups` |
| 1 RDS PostgreSQL 15 (`db.t3.micro`, 20 GB) | Shared metadata DB (mlflow/airflow databases self-create on first container boot) |
| 2 ECR repos | `mlflow`, `app` (airflow/app/serving reuse `app`) |
| 3 IAM roles | EC2 instance role (ECS agent registration + SSM Session Manager), ECS task execution role, ECS task role |
| ECS cluster + EC2 capacity provider | Managed scaling disabled — schedules onto the fixed-size ASG only |
| Auto Scaling Group (`instance_count`, default 2) | ECS container instances, `min=max=desired` (static fleet, not elastic) |
| Cloud Map private DNS namespace | In-VPC service discovery |
| 5 ECS task definitions + services | mlflow, airflow-webserver, airflow-scheduler, app, serving |
| 4 SSM SecureString params | DB password, Airflow Fernet key, Airflow web secret, Airflow admin password |
| 5 CloudWatch log groups | One per ECS service, 7-day retention |

**Free-Tier caveat:** with the default `instance_count = 2`, running
both t3.micro instances 24/7 for a full month is ~1460 instance-hours
against the shared 750-hour/month Free Tier pool — roughly $7-8/month
of on-demand overage. Set `instance_count = 1` to stay strictly
inside Free Tier (some services may be unschedulable on a single 1 GB
instance). No ALB, no NAT Gateway, no elastic Auto Scaling policy.

## Prerequisites

1. **AWS CLI v2** with an account whose IAM user has at least
   `AmazonEC2FullAccess`, `AmazonRDSFullAccess`, `AmazonS3FullAccess`,
   `AmazonEC2ContainerRegistryFullAccess`, `AmazonECS_FullAccess`,
   `AmazonVPCFullAccess`, `IAMFullAccess`, `AmazonSSMFullAccess`,
   `AWSCloudMapFullAccess`. For production, narrow this.
2. **Terraform ≥ 1.5**.
3. **An SSH key pair** (optional — only needed if you want to SSH
   into a container instance directly instead of using SSM Session
   Manager):
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/mlops-framework -N ""
   cat ~/.ssh/mlops-framework.pub   # paste this into ssh_public_key
   ```
4. **GitHub Actions configured** (for the image build/push pipeline —
   see below) with repo secrets `AWS_ACCESS_KEY_ID` /
   `AWS_SECRET_ACCESS_KEY` and repo variables `AWS_REGION`,
   `ECS_CLUSTER_NAME`, `ECR_MLFLOW_REPOSITORY`, `ECR_APP_REPOSITORY`
   (values come from `terraform output` after the first apply).

## Bootstrap order

`terraform apply` alone is not enough for a fully healthy stack on
the very first deploy, because Terraform deliberately never builds or
pushes container images:

```bash
cd infrastructure/terraform/environments/prod
cp terraform.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars

terraform init
terraform fmt -recursive
terraform validate
terraform plan -var-file=terraform.tfvars
terraform apply -var-file=terraform.tfvars

# ECS services now exist, pointed at ":latest" images that don't
# exist in ECR yet — tasks show CannotPullContainerError until CI runs.

# Populate the GitHub Actions repo variables from terraform output:
terraform output ecs_cluster_name
terraform output ecr_repositories

# Trigger the image build/push pipeline (or just push to main):
gh workflow run deploy.yml

# Watch tasks go healthy:
aws ecs describe-services \
  --cluster "$(terraform output -raw ecs_cluster_name)" \
  --services mlflow airflow-webserver airflow-scheduler app serving
```

After the first successful CI run, every subsequent `git push` to
`main` (that touches `infrastructure/mlflow/**`,
`infrastructure/airflow/**`, `src/**`, or `pyproject.toml`) rebuilds
and redeploys automatically — no further `terraform apply` needed
unless infrastructure itself changes.

## After apply

```bash
# Show outputs
terraform output

# Current container instance public IPs (recomputed on every plan/apply,
# since there's no stable EIP in this stack)
terraform output ec2_public_ips

# Reach a service directly (bridge-mode host-port mapping):
curl http://<instance-ip>:5000/            # MLflow
curl http://<instance-ip>:8080/health      # Airflow
curl http://<instance-ip>:8000/            # Framework app
curl http://<instance-ip>:8001/healthz     # ServingBridge

# SSM Session Manager (no SSH key needed):
aws ssm start-session --target $(terraform output -json ec2_instance_ids | jq -r '.[0]')
```

## Verification

| Check | Command |
|---|---|
| RDS reachable | `psql -h $(terraform output -raw rds_address) -U mlops_admin -d postgres -c '\l'` |
| ECS services healthy | `aws ecs describe-services --cluster $(terraform output -raw ecs_cluster_name) --services mlflow airflow-webserver airflow-scheduler app serving` |
| Container instances registered | `aws ecs list-container-instances --cluster $(terraform output -raw ecs_cluster_name)` |
| S3 accessible | `aws s3 ls s3://$(terraform output -raw s3_mlflow_artifacts_bucket)` |
| ECR has images | `aws ecr list-images --repository-name $(terraform output -json ecr_repositories | jq -r .mlflow | cut -d/ -f2)` |
| Service reachable | `curl http://<instance-ip>:<port>/...` (see After apply) |

## Free-Tier safety

The configuration stays close to the Free Tier provided you:

- Use `t3.micro` (default) and `db.t3.micro` (default), single-AZ, 20 GB.
- Set `instance_count = 1` if you want to stay strictly inside the
  750 EC2 instance-hour/month pool (default is 2, ~$7-8/month
  overage — see "What gets created" above).
- Do not enable ALB / NAT Gateway / elastic Auto Scaling policies /
  CloudWatch detailed monitoring / Container Insights (all off by
  default in this stack).
- Keep S3 usage under 5 GB (lifecycle expires noncurrent versions at
  30 days).
- Keep CloudWatch Logs retention short (7 days, configurable via
  `ecs.log_retention_days`) to stay under the 5 GB/month ingestion
  Free Tier allowance.

## Adding a new environment

```bash
cp -r environments/prod environments/staging
cd environments/staging
$EDITOR terraform.tfvars
terraform init
terraform apply -var-file=terraform.tfvars
```

Each environment is fully isolated in its own state file.

## Cleanup

```bash
cd infrastructure/terraform/environments/prod
terraform destroy -var-file=terraform.tfvars
```

This deletes every resource created by this module, including the
RDS instance (`skip_final_snapshot = true`). The S3 buckets and ECR
repos survive only if they were empty at destroy time — empty them
first for a clean teardown.
