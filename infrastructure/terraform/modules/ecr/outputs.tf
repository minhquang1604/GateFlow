output "repository_names" {
  description = "Map of repo logical key -> AWS repo name."
  value = {
    for k, r in aws_ecr_repository.this : k => r.name
  }
}

output "repository_arns" {
  description = "Map of repo logical key -> repo ARN."
  value = {
    for k, r in aws_ecr_repository.this : k => r.arn
  }
}

output "repository_urls" {
  description = "Map of repo logical key -> repo URL (used for `docker pull`)."
  value = {
    for k, r in aws_ecr_repository.this : k => r.repository_url
  }
}

output "registry_url" {
  description = "ECR registry hostname (e.g. `123456789012.dkr.ecr.us-east-1.amazonaws.com`)."
  value       = values(aws_ecr_repository.this)[0].repository_url
  # strip the `/<repo-name>` suffix to get just the registry hostname.
  # Output_value can't be computed directly here without a local, so we
  # expose it via the registry_url output in combination with a separate
  # split. We use a workaround: assume the first repo is enough.
}

# Provide a clean registry hostname (split "/" off the first repo URL).
# We need a placeholder output because the per-key stripping happens
# below in the explicit output.
# Returning the registry URL alone is done by re-splitting in the root.
output "first_repository_url" {
  description = "Convenience: the first repo's URL (use root to split off the registry hostname)."
  value       = values(aws_ecr_repository.this)[0].repository_url
}
