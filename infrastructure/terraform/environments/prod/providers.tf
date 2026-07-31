provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project = var.project_name
      Env     = var.env
      Owner   = var.owner
      Managed = "terraform"
    }
  }
}
