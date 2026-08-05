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

  # ECS Service Connect between container instances.
  #
  # The five rules above open exactly the five published ports to the
  # internet, which covers a browser reaching a service — but nothing
  # covers one container instance reaching another. Service Connect's
  # Envoy sidecars talk to each other's ingress listeners on *ephemeral*
  # ports assigned per task, not on the published ones, so without this
  # rule every cross-host call is dropped.
  #
  # The symptom is silent and easy to misread: same-host calls succeed
  # (they never leave the box) while cross-host calls to the identical
  # DNS name time out, so it looks like one service is unhealthy rather
  # than like a firewall. This is what kept the Airflow DAG from ever
  # running — its tasks call the app, and mlflow's tracking URI resolves
  # the same way.
  ingress {
    description = "Service Connect / dynamic port mapping between container instances"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    self        = true
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
