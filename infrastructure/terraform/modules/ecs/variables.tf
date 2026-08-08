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

variable "managed_draining" {
  description = <<-EOT
    Whether the ECS capacity provider drains tasks off a container
    instance before the ASG terminates it. AWS defaults this to
    ENABLED, which installs an ASG lifecycle hook with a 3600s
    heartbeat.

    DISABLED here deliberately. The hook pins a terminating instance
    in Terminating:Wait until ECS confirms the drain, and when an
    instance's ECS agent is unreachable that confirmation never
    comes — the slot is then held for the full hour, well past
    Terraform's 10-minute ASG-drain wait, so `terraform destroy`
    fails. This stack runs one task per service with no in-flight
    request draining worth protecting, so the hook adds teardown
    fragility without a benefit.

    Set to "ENABLED" if you later add multi-replica services where
    graceful connection draining actually matters.
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

variable "force_delete_services" {
  description = <<-EOT
    Whether `terraform destroy` deletes ECS services without requiring
    them to be scaled to 0 running tasks first.

    true here because the graceful path depends on the container
    instance's ECS agent confirming teardown. When an agent is
    unreachable — which this stack hit repeatedly, on t3.micro,
    t3.small and m7i-flex.large alike — that confirmation never
    arrives and every service hangs in DRAINING until Terraform
    times out after 20 minutes, leaving a half-destroyed stack that
    needs manual `aws ecs delete-service --force` calls.

    This does NOT fully close that gap, confirmed on a later destroy:
    the AWS provider still waits for the service's own status field to
    read INACTIVE after the delete call succeeds, and that field has
    been observed lagging `aws ecs list-services` (which only lists
    ACTIVE/DRAINING services — an empty result means AWS already
    considers them gone) by well over 20 minutes. `force_delete` avoids
    one cause of the hang (an unconfirmed graceful drain); it does not
    avoid this one (a stale status read). See
    `environments/prod/destroy.sh`, which detects exactly this pattern
    (list-services empty, describe-services stuck) and recovers with
    `terraform state rm` rather than waiting on a field that may not
    update for a long time.

    Set to false if you later run multi-replica services where
    letting in-flight requests finish during teardown matters.
  EOT
  type        = bool
  default     = true
}

variable "placement_strategy" {
  description = <<-EOT
    Ordered placement strategy applied to every service, outermost
    first. Spread across container instances, and nothing else.

    Do NOT add `binpack` here. A placement strategy is evaluated
    over the tasks of a *single* service, not across the cluster —
    and every service in this stack runs desired_count = 1. That
    leaves `spread` on instanceId with exactly one task to place and
    therefore nothing to balance, so it always ties, and whatever
    strategy sits behind it makes the real decision. With
    `binpack` on memory in that slot the decision is, by definition,
    "pick the instance with the least free memory" — so each service
    in turn chose the host the previous one had just filled.

    Observed result on a 2 x t3.small fleet: four of the five
    services landed on one instance (1856 of 1913 MiB reserved, 57
    MiB free) while the other ran a single task with 1273 MiB idle.
    The packed instance then stopped responding on every published
    port and dropped to agentConnected = false.

    Note also that ECS's accounting understates real usage here:
    Service Connect injects an Envoy proxy container into every
    task, and its footprint is not part of the task's declared
    memory (a task showing memory = 768 reserves 768 for the
    application container alone). Four tasks means four unaccounted
    proxies, so a host ECS believes has 57 MiB free has rather less
    than that.
  EOT
  type = list(object({
    type  = string
    field = string
  }))
  default = [
    {
      type  = "spread"
      field = "instanceId"
    }
  ]
}
