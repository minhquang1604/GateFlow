output "instance_id" {
  description = "EC2 instance ID."
  value       = aws_instance.main.id
}

output "public_ip" {
  description = "Elastic IP attached to the instance."
  value       = aws_eip.ec2.public_ip
}

output "private_ip" {
  description = "Private IP of the instance."
  value       = aws_instance.main.private_ip
}

output "elastic_ip_allocation_id" {
  description = "Allocation ID of the Elastic IP."
  value       = aws_eip.ec2.id
}

output "key_pair_name" {
  description = "Name of the key pair, or null if ssh_public_key was empty."
  value       = try(aws_key_pair.main[0].key_name, null)
}

output "ssh_command" {
  description = "Convenience SSH command (skips when no key pair)."
  value = try(
    "ssh -i ~/.ssh/${aws_key_pair.main[0].key_name} ec2-user@${aws_eip.ec2.public_ip}",
    null,
  )
}
