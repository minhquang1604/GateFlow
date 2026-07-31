variable "name_prefix" {
  description = "Prefix used to name the IAM role, profile, and inline policies."
  type        = string
}

variable "ecr_repository_arns" {
  description = "List of ECR repository ARNs the role can pull from."
  type        = list(string)
}

variable "s3_bucket_arns" {
  description = "List of S3 bucket ARNs the role can read/write."
  type        = list(string)
}

variable "ssm_parameter_arn_prefix" {
  description = <<-EOT
    ARN prefix for SSM parameters the role can read. Format:
    `arn:aws:ssm:<region>:<account>:parameter/<prefix>/*`.
  EOT
  type        = string
}

variable "aws_region" {
  description = "Region of the AWS-managed SSM KMS key."
  type        = string
}

variable "account_id" {
  description = "AWS account ID for the AWS-managed KMS key ARN."
  type        = string
}

variable "s3_actions" {
  description = "S3 actions allowed on the bucket ARNs."
  type        = list(string)
  default = [
    "s3:GetObject",
    "s3:PutObject",
    "s3:DeleteObject",
    "s3:ListBucket",
    "s3:GetBucketVersioning",
    "s3:GetBucketLocation",
  ]
}

variable "ecr_actions" {
  description = "ECR actions allowed on the repository ARNs."
  type        = list(string)
  default = [
    "ecr:GetAuthorizationToken",
    "ecr:BatchCheckLayerAvailability",
    "ecr:GetDownloadUrlForLayer",
    "ecr:BatchGetImage",
    "ecr:DescribeRepositories",
    "ecr:ListImages",
  ]
}

variable "ssm_actions" {
  description = "SSM actions allowed on the parameter ARN prefix."
  type        = list(string)
  default = [
    "ssm:GetParameter",
    "ssm:GetParameters",
    "ssm:GetParametersByPath",
  ]
}

variable "kms_decrypt_actions" {
  description = "KMS actions allowed (used to decrypt SecureString SSM params)."
  type        = list(string)
  default     = ["kms:Decrypt"]
}

variable "kms_key_ssm_alias" {
  description = <<-EOT
    KMS alias for the AWS-managed key used to decrypt SecureString
    SSM parameters. Default is the AWS-managed alias for SSM.
  EOT
  type        = string
  default     = "alias/aws/ssm"
}

variable "cw_agent_policy_arn" {
  description = "AWS-managed policy ARN for CloudWatch agent (publish metrics, write logs)."
  type        = string
  default     = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
}

variable "ssm_session_policy_arn" {
  description = "AWS-managed policy ARN for SSM Session Manager."
  type        = string
  default     = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

variable "enable_cw_agent" {
  description = "Whether to attach the CloudWatch agent managed policy."
  type        = bool
  default     = true
}

variable "enable_ssm_session" {
  description = "Whether to attach the SSM Session Manager managed policy."
  type        = bool
  default     = true
}

variable "ecr_resource_arns" {
  description = "Deprecated: alias for `ecr_repository_arns`."
  type        = list(string)
  default     = null
}

variable "s3_resource_arns" {
  description = "Deprecated: alias for `s3_bucket_arns`."
  type        = list(string)
  default     = null
}

# Backward-compat local: prefer the new var names, fall back to old names.
locals {
  ecr_repository_arns_effective = coalesce(var.ecr_repository_arns, var.ecr_resource_arns)
  s3_bucket_arns_effective      = coalesce(var.s3_bucket_arns, var.s3_resource_arns)
}
