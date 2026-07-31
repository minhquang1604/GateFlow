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
  description = "ECR repository URIs created by this stack."
  value = {
    mlflow = module.ecr.repository_urls["mlflow"]
    app    = module.ecr.repository_urls["app"]
  }
}

output "ec2_public_ip" {
  description = "Elastic IP attached to the EC2 instance."
  value       = module.compute.public_ip
}

output "ec2_instance_id" {
  description = "EC2 instance ID (for SSM Session Manager lookups)."
  value       = module.compute.instance_id
}

output "ssh_command" {
  description = "Convenience SSH command (requires the SSH key at the default path)."
  value       = module.compute.ssh_command
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
  description = "Plaintext Airflow admin password (sensitive — only the first-boot create-user script needs it; the value lives in SSM for EC2 to pull)."
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
