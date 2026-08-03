# Terraform — MLOps Framework on AWS (ECS EC2 launch type)

This directory contains a production-ready Terraform layout that stands up
the **MLflow + Airflow + framework API + ServingBridge** stack on Amazon
ECS (EC2 launch type), backed by RDS, S3, ECR, IAM, SSM Parameter Store,
and CloudWatch Logs.

> **Not Free-Tier for compute.** The container instances run
> `t3.small` (not `t3.micro`) — see "Why not t3.micro" below for what
> happened when this stack ran on the Free-Tier-eligible instance
> type. Every other resource (RDS `db.t3.micro`, S3, ECR, no ALB/NAT)
> stays Free-Tier-safe; only the ECS compute layer costs money.

## Layout

```
infrastructure/terraform/
├── modules/                 # Reusable Terraform modules (one per concern)
│   ├── network/              # VPC + subnets + IGW + RTs + DB subnet group
│   ├── security_groups/      # sg-app, sg-rds
│   ├── s3/                   # Buckets with versioning + lifecycle
│   ├── ecr/                  # ECR repositories (mlflow, airflow, app)
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
network mode, host-port-mapped — there is no ALB in this
stack, so services publish directly on each container instance's
public IP). Tasks are placed with a memory-aware `binpack`
`ordered_placement_strategy`, which keeps contiguous free space on the
fleet for rolling-deploy replacements. Inter-service calls (e.g. `app` →
`mlflow`) go through **ECS Service Connect** — plain
`http://<service>:<port>` calls that a per-task proxy resolves and
routes correctly regardless of which container instance the target
task lands on. (Classic Cloud Map `service_registries` only supports
SRV records for bridge-mode networking, which ordinary HTTP clients
can't resolve — Service Connect is the mechanism built for this case.)

**Terraform only creates the ECR repositories — it never builds or
pushes images.** `.github/workflows/deploy.yml` builds three images —
`infrastructure/mlflow/Dockerfile`, `infrastructure/airflow/Dockerfile`
(airflow-webserver + airflow-scheduler), and
`infrastructure/app/Dockerfile` (app + serving) — and pushes them to
ECR on every push to `main`. Airflow and the framework need separate
images because Airflow 2.10.4 pins SQLAlchemy 1.4.x internally and
cannot coexist with the framework's `sqlalchemy>=2.0.0`. See **Bootstrap order** below — this is the one place
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
| 3 ECR repos | `mlflow`, `airflow` (webserver + scheduler), `app` (app + serving) |
| 3 IAM roles | EC2 instance role (ECS agent registration + SSM Session Manager), ECS task execution role, ECS task role |
| ECS cluster + EC2 capacity provider | Managed scaling disabled — schedules onto the fixed-size ASG only |
| Auto Scaling Group (`instance_count`, default 2, `t3.small`) | ECS container instances, `min=max=desired` (static fleet, not elastic) |
| Cloud Map private DNS namespace | Backs ECS Service Connect for in-cluster service discovery |
| 5 ECS task definitions + services | mlflow, airflow-webserver, airflow-scheduler, app, serving |
| 4 SSM SecureString params | DB password, Airflow Fernet key, Airflow web secret, Airflow admin password |
| 5 CloudWatch log groups | One per ECS service, 7-day retention |

**Cost:** with `instance_count = 2` on `t3.small`, running both
instances 24/7 for a full month is roughly 2 × 730 hrs ×
$0.0208/hr ≈ **$30/month** (t3.small has no Free Tier). 2 instances
is a hard floor — the five services reserve ~2570 MiB including
Service Connect sidecars, more than one t3.small's ~1913 MiB of
schedulable memory, so `instance_count = 1` leaves tasks stuck
PENDING. No ALB, no NAT Gateway, no elastic Auto Scaling policy —
those stay avoided.

### Why not t3.micro

The original design targeted `t3.micro` (Free Tier) with the 5
services' memory reservations trimmed to fit its ~916 MiB
ECS-schedulable RAM. In practice, both container instances
repeatedly went dark — their ECS *and* SSM agents stopped responding
(`agentConnected: false`, SSM `PingStatus: ConnectionLost`) while
EC2's own health checks still reported the instances as healthy
(hypervisor-level reachability only, not host memory pressure).
mlflow itself was also directly OOM-killed (`exitCode 137`) at a
300 MiB reservation even with `--workers 1`. The pattern is
consistent with the Linux OOM killer, under real memory pressure,
occasionally killing the host's own management daemons (Docker,
ECS agent, SSM agent) instead of just a container — t3.micro's 1 GB
left no real margin once every layer of overhead (kernel, Docker
daemon, ECS agent, SSM agent, 5 Service Connect proxy sidecars) was
accounted for, even though the *reported* schedulable memory number
looked sufficient on paper. `t3.small`'s 2 GB gives enough headroom
that this hasn't recurred.

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
   `ECS_CLUSTER_NAME`, `ECR_MLFLOW_REPOSITORY`, `ECR_AIRFLOW_REPOSITORY`,
   `ECR_APP_REPOSITORY`
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

## Cost control

Everything except the ECS container instances stays inside (or close
to) the Free Tier:

- RDS uses `db.t3.micro` (default), single-AZ, 20 GB — Free-Tier
  eligible for the first 12 months.
- No ALB / NAT Gateway / elastic Auto Scaling policies / CloudWatch
  detailed monitoring / Container Insights (all off by default in
  this stack).
- Keep S3 usage under 5 GB (lifecycle expires noncurrent versions at
  30 days).
- Keep CloudWatch Logs retention short (7 days, configurable via
  `ecs.log_retention_days`) to stay under the 5 GB/month ingestion
  Free Tier allowance.

The ECS compute layer (`t3.small` × 2) is the one deliberate
exception — see "Why not t3.micro" above. It cannot be trimmed to a
single instance: the stack's memory reservations exceed what one
t3.small can schedule.

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
