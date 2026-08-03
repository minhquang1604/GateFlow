###########################################################################
# Security groups — sg-app, sg-rds.
#
# Layout:
#   sg-app  <- SSH from admin_cidr, app ports (mlflow/airflow/app/serving)
#              opened directly to the internet (bridge-mode ECS tasks
#              publish host ports; there is no ALB in this Free-Tier
#              stack, so ingress happens straight to the container
#              instances' public IPs).
#   sg-rds  <- PostgreSQL from sg-app only.
###########################################################################

# ---------------------------------------------------------------------- #
# sg-app — ECS container instances running MLflow, Airflow, app,        #
# serving (bridge network mode, hostPort-mapped).                        #
# ---------------------------------------------------------------------- #
resource "aws_security_group" "app" {
  name        = "${var.name_prefix}-sg-app"
  description = "ECS container instances running MLflow, Airflow, framework app, and serving."
  vpc_id      = var.vpc_id

  ingress {
    description = "SSH from admin CIDR"
    from_port   = var.ssh_port
    to_port     = var.ssh_port
    protocol    = "tcp"
    cidr_blocks = [var.admin_cidr]
  }

  ingress {
    description = "MLflow UI"
    from_port   = var.mlflow_port
    to_port     = var.mlflow_port
    protocol    = "tcp"
    cidr_blocks = [var.ingress_cidr_internet]
  }

  ingress {
    description = "Airflow UI"
    from_port   = var.airflow_port
    to_port     = var.airflow_port
    protocol    = "tcp"
    cidr_blocks = [var.ingress_cidr_internet]
  }

  ingress {
    description = "Framework app"
    from_port   = var.app_port
    to_port     = var.app_port
    protocol    = "tcp"
    cidr_blocks = [var.ingress_cidr_internet]
  }

  ingress {
    description = "ServingBridge"
    from_port   = var.serving_port
    to_port     = var.serving_port
    protocol    = "tcp"
    cidr_blocks = [var.ingress_cidr_internet]
  }

  egress {
    description = "Allow all egress (pull images, talk to S3/RDS via AWS APIs)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.egress_cidr]
  }

  tags = {
    Name = "${var.name_prefix}-sg-app"
  }
}

# ---------------------------------------------------------------------- #
# sg-rds — only the app SG may connect.                                  #
# ---------------------------------------------------------------------- #
resource "aws_security_group" "rds" {
  name        = "${var.name_prefix}-sg-rds"
  description = "RDS PostgreSQL - only the app SG may connect."
  vpc_id      = var.vpc_id

  ingress {
    description     = "PostgreSQL from app SG"
    from_port       = var.rds_port
    to_port         = var.rds_port
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }

  egress {
    description = "RDS initiates no outbound traffic in our stack"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.egress_cidr]
  }

  tags = {
    Name = "${var.name_prefix}-sg-rds"
  }
}
