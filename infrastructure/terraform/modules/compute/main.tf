###########################################################################
# Compute module — ECS container instance fleet.
#
# Wraps the boot-time concerns of the ECS-EC2 launch type: the
# ECS-optimized AMI, a launch template, and a fixed-size Auto Scaling
# Group (min = max = desired = var.instance_count, no scaling policy
# attached) that gives the `ecs` module's capacity provider something
# to register instances against.
#
# A fixed-size ASG is used instead of N standalone aws_instance
# resources because ECS capacity providers require an ASG to manage
# instance-protection during scale-in and to self-heal a terminated
# instance. It does not autoscale — the requirement to avoid "Auto
# Scaling" is about elastic/dynamic capacity, not the instance-fleet
# primitive ECS itself needs.
###########################################################################

data "aws_region" "current" {}

# AWS publishes the latest ECS-optimized AMI id as a public SSM
# parameter — always current, no manual AMI id/version tracking.
data "aws_ssm_parameter" "ecs_optimized_ami" {
  name = var.ecs_ami_ssm_parameter
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
# Launch template — ECS container instance.                              #
# ---------------------------------------------------------------------- #
resource "aws_launch_template" "ecs" {
  name_prefix   = "${var.name_prefix}-ecs-"
  image_id      = data.aws_ssm_parameter.ecs_optimized_ami.value
  instance_type = var.instance_type
  key_name      = try(aws_key_pair.main[0].key_name, null)

  iam_instance_profile {
    name = var.iam_instance_profile_name
  }

  # Launch templates used by an ASG do not inherit the subnet's
  # map_public_ip_on_launch setting — it must be set explicitly here
  # so container instances get a public IP at boot (no NAT Gateway /
  # ALB in this stack, so this is how they reach the internet and are
  # reached by it).
  network_interfaces {
    associate_public_ip_address = var.associate_public_ip_address
    security_groups             = var.vpc_security_group_ids
    delete_on_termination       = true
  }

  block_device_mappings {
    device_name = var.root_device_name

    ebs {
      volume_type = var.ebs_volume_type
      volume_size = var.ebs_size_gb
      encrypted   = var.ebs_encrypted
    }
  }

  monitoring {
    # CloudWatch detailed monitoring is NOT free — keep basic.
    enabled = var.monitoring
  }

  user_data = base64encode(templatefile(
    coalesce(var.user_data_template_path, "${path.module}/userdata/ec2_init.sh.tftpl"),
    var.user_data_vars,
  ))

  tag_specifications {
    resource_type = "instance"
    tags = {
      Name = "${var.name_prefix}-ecs"
    }
  }

  tags = {
    Name = "${var.name_prefix}-ecs-lt"
  }
}

# ---------------------------------------------------------------------- #
# Auto Scaling Group — fixed-size ECS container instance fleet.          #
#                                                                          #
# min = max = desired = var.instance_count keeps this a static fleet:    #
# the ASG's only job is self-healing (replace a terminated instance)     #
# and giving the ECS capacity provider a managed target, not elastic     #
# scaling.                                                                #
# ---------------------------------------------------------------------- #
resource "aws_autoscaling_group" "ecs" {
  name                  = "${var.name_prefix}-ecs-asg"
  vpc_zone_identifier   = var.subnet_ids
  min_size              = var.instance_count
  max_size              = var.instance_count
  desired_capacity      = var.instance_count
  protect_from_scale_in = var.protect_from_scale_in

  launch_template {
    id      = aws_launch_template.ecs.id
    version = "$Latest"
  }

  # Required so the instance is reachable at boot; the subnet already
  # sets map_public_ip_on_launch, but the ASG needs this explicit
  # override to honor it (launch templates otherwise default to false
  # inside an ASG).
  # (Set at the launch template's network_interfaces if unset here.)

  tag {
    key                 = "Name"
    value               = "${var.name_prefix}-ecs"
    propagate_at_launch = true
  }

  # ECS discovers cluster membership via /etc/ecs/ecs.config on each
  # instance (rendered by userdata), not via this tag — no
  # AmazonECSManaged tag is required unless using ECS-managed draining,
  # which this fixed-size stack does not use.

  lifecycle {
    create_before_destroy = true
  }
}
