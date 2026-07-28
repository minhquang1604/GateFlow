# Kế hoạch triển khai MLOps Framework lên AWS (Free Tier)

> Ngày lập: 2026-07-28
> Phạm vi: deploy **MLflow tracking server** + **Airflow** (cùng các thành phần phụ thuộc: Postgres, S3, framework API, ServingBridge, demo runner) cho môi trường production 1 account AWS, **ưu tiên dịch vụ nằm trong AWS Free Tier** vì ràng buộc ngân sách.

## 1. Tổng quan stack hiện tại (local)

File `docker-compose.yml` hiện chạy các service sau trong bridge network `mlops_network`:

| Service | Image | Cổng | Phụ thuộc |
|---|---|---|---|
| `postgres` | `postgres:15-alpine` | 5432 | — |
| `minio` + `minio-init` | `minio/minio` + `minio/mc` | 9000/9001 | — |
| `mlflow` | custom (Python 3.11 + MLflow 2.20.3) | 5000 | postgres, minio |
| `airflow-common` / `airflow-webserver` / `airflow-scheduler` | custom Airflow 2.10.4 | 8080 | postgres |
| `app` (FastAPI mgmt + UI) | custom Airflow image (reused) | 8000 | postgres, mlflow, airflow-webserver, serving |
| `serving` (ServingBridge) | custom | 8001 | postgres |
| `demo` (one-shot) | custom | — | tất cả |

Backend MLflow = Postgres `mlflow` DB; artifact root = MinIO bucket `mlflow-artifacts`. Toàn bộ framework code nằm trong `src/mlops_framework/` (đã được COPY vào image Airflow/MLflow qua Dockerfile tại `infrastructure/`).

## 2. Ràng buộc & nguyên tắc thiết kế

1. **Free Tier 12 tháng + luôn miễn phí** — tránh MWAA, EKS, ECS Fargate (không có Free Tier hoặc tốn phí control plane). Chọn EC2 t2.micro/t3.micro + Docker cho application runtime.
2. **Đổi MinIO → S3** — Free Tier S3 5 GB. Artifact root đổi từ `http://minio:9000` sang S3 thật, bỏ `MLFLOW_S3_ENDPOINT_URL`.
3. **Đổi Postgres container → RDS db.t3.micro PostgreSQL** — Free Tier 20 GB. Tạo 3 database: `mlops_framework`, `mlflow`, `airflow`.
4. **Statefulness** — Dùng EBS `gp3` ~20 GB cho EC2 (free tier 30 GB), tránh phụ thuộc ổ đĩa instance store.
5. **Single AZ** để tiết kiệm (EC2 t2.micro + RDS db.t3.micro single-AZ). Multi-AZ sẽ tốn thêm và không thuộc Free Tier RDS.
6. **Không dùng Secrets Manager** (tính phí $0.40/secret/tháng). Thay bằng SSM Parameter Store SecureString (miễn phí cho standard params).
7. **CI/CD thủ công + script** — Free Tier CodePipeline/CodeBuild có giới hạn nhưng vẫn tốn. Ở phase đầu dùng `docker build` + `docker push` lên ECR (500 MB free) + deploy qua SSH.
8. **Domain/TLS**: dùng subdomain của CloudFront + ACM chỉ khi cần HTTPS. Ở phase đầu, dùng ALB với ACM cert (ACM cert miễn phí).

## 3. Kiến trúc mục tiêu

```
                ┌──────────────────────────────────────────────────────┐
                │                    VPC (10.0.0.0/16)                 │
                │                                                      │
                │   ┌────────────────────────┐   ┌─────────────────┐  │
                │   │ Public Subnet          │   │ Public Subnet   │  │
                │   │ 10.0.1.0/24 (AZ-a)     │   │ 10.0.2.0/24     │  │
                │   │                        │   │ (AZ-b reserved) │  │
                │   │  ┌──────────────────┐  │   │                 │  │
                │   │  │ ALB (public)     │──┼───┼── Internet GW   │  │
                │   │  └──────────────────┘  │   │                 │  │
                │   │  ┌──────────────────┐  │   │  ┌────────────┐ │  │
                │   │  │ NAT Gateway ❌   │  │   │  │ Bastion EC2 │ │  │
                │   │  │ (không dùng)     │  │   │  │ t3.micro   │ │  │
                │   │  └──────────────────┘  │   │  └────────────┘ │  │
                │   └────────────────────────┘   └─────────────────┘  │
                │                                                      │
                │   ┌────────────────────────┐                         │
                │   │ Private Subnet         │                         │
                │   │ 10.0.10.0/24 (AZ-a)    │                         │
                │   │                        │                         │
                │   │  ┌──────────────────┐  │                         │
                │   │  │ App EC2 t3.micro │  │                         │
                │   │  │ - Docker:        │  │                         │
                │   │  │   mlflow         │  │                         │
                │   │  │   airflow-web    │  │                         │
                │   │  │   airflow-sched  │  │                         │
                │   │  │   app (api+ui)   │  │                         │
                │   │  │   serving        │  │                         │
                │   │  └──────────────────┘  │                         │
                │   └────────────────────────┘                         │
                │                                                      │
                │   ┌────────────────────────┐   ┌─────────────────┐  │
                │   │ RDS Subnet Group      │   │ S3 buckets      │  │
                │   │ (private, 2 AZ)        │   │ mlflow-artifacts│  │
                │   │  ┌──────────────────┐  │   │ mlops-app-data  │  │
                │   │  │ RDS db.t3.micro  │  │   └─────────────────┘  │
                │   │  │ PostgreSQL 15   │──┼─── IAM (task role)     │
                │   │  └──────────────────┘  │                         │
                │   └────────────────────────┘                         │
                │                                                      │
                │   CloudWatch Logs (free 5 GB) + Alarms (free 10)    │
                │   ECR (free 500 MB)                                   │
                └──────────────────────────────────────────────────────┘
```

**Lưu ý:** EC2 t3.micro nằm private subnet sẽ cần NAT GW để pull image từ ECR — NAT GW không có Free Tier (~$0.045/giờ + data). Hai lựa chọn:

- **A.** Đặt EC2 ở **public subnet** (có public IP + Elastic IP), mở Security Group chỉ cho phép ALB → app, SSH từ bastion hoặc IP của bạn. Đơn giản, không tốn NAT.
- **B.** EC2 private + NAT GW. Tốn thêm ~$33/tháng. **Không khuyến nghị** cho Free Tier.

→ **Chọn A**: EC2 ở public subnet với Security Group chặt, không cần NAT GW.

## 4. Dịch vụ AWS sử dụng (tất cả nằm trong Free Tier nếu dùng đúng hạn mức)

| Dịch vụ | Cấu hình Free Tier | Vai trò | Chi phí ngoài Free Tier |
|---|---|---|---|
| EC2 t3.micro | 750 giờ/tháng × 12 tháng | App server (MLflow + Airflow + framework API + serving) | ~$0.0104/giờ sau 12 tháng |
| EBS gp3 20 GB | 30 GB/tháng × 12 tháng | Volume cho EC2 | ~$0.08/GB-tháng |
| RDS db.t3.micro PostgreSQL 15 | 750 giờ/tháng + 20 GB × 12 tháng | Backend store cho cả framework, MLflow, Airflow | ~$0.018/giờ sau 12 tháng |
| S3 Standard | 5 GB + 20k GET + 2k PUT/tháng × 12 tháng | Artifact root cho MLflow, log archive, backup | $0.023/GB-tháng |
| ALB | 750 giờ LCU × 12 tháng | Reverse proxy cho MLflow (5000) + Airflow (8080) + App (8000) + Serving (8001) | ~$0.0225/giờ |
| ECR | 500 MB storage/tháng (luôn free) | Lưu image Docker | $0.10/GB-tháng |
| CloudWatch Logs | 5 GB ingestion + 5 GB archive (luôn free) | Logs container, RDS | $0.50/GB |
| CloudWatch Metrics | 10 metrics (luôn free) | CPU, memory, RDS | $0.30/metric |
| CloudWatch Alarms | 10 alarms (luôn free) | Cảnh báo | $0.10/alarm |
| SSM Parameter Store | Standard params miễn phí | Secrets, DB URLs | $0.05/secret advanced |
| IAM | Free | Roles, policies | — |
| ACM | Cert TLS miễn phí | HTTPS cho ALB | — |
| Data transfer out | 100 GB/tháng × 12 tháng | Egress | $0.09/GB |

**Tổng ước tính (nằm trong Free Tier):** $0/tháng trong 12 tháng đầu nếu giữ usage đúng hạn mức.

**Sau 12 tháng (1 instance single-AZ, RDS single-AZ, ALB chạy 24/7):**
- EC2 t3.micro 730h: ~$7.6
- RDS db.t3.micro 730h + 20 GB: ~$13.1
- ALB 730h: ~$16.4
- EBS 20 GB: ~$1.6
- S3 5 GB: ~$0.12
- CloudWatch logs/metrics: ~$2
- **Tổng ≈ $41/tháng** (single-AZ, 1 instance, không NAT).

> Lưu ý Free Tier: RDS db.t3.micro **chỉ free nếu dùng engine db.t3.micro, single-AZ, dưới 20 GB, dưới 750 giờ/tháng**. Nếu chọn db.t4g.micro hay multi-AZ sẽ bị tính phí ngay.

## 5. Phases triển khai

### Phase 0 — Chuẩn bị tài khoản & quyền (½ ngày)

- Tạo IAM user `mlops-deployer` với policy `AdministratorAccess` (chỉ dùng cho setup; sau đó thu hẹp lại).
- Cài AWS CLI v2, cấu hình profile `mlops-prod` (`aws configure sso` nếu dùng Identity Center).
- Tạo key pair `mlops-keypair` ở `us-east-1`, lưu `.pem` ở local (chmod 600).
- Cài Docker locally, đăng nhập ECR (`aws ecr get-login-password | docker login ...`).
- Đăng ký domain (tùy chọn) hoặc dùng DNS của CloudFront/Route 53.

**Checklist**:
- [ ] `aws sts get-caller-identity` trả về account ID hợp lệ
- [ ] ECR repo `mlops-framework` đã tạo (private)

### Phase 1 — Hạ tầng nền tảng (1–2 ngày)

Thực hiện thủ công hoặc viết Terraform đơn giản (khuyến nghị Terraform từ đầu để reproducible).

**Tài nguyên cần tạo:**

1. **VPC** `mlops-vpc` 10.0.0.0/16
2. **Subnets:**
   - `mlops-public-1a` 10.0.1.0/24 (AZ-a, dùng cho ALB + EC2 public)
   - `mlops-public-1b` 10.0.2.0/24 (AZ-b, cho ALB HA)
   - `mlops-private-db-1a` 10.0.10.0/24 (RDS subnet group)
   - `mlops-private-db-1b` 10.0.11.0/24 (RDS subnet group, yêu cầu ≥2 AZ)
3. **Internet Gateway** `mlops-igw` gắn vào VPC.
4. **Route table** public: route 0.0.0.0/0 → IGW, gắn cho cả 2 public subnets.
5. **Route table** private: local only (RDS chỉ cần truy cập nội bộ VPC).
6. **Security Groups:**
   - `sg-alb`: in 80, 443 từ `0.0.0.0/0`; out all.
   - `sg-app`: in 8000, 5000, 8080, 8001 từ `sg-alb`; in 22 từ IP của bạn (SSH); out all.
   - `sg-rds`: in 5432 từ `sg-app`; out all.
7. **S3 buckets** (tên globally unique):
   - `mlops-mlflow-artifacts-<account-id>` — artifact root MLflow, block public access, versioned, lifecycle rule (expire noncurrent sau 30 ngày).
   - `mlops-airflow-logs-<account-id>` — copy log Airflow để giảm phụ thuộc EBS.
   - `mlops-backups-<account-id>` — daily snapshot của RDS (lifecycle 30 ngày).
8. **RDS Subnet Group** `mlops-rds-subnets` dùng 2 private subnets.
9. **RDS Parameter Group** `mlops-pg15` (tweaks: `log_statement=none`, `shared_buffers` mặc định, `max_connections=100`).
10. **RDS PostgreSQL 15** `mlops-postgres`:
    - Instance class `db.t3.micro`
    - Allocated storage 20 GB gp2
    - DB name `mlops_framework` (mặc định), sẽ tạo thêm `mlflow`, `airflow` sau khi boot.
    - Master user `mlops_admin`
    - **Không** bật Multi-AZ, **không** bật Performance Insights (tốn thêm).
    - Backup retention 7 ngày (free).
    - Encryption at rest on.
    - Vào RDS → tạo DB `mlflow` và DB `airflow` (`CREATE DATABASE mlflow; CREATE DATABASE airflow;`).

> **Cảnh báo Free Tier:** đừng chọn db.t4g.micro, đừng bật Multi-AZ, đừng tăng storage quá 20 GB.

**Checklist**:
- [ ] `aws ec2 describe-vpcs` thấy `mlops-vpc`
- [ ] RDS endpoint reachable từ máy local: `psql -h <endpoint> -U mlops_admin -d postgres -c '\l'` thấy 3 database
- [ ] S3 buckets đã tạo, block public access on
- [ ] ALB tạm thời chưa cần — tạo ở Phase 3

### Phase 2 — Build & push image (1 ngày)

1. **Build MLflow image** (tận dụng `infrastructure/mlflow/Dockerfile`):
   - Tweak `infrastructure/mlflow/entrypoint.sh` để default artifact root trỏ S3 thật:
     - `ARTIFACT_ROOT="s3://${MLFLOW_BUCKET}"` (giữ nguyên, đã là `s3://${MLFLOW_BUCKET}`).
     - Xóa/bypass các đoạn `MLFLOW_S3_ENDPOINT_URL` (vì giờ là AWS S3 thật).
   - Tạo `infrastructure/mlflow/entrypoint.aws.sh` chuyên biệt cho AWS (dùng IAM role, không hardcode key).
2. **Build Airflow image** (tận dụng `infrastructure/airflow/Dockerfile`):
   - Không cần thay đổi gì lớn; DAG `infrastructure/airflow/dags/mlops_training_pipeline.py` đã đọc config từ env.
   - Cân nhắc thêm `--executor CeleryExecutor` hoặc giữ `LocalExecutor` (Free Tier 1 instance → LocalExecutor là đủ).
3. **Build image framework app** (FastAPI mgmt + UI + serving):
   - Tạo `infrastructure/app/Dockerfile` riêng cho service `app` và `serving` (tách khỏi Airflow image, image gốc là apache/airflow rất nặng ~1.5 GB, có thể optimize bằng cách base trên `python:3.11-slim`).
4. **Push lên ECR:**
   - Tag và push:
     ```
     aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin <account>.dkr.ecr.us-east-1.amazonaws.com
     docker build -f infrastructure/mlflow/Dockerfile -t mlops-framework/mlflow:aws-1.0.0 .
     docker push <account>.dkr.ecr.us-east-1.amazonaws.com/mlops-framework/mlflow:aws-1.0.0
     ```
   - Tương tự cho `app`, `serving`, `airflow`.
   - **Lưu ý:** 500 MB ECR free, image Airflow ~1.5 GB → chỉ chứa được 1 image lớn. Hai lựa chọn:
     - **a)** Dùng từng image qua public registry (apache/airflow pull trực tiếp từ Docker Hub lúc run) → chỉ cần push image `app`/`serving`/`mlflow` custom lên ECR.
     - **b)** Tối ưu Airflow image: base trên `python:3.11-slim` + pip install apache-airflow, kéo từ PyPI. Nhẹ hơn.
   → **Chọn (a)** ở giai đoạn đầu để nhanh.

**Checklist**:
- [ ] `docker run` local với env AWS thật (S3 bucket) → MLflow ghi artifact lên S3 thành công
- [ ] Tất cả image push lên ECR thành công
- [ ] `docker pull` từ EC2 thành công (kiểm tra IAM role có quyền `ecr:Get*`)

### Phase 3 — Khởi tạo EC2 + Docker host (1 ngày)

1. **Launch EC2 t3.micro** ở `mlops-public-1a`:
   - AMI: Ubuntu 24.04 LTS (hoặc Amazon Linux 2023) — Amazon Linux 2023 nhẹ hơn.
   - EBS 20 GB gp3 (Free Tier 30 GB/tháng).
   - Subnet: public (để có public IP, không cần NAT).
   - Elastic IP gắn cố định.
   - IAM Instance Profile: tạo role `ec2-mlops-role` với policies:
     - `AmazonEC2ContainerRegistryReadOnly`
     - `AmazonS3FullAccess` (hoặc scope xuống 2 bucket) — **cảnh báo:** `AmazonS3FullAccess` rộng, nên tạo custom policy chỉ cho phép `s3:GetObject`, `s3:PutObject`, `s3:ListBucket` trên 2 bucket.
     - `AmazonRDSReadOnly` (chỉ connect, app dùng password).
     - `CloudWatchAgentServerPolicy` (gửi metrics/logs).
     - `AmazonSSMReadOnlyAccess` (đọc secrets từ Parameter Store).
2. **User data script** (chạy lúc boot):
   - Cài Docker + docker-compose-plugin.
   - Cài CloudWatch Agent.
   - Tạo thư mục `/opt/mlops/` chứa `docker-compose.aws.yml` (file này sẽ mount từ Git ở Phase 5).
3. **SSH** vào EC2 bằng key pair, kiểm tra `docker --version`, `docker compose version`.

**Checklist**:
- [ ] EC2 reachable qua SSH
- [ ] `docker run hello-world` OK
- [ ] `aws s3 ls s3://mlops-mlflow-artifacts-<id>` từ EC2 OK (IAM role hoạt động)
- [ ] CloudWatch Agent gửi metric `CPUUtilization` về namespace `MLOps/EC2`

### Phase 4 — Triển khai MLflow + Airflow trên EC2 (2–3 ngày)

Tạo `docker-compose.aws.yml` (tách riêng với file local) để chạy trên EC2:

```yaml
services:
  mlflow:
    image: <account>.dkr.ecr.us-east-1.amazonaws.com/mlops-framework/mlflow:aws-1.0.0
    restart: unless-stopped
    environment:
      POSTGRES_HOST: <rds-endpoint>
      POSTGRES_USER: mlops_admin
      POSTGRES_PASSWORD: ${DB_PASSWORD}   # lấy từ SSM Parameter Store
      POSTGRES_DB: mlflow
      MLFLOW_BUCKET: mlops-mlflow-artifacts-<id>
      AWS_DEFAULT_REGION: us-east-1
    ports:
      - "5000:5000"

  airflow-webserver:
    image: apache/airflow:2.10.4-python3.11
    restart: unless-stopped
    user: airflow
    environment:
      AIRFLOW__CORE__EXECUTOR: LocalExecutor
      AIRFLOW__DATABASE__SQL_ALCHEMY_CONN: postgresql+psycopg2://mlops_admin:${DB_PASSWORD}@<rds-endpoint>:5432/airflow
      AIRFLOW__CORE__FERNET_KEY: ${AIRFLOW_FERNET_KEY}
      AIRFLOW__CORE__LOAD_EXAMPLES: "false"
      AIRFLOW__WEBSERVER__SECRET_KEY: ${AIRFLOW_WEB_SECRET}
      AIRFLOW__API__AUTH_BACKEND: airflow.api.auth.backend.basic_auth
    command: webserver
    ports:
      - "8080:8080"
    volumes:
      - ./dags:/opt/airflow/dags:ro
      - airflow_logs:/opt/airflow/logs

  airflow-scheduler:
    image: apache/airflow:2.10.4-python3.11
    restart: unless-stopped
    user: airflow
    environment: &airflow_env
      AIRFLOW__CORE__EXECUTOR: LocalExecutor
      AIRFLOW__DATABASE__SQL_ALCHEMY_CONN: postgresql+psycopg2://mlops_admin:${DB_PASSWORD}@<rds-endpoint>:5432/airflow
      AIRFLOW__CORE__FERNET_KEY: ${AIRFLOW_FERNET_KEY}
      AIRFLOW__CORE__LOAD_EXAMPLES: "false"
    command: scheduler
    volumes:
      - ./dags:/opt/airflow/dags:ro
      - airflow_logs:/opt/airflow/logs
    depends_on: [airflow-webserver]

  app:
    image: <account>.dkr.ecr.us-east-1.amazonaws.com/mlops-framework/app:aws-1.0.0
    restart: unless-stopped
    environment:
      DATABASE_URL: postgresql+psycopg://mlops_admin:${DB_PASSWORD}@<rds-endpoint>:5432/mlops_framework
      MLFLOW_TRACKING_URI: http://localhost:5000
      AIRFLOW_BASE_URL: http://localhost:8080
      SERVING_BRIDGE_URL: http://localhost:8001
    ports:
      - "8000:8000"

  serving:
    image: <account>.dkr.ecr.us-east-1.amazonaws.com/mlops-framework/serving:aws-1.0.0
    restart: unless-stopped
    environment:
      DATABASE_URL: postgresql+psycopg://mlops_admin:${DB_PASSWORD}@<rds-endpoint>:5432/mlops_framework
    ports:
      - "8001:8001"

volumes:
  airflow_logs: {}
```

**Các bước thực hiện:**

1. Push `dags/` lên EC2 (scp hoặc git pull).
2. Tạo file `.env.aws` chứa `DB_PASSWORD`, `AIRFLOW_FERNET_KEY`, `AIRFLOW_WEB_SECRET` (lấy giá trị từ SSM Parameter Store, KHÔNG commit lên git).
3. **Init database Airflow**: chạy 1 lần container `airflow-webserver` với command `airflow db migrate` để tạo schema trong DB `airflow` trên RDS.
4. Tạo admin user Airflow: `airflow users create --username admin --password ... --role Admin --email ...`.
5. `docker compose -f docker-compose.aws.yml --env-file .env.aws up -d`.
6. Chạy alembic cho framework: trong container `app` chạy `alembic upgrade head`.
7. Verify:
   - Truy cập `http://<ec2-public-ip>:5000` → MLflow UI.
   - Truy cập `http://<ec2-public-ip>:8080` → Airflow UI (login admin).
   - Truy cập `http://<ec2-public-ip>:8000/docs` → FastAPI mgmt API.
   - Truy cập `http://<ec2-public-ip>:8001` → ServingBridge.

**Checklist**:
- [ ] MLflow UI load OK, list experiments được
- [ ] Airflow UI login OK, DAG `mlops_training_pipeline` hiện trong list
- [ ] Trigger DAG thủ công → chạy thành công end-to-end (resolve_context → train → register_and_promote)
- [ ] Artifact MLflow ghi vào S3 thật (check bucket có file mới)
- [ ] ServingBridge reload được sau khi promote

### Phase 5 — HTTPS + ALB (1–2 ngày)

1. **Đăng ký domain** (Route 53 hoặc domain có sẵn) — ví dụ `mlflow.example.com`, `airflow.example.com`, `app.example.com`, `serving.example.com`. Hoặc dùng 1 domain wildcard `*.mlops.example.com`.
2. **Request ACM cert** ở `us-east-1` cho các domain trên (ACM cert miễn phí).
3. **Tạo ALB** trong public subnets (cả 2 AZ):
   - Listener 443 → target group `tg-app` (port 8000, container `app`).
   - Listener 443 → target group `tg-mlflow` (port 5000).
   - Listener 443 → target group `tg-airflow` (port 8080).
   - Listener 443 → target group `tg-serving` (port 8001).
   - **Cách routing:** dùng host-based rules:
     - `app.example.com` → `tg-app`
     - `mlflow.example.com` → `tg-mlflow`
     - `airflow.example.com` → `tg-airflow`
     - `serving.example.com` → `tg-serving`
   - Health check path tương ứng: `/api/2.0/mlflow/experiments/search`, `/health`, `/docs`, `/`.
4. **Gắn ACM cert** vào listener 443; redirect 80 → 443.
5. **Cập nhật Security Group** `sg-app`: chỉ mở port từ `sg-alb` (KHÔNG mở từ internet).
6. **Bỏ public IP của EC2** (optional, giữ lại nếu cần SSH) — khuyến nghị giữ + chỉ mở port 22 từ IP admin.

**Checklist**:
- [ ] `https://app.example.com/docs` load OK
- [ ] `https://mlflow.example.com` load OK
- [ ] Cert valid (không cảnh báo trình duyệt)
- [ ] Health check ALB đều `healthy`

### Phase 6 — Observability & backup (1–2 ngày)

1. **CloudWatch Logs:**
   - Tạo log group `/aws/ec2/mlops/app`, `/aws/ec2/mlops/mlflow`, `/aws/ec2/mlops/airflow`.
   - CloudWatch Agent trên EC2 scrape Docker logs (`/var/lib/docker/containers/*/*.log`).
   - Retention 7 ngày (Free Tier 5 GB).
2. **CloudWatch Alarms (10 free):**
   - EC2 `CPUUtilization > 80%` trong 5 phút.
   - RDS `CPUUtilization > 80%`.
   - RDS `FreeStorageSpace < 2 GB`.
   - RDS `DatabaseConnections > 80`.
   - ALB `UnHealthyHostCount > 0`.
   - ALB `5xx count > 5` trong 5 phút.
   - MLflow container down (custom metric qua CloudWatch Agent).
3. **Backup:**
   - RDS automated backup (đã bật ở Phase 1, 7 ngày retention).
   - S3 versioning (đã bật ở Phase 1).
   - Lifecycle rule xóa noncurrent version S3 sau 30 ngày.
4. **Dashboard:**
   - Tạo CloudWatch Dashboard `MLOps-Production` với widgets CPU, memory, RDS, request count ALB.

**Checklist**:
- [ ] Log group có log mới mỗi 1 phút
- [ ] Alarm test: stop 1 service → alarm trigger trong vòng 5 phút
- [ ] Dashboard render OK

### Phase 7 — CI/CD & hardening (2–3 ngày)

1. **GitHub Actions workflow** `.github/workflows/deploy-aws.yml`:
   - Job `build`: checkout → docker build (3 image: app, serving, mlflow) → push lên ECR với tag `aws-<git-sha>`.
   - Job `deploy`: SSH vào EC2 → `aws ecr get-login-password | docker login` → `docker compose pull` → `docker compose up -d` → health check.
2. **Secrets lưu ở GitHub Actions secrets**: `EC2_SSH_KEY`, `EC2_HOST`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (dùng OIDC nếu có thể, an toàn hơn).
3. **SSL renewal** tự động (ACM cert tự renew).
4. **Database migration automation**: khi deploy image `app` mới, container chạy `alembic upgrade head` trước khi start uvicorn.
5. **Rolling update** (chưa cần ECS): vì là single instance nên downtime ~30 giây. Có thể giảm bằng cách:
   - `docker compose up -d --no-deps --scale <service>=2` rồi `docker compose stop <service>` — nhưng EC2 t3.micro chỉ 1 GB RAM, scale >1 sẽ OOM.
   → Chấp nhận downtime ngắn ở Free Tier.
6. **Security hardening:**
   - Bật VPC Flow Logs → CloudWatch Logs (free 5 GB).
   - Đặt GuardDuty (30 ngày free ở một số region) — tùy chọn.
   - Tắt password SSH, dùng SSM Session Manager (free) thay cho SSH key.
   - Cấu hình ALB access log → S3 (free 7 ngày).

**Checklist**:
- [ ] Push commit mới → image mới được build & deploy trong <10 phút
- [ ] Không cần SSH thủ công để deploy
- [ ] Health check pass sau deploy

### Phase 8 — Cắm thiết bị cảnh báo & tài liệu vận hành (1 ngày)

1. **Runbook** `docs/runbook-aws.md`:
   - Cách SSH vào EC2 (qua Session Manager hoặc SSH key).
   - Cách xem logs: `docker logs`, CloudWatch Logs Insights.
   - Cách trigger DAG thủ công.
   - Cách rollback image: chỉnh tag trong `.env.aws` rồi `docker compose up -d`.
   - Cách scale up (chuyển sang EC2 t3.small hoặc ECS).
   - Quy trình khi RDS OOM.
   - Quy trình khi S3 đầy (lifecycle rule + Glacier).
2. **Disaster recovery drill:**
   - Snapshot RDS thủ công → terminate RDS → restore từ snapshot → cập nhật endpoint → restart app. Đo thời gian RTO.
3. **Onboarding:**
   - `docs/aws-deployment-plan.md` (file này).
   - README trỏ đến runbook.

## 6. Tóm tắt deliverables

| # | Deliverable | Vị trí |
|---|---|---|
| 1 | Terraform module (VPC, subnets, SGs, ALB, RDS, S3, IAM, CloudWatch) | `infrastructure/terraform/` |
| 2 | `docker-compose.aws.yml` chạy trên EC2 | repo root, ignored bởi compose local |
| 3 | `infrastructure/mlflow/entrypoint.aws.sh` cho S3 thật | `infrastructure/mlflow/` |
| 4 | Image `app`, `serving`, `mlflow` trong ECR | ECR repo `mlops-framework` |
| 5 | GitHub Actions workflow deploy | `.github/workflows/deploy-aws.yml` |
| 6 | CloudWatch dashboard + alarms | CloudWatch console |
| 7 | Runbook | `docs/runbook-aws.md` |
| 8 | Alembic auto-migration trong entrypoint `app` | `infrastructure/app/Dockerfile` |

## 7. Rủi ro & mitigation

| Rủi ro | Tác động | Mitigation |
|---|---|---|
| EC2 t3.micro chỉ 1 GB RAM → Airflow + MLflow + app cùng chạy dễ OOM | Service crash | Giữ Airflow ở `LocalExecutor` (không Celery), không scale, set Docker memory limit 300 MB mỗi service; nếu OOM thì nâng lên t3.small (~$15/tháng ngoài Free Tier) |
| Single-AZ → downtime khi AZ down | Mất dịch vụ | Free Tier không cover multi-AZ; chấp nhận rủi ro cho dev/POC; khi production thật → chuyển ECS Fargate multi-AZ |
| RDS 20 GB mau đầy (MLflow + Airflow + framework DB) | Ghi fail | Bật lifecycle S3 để archive log cũ; export MLflow runs cũ sang S3 Glacier; tăng storage RDS từng GB (~$0.115/GB-tháng ngoài Free Tier) |
| S3 5 GB mau đầy do artifact MLflow | Upload fail | Bật lifecycle xóa run >30 ngày; khuyến khích user dọn experiment; tắt upload model artifact không cần thiết |
| ECR 500 MB không đủ | Push fail | Giữ image trên Docker Hub public (chỉ custom image); tối ưu Dockerfile bằng multi-stage build; archive image cũ về S3 |
| Egress vượt 100 GB/tháng (nếu dùng MLflow UI tải artifact lớn) | Tốn thêm | CloudFront cache trước ALB (Free Tier 1 TB egress 12 tháng + 10M requests) |
| Bí mật hardcode trong `.env.aws` | Lộ credentials | Dùng SSM Parameter Store + IAM role, KHÔNG commit `.env.aws` lên git; thêm `.env.aws` vào `.gitignore` |
| Không có NAT → EC2 public subnet có public IP | Tăng attack surface | Chỉ mở port 22 từ IP admin; Security Group chặt; ALB dùng WAF (có free tier 1 năm cho WAF hoặc dùng managed rule miễn phí) |
| Chi phí vượt Free Tier vì quên tắt resource | Bill shock | Bật **AWS Budgets** với alert ở $5, $20, $50; tag mọi resource với `Project=mlops-framework`, `Env=prod`; dùng Cost Explorer |

## 8. Timeline tổng

| Phase | Thời gian | Effort |
|---|---|---|
| Phase 0: Account setup | 0.5 ngày | 0.5 |
| Phase 1: Infra nền tảng (VPC, RDS, S3) | 1–2 ngày | 1.5 |
| Phase 2: Build & push image | 1 ngày | 1 |
| Phase 3: EC2 + Docker host | 1 ngày | 1 |
| Phase 4: Deploy MLflow + Airflow | 2–3 ngày | 2.5 |
| Phase 5: HTTPS + ALB | 1–2 ngày | 1.5 |
| Phase 6: Observability & backup | 1–2 ngày | 1.5 |
| Phase 7: CI/CD & hardening | 2–3 ngày | 2.5 |
| Phase 8: Runbook & DR drill | 1 ngày | 1 |
| **Tổng** | **~12–16 ngày** | **~13** |

## 9. Lưu ý cuối

- **Cảnh báo Free Tier:** RDS db.t3.micro, EC2 t3.micro, S3 5 GB, ALB chỉ free 12 tháng kể từ khi tạo account AWS đầu tiên. Sau đó sẽ bị tính phí — xem section 4 để biết ước lượng.
- **SLA mục tiêu thực tế** trên Free Tier: single-AZ + 1 instance EC2 → SLA ~95% (chỉ dùng cho dev/staging/POC). Production thật cần ít nhất 2 instance + multi-AZ + ECS Fargate.
- **Khi vượt Free Tier**, nâng cấp theo thứ tự:
  1. EC2 t3.micro → t3.small (nếu OOM).
  2. RDS single-AZ → multi-AZ (~$33/tháng).
  3. EC2 đơn → ECS Fargate cluster 2 task (~$18/tháng/task t3.small).
  4. Thêm ALB internal + Route 53 health check failover.
- **Không recommend**: MWAA, EKS, ECS Fargate nếu ưu tiên Free Tier. Đây là lý do kế hoạch dùng EC2 single instance.
