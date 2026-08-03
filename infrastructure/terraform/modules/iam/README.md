# iam module

Creates the three IAM roles used by the ECS-on-EC2 stack:

1. **EC2 instance role** (`aws_iam_instance_profile.ec2`) — attached to
   the ECS container instances. Lets the ECS agent register with the
   cluster (`AmazonEC2ContainerServiceforEC2Role`), plus optional SSM
   Session Manager (operator access without SSH) and CloudWatch agent.
2. **ECS task execution role** — assumed by the ECS agent on behalf of
   a task at launch time. Pulls images from ECR, writes task logs to
   CloudWatch (AWS-managed `AmazonECSTaskExecutionRolePolicy`), and
   resolves each task definition's `secrets` blocks by reading the
   referenced SSM SecureString parameters (inline policy, scoped to
   `ssm_parameter_arn_prefix`).
3. **ECS task role** — assumed by the running container's application
   code. Scoped to S3 read/write on the buckets it needs (e.g. MLflow
   writing artifacts).

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| `name_prefix` | `string` | required | Used in role / profile names |
| `s3_bucket_arns` | `list(string)` | required | ARNs the ECS task role can read/write |
| `ssm_parameter_arn_prefix` | `string` | required | `arn:aws:ssm:<region>:<account>:parameter/<prefix>/*` |
| `aws_region` | `string` | required | For KMS ARN |
| `account_id` | `string` | required | For KMS ARN |
| `s3_actions` | `list(string)` | 6 standard actions | Override allowed |
| `ssm_actions` | `list(string)` | 3 standard actions | Override allowed |
| `kms_decrypt_actions` | `list(string)` | `["kms:Decrypt"]` | |
| `kms_key_ssm_alias` | `string` | `"alias/aws/ssm"` | AWS-managed alias |
| `ecs_instance_policy_arn` | `string` | AWS-managed ARN | `AmazonEC2ContainerServiceforEC2Role` |
| `ecs_task_execution_policy_arn` | `string` | AWS-managed ARN | `AmazonECSTaskExecutionRolePolicy` |
| `cw_agent_policy_arn` | `string` | AWS-managed ARN | |
| `ssm_session_policy_arn` | `string` | AWS-managed ARN | |
| `enable_cw_agent` | `bool` | `true` | |
| `enable_ssm_session` | `bool` | `true` | |

## Outputs

| Name | Description |
|---|---|
| `instance_profile_name` | Attach to the compute module's launch template |
| `instance_profile_arn` | |
| `ec2_role_arn` | |
| `ec2_role_name` | |
| `ecs_task_execution_role_arn` | Pass to `aws_ecs_task_definition.execution_role_arn` |
| `ecs_task_role_arn` | Pass to `aws_ecs_task_definition.task_role_arn` |

## Example

```hcl
module "iam" {
  source                   = "../../modules/iam"
  name_prefix              = local.name_prefix
  s3_bucket_arns           = values(module.s3.bucket_arns)
  ssm_parameter_arn_prefix = "arn:aws:ssm:${var.aws_region}:${local.account_id}:parameter${local.ssm_prefix}/*"
  aws_region               = var.aws_region
  account_id               = local.account_id
}
```
