###########################################################################
# IAM — three roles for the ECS-on-EC2 stack:
#
#   1. EC2 instance role  — assumed by the container instances so the
#      ECS agent can register with the cluster, plus SSM Session
#      Manager for operator access without SSH.
#   2. ECS task execution role — assumed by the ECS agent on behalf of
#      a task to pull images from ECR, write task logs to CloudWatch,
#      and resolve `secrets` blocks (SSM SecureString) into container
#      environment variables at launch time.
#   3. ECS task role — assumed by the running container's application
#      code for its own AWS API calls (e.g. MLflow writing artifacts
#      to S3).
###########################################################################

# ---------------------------------------------------------------------- #
# 1. EC2 instance role (ECS container instance).                         #
# ---------------------------------------------------------------------- #
data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ec2" {
  name               = "${var.name_prefix}-ec2-role"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json

  tags = {
    Name = "${var.name_prefix}-ec2-role"
  }
}

# Lets the ECS agent register the instance with the cluster, report
# container instance state, and pull images referenced by tasks
# scheduled onto it.
resource "aws_iam_role_policy_attachment" "ecs_instance" {
  role       = aws_iam_role.ec2.name
  policy_arn = var.ecs_instance_policy_arn
}

resource "aws_iam_role_policy_attachment" "ssm_session" {
  count = var.enable_ssm_session ? 1 : 0

  role       = aws_iam_role.ec2.name
  policy_arn = var.ssm_session_policy_arn
}

resource "aws_iam_role_policy_attachment" "cw_agent" {
  count = var.enable_cw_agent ? 1 : 0

  role       = aws_iam_role.ec2.name
  policy_arn = var.cw_agent_policy_arn
}

resource "aws_iam_instance_profile" "ec2" {
  name = "${var.name_prefix}-ec2-profile"
  role = aws_iam_role.ec2.name
}

# ---------------------------------------------------------------------- #
# 2. ECS task execution role.                                            #
# ---------------------------------------------------------------------- #
data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ecs_task_execution" {
  name               = "${var.name_prefix}-ecs-task-execution-role"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json

  tags = {
    Name = "${var.name_prefix}-ecs-task-execution-role"
  }
}

# AWS-managed policy: ECR pull + CloudWatch Logs write.
resource "aws_iam_role_policy_attachment" "ecs_task_execution" {
  role       = aws_iam_role.ecs_task_execution.name
  policy_arn = var.ecs_task_execution_policy_arn
}

# Not covered by the managed policy above: reading the SSM
# SecureString parameters referenced by each task definition's
# `secrets` block, and decrypting them with the AWS-managed SSM KMS
# key.
data "aws_iam_policy_document" "ecs_task_execution_ssm" {
  statement {
    sid       = "AllowSSMRead"
    effect    = "Allow"
    actions   = var.ssm_actions
    resources = [var.ssm_parameter_arn_prefix]
  }

  statement {
    sid       = "AllowKMSDecrypt"
    effect    = "Allow"
    actions   = var.kms_decrypt_actions
    resources = ["arn:aws:kms:${var.aws_region}:${var.account_id}:${var.kms_key_ssm_alias}"]
  }
}

resource "aws_iam_role_policy" "ecs_task_execution_ssm" {
  name   = "${var.name_prefix}-ecs-task-execution-ssm-read"
  role   = aws_iam_role.ecs_task_execution.id
  policy = data.aws_iam_policy_document.ecs_task_execution_ssm.json
}

# ---------------------------------------------------------------------- #
# 3. ECS task role — application-level permissions at runtime.          #
# ---------------------------------------------------------------------- #
resource "aws_iam_role" "ecs_task" {
  name               = "${var.name_prefix}-ecs-task-role"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json

  tags = {
    Name = "${var.name_prefix}-ecs-task-role"
  }
}

data "aws_iam_policy_document" "ecs_task_s3" {
  statement {
    sid     = "AllowS3OnConfiguredBuckets"
    effect  = "Allow"
    actions = var.s3_actions
    resources = flatten([
      var.s3_bucket_arns,
      [for a in var.s3_bucket_arns : "${a}/*"],
    ])
  }
}

resource "aws_iam_role_policy" "ecs_task_s3" {
  name   = "${var.name_prefix}-ecs-task-s3-rw"
  role   = aws_iam_role.ecs_task.id
  policy = data.aws_iam_policy_document.ecs_task_s3.json
}
