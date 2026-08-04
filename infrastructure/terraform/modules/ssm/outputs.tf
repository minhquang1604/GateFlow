locals {
  # Helper: build the parameter_names / parameter_arns maps. Always
  # includes db/password if it was created.
  parameter_names = merge(
    var.db_password != null ? {
      (var.db_password_name_suffix) = aws_ssm_parameter.db_password[0].name
    } : {},
    {
      for k, p in aws_ssm_parameter.generated : k => p.name
    }
  )

  parameter_arns = merge(
    var.db_password != null ? {
      (var.db_password_name_suffix) = aws_ssm_parameter.db_password[0].arn
    } : {},
    {
      for k, p in aws_ssm_parameter.generated : k => p.arn
    }
  )
}

output "parameter_names" {
  description = "Map of secret suffix -> full SSM parameter name."
  value       = local.parameter_names
}

output "parameter_arns" {
  description = "Map of secret suffix -> full SSM parameter ARN (pass to ECS task definition `secrets` blocks)."
  value       = local.parameter_arns
}

output "generated_secret_values" {
  description = "Map of secret suffix -> the value actually stored in SSM (sensitive)."
  value = {
    # Mirrors aws_ssm_parameter.generated.value, base64 encoding included,
    # so a caller comparing this against Parameter Store sees the same string.
    for k, v in var.generated_secrets :
    k => (
      v.base64_encode
      ? base64encode(random_password.generated[k].result)
      : random_password.generated[k].result
    )
  }
  sensitive = true
}
