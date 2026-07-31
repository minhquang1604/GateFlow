output "bucket_names" {
  description = "List of all bucket AWS names (in for_each iteration order)."
  value       = [for b in aws_s3_bucket.this : b.bucket]
}

# Map of bucket logical key -> bucket name (matching the input keys).
output "bucket_names_by_key" {
  description = "Map of bucket logical key -> actual AWS bucket name."
  value = {
    for k, b in aws_s3_bucket.this : k => b.bucket
  }
}

output "bucket_arns" {
  description = "Map of bucket logical key -> bucket ARN."
  value = {
    for k, b in aws_s3_bucket.this : k => b.arn
  }
}

output "bucket_domain_names" {
  description = "Map of bucket logical key -> bucket domain name."
  value = {
    for k, b in aws_s3_bucket.this : k => b.bucket_domain_name
  }
}
