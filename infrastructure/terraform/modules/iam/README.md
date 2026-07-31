# iam module

Creates an instance role suitable for attaching to the EC2 host. The
role has inline policies for ECR pull, S3 RW, and SSM read (with KMS
decrypt), plus optional AWS-managed attachments for CloudWatch agent
and SSM Session Manager.

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| `name_prefix` | `string` | required | Used in role / profile names |
| `ecr_repository_arns` | `list(string)` | required | ECR ARN list |
| `s3_bucket_arns` | `list(string)` | required | S3 ARN list |
| `ssm_parameter_arn_prefix` | `string` | required | `arn:aws:ssm:<region>:<account>:parameter/<prefix>/*` |
| `aws_region` | `string` | required | For KMS ARN |
| `account_id` | `string` | required | For KMS ARN |
| `s3_actions` | `list(string)` | 6 standard actions | Override allowed |
| `ecr_actions` | `list(string)` | 6 standard actions | Override allowed |
| `ssm_actions` | `list(string)` | 3 standard actions | Override allowed |
| `kms_decrypt_actions` | `list(string)` | `["kms:Decrypt"]` | |
| `kms_key_ssm_alias` | `string` | `"alias/aws/ssm"` | AWS-managed alias |
| `cw_agent_policy_arn` | `string` | AWS-managed ARN | |
| `ssm_session_policy_arn` | `string` | AWS-managed ARN | |
| `enable_cw_agent` | `bool` | `true` | |
| `enable_ssm_session` | `bool` | `true` | |

## Outputs

| Name | Description |
|---|---|
| `instance_profile_name` | Attach to `aws_instance` |
| `instance_profile_arn` | |
| `role_arn` | |
| `role_name` | |

## Example

```hcl
module "iam" {
  source                   = "../../modules/iam"
  name_prefix              = local.name_prefix
  ecr_repository_arns      = values(module.ecr.repository_arns)
  s3_bucket_arns           = values(module.s3.bucket_arns)
  ssm_parameter_arn_prefix = "arn:aws:ssm:${var.aws_region}:${local.account_id}:parameter${local.ssm_prefix}/*"
  aws_region               = var.aws_region
  account_id               = local.account_id
}
```
