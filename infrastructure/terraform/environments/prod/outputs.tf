output "rds_endpoint" {
  description = "RDS PostgreSQL endpoint (host:port)."
  value       = module.rds.endpoint
}

output "rds_address" {
  description = "RDS PostgreSQL hostname (no port)."
  value       = module.rds.address
}

output "s3_mlflow_artifacts_bucket" {
  description = "S3 bucket for MLflow artifacts."
  value       = module.s3.bucket_names_by_key["mlflow-artifacts"]
}

output "s3_airflow_logs_bucket" {
  description = "S3 bucket for Airflow log archive."
  value       = module.s3.bucket_names_by_key["airflow-logs"]
}

output "s3_app_backups_bucket" {
  description = "S3 bucket for application backups."
  value       = module.s3.bucket_names_by_key["app-backups"]
}

output "ecr_repositories" {
  description = "ECR repository URIs created by this stack. Empty until CI/CD pushes images — see deploy_instructions."
  value = {
    mlflow  = module.ecr.repository_urls["mlflow"]
    airflow = module.ecr.repository_urls["airflow"]
    app     = module.ecr.repository_urls["app"]
  }
}

output "ecs_cluster_name" {
  description = "ECS cluster name (pass to `aws ecs` CLI commands and to the GitHub Actions workflow's ECS_CLUSTER_NAME secret)."
  value       = module.ecs.cluster_name
}

output "ecs_service_names" {
  description = "Map of service key -> ECS service name."
  value       = module.ecs.service_names
}

output "ec2_instance_ids" {
  description = "Current ECS container instance IDs (list; changes across instance replacement)."
  value       = module.compute.instance_ids
}

output "ec2_public_ips" {
  description = "Current public IPs of the ECS container instances (list; not stable across instance replacement — re-run `terraform output` after any replacement)."
  value       = module.compute.instance_public_ips
}

output "ssh_commands" {
  description = "Convenience SSH commands, one per current container instance (empty list when no key pair)."
  value       = module.compute.ssh_commands
}

output "ssm_parameters" {
  description = "Names of the SSM SecureString parameters populated by this stack."
  value       = module.ssm.parameter_names
  # Marked sensitive because the value transitively depends on
  # `random_password.generated[*].result`, which is sensitive (Terraform
  # requires outputs to opt-in if any of their input dependencies are
  # sensitive). The names themselves are not actually secret, but the
  # dependency edges force this.
  sensitive = true
}

output "ssm_airflow_admin_password_value" {
  description = "Plaintext Airflow admin password (sensitive — for the operator to log in with; ECS tasks read it directly from SSM via the `secrets` block, not from this output)."
  value       = module.ssm.generated_secret_values["airflow/admin-password"]
  sensitive   = true
}

output "vpc_id" {
  description = "ID of the VPC created by this stack."
  value       = module.network.vpc_id
}

output "public_subnet_ids" {
  description = "Public subnet IDs created by this stack."
  value       = module.network.public_subnet_ids
}

output "private_subnet_ids" {
  description = "Private subnet IDs created by this stack."
  value       = module.network.private_subnet_ids
}

output "deploy_instructions" {
  description = "What to do after `terraform apply` if ECS tasks are stuck in CannotPullContainerError."
  value       = <<-EOT
    ECS services are created pointed at ECR tags '${var.mlflow_image_tag}' /
    '${var.airflow_image_tag}' / '${var.app_image_tag}', but Terraform
    never builds or pushes images —
    that is .github/workflows/deploy.yml's job. If this is the first
    apply, either:
      1. Push to main (workflow runs automatically on infra/app changes), or
      2. Trigger it manually: gh workflow run deploy.yml
    Then re-check task status:
      aws ecs describe-services --cluster ${module.ecs.cluster_name} \
        --services ${join(" ", values(module.ecs.service_names))}
  EOT
}
