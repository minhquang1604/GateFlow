variable "project_name" {
  description = "Project name prefixed to each repo name (e.g. `mlops-framework/mlflow`)."
  type        = string
}

variable "repositories" {
  description = <<-EOT
    Map of ECR repositories to create. The key is the repo's short name
    (used in outputs). The actual AWS repo name is `project_name/key`.
  EOT
  type = map(object({
    purpose = string
  }))
  default = {}
}

variable "image_tag_mutability" {
  description = "Whether image tags are MUTABLE or IMMUTABLE. Free-Tier stack uses MUTABLE for local dev convenience."
  type        = string
  default     = "MUTABLE"
}

variable "force_delete" {
  description = "Allow repo to be deleted even if it contains images. Set false for production."
  type        = bool
  default     = true
}

variable "scan_on_push" {
  description = "Whether images are scanned for vulnerabilities when pushed."
  type        = bool
  default     = true
}
