output "cluster_name" {
  description = "Name of the ECS cluster."
  value       = aws_ecs_cluster.this.name
}

output "cluster_arn" {
  description = "ARN of the ECS cluster."
  value       = aws_ecs_cluster.this.arn
}

output "capacity_provider_name" {
  description = "Name of the ECS capacity provider (backed by the compute module's ASG)."
  value       = aws_ecs_capacity_provider.this.name
}

output "service_discovery_namespace_id" {
  description = "ID of the Cloud Map private DNS namespace."
  value       = aws_service_discovery_private_dns_namespace.this.id
}

output "service_discovery_namespace_name" {
  description = "Name of the Cloud Map private DNS namespace (e.g. mlops-framework-prod.local)."
  value       = aws_service_discovery_private_dns_namespace.this.name
}

output "service_names" {
  description = "Map of service key -> ECS service name."
  value       = { for k, s in aws_ecs_service.this : k => s.name }
}

output "task_definition_arns" {
  description = "Map of service key -> current task definition ARN."
  value       = { for k, t in aws_ecs_task_definition.this : k => t.arn }
}

output "log_group_names" {
  description = "Map of service key -> CloudWatch log group name."
  value       = { for k, l in aws_cloudwatch_log_group.this : k => l.name }
}

output "service_discovery_dns_names" {
  description = "Map of service key -> in-VPC DNS name (e.g. mlflow.mlops-framework-prod.local)."
  value = {
    for k, _ in var.services :
    k => "${k}.${aws_service_discovery_private_dns_namespace.this.name}"
  }
}
