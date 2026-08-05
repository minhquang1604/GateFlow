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
    # Training data. Dataset versions are registered by S3 URI rather than
    # by a path inside an image: the fraud case study's CSV is 144 MB, kept
    # out of git and out of the images (.dockerignore), and the Airflow
    # worker reads it straight from here via s3fs. Versioned, so a
    # DatasetVersion's content hash stays resolvable after an overwrite.
    "datasets" = {
      purpose              = "Training datasets read by pipelines"
      multipart_abort_days = 7
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
    # 32 characters, base64-encoded to 44 — NOT 44 raw characters.
    # Airflow feeds this straight to cryptography.fernet.Fernet, which
    # requires the value to decode to exactly 32 bytes; 44 raw
    # alphanumeric characters decode to 33 and are rejected. The failure
    # is latent: Fernet is only constructed when Airflow encrypts or
    # decrypts a Connection/Variable, so the containers boot fine and
    # blow up later.
    "airflow/fernet-key" = {
      length        = 32
      special       = false
      base64_encode = true
      description   = "Fernet key for Airflow connection encryption (base64 of 32 bytes)."
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
  engine_version         = var.db_engine_version
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

  # Resource budget on 2x t3.small (2 GiB / 2 vCPU each = ~1913 MiB
  # and 2048 CPU units schedulable per instance):
  #
  #   memory  2446 MiB + ~40 MiB/task Service Connect sidecar
  #           (5 * 40 = 200) = ~2696 MiB of 3826   (~70%)
  #   cpu     1984 units of 4096                    (~48%)
  #
  # Neither total may exceed one instance's share of the fleet for
  # any single task — ECS won't split a task across hosts, and won't
  # partially schedule one it can't fully fit, so an over-subscribed
  # reservation leaves tasks stuck PENDING forever.
  #
  # Sizing rationale: CPU is this stack's binding constraint. On a
  # single 2 vCPU instance these same reservations consumed 97% of
  # available CPU while 65% of RAM sat idle. Two t3.small give 4
  # vCPU total for less than half the cost. See ec2_instance_type in
  # variables.tf for the burstable-throttling caveat that comes with
  # t3, and modules/ecs/variables.tf placement_strategy for why the
  # two Airflow services are spread across hosts rather than packed
  # onto one.
  #
  services = {
    mlflow = {
      image          = local.mlflow_image
      container_port = 5000
      # 640 MiB / 384 CPU units. CloudWatch measured this service at
      # 94% average and 109% peak MemoryUtilization against a 400 MiB
      # reservation — i.e. it was riding the limit and spilling over
      # it, which is what produced the earlier exit-137 kills. CPU is
      # raised off the 128-unit default because gunicorn + sqlalchemy
      # + boto3 contend badly at low shares.
      memory = 640
      cpu    = 384
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
      # 768 MiB / 512 CPU units. Airflow's webserver runs a heavy
      # Flask/FAB stack and needed both raised, for two separate
      # failures seen in production:
      #   * 128 CPU units was too slow to answer Airflow's own
      #     gunicorn-master probe within its 120s default, killing
      #     the webserver in a loop ("No response from gunicorn
      #     master within 120 seconds").
      #   * 320 and then 600 MiB were both OOM-killed (exit 137)
      #     once it got far enough to actually serve.
      # 1024 turned out to overshoot — CloudWatch then measured 36%
      # average / 86% peak MemoryUtilization — so this trims back to
      # 768, which still clears the observed peak with headroom.
      memory  = 768
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
        # The webserver runs its own DAG-parsing loop to render the
        # UI. Same reasoning as the scheduler's tuning below — one
        # rarely-changing DAG doesn't need constant re-parsing, and
        # the default drove 142-185% CPU spikes here too.
        AIRFLOW__SCHEDULER__MIN_FILE_PROCESS_INTERVAL = "300"
        AIRFLOW__SCHEDULER__DAG_DIR_LIST_INTERVAL     = "300"
        # Gunicorn workers were being recycled every 30s by default,
        # re-importing the whole Flask/FAB stack each time.
        AIRFLOW__WEBSERVER__WORKER_REFRESH_INTERVAL = "1800"
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
      # 1280 MiB / 512 CPU units. At the default 128 CPU units the
      # scheduler measured 150-200% average CPU with ~700% peaks
      # (i.e. 2-7x its reservation), starving whichever instance it
      # landed on. The tuning below cuts the actual work; this
      # raises the reservation so ECS accounts for it honestly.
      #
      # Memory went 512 -> 1280 because LocalExecutor runs DAG tasks as
      # subprocesses of the scheduler, so the training itself lives in
      # this reservation. mlops_training_pipeline reads a 144 MB CSV into
      # pandas (284,807 x 31), converts to a float32 matrix and fits 200
      # XGBoost trees; 512 MiB is an OOM kill, not a tight fit.
      #
      # This puts the fleet at 91% of schedulable memory (3464 of 3826
      # MiB across 2x t3.small). It packs — scheduler+serving on one
      # host, webserver+mlflow+app on the other — but there is no room
      # left for a sixth task. If another service is added, or training
      # grows, move to t3.medium rather than shaving this back.
      memory  = 1280
      cpu     = 512
      command = ["scheduler"]
      environment = {
        POSTGRES_HOST                = module.rds.address
        POSTGRES_PORT                = tostring(module.rds.port)
        POSTGRES_USER                = var.db_username
        POSTGRES_DB                  = "airflow"
        AIRFLOW__CORE__EXECUTOR      = "LocalExecutor"
        AIRFLOW__CORE__LOAD_EXAMPLES = "false"
        # Airflow's defaults re-scan and re-parse the DAG folder
        # almost continuously, which is what drove the CPU burn.
        # This stack has exactly one DAG that changes only on image
        # rebuild, so parse it far less aggressively.
        AIRFLOW__SCHEDULER__MIN_FILE_PROCESS_INTERVAL     = "300"
        AIRFLOW__SCHEDULER__DAG_DIR_LIST_INTERVAL         = "300"
        AIRFLOW__SCHEDULER__PARSING_PROCESSES             = "1"
        AIRFLOW__SCHEDULER__SCHEDULER_IDLE_SLEEP_TIME     = "5"
        AIRFLOW__SCHEDULER__SCHEDULER_HEARTBEAT_SEC       = "15"
        AIRFLOW__SCHEDULER__JOB_HEARTBEAT_SEC             = "15"
        AIRFLOW__CORE__PARALLELISM                        = "4"
        AIRFLOW__CORE__MAX_ACTIVE_TASKS_PER_DAG           = "4"
        AIRFLOW__CORE__MAX_ACTIVE_RUNS_PER_DAG            = "1"
        AIRFLOW__SCHEDULER__USE_ROW_LEVEL_LOCKING         = "False"
        AIRFLOW__METRICS__STATSD_ON                       = "False"
        AIRFLOW__SCHEDULER__SCHEDULE_AFTER_TASK_EXECUTION = "False"
        # LocalExecutor runs DAG tasks as subprocesses of the
        # scheduler (not the webserver), so this is where
        # mlops_training_pipeline.py's HTTP calls need these.
        APP_BASE_URL       = "http://app:8000"
        SERVING_BRIDGE_URL = "http://serving:8001"
        # Training reads its dataset straight from S3 (s3fs), so the
        # scheduler needs a region the same way mlflow does. boto3 can
        # usually recover this from instance metadata, but s3fs reaches S3
        # through aiobotocore and that fallback is not dependable — an
        # unset region surfaces as a bucket-region error mid-task rather
        # than as a missing configuration.
        AWS_DEFAULT_REGION = var.aws_region
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
      # 320 MiB / 320 CPU units — raised off the 128-unit default so
      # uvicorn isn't starved during alembic migrations and request
      # bursts. Memory is cheap on this instance (only ~33% used).
      memory = 320
      cpu    = 320
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
      # 256 MiB / 256 CPU units — same reasoning as app above.
      memory = 256
      cpu    = 256
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
