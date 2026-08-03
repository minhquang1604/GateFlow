###########################################################################
# Production environment — orchestrates the 9 reusable modules.
#
# The only file in this directory that creates resources directly is
# `main.tf`. Variables, providers, versions, and outputs are split out
# for clarity.
###########################################################################

data "aws_caller_identity" "current" {}

locals {
  name_prefix              = "${var.project_name}-${var.env}"
  ssm_prefix               = "/${var.project_name}/${var.env}"
  account_id               = data.aws_caller_identity.current.account_id
  service_discovery_domain = "${local.name_prefix}.local"

  # Three images, not one shared image: airflow-webserver/scheduler
  # build from infrastructure/airflow/Dockerfile (no framework
  # install — SQLAlchemy 1.4/2.0 conflict, see that Dockerfile's
  # header); app/serving build from infrastructure/app/Dockerfile
  # (framework installed); mlflow has its own distinct Dockerfile.
  app_image     = "${module.ecr.repository_urls["app"]}:${var.app_image_tag}"
  airflow_image = "${module.ecr.repository_urls["airflow"]}:${var.airflow_image_tag}"
  mlflow_image  = "${module.ecr.repository_urls["mlflow"]}:${var.mlflow_image_tag}"
}

# ---------------------------------------------------------------------- #
# network                                                                 #
# ---------------------------------------------------------------------- #
module "network" {
  source      = "../../modules/network"
  name_prefix = local.name_prefix
  vpc_cidr    = var.vpc_cidr
}

# ---------------------------------------------------------------------- #
# s3                                                                      #
# ---------------------------------------------------------------------- #
module "s3" {
  source       = "../../modules/s3"
  project_name = var.project_name
  buckets = {
    "mlflow-artifacts" = {
      purpose                    = "MLflow artifact store"
      noncurrent_expiration_days = 30
      multipart_abort_days       = 7
    }
    "airflow-logs" = {
      purpose = "Airflow log archive"
    }
    "app-backups" = {
      purpose = "Application backup target"
    }
  }
}

# ---------------------------------------------------------------------- #
# ecr                                                                     #
#                                                                          #
# Repos only — Terraform never builds or pushes images. CI/CD             #
# (.github/workflows/deploy.yml) builds infrastructure/mlflow,           #
# infrastructure/airflow, and infrastructure/app, and pushes to these     #
# repos; ECS services below reference the resulting URIs by tag.         #
#                                                                         #
# Two separate images for the framework side (airflow vs app) — not one  #
# shared image — because Airflow 2.10.4 pins SQLAlchemy 1.4.x internally #
# and cannot tolerate the framework's sqlalchemy>=2.0.0 requirement in   #
# the same Python environment. See infrastructure/airflow/Dockerfile's   #
# header comment for the full explanation.                              #
# ---------------------------------------------------------------------- #
module "ecr" {
  source       = "../../modules/ecr"
  project_name = var.project_name
  repositories = {
    mlflow  = { purpose = "MLflow tracking server image" }
    airflow = { purpose = "Airflow webserver/scheduler image - no framework install" }
    app     = { purpose = "Framework image: app + serving" }
  }
}

# ---------------------------------------------------------------------- #
# security_groups                                                         #
# ---------------------------------------------------------------------- #
module "security_groups" {
  source      = "../../modules/security_groups"
  name_prefix = local.name_prefix
  vpc_id      = module.network.vpc_id
  admin_cidr  = var.admin_cidr
}

# ---------------------------------------------------------------------- #
# ssm                                                                     #
# ---------------------------------------------------------------------- #
module "ssm" {
  source      = "../../modules/ssm"
  ssm_prefix  = local.ssm_prefix
  db_password = var.db_password
  generated_secrets = {
    "airflow/fernet-key" = {
      length      = 44
      special     = false
      description = "Fernet key for Airflow connection encryption."
    }
    "airflow/web-secret" = {
      length      = 32
      special     = false
      description = "Airflow webserver secret key."
    }
    "airflow/admin-password" = {
      length      = 24
      special     = true
      description = "Initial Airflow admin user password."
    }
  }
}

# ---------------------------------------------------------------------- #
# iam                                                                     #
# ---------------------------------------------------------------------- #
module "iam" {
  source      = "../../modules/iam"
  name_prefix = local.name_prefix

  s3_bucket_arns = values(module.s3.bucket_arns)

  ssm_parameter_arn_prefix = "arn:aws:ssm:${var.aws_region}:${local.account_id}:parameter${local.ssm_prefix}/*"
  aws_region               = var.aws_region
  account_id               = local.account_id
}

# ---------------------------------------------------------------------- #
# rds                                                                     #
# ---------------------------------------------------------------------- #
module "rds" {
  source                 = "../../modules/rds"
  name_prefix            = local.name_prefix
  instance_class         = var.db_instance_class
  allocated_storage_gb   = var.db_allocated_storage_gb
  db_name                = var.db_name
  db_username            = var.db_username
  db_password            = var.db_password
  db_subnet_group_name   = module.network.db_subnet_group_name
  vpc_security_group_ids = [module.security_groups.rds_security_group_id]
}

# ---------------------------------------------------------------------- #
# compute — ECS container instance fleet (EC2 launch type).              #
# ---------------------------------------------------------------------- #
module "compute" {
  source      = "../../modules/compute"
  name_prefix = local.name_prefix

  instance_type  = var.ec2_instance_type
  instance_count = var.instance_count
  ebs_size_gb    = var.ec2_ebs_size_gb
  ssh_public_key = var.ssh_public_key

  subnet_ids                = module.network.public_subnet_ids
  vpc_security_group_ids    = [module.security_groups.app_security_group_id]
  iam_instance_profile_name = module.iam.instance_profile_name

  user_data_vars = {
    ecs_cluster_name = local.name_prefix
  }
}

# ---------------------------------------------------------------------- #
# ecs — cluster, capacity provider, task definitions, services.          #
# ---------------------------------------------------------------------- #
module "ecs" {
  source      = "../../modules/ecs"
  name_prefix = local.name_prefix
  aws_region  = var.aws_region
  vpc_id      = module.network.vpc_id

  service_discovery_namespace = local.service_discovery_domain
  autoscaling_group_arn       = module.compute.autoscaling_group_arn
  ecs_task_execution_role_arn = module.iam.ecs_task_execution_role_arn
  ecs_task_role_arn           = module.iam.ecs_task_role_arn

  # Memory budget: a t3.small registers ~1913 MiB of schedulable
  # memory (2 GiB minus ECS agent/OS reserve). With instance_count = 2,
  # total capacity is ~3826 MiB. The 5 services below sum to 1700 MiB
  # of memoryReservation, plus ~40 MiB per task for the Service
  # Connect proxy sidecar (5 tasks * 40 = 200 MiB) = ~1900 MiB total —
  # comfortable headroom, and the whole stack even fits on a single
  # instance if instance_count is dropped to 1. Raising any of these
  # without checking the total against the per-instance 1913 MiB will
  # cause tasks to get stuck PENDING forever — ECS won't partially
  # schedule a task it can't fully fit.
  #
  # Two values were raised after observing production failures:
  # mlflow needs 400 (not 300) even at --workers 1 — gunicorn +
  # mlflow + sqlalchemy + boto3's combined resident footprint
  # OOM-killed (exit 137) repeatedly at 300 MiB. airflow-webserver
  # needs 600 MiB / 512 CPU units — see its own comment below.
  services = {
    mlflow = {
      image          = local.mlflow_image
      container_port = 5000
      memory         = 400
      environment = {
        POSTGRES_HOST      = module.rds.address
        POSTGRES_PORT      = tostring(module.rds.port)
        POSTGRES_USER      = var.db_username
        POSTGRES_DB        = "mlflow"
        MLFLOW_BUCKET      = module.s3.bucket_names_by_key["mlflow-artifacts"]
        AWS_DEFAULT_REGION = var.aws_region
      }
      secrets = {
        POSTGRES_PASSWORD = module.ssm.parameter_arns["db/password"]
      }
      # /api/2.0/mlflow/experiments/search requires POST with a JSON
      # body — a bare curl GET returns 405 and `curl -f` treats that
      # as failure, so the health check never passed. MLflow's
      # dedicated /health endpoint is GET, returns 200 with no body
      # required, and exists specifically for this purpose.
      health_check_command = [
        "CMD-SHELL",
        "curl -fsS http://localhost:5000/health -o /dev/null",
      ]
      health_check_start_period = 60
    }

    # command[0] selects the role inside infrastructure/airflow/entrypoint.sh
    # ("webserver"/"scheduler"); that script builds
    # AIRFLOW__DATABASE__SQL_ALCHEMY_CONN itself from the POSTGRES_*
    # vars below, same as mlflow's entrypoint does for its own DB.
    airflow-webserver = {
      image          = local.airflow_image
      container_port = 8080
      # 600 MiB / 512 CPU units: Airflow's webserver imports a heavy
      # Flask/FAB stack at boot. At 320 MiB and 128 CPU units it was
      # too slow to answer Airflow's own gunicorn-master health probe
      # within the 120s default, and got killed in a loop with
      # "No response from gunicorn master within 120 seconds".
      memory  = 600
      cpu     = 512
      command = ["webserver"]
      environment = {
        POSTGRES_HOST                     = module.rds.address
        POSTGRES_PORT                     = tostring(module.rds.port)
        POSTGRES_USER                     = var.db_username
        POSTGRES_DB                       = "airflow"
        AIRFLOW__CORE__EXECUTOR           = "LocalExecutor"
        AIRFLOW__CORE__LOAD_EXAMPLES      = "false"
        AIRFLOW__API__AUTH_BACKEND        = "airflow.api.auth.backend.basic_auth"
        AIRFLOW__WEBSERVER__EXPOSE_CONFIG = "true"
        AIRFLOW_ADMIN_USERNAME            = "admin"
        # Default is 4 gunicorn workers, which OOM-kills at this
        # task's memory reservation. 1 is enough for a
        # single-user/demo Airflow UI.
        AIRFLOW__WEBSERVER__WORKERS = "1"
        # Give the single worker room to finish importing before
        # Airflow's monitor declares the master unresponsive.
        AIRFLOW__WEBSERVER__WEB_SERVER_MASTER_TIMEOUT = "300"
        AIRFLOW__WEBSERVER__WEB_SERVER_WORKER_TIMEOUT = "300"
      }
      secrets = {
        POSTGRES_PASSWORD              = module.ssm.parameter_arns["db/password"]
        AIRFLOW__CORE__FERNET_KEY      = module.ssm.parameter_arns["airflow/fernet-key"]
        AIRFLOW__WEBSERVER__SECRET_KEY = module.ssm.parameter_arns["airflow/web-secret"]
        AIRFLOW_ADMIN_PASSWORD         = module.ssm.parameter_arns["airflow/admin-password"]
      }
      health_check_command = [
        "CMD-SHELL",
        "curl -fsS -u admin:$AIRFLOW_ADMIN_PASSWORD http://localhost:8080/health | grep -q healthy",
      ]
      # Matches the extended gunicorn master timeout above — the
      # webserver needs the full import window before it can answer
      # /health at all.
      health_check_start_period = 300
    }

    airflow-scheduler = {
      image          = local.airflow_image
      container_port = 8793
      memory         = 250
      command        = ["scheduler"]
      environment = {
        POSTGRES_HOST                = module.rds.address
        POSTGRES_PORT                = tostring(module.rds.port)
        POSTGRES_USER                = var.db_username
        POSTGRES_DB                  = "airflow"
        AIRFLOW__CORE__EXECUTOR      = "LocalExecutor"
        AIRFLOW__CORE__LOAD_EXAMPLES = "false"
        # LocalExecutor runs DAG tasks as subprocesses of the
        # scheduler (not the webserver), so this is where
        # mlops_training_pipeline.py's HTTP calls need these.
        APP_BASE_URL       = "http://app:8000"
        SERVING_BRIDGE_URL = "http://serving:8001"
      }
      secrets = {
        POSTGRES_PASSWORD              = module.ssm.parameter_arns["db/password"]
        AIRFLOW__CORE__FERNET_KEY      = module.ssm.parameter_arns["airflow/fernet-key"]
        AIRFLOW__WEBSERVER__SECRET_KEY = module.ssm.parameter_arns["airflow/web-secret"]
      }
    }

    # app/serving build DATABASE_URL inline from POSTGRES_* + the
    # shared db/password secret — same construction the framework's
    # own docker-compose.yml uses, just assembled in the command
    # instead of docker-compose's `environment:` block since ECS
    # `secrets` only injects individual values, not composed URLs.
    # Built from infrastructure/app/Dockerfile — a separate image
    # from Airflow's, see local.airflow_image's comment above.
    app = {
      image          = local.app_image
      container_port = 8000
      memory         = 250
      command = [
        "/bin/bash", "-c",
        "export DATABASE_URL=\"postgresql+psycopg://$POSTGRES_USER:$POSTGRES_PASSWORD@$POSTGRES_HOST:$POSTGRES_PORT/$POSTGRES_DB\" && cd /opt/framework && PYTHONPATH=/opt/framework/src alembic upgrade head && uvicorn mlops_framework.api.app:create_app --factory --host 0.0.0.0 --port 8000",
      ]
      environment = {
        POSTGRES_HOST = module.rds.address
        POSTGRES_PORT = tostring(module.rds.port)
        POSTGRES_USER = var.db_username
        POSTGRES_DB   = var.db_name
        # ECS Service Connect resolves these bare discovery names to
        # whichever container instance currently runs the target
        # service — no ".namespace" suffix needed (unlike classic
        # Cloud Map DNS).
        MLFLOW_TRACKING_URI    = "http://mlflow:5000"
        MLFLOW_EXPERIMENT_NAME = "mlops-framework"
        AIRFLOW_BASE_URL       = "http://airflow-webserver:8080"
        AIRFLOW_USERNAME       = "admin"
        SERVING_BRIDGE_URL     = "http://serving:8001"
      }
      secrets = {
        POSTGRES_PASSWORD = module.ssm.parameter_arns["db/password"]
        AIRFLOW_PASSWORD  = module.ssm.parameter_arns["airflow/admin-password"]
      }
      health_check_command = [
        "CMD-SHELL",
        "curl -fsS http://localhost:8000/ -o /dev/null",
      ]
      health_check_start_period = 60
    }

    serving = {
      image          = local.app_image
      container_port = 8001
      memory         = 200
      command = [
        "/bin/bash", "-c",
        "export DATABASE_URL=\"postgresql+psycopg://$POSTGRES_USER:$POSTGRES_PASSWORD@$POSTGRES_HOST:$POSTGRES_PORT/$POSTGRES_DB\" && cd /opt/framework && PYTHONPATH=/opt/framework/src python -m mlops_framework.serving.run --host 0.0.0.0 --port 8001",
      ]
      environment = {
        POSTGRES_HOST = module.rds.address
        POSTGRES_PORT = tostring(module.rds.port)
        POSTGRES_USER = var.db_username
        POSTGRES_DB   = var.db_name
      }
      secrets = {
        POSTGRES_PASSWORD = module.ssm.parameter_arns["db/password"]
      }
      health_check_command = [
        "CMD-SHELL",
        "curl -fsS http://localhost:8001/healthz -o /dev/null",
      ]
      health_check_start_period = 30
    }
  }
}
