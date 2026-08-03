variable "name_prefix" {
  description = "Prefix used to name IAM roles, profiles, and inline policies."
  type        = string
}

variable "s3_bucket_arns" {
  description = "List of S3 bucket ARNs the ECS task role can read/write (e.g. MLflow artifacts)."
  type        = list(string)
}

variable "ssm_parameter_arn_prefix" {
  description = <<-EOT
    ARN prefix for SSM parameters the ECS task execution role can read
    (to resolve container `secrets` blocks). Format:
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
  description = "S3 actions allowed on the bucket ARNs (ECS task role)."
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

variable "ssm_actions" {
  description = "SSM actions allowed on the parameter ARN prefix (ECS task execution role)."
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

variable "ecs_instance_policy_arn" {
  description = "AWS-managed policy ARN letting the ECS agent register the EC2 instance with the cluster."
  type        = string
  default     = "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role"
}

variable "ecs_task_execution_policy_arn" {
  description = "AWS-managed policy ARN for the ECS task execution role (ECR pull, CloudWatch Logs write)."
  type        = string
  default     = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
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
  description = "Whether to attach the CloudWatch agent managed policy to the EC2 instance role."
  type        = bool
  default     = true
}

variable "enable_ssm_session" {
  description = "Whether to attach the SSM Session Manager managed policy to the EC2 instance role."
  type        = bool
  default     = true
}
