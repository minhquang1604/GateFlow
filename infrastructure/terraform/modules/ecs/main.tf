###########################################################################
# ECS module — cluster, EC2 capacity provider, ECS Service Connect,
# task definitions, and services.
#
# Launch type: EC2 (not Fargate). Network mode: bridge — each task
# binds a static host port on whichever container instance it lands
# on (there is no ALB in this Free-Tier stack, so containers publish
# directly on the instance's public IP).
#
# Service discovery: ECS Service Connect, not classic Cloud Map
# service_registries. AWS Cloud Map's `service_registries` only
# supports type-A DNS records for bridge/host network mode with SRV
# records, which plain `http://host:port` clients (curl, httpx,
# uvicorn) can't consume without SRV-aware resolution. Service Connect
# solves this by injecting a small per-task proxy that makes plain
# `http://<discovery_name>:<port>` calls resolve and route correctly
# regardless of which container instance a task lands on — no
# application code changes, and it still uses the same Cloud Map
# namespace under the hood.
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
# Service Connect namespace — Cloud Map namespace used as the backing   #
# registry, but discovery/routing for bridge-mode tasks goes through    #
# the Service Connect proxy (see module header), not raw DNS records.   #
# ---------------------------------------------------------------------- #
resource "aws_service_discovery_private_dns_namespace" "this" {
  name = var.service_discovery_namespace
  vpc  = var.vpc_id

  tags = {
    Name = var.service_discovery_namespace
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
          name          = each.key
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

  # Placement. `spread` on instanceId balances the *number* of tasks,
  # not their size — with tasks ranging 200-1024 MiB that let four of
  # them (including the largest) pile onto one instance while the
  # other sat nearly empty, leaving no room for rolling-deploy
  # replacements. `binpack` on memory is size-aware; see
  # var.placement_strategy.
  dynamic "ordered_placement_strategy" {
    for_each = var.placement_strategy
    content {
      type  = ordered_placement_strategy.value.type
      field = ordered_placement_strategy.value.field
    }
  }

  # Service Connect: makes plain http://<discovery_name>:<port> calls
  # from other tasks resolve and route to this service, regardless of
  # which container instance a copy currently runs on. Required for
  # bridge network mode because classic Cloud Map service_registries
  # only supports SRV records there (see module header).
  service_connect_configuration {
    enabled   = true
    namespace = aws_service_discovery_private_dns_namespace.this.arn

    service {
      port_name      = each.key
      discovery_name = each.key

      client_alias {
        port     = each.value.container_port
        dns_name = each.key
      }
    }

    log_configuration {
      log_driver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.this[each.key].name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "${each.key}-service-connect"
      }
    }
  }

  depends_on = [aws_ecs_cluster_capacity_providers.this]
}
