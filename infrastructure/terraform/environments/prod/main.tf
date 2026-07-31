###########################################################################
# Production environment — orchestrates the 8 reusable modules.
#
# The only file in this directory that creates resources directly is
# `main.tf`. Variables, providers, versions, and outputs are split out
# for clarity.
###########################################################################

data "aws_caller_identity" "current" {}

locals {
  name_prefix  = "${var.project_name}-${var.env}"
  ssm_prefix   = "/${var.project_name}/${var.env}"
  account_id   = data.aws_caller_identity.current.account_id
  ecr_registry = split("/", module.ecr.first_repository_url)[0]
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
# ---------------------------------------------------------------------- #
module "ecr" {
  source       = "../../modules/ecr"
  project_name = var.project_name
  repositories = {
    mlflow = { purpose = "MLflow tracking server image" }
    app    = { purpose = "Framework FastAPI app / serving runtime" }
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

  ecr_repository_arns = values(module.ecr.repository_arns)
  s3_bucket_arns      = values(module.s3.bucket_arns)

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
# compute                                                                 #
# ---------------------------------------------------------------------- #
module "compute" {
  source      = "../../modules/compute"
  name_prefix = local.name_prefix

  instance_type  = var.ec2_instance_type
  ebs_size_gb    = var.ec2_ebs_size_gb
  ssh_public_key = var.ssh_public_key

  subnet_id                 = module.network.public_subnet_ids[0]
  vpc_security_group_ids    = [module.security_groups.app_security_group_id]
  iam_instance_profile_name = module.iam.instance_profile_name

  user_data_vars = {
    ssm_prefix              = local.ssm_prefix
    aws_region              = var.aws_region
    db_host                 = module.rds.address
    db_port                 = module.rds.port
    db_username             = var.db_username
    db_name                 = var.db_name
    mlflow_artifacts_bucket = module.s3.bucket_names_by_key["mlflow-artifacts"]
    ecr_mlflow              = module.ecr.repository_urls["mlflow"]
    ecr_app                 = module.ecr.repository_urls["app"]
    ecr_registry            = local.ecr_registry
    auto_deploy             = var.auto_deploy
  }

  # Implicit ordering: module.network creates the IGW before compute runs;
  # the EIP can attach as soon as aws_instance.main.id is known.
}
