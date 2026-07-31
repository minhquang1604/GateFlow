variable "name_prefix" {
  description = <<-EOT
    Prefix used to name all networking resources in this module.
    The environment root composes it as `project_name-env` (e.g.
    `mlops-framework-prod`).
  EOT
  type        = string
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC. Default /16 leaves room for 4 /24 subnets across 2 AZs."
  type        = string
  default     = "10.0.0.0/16"
}

variable "az_count" {
  description = <<-EOT
    Number of availability zones to span. Free-Tier safe default is 2.
    Each AZ gets one public and one private subnet.
  EOT
  type        = number
  default     = 2
}

variable "public_subnet_cidrs" {
  description = <<-EOT
    Optional explicit CIDRs for public subnets. If empty, the module
    derives two /24s from the input VPC CIDR using cidrsubnet offsets
    1 and 2.
  EOT
  type        = list(string)
  default     = []
}

variable "private_subnet_cidrs" {
  description = <<-EOT
    Optional explicit CIDRs for private subnets. If empty, the module
    derives two /24s from the input VPC CIDR using cidrsubnet offsets
    10 and 11.
  EOT
  type        = list(string)
  default     = []
}

variable "enable_dns_support" {
  description = "Whether the VPC supports DNS resolution through the AWS DNS server."
  type        = bool
  default     = true
}

variable "enable_dns_hostnames" {
  description = "Whether instances launched in the VPC get public DNS hostnames."
  type        = bool
  default     = true
}

variable "public_subnet_map_public_ip_on_launch" {
  description = "Whether instances launched in public subnets receive a public IP automatically."
  type        = bool
  default     = true
}

variable "public_subnet_tag_tier" {
  description = "Value of the `Tier` tag for public subnets."
  type        = string
  default     = "public"
}

variable "private_subnet_tag_tier" {
  description = "Value of the `Tier` tag for private subnets."
  type        = string
  default     = "private"
}

variable "public_route_default_cidr" {
  description = "Destination CIDR for the public route table's default route (typically 0.0.0.0/0)."
  type        = string
  default     = "0.0.0.0/0"
}
