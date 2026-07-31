###########################################################################
# Security groups — sg-alb, sg-app, sg-rds.
#
# Layout:
#   sg-alb  <- public ingress on alb_http_port / alb_https_port; egress to sg-app.
#   sg-app  <- SSH from admin_cidr, app ports from sg-alb.
#   sg-rds  <- PostgreSQL from sg-app only.
###########################################################################

# ---------------------------------------------------------------------- #
# sg-alb — created even though the load balancer is Phase 5. Keeps the  #
# network topology honest and avoids SG churn later.                     #
# ---------------------------------------------------------------------- #
resource "aws_security_group" "alb" {
  name        = "${var.name_prefix}-sg-alb"
  description = "ALB ingress for MLflow / Airflow / framework app / serving."
  vpc_id      = var.vpc_id

  ingress {
    description = "HTTP from internet"
    from_port   = var.alb_http_port
    to_port     = var.alb_http_port
    protocol    = "tcp"
    cidr_blocks = [var.ingress_cidr_internet]
  }

  ingress {
    description = "HTTPS from internet"
    from_port   = var.alb_https_port
    to_port     = var.alb_https_port
    protocol    = "tcp"
    cidr_blocks = [var.ingress_cidr_internet]
  }

  egress {
    description = "Forward to app target group"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.egress_cidr]
  }

  tags = {
    Name = "${var.name_prefix}-sg-alb"
  }
}

# ---------------------------------------------------------------------- #
# sg-app — EC2 host running MLflow, Airflow, framework app, serving.    #
# ---------------------------------------------------------------------- #
resource "aws_security_group" "app" {
  name        = "${var.name_prefix}-sg-app"
  description = "EC2 running MLflow, Airflow, framework app, and serving."
  vpc_id      = var.vpc_id

  ingress {
    description = "SSH from admin CIDR"
    from_port   = var.ssh_port
    to_port     = var.ssh_port
    protocol    = "tcp"
    cidr_blocks = [var.admin_cidr]
  }

  ingress {
    description     = "MLflow UI from ALB"
    from_port       = var.mlflow_port
    to_port         = var.mlflow_port
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  ingress {
    description     = "Airflow UI from ALB"
    from_port       = var.airflow_port
    to_port         = var.airflow_port
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  ingress {
    description     = "Framework app from ALB"
    from_port       = var.app_port
    to_port         = var.app_port
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  ingress {
    description     = "ServingBridge from ALB"
    from_port       = var.serving_port
    to_port         = var.serving_port
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
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
