variable "name_prefix" {
  description = "Prefix used to name the launch template, ASG, keypair, and root volume."
  type        = string
}

variable "instance_type" {
  description = "EC2 instance type. Free-Tier covers t3.micro for the first 12 months."
  type        = string
}

variable "instance_count" {
  description = <<-EOT
    Number of ECS container instances to run (ASG min = max = desired
    = this value; a fixed-size fleet, not elastic autoscaling). 2 is
    the default so the full stack (MLflow, Airflow webserver +
    scheduler, app, serving) fits across the fleet's combined memory;
    set to 1 to stay strictly inside the 750 EC2 instance-hour/month
    Free Tier pool at the cost of some services being unschedulable.
  EOT
  type        = number
  default     = 2
}

variable "root_device_name" {
  description = "Root block device name for the AMI in use (al2023 ECS-optimized uses /dev/xvda)."
  type        = string
  default     = "/dev/xvda"
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
  description = <<-EOT
    Whether container instances get a public IP at boot. There is no
    EIP or ALB in this stack, so this is the only way instances are
    reachable — note that the IP is not stable across instance
    replacement (re-run `terraform output` or query the ASG after any
    replacement).
  EOT
  type        = bool
  default     = true
}

variable "subnet_ids" {
  description = "Subnet IDs the ASG launches instances into (typically the public subnets, one per AZ)."
  type        = list(string)
}

variable "vpc_security_group_ids" {
  description = "Security group IDs to attach to each instance's primary network interface."
  type        = list(string)
}

variable "iam_instance_profile_name" {
  description = "IAM instance profile name (created by the iam module)."
  type        = string
}

variable "protect_from_scale_in" {
  description = "Whether ASG instances are protected from scale-in (irrelevant at fixed size, but required by some ECS capacity-provider configurations)."
  type        = bool
  default     = false
}

variable "ssh_public_key" {
  description = <<-EOT
    SSH public key (single line, e.g. contents of ~/.ssh/id_ed25519.pub).
    If empty, no key pair is created (instances can be reached via SSM
    Session Manager only).
  EOT
  type        = string
  default     = ""
}

variable "ecs_ami_ssm_parameter" {
  description = <<-EOT
    Public SSM parameter name that resolves to the latest
    ECS-optimized Amazon Linux 2023 AMI id. AWS keeps this parameter
    current, so no AMI id/version needs to be tracked manually.
  EOT
  type        = string
  default     = "/aws/service/ecs/optimized-ami/amazon-linux-2023/recommended/image_id"
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
    match the placeholders in the userdata script (e.g.
    `ecs_cluster_name`).
  EOT
  type        = map(any)
  default     = {}
}
