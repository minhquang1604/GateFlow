output "endpoint" {
  description = "RDS endpoint (host:port)."
  value       = aws_db_instance.main.endpoint
}

output "address" {
  description = "RDS hostname (no port)."
  value       = aws_db_instance.main.address
}

output "port" {
  description = "RDS port."
  value       = aws_db_instance.main.port
}

output "db_name" {
  description = "Initial database name (echo of the input)."
  value       = aws_db_instance.main.db_name
}

output "db_username" {
  description = "Master username (echo of the input)."
  value       = aws_db_instance.main.username
}

output "arn" {
  description = "RDS instance ARN."
  value       = aws_db_instance.main.arn
}
