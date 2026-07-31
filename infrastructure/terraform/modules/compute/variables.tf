variable "name_prefix" {
  description = "Prefix used to name the EC2 instance, EIP, keypair, and root volume."
  type        = string
}

variable "instance_type" {
  description = "EC2 instance type. Free-Tier covers t3.micro for the first 12 months."
  type        = string
}

variable "ebs_size_gb" {
  description = "Root EBS volume size in GB. Free-Tier includes 30 GB/month of gp2/gp3 storage."
  type        = number
}

variable "ebs_volume_type" {
  description = "Root EBS volume type."
  type        = string
  default     = "gp3"
}

variable "ebs_encrypted" {
  description = "Whether the root EBS volume is encrypted."
  type        = bool
  default     = true
}

variable "monitoring" {
  description = "Whether detailed CloudWatch monitoring is enabled (extra cost)."
  type        = bool
  default     = false
}

variable "associate_public_ip_address" {
  description = "Whether to attach a public IP at boot. The EIP provides the durable public address."
  type        = bool
  default     = true
}

variable "subnet_id" {
  description = "Subnet ID where the instance is launched (typically a public subnet)."
  type        = string
}

variable "vpc_security_group_ids" {
  description = "Security group IDs to attach to the instance."
  type        = list(string)
}

variable "iam_instance_profile_name" {
  description = "IAM instance profile name (created by the iam module)."
  type        = string
}

variable "ssh_public_key" {
  description = <<-EOT
    SSH public key (single line, e.g. contents of ~/.ssh/id_ed25519.pub).
    If empty, no key pair is created (instance can be reached via SSM
    Session Manager only).
  EOT
  type        = string
  default     = ""
}

variable "ami_owners" {
  description = "List of AMI owner IDs. Default is Amazon's official account."
  type        = list(string)
  default     = ["137112412989"]
}

variable "ami_name_filter" {
  description = "Name filter for the AMI selection. Default is the latest Amazon Linux 2023 x86_64."
  type        = string
  default     = "al2023-ami-2023.*-x86_64"
}

variable "ami_virtualization_type" {
  description = "Virtualization type filter for the AMI selection."
  type        = string
  default     = "hvm"
}

variable "user_data_template_path" {
  description = <<-EOT
    Path to the userdata template. If null, the module uses the
    bundled script `userdata/ec2_init.sh.tftpl` in this module.
  EOT
  type        = string
  default     = null
}

variable "user_data_vars" {
  description = <<-EOT
    Map of template variables to pass to templatefile(). Keys must
    match the placeholders in the userdata script (e.g. `db_host`,
    `ssm_prefix`, `auto_deploy`).
  EOT
  type        = map(any)
  default     = {}
}
