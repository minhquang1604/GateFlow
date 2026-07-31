###########################################################################
# IAM — instance role for the EC2 host.
#
# Permissions:
#   * ECR read on the caller-specified repos.
#   * S3 read/write on the caller-specified buckets.
#   * SSM read on a caller-specified ARN prefix (for /${project}/${env}/*).
#   * CloudWatch agent (publish metrics, write logs).
#   * SSM Session Manager (optional SSH alternative).
###########################################################################

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

# ---------------------------------------------------------------------- #
# ECR read                                                                #
# ---------------------------------------------------------------------- #
data "aws_iam_policy_document" "ecr_read" {
  statement {
    sid     = "AllowECRPull"
    effect  = "Allow"
    actions = var.ecr_actions
    resources = concat(
      local.ecr_repository_arns_effective,
      # The ecr:GetAuthorizationToken action requires the * as resource.
      ["*"],
    )
  }
}

resource "aws_iam_role_policy" "ecr_read" {
  name   = "${var.name_prefix}-ec2-role-ecr-read"
  role   = aws_iam_role.ec2.id
  policy = data.aws_iam_policy_document.ecr_read.json
}

# ---------------------------------------------------------------------- #
# S3 RW                                                                   #
# ---------------------------------------------------------------------- #
data "aws_iam_policy_document" "s3_rw" {
  statement {
    sid     = "AllowS3OnConfiguredBuckets"
    effect  = "Allow"
    actions = var.s3_actions
    resources = flatten([
      local.s3_bucket_arns_effective,
      [for a in local.s3_bucket_arns_effective : "${a}/*"],
    ])
  }
}

resource "aws_iam_role_policy" "s3_rw" {
  name   = "${var.name_prefix}-ec2-role-s3-rw"
  role   = aws_iam_role.ec2.id
  policy = data.aws_iam_policy_document.s3_rw.json
}

# ---------------------------------------------------------------------- #
# SSM read + KMS decrypt for SecureString params                         #
# ---------------------------------------------------------------------- #
data "aws_iam_policy_document" "ssm_read" {
  statement {
    sid     = "AllowSSMRead"
    effect  = "Allow"
    actions = var.ssm_actions
    resources = [
      var.ssm_parameter_arn_prefix,
    ]
  }

  statement {
    sid       = "AllowKMSDecrypt"
    effect    = "Allow"
    actions   = var.kms_decrypt_actions
    resources = ["arn:aws:kms:${var.aws_region}:${var.account_id}:${var.kms_key_ssm_alias}"]
  }
}

resource "aws_iam_role_policy" "ssm_read" {
  name   = "${var.name_prefix}-ec2-role-ssm-read"
  role   = aws_iam_role.ec2.id
  policy = data.aws_iam_policy_document.ssm_read.json
}

# ---------------------------------------------------------------------- #
# CloudWatch agent (AWS managed)                                          #
# ---------------------------------------------------------------------- #
resource "aws_iam_role_policy_attachment" "cw_agent" {
  count = var.enable_cw_agent ? 1 : 0

  role       = aws_iam_role.ec2.id
  policy_arn = var.cw_agent_policy_arn
}

# ---------------------------------------------------------------------- #
# SSM Session Manager (AWS managed)                                       #
# ---------------------------------------------------------------------- #
resource "aws_iam_role_policy_attachment" "ssm_session" {
  count = var.enable_ssm_session ? 1 : 0

  role       = aws_iam_role.ec2.id
  policy_arn = var.ssm_session_policy_arn
}

# ---------------------------------------------------------------------- #
# Instance profile — associated with the EC2 instance.                   #
# ---------------------------------------------------------------------- #
resource "aws_iam_instance_profile" "ec2" {
  name = "${var.name_prefix}-ec2-profile"
  role = aws_iam_role.ec2.name
}
