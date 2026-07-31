###########################################################################
# S3 buckets — for_each over var.buckets.
#
# Each bucket gets versioning + an explicit public-access-block on by
# default. A lifecycle rule is added only when the caller sets
# noncurrent_expiration_days or multipart_abort_days.
###########################################################################

resource "random_id" "suffix" {
  count       = var.name_suffix == "" ? 1 : 0
  byte_length = 4
}

locals {
  effective_suffix = var.name_suffix != "" ? var.name_suffix : random_id.suffix[0].hex

  bucket_names = {
    for k, _ in var.buckets : k => "${var.project_name}-${k}-${local.effective_suffix}"
  }
}

resource "aws_s3_bucket" "this" {
  for_each = var.buckets

  bucket        = local.bucket_names[each.key]
  force_destroy = each.value.force_destroy

  tags = {
    Purpose = each.value.purpose
  }
}

resource "aws_s3_bucket_versioning" "this" {
  for_each = {
    for k, v in var.buckets : k => v if v.versioning
  }

  bucket = aws_s3_bucket.this[each.key].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "this" {
  for_each = var.buckets

  # S3 bucket names generated via for_each
  bucket = aws_s3_bucket.this[each.key].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "this" {
  for_each = {
    for k, v in var.buckets :
    k => v if v.noncurrent_expiration_days != null || v.multipart_abort_days != null
  }

  bucket = aws_s3_bucket.this[each.key].id

  rule {
    id     = "expire-noncurrent"
    status = "Enabled"

    filter {}

    dynamic "noncurrent_version_expiration" {
      for_each = each.value.noncurrent_expiration_days != null ? [each.value.noncurrent_expiration_days] : []
      content {
        noncurrent_days = noncurrent_version_expiration.value
      }
    }

    dynamic "abort_incomplete_multipart_upload" {
      for_each = each.value.multipart_abort_days != null ? [each.value.multipart_abort_days] : []
      content {
        days_after_initiation = abort_incomplete_multipart_upload.value
      }
    }
  }
}
