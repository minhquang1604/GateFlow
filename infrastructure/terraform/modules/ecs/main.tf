###########################################################################
# ECS module — cluster, EC2 capacity provider, Cloud Map service
# discovery, task definitions, and services.
#
# Launch type: EC2 (not Fargate). Network mode: bridge — each task
# binds a static host port on whichever container instance it lands
# on (there is no ALB in this Free-Tier stack, so containers publish
# directly on the instance's public IP). Because bridge mode gives
# every copy of a service the same well-known port on every instance,
# a Cloud Map "MULTIVALUE" A-record per service is enough for
# in-cluster service discovery (mlflow.<namespace>:5000, etc.) without
# needing the SRV-record dance that dynamic host ports would require.
#
# The capacity provider has managed scaling DISABLED — it schedules
# tasks onto whatever instances the (fixed-size) ASG already has
# running; it does not grow or shrink the ASG itself. That keeps this
# stack's container-instance fleet a static size, per the "no Auto
# Scaling" requirement, while still letting ECS use the standard
# EC2-launch-type capacity provider mechanism.
###########################################################################

# ---------------------------------------------------------------------- #
# Cluster + capacity provider                                            #
# ---------------------------------------------------------------------- #
resource "aws_ecs_cluster" "this" {
  name = var.name_prefix

  # Container Insights is a CloudWatch metrics feature billed per
  # metric — leave off for the Free-Tier stack.
  setting {
    name  = "containerInsights"
    value = "disabled"
  }

  tags = {
    Name = var.name_prefix
  }
}

resource "aws_ecs_capacity_provider" "this" {
  name = "${var.name_prefix}-cp"

  auto_scaling_group_provider {
    auto_scaling_group_arn         = var.autoscaling_group_arn
    managed_termination_protection = var.managed_termination_protection

    managed_scaling {
      status          = var.managed_scaling_status
      target_capacity = 100
    }
  }

  tags = {
    Name = "${var.name_prefix}-cp"
  }
}

resource "aws_ecs_cluster_capacity_providers" "this" {
  cluster_name       = aws_ecs_cluster.this.name
  capacity_providers = [aws_ecs_capacity_provider.this.name]

  default_capacity_provider_strategy {
    capacity_provider = aws_ecs_capacity_provider.this.name
    weight            = 1
  }
}

# ---------------------------------------------------------------------- #
# Cloud Map — private DNS namespace for in-VPC service discovery.       #
# ---------------------------------------------------------------------- #
resource "aws_service_discovery_private_dns_namespace" "this" {
  name = var.service_discovery_namespace
  vpc  = var.vpc_id

  tags = {
    Name = var.service_discovery_namespace
  }
}

resource "aws_service_discovery_service" "this" {
  for_each = var.services

  name = each.key

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.this.id

    dns_records {
      type = "A"
      ttl  = 10
    }

    routing_policy = "MULTIVALUE"
  }

  health_check_custom_config {
    failure_threshold = 1
  }
}

# ---------------------------------------------------------------------- #
# CloudWatch log groups — one per service.                               #
# ---------------------------------------------------------------------- #
resource "aws_cloudwatch_log_group" "this" {
  for_each = var.services

  name              = "/ecs/${var.name_prefix}/${each.key}"
  retention_in_days = var.log_retention_days

  tags = {
    Name = "/ecs/${var.name_prefix}/${each.key}"
  }
}

# ---------------------------------------------------------------------- #
# Task definitions.                                                      #
# ---------------------------------------------------------------------- #
resource "aws_ecs_task_definition" "this" {
  for_each = var.services

  family                   = "${var.name_prefix}-${each.key}"
  requires_compatibilities = ["EC2"]
  network_mode             = "bridge"
  execution_role_arn       = var.ecs_task_execution_role_arn
  task_role_arn            = var.ecs_task_role_arn

  container_definitions = jsonencode([
    {
      name      = each.key
      image     = each.value.image
      essential = each.value.essential
      cpu       = each.value.cpu
      memory    = each.value.memory
      memoryReservation = coalesce(
        each.value.memory_reservation,
        each.value.memory,
      )
      command = each.value.command

      portMappings = [
        {
          containerPort = each.value.container_port
          hostPort      = coalesce(each.value.host_port, each.value.container_port)
          protocol      = "tcp"
        }
      ]

      environment = [
        for k, v in each.value.environment : { name = k, value = v }
      ]

      secrets = [
        for k, v in each.value.secrets : { name = k, valueFrom = v }
      ]

      healthCheck = each.value.health_check_command == null ? null : {
        command     = each.value.health_check_command
        interval    = each.value.health_check_interval
        timeout     = each.value.health_check_timeout
        retries     = each.value.health_check_retries
        startPeriod = each.value.health_check_start_period
      }

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.this[each.key].name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = each.key
        }
      }
    }
  ])

  tags = {
    Name = "${var.name_prefix}-${each.key}"
  }
}

# ---------------------------------------------------------------------- #
# Services.                                                               #
# ---------------------------------------------------------------------- #
resource "aws_ecs_service" "this" {
  for_each = var.services

  name            = each.key
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.this[each.key].arn
  desired_count   = each.value.desired_count

  # Rolling updates on a memory-constrained fleet: don't require 2x
  # capacity mid-deploy (min 0%), and don't try to run more than one
  # copy at a time (max 100%).
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  capacity_provider_strategy {
    capacity_provider = aws_ecs_capacity_provider.this.name
    weight            = 1
  }

  # Spread tasks across container instances so the stack's combined
  # memory footprint is distributed across the fleet instead of
  # piling onto one instance.
  ordered_placement_strategy {
    type  = "spread"
    field = "instanceId"
  }

  service_registries {
    registry_arn = aws_service_discovery_service.this[each.key].arn
  }

  depends_on = [aws_ecs_cluster_capacity_providers.this]
}
