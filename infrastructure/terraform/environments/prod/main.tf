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

  # Every ECS task in this stack shares one image built from
  # infrastructure/airflow/Dockerfile (mirrors docker-compose, where
  # airflow-webserver/scheduler/app/serving all build from that same
  # Dockerfile with different `command` overrides). mlflow gets its
  # own image/repo because it has a distinct Dockerfile.
  app_image    = "${module.ecr.repository_urls["app"]}:${var.app_image_tag}"
  mlflow_image = "${module.ecr.repository_urls["mlflow"]}:${var.mlflow_image_tag}"
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
# (.github/workflows/deploy.yml) builds infrastructure/mlflow and         #
# infrastructure/airflow and pushes to these repos; ECS services below    #
# reference the resulting URIs by tag.                                    #
# ---------------------------------------------------------------------- #
module "ecr" {
  source       = "../../modules/ecr"
  project_name = var.project_name
  repositories = {
    mlflow = { purpose = "MLflow tracking server image" }
    app    = { purpose = "Framework image: airflow webserver/scheduler, app, serving" }
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

  services = {
    mlflow = {
      image          = local.mlflow_image
      container_port = 5000
      memory         = 350
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
      health_check_command = [
        "CMD-SHELL",
        "curl -fsS http://localhost:5000/api/2.0/mlflow/experiments/search -o /dev/null",
      ]
      health_check_start_period = 60
    }

    # command[0] selects the role inside infrastructure/airflow/entrypoint.sh
    # ("webserver"/"scheduler"); that script builds
    # AIRFLOW__DATABASE__SQL_ALCHEMY_CONN itself from the POSTGRES_*
    # vars below, same as mlflow's entrypoint does for its own DB.
    airflow-webserver = {
      image          = local.app_image
      container_port = 8080
      memory         = 400
      command        = ["webserver"]
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
      health_check_start_period = 90
    }

    airflow-scheduler = {
      image          = local.app_image
      container_port = 8793
      memory         = 300
      command        = ["scheduler"]
      environment = {
        POSTGRES_HOST                = module.rds.address
        POSTGRES_PORT                = tostring(module.rds.port)
        POSTGRES_USER                = var.db_username
        POSTGRES_DB                  = "airflow"
        AIRFLOW__CORE__EXECUTOR      = "LocalExecutor"
        AIRFLOW__CORE__LOAD_EXAMPLES = "false"
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
    app = {
      image          = local.app_image
      container_port = 8000
      memory         = 300
      command = [
        "/bin/bash", "-c",
        "export DATABASE_URL=\"postgresql+psycopg://$POSTGRES_USER:$POSTGRES_PASSWORD@$POSTGRES_HOST:$POSTGRES_PORT/$POSTGRES_DB\" && cd /opt/framework && PYTHONPATH=/opt/framework/src alembic upgrade head && uvicorn mlops_framework.api.app:create_app --factory --host 0.0.0.0 --port 8000",
      ]
      environment = {
        POSTGRES_HOST          = module.rds.address
        POSTGRES_PORT          = tostring(module.rds.port)
        POSTGRES_USER          = var.db_username
        POSTGRES_DB            = var.db_name
        MLFLOW_TRACKING_URI    = "http://mlflow.${local.service_discovery_domain}:5000"
        MLFLOW_EXPERIMENT_NAME = "mlops-framework"
        AIRFLOW_BASE_URL       = "http://airflow-webserver.${local.service_discovery_domain}:8080"
        AIRFLOW_USERNAME       = "admin"
        SERVING_BRIDGE_URL     = "http://serving.${local.service_discovery_domain}:8001"
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
      memory         = 250
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
