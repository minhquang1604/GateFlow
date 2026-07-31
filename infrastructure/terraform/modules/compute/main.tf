###########################################################################
# Compute module — EC2 instance + EIP + keypair + AMI data + userdata.
#
# Wraps the boot-time concerns of the stack so the root only needs to
# pass `user_data_vars`.
###########################################################################

data "aws_partition" "current" {}
data "aws_region" "current" {}

data "aws_ami" "al2023" {
  most_recent = true
  owners      = var.ami_owners

  filter {
    name   = "name"
    values = [var.ami_name_filter]
  }

  filter {
    name   = "virtualization-type"
    values = [var.ami_virtualization_type]
  }
}

# ---------------------------------------------------------------------- #
# Key pair (conditional on var.ssh_public_key).                          #
# ---------------------------------------------------------------------- #
resource "aws_key_pair" "main" {
  count = var.ssh_public_key != "" ? 1 : 0

  key_name   = "${var.name_prefix}-keypair"
  public_key = var.ssh_public_key

  tags = {
    Name = "${var.name_prefix}-keypair"
  }
}

# ---------------------------------------------------------------------- #
# EC2 instance.                                                           #
# ---------------------------------------------------------------------- #
resource "aws_instance" "main" {
  ami                    = data.aws_ami.al2023.id
  instance_type          = var.instance_type
  subnet_id              = var.subnet_id
  vpc_security_group_ids = var.vpc_security_group_ids
  iam_instance_profile   = var.iam_instance_profile_name

  # Required to fetch the SSM params and talk to AWS APIs. The EIP
  # resource is the durable public address; this boolean just gets the
  # box online at boot.
  associate_public_ip_address = var.associate_public_ip_address

  root_block_device {
    volume_type = var.ebs_volume_type
    volume_size = var.ebs_size_gb
    encrypted   = var.ebs_encrypted

    tags = {
      Name = "${var.name_prefix}-root"
    }
  }

  key_name = try(aws_key_pair.main[0].key_name, null)

  user_data = templatefile(
    coalesce(var.user_data_template_path, "${path.module}/userdata/ec2_init.sh.tftpl"),
    var.user_data_vars,
  )

  # CloudWatch detailed monitoring is NOT free — keep basic.
  monitoring = var.monitoring

  tags = {
    Name = "${var.name_prefix}-ec2"
  }
}

# ---------------------------------------------------------------------- #
# Elastic IP.                                                             #
#                                                                       #
# EIP attaches via `instance` and the implicit dependency on             #
# `aws_instance.main.id` is enough for ordering. If the root needs     #
# the IGW to be created first (e.g. when the IGW lives in another      #
# module), pass `module.network` as `depends_on` at the module call.   #
# The depends_on passes through Terraform's graph automatically.        #
# ---------------------------------------------------------------------- #
resource "aws_eip" "ec2" {
  domain = "vpc"

  instance = aws_instance.main.id

  tags = {
    Name = "${var.name_prefix}-eip"
  }
}
