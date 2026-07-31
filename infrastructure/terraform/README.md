# Terraform — MLOps Framework on AWS Free Tier

This directory contains a production-ready Terraform layout that stands up
the **MLflow + Airflow + framework API + ServingBridge** stack on a single
EC2 instance backed by RDS, S3, ECR, IAM, and SSM Parameter Store.

> **Status:** Phase 1–4 of `docs/aws-deployment-plan.md` (VPC, RDS,
> S3, ECR, IAM, EC2, EIP, SSM). The ALB, ACM cert, CloudWatch
> alarms, and CI/CD are added in later phases and live in follow-up
> PRs.

## Layout

```
infrastructure/terraform/
├── modules/                 # Reusable Terraform modules (one per concern)
│   ├── network/             # VPC + subnets + IGW + RTs + DB subnet group
│   ├── security_groups/     # sg-alb, sg-app, sg-rds
│   ├── s3/                  # Buckets with versioning + lifecycle
│   ├── ecr/                 # ECR repositories
│   ├── rds/                 # RDS PostgreSQL instance + parameter group
│   ├── ssm/                 # SecureString parameters + generated secrets
│   ├── iam/                 # Instance role + scoped inline policies
│   └── compute/             # EC2 + EIP + keypair + userdata template
└── environments/
    └── prod/                # Production environment root (orchestration only)
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

## What gets created

| Resource | Purpose |
|---|---|
| VPC + 2 public + 2 private subnets + IGW + route tables | Networking |
| DB subnet group | Hosts the RDS instance in private subnets |
| 3 security groups | `sg-alb`, `sg-app`, `sg-rds` |
| 3 S3 buckets | `mlflow-artifacts`, `airflow-logs`, `app-backups` |
| 1 RDS PostgreSQL 15 (`db.t3.micro`, 20 GB) | Shared metadata DB |
| 2 ECR repos | `mlflow`, `app` (serving reuses `app`) |
| IAM role + instance profile | ECR pull, S3 RW scoped, SSM read, CloudWatch agent, SSM Session Manager |
| EC2 key pair | From `var.ssh_public_key` |
| EC2 t3.micro + gp3 20 GB | Docker host running the full stack |
| Elastic IP | Stable public IP for SSH + future ALB target |
| 4 SSM SecureString params | DB password, Airflow Fernet key, Airflow web secret, Airflow admin password |

Total: ~25–30 resources. **Free-Tier safe** (`db.t3.micro` single-AZ,
storage ≤ 20 GB, no NAT, no ALB, no MWAA/EKS/Fargate).

## Prerequisites

1. **AWS CLI v2** with an account whose IAM user has at least
   `AmazonEC2FullAccess`, `AmazonRDSFullAccess`, `AmazonS3FullAccess`,
   `AmazonEC2ContainerRegistryFullAccess`,
   `AmazonVPCFullAccess`, `IAMFullAccess`,
   `AmazonSSMFullAccess`. For production, narrow this.
2. **Terraform ≥ 1.5** (`brew install terraform` or download from
   developer.hashicorp.com).
3. **An SSH key pair** — generate it locally and paste the *public*
   half into `terraform.tfvars`:
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/mlops-framework -N ""
   cat ~/.ssh/mlops-framework.pub   # paste this into ssh_public_key
   ```

## Bootstrap

```bash
cd infrastructure/terraform/environments/prod

# 1. Copy the example vars and fill in your SSH public key + DB password.
cp terraform.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars

# 2. Initialise (local state, no S3 backend yet).
terraform init

# 3. Format + validate.
terraform fmt -recursive
terraform validate

# 4. Plan (read-only).
terraform plan -var-file=terraform.tfvars

# 5. Apply.
terraform apply -var-file=terraform.tfvars
```

> **First apply takes ~5–10 minutes** because RDS is the slowest
> resource. Everything else finishes in seconds.

## After apply

```bash
# Show outputs
terraform output

# Copy the public IP and SSH in
ssh -i ~/.ssh/mlops-framework ec2-user@$(terraform output -raw ec2_public_ip)
```

The EC2 user data script (in `modules/compute/userdata/ec2_init.sh.tftpl`):

- Installs Docker + AWS CLI v2 + psql.
- Pulls the 4 SSM secrets into `/opt/mlops/.env` (chmod 600).
- Creates the `mlflow` and `airflow` databases on the RDS instance.
- Renders a starter `docker-compose.aws.yml` referencing the ECR URIs.
- If `auto_deploy = true`, runs `docker compose up -d` and the one-time
  Airflow `db migrate` + `users create`.

**Operator steps (run as `ec2-user` on the box):**

```bash
cd /opt/mlops

# 1. Log in to ECR so docker can pull images.
aws ecr get-login-password --region us-east-1 | \
    docker login --username AWS --password-stdin "$(grep ECR_REGISTRY .env | cut -d= -f2)"

# 2. Push your custom images to ECR (one-time, from your laptop):
#    docker build -f infrastructure/mlflow/Dockerfile -t mlops-framework/mlflow:aws-1.0.0 .
#    docker push <ecr_mlflow_repo_uri>:aws-1.0.0
#    ... repeat for the `app` image.

# 3. (Optional) If auto_deploy was false, start the stack manually:
docker compose -f docker-compose.aws.yml pull
docker compose -f docker-compose.aws.yml up -d

# 4. Run the one-time Airflow DB migrate and admin user create.
docker compose -f docker-compose.aws.yml run --rm airflow-webserver \
    bash -c "airflow db migrate && \
             airflow users create --username admin \
             --password \"\$AIRFLOW_ADMIN_PASSWORD\" \
             --firstname Admin --lastname User --role Admin --email admin@example.com"
```

## Verification

| Check | Command |
|---|---|
| RDS reachable | `psql -h $(terraform output -raw rds_address) -U mlops_admin -d postgres -c '\l'` (3 DBs) |
| EC2 reachable | `ssh -i ~/.ssh/mlops-framework ec2-user@$(terraform output -raw ec2_public_ip)` |
| Docker installed | `docker --version` on EC2 |
| IAM role works | `aws sts get-caller-identity` on EC2 (returns instance role) |
| S3 accessible | `aws s3 ls s3://$(terraform output -raw s3_mlflow_artifacts_bucket)` from EC2 |
| ECR pull works | `docker pull <ecr_uri>/mlflow:latest` (after push) |

## Free-Tier safety

The configuration stays inside the Free Tier provided you:

- Use `t3.micro` (default).
- Use `db.t3.micro` (default), single-AZ, 20 GB.
- Do not enable ALB / NAT Gateway / CloudWatch detailed monitoring.
- Keep S3 usage under 5 GB (lifecycle expires noncurrent versions
  at 30 days).
- Stay under 750 instance-hours/month on EC2 and RDS (don't run
  multiple instances).

If you go above these limits, the file `docs/aws-deployment-plan.md`
section 4 documents the expected monthly bill.

## State migration from the legacy flat layout

If you previously deployed from the old flat-file layout (`.tf` files
at this directory's root), the resource addresses changed because
everything moved into modules. The cleanest path:

```bash
# 1. From the OLD root (the directory this README replaces):
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

## Adding a new environment

```bash
cp -r environments/prod environments/staging

cd environments/staging
$EDITOR terraform.tfvars

terraform init
terraform apply -var-file=terraform.tfvars
```

Each environment is fully isolated in its own state file. To override
just `env` for testing:

```bash
terraform apply -var-file=terraform.tfvars -var=env=staging
```

## Out of scope (next phases)

- **Phase 5** — ALB + ACM cert + Route 53 + HTTPS routing.
- **Phase 6** — CloudWatch alarms (10 free), logs, dashboards.
- **Phase 7** — GitHub Actions deploy workflow + image build + ECR push.
- **Phase 8** — Runbook + DR drill.

Until those land, the stack is reachable only through the EC2
public IP and SSH.

## Cleanup

```bash
cd infrastructure/terraform/environments/prod
terraform destroy -var-file=terraform.tfvars
```

This deletes every resource created by this module, including the
RDS instance (because `skip_final_snapshot = true` is set). The
S3 buckets and ECR repos survive only if they were empty at destroy
time — empty them first if you want a clean teardown.
