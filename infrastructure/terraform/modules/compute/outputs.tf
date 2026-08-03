# The ASG itself doesn't expose instance ids/IPs directly, so we look
# them up via a filtered data source. This re-evaluates on every plan,
# which is correct — the whole point is surfacing current, possibly
# post-replacement instance state.
data "aws_instances" "ecs" {
  filter {
    name   = "tag:aws:autoscaling:groupName"
    values = [aws_autoscaling_group.ecs.name]
  }

  instance_state_names = ["running", "pending"]

  depends_on = [aws_autoscaling_group.ecs]
}

output "autoscaling_group_name" {
  description = "Name of the ECS container instance Auto Scaling Group."
  value       = aws_autoscaling_group.ecs.name
}

output "autoscaling_group_arn" {
  description = "ARN of the ECS container instance Auto Scaling Group (consumed by the ecs module's capacity provider)."
  value       = aws_autoscaling_group.ecs.arn
}

output "launch_template_id" {
  description = "ID of the launch template used by the ASG."
  value       = aws_launch_template.ecs.id
}

output "instance_ids" {
  description = "Current instance IDs in the ASG. Changes across instance replacement."
  value       = data.aws_instances.ecs.ids
}

output "instance_public_ips" {
  description = "Current public IPs of the ASG instances. Not stable across instance replacement — re-run `terraform output` (or query the ASG) after any replacement."
  value       = data.aws_instances.ecs.public_ips
}

output "instance_private_ips" {
  description = "Current private IPs of the ASG instances."
  value       = data.aws_instances.ecs.private_ips
}

output "key_pair_name" {
  description = "Name of the key pair, or null if ssh_public_key was empty."
  value       = try(aws_key_pair.main[0].key_name, null)
}

output "ssh_commands" {
  description = "Convenience SSH commands, one per current instance public IP (empty list when no key pair)."
  value = try(
    [for ip in data.aws_instances.ecs.public_ips : "ssh -i ~/.ssh/${aws_key_pair.main[0].key_name} ec2-user@${ip}"],
    [],
  )
}
