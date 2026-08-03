output "instance_profile_name" {
  description = "Name of the IAM instance profile attached to the ECS container instances."
  value       = aws_iam_instance_profile.ec2.name
}

output "instance_profile_arn" {
  description = "ARN of the IAM instance profile."
  value       = aws_iam_instance_profile.ec2.arn
}

output "ec2_role_arn" {
  description = "ARN of the EC2 (ECS container instance) IAM role."
  value       = aws_iam_role.ec2.arn
}

output "ec2_role_name" {
  description = "Name of the EC2 (ECS container instance) IAM role."
  value       = aws_iam_role.ec2.name
}

output "ecs_task_execution_role_arn" {
  description = "ARN of the ECS task execution role (pass to aws_ecs_task_definition.execution_role_arn)."
  value       = aws_iam_role.ecs_task_execution.arn
}

output "ecs_task_role_arn" {
  description = "ARN of the ECS task role (pass to aws_ecs_task_definition.task_role_arn)."
  value       = aws_iam_role.ecs_task.arn
}
