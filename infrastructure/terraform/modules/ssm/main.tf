###########################################################################
# SSM Parameter Store — secrets consumed by the EC2 host.
#
# The user data script pulls these via `aws ssm get-parameter
# --with-decryption` and writes them to /opt/mlops/.env (chmod 600).
#
# No Secrets Manager — it would be $0.40/secret/month, out of budget.
###########################################################################

# ---------------------------------------------------------------------- #
# DB master password — fed from var.db_password (sensitive).            #
# ---------------------------------------------------------------------- #
resource "aws_ssm_parameter" "db_password" {
  count = var.db_password != null ? 1 : 0

  name        = "${var.ssm_prefix}/${var.db_password_name_suffix}"
  description = var.db_password_description
  type        = "SecureString"
  value       = var.db_password

  tags = {
    Purpose = "Database"
  }
}

# ---------------------------------------------------------------------- #
# Generated secrets — for_each over var.generated_secrets.              #
# ---------------------------------------------------------------------- #
resource "random_password" "generated" {
  for_each = var.generated_secrets

  length  = each.value.length
  special = each.value.special
}

resource "aws_ssm_parameter" "generated" {
  for_each = var.generated_secrets

  name        = "${var.ssm_prefix}/${each.key}"
  description = each.value.description
  type        = "SecureString"
  value = (
    each.value.base64_encode
    ? base64encode(random_password.generated[each.key].result)
    : random_password.generated[each.key].result
  )

  tags = {
    Purpose = "Airflow"
  }
}
