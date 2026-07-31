locals {
  # Helper: build the parameter_names map for output. Always includes
  # db/password if it was created.
  parameter_names = merge(
    var.db_password != null ? {
      (var.db_password_name_suffix) = aws_ssm_parameter.db_password[0].name
    } : {},
    {
      for k, p in aws_ssm_parameter.generated : k => p.name
    }
  )
}

output "parameter_names" {
  description = "Map of secret suffix -> full SSM parameter name."
  value       = local.parameter_names
}

output "generated_secret_values" {
  description = "Map of secret suffix -> generated plaintext value (sensitive)."
  value = {
    for k, p in random_password.generated : k => p.result
  }
  sensitive = true
}
