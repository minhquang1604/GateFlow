###########################################################################
# RDS — PostgreSQL 15, db.t3.micro, single-AZ.
#
# Free-Tier caveats baked into the defaults:
#   * db.t3.micro (db.t4g.micro is NOT free).
#   * single-AZ (Multi-AZ bills immediately).
#   * storage <= 20 GB for the first 12 months.
#   * Performance Insights + Enhanced Monitoring disabled.
###########################################################################

locals {
  effective_identifier = coalesce(var.identifier, "${var.name_prefix}-postgres")
}

resource "aws_db_parameter_group" "this" {
  name        = "${var.name_prefix}-pg15"
  family      = var.parameter_group_family
  description = var.parameter_group_description

  dynamic "parameter" {
    for_each = var.parameters
    content {
      name         = parameter.value.name
      value        = parameter.value.value
      apply_method = parameter.value.apply_method
    }
  }

  tags = {
    Name = "${var.name_prefix}-pg15"
  }
}

resource "aws_db_instance" "main" {
  identifier     = local.effective_identifier
  engine         = "postgres"
  engine_version = var.engine_version
  instance_class = var.instance_class

  allocated_storage     = var.allocated_storage_gb
  max_allocated_storage = var.max_allocated_storage_gb
  storage_type          = var.storage_type
  storage_encrypted     = var.storage_encrypted
  kms_key_id            = var.kms_key_id

  db_name  = var.db_name
  username = var.db_username
  password = var.db_password

  db_subnet_group_name   = var.db_subnet_group_name
  vpc_security_group_ids = var.vpc_security_group_ids
  parameter_group_name   = aws_db_parameter_group.this.name

  multi_az              = var.multi_az
  publicly_accessible   = var.publicly_accessible
  skip_final_snapshot   = var.skip_final_snapshot
  copy_tags_to_snapshot = var.copy_tags_to_snapshot
  deletion_protection   = var.deletion_protection

  backup_retention_period = var.backup_retention_period
  backup_window           = var.backup_window
  maintenance_window      = var.maintenance_window

  enabled_cloudwatch_logs_exports = var.enabled_cloudwatch_logs_exports
  performance_insights_enabled    = var.performance_insights_enabled
  monitoring_interval             = var.monitoring_interval

  tags = {
    Name = local.effective_identifier
  }
}
