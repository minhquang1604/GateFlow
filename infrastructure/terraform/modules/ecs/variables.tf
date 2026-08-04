variable "name_prefix" {
  description = "Prefix used to name the cluster, capacity provider, task definitions, services, and log groups."
  type        = string
}

variable "aws_region" {
  description = "Region used in the awslogs log driver configuration."
  type        = string
}

variable "vpc_id" {
  description = "VPC ID the Cloud Map private DNS namespace attaches to."
  type        = string
}

variable "service_discovery_namespace" {
  description = <<-EOT
    Private DNS namespace name (e.g. "mlops-framework-prod.local").
    Each service registers as `<service-key>.<namespace>`, resolvable
    only from inside the VPC. Bridge-mode ECS services register their
    container instance's private IP under this name, so callers reach
    a service by DNS name + its static host port instead of tracking
    which of the N container instances a task landed on.
  EOT
  type        = string
}

variable "autoscaling_group_arn" {
  description = "ARN of the ECS container instance ASG (from the compute module) that the capacity provider manages."
  type        = string
}

variable "ecs_task_execution_role_arn" {
  description = "ARN of the ECS task execution role (from the iam module)."
  type        = string
}

variable "ecs_task_role_arn" {
  description = "ARN of the ECS task role (from the iam module)."
  type        = string
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for each service's log group. Free Tier includes 5 GB of log ingestion/storage; a short retention keeps a demo stack well under that."
  type        = number
  default     = 7
}

variable "managed_termination_protection" {
  description = <<-EOT
    Whether the ECS capacity provider enables managed termination
    protection. Requires the ASG's protect_from_scale_in to be true
    when enabled; this stack uses a fixed-size ASG with
    protect_from_scale_in = false, so this stays DISABLED.
  EOT
  type        = string
  default     = "DISABLED"
}

variable "managed_scaling_status" {
  description = <<-EOT
    Whether the ECS capacity provider actively scales the underlying
    ASG based on task demand. DISABLED keeps the ASG a static,
    externally-sized fleet (matches the "no elastic Auto Scaling"
    requirement) — ECS only schedules tasks onto whatever capacity
    already exists.
  EOT
  type        = string
  default     = "DISABLED"
}

variable "services" {
  description = <<-EOT
    Map of ECS services to create. Each key is the service's short
    name (used for task family, log group, and Cloud Map DNS name —
    reachable in-VPC as `<key>.<service_discovery_namespace>`).

    Fields:
      image                     — full image URI (ECR repo URL + tag).
      container_port            — port the container listens on.
      host_port                 — host port to map to (bridge mode);
                                   defaults to container_port. Must be
                                   unique per container instance across
                                   services that can co-locate.
      cpu                       — container CPU units (soft; EC2
                                   launch type doesn't require task-
                                   level cpu/memory).
      memory                    — hard memory limit (MiB); container is
                                   killed if it exceeds this.
      memory_reservation        — soft memory reservation (MiB); used
                                   for task placement.
      command                   — optional container command override.
      environment               — map of plain (non-secret) env vars.
      secrets                   — map of env var name -> SSM parameter
                                   ARN, resolved into the container's
                                   environment at launch by the task
                                   execution role.
      health_check_command      — optional container HEALTHCHECK
                                   command (e.g.
                                   ["CMD-SHELL", "curl -f http://localhost:5000/ || exit 1"]).
                                   Omit to skip the container health
                                   check.
      health_check_interval     — seconds between health checks.
      health_check_timeout      — seconds before a health check attempt
                                   is considered failed.
      health_check_retries      — consecutive failures before the
                                   container is marked unhealthy.
      health_check_start_period — grace period before health checks
                                   count against the container.
      desired_count              — number of task copies to run.
      essential                  — whether the container's failure
                                   stops the task (always true; single-
                                   container tasks in this stack).
  EOT
  type = map(object({
    image                     = string
    container_port            = number
    host_port                 = optional(number)
    cpu                       = optional(number, 128)
    memory                    = number
    memory_reservation        = optional(number)
    command                   = optional(list(string))
    environment               = optional(map(string), {})
    secrets                   = optional(map(string), {})
    health_check_command      = optional(list(string))
    health_check_interval     = optional(number, 30)
    health_check_timeout      = optional(number, 5)
    health_check_retries      = optional(number, 3)
    health_check_start_period = optional(number, 60)
    desired_count             = optional(number, 1)
    essential                 = optional(bool, true)
  }))
}

variable "placement_strategy" {
  description = <<-EOT
    Ordered placement strategy applied to every service, outermost
    first. Defaults to spread across instances, then binpack on
    memory as the tie-breaker.

    Why this order on a multi-instance fleet: CPU is this stack's
    binding constraint, and `binpack` alone actively works against
    that — it fills one instance before touching the next, so the
    CPU-hungry services (airflow-webserver and airflow-scheduler)
    end up contending for the same host's vCPUs while the other host
    idles. Leading with `spread` on `instanceId` distributes tasks
    across hosts so those two land apart; the trailing `binpack` on
    memory then breaks ties toward the fuller instance, which keeps
    contiguous free space for rolling-deploy replacements.

    History: a pure `spread` was tried first and packed four tasks
    onto one instance because it balances task *count*, ignoring
    size. A pure `binpack` was then tried and had the CPU-contention
    problem above. The combination addresses both.
  EOT
  type = list(object({
    type  = string
    field = string
  }))
  default = [
    {
      type  = "spread"
      field = "instanceId"
    },
    {
      type  = "binpack"
      field = "memory"
    }
  ]
}
