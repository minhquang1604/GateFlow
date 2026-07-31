###########################################################################
# ECR repositories — for_each over var.repositories.
#
# Only the two custom images live in ECR. Public images (e.g.
# apache/airflow:2.10.4-python3.11) are pulled from Docker Hub at runtime
# to keep ECR free-tier usage under the 500 MB cap.
###########################################################################

resource "aws_ecr_repository" "this" {
  for_each = var.repositories

  name                 = "${var.project_name}/${each.key}"
  image_tag_mutability = var.image_tag_mutability
  force_delete         = var.force_delete

  image_scanning_configuration {
    scan_on_push = var.scan_on_push
  }

  tags = {
    Purpose = each.value.purpose
  }
}
