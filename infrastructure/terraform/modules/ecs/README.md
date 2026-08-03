# ecs module

Creates the ECS control plane for the stack: a cluster, an EC2
capacity provider backed by the `compute` module's Auto Scaling Group,
a Cloud Map private DNS namespace used by ECS Service Connect, and one
task definition + service per entry in `var.services`.

## Design notes

- **Launch type: EC2, network mode: bridge.** Each task publishes a
  static host port on whichever container instance it lands on — there
  is no ALB in this Free-Tier stack, so services are reached directly
  on a container instance's public IP:port (see the `compute` module's
  `instance_public_ips` output).
- **Capacity provider managed scaling is `DISABLED`.** ECS schedules
  tasks onto whatever capacity the (fixed-size) ASG already has
  running; it does not grow or shrink the ASG. The container-instance
  fleet size is controlled entirely by `compute.instance_count`.
- **Service discovery via ECS Service Connect, not classic Cloud Map
  `service_registries`.** AWS Cloud Map's `service_registries` only
  supports SRV records (not plain A records) for bridge/host network
  mode, which ordinary `http://host:port` clients (curl, httpx,
  uvicorn) can't consume without SRV-aware resolution. Service Connect
  solves this by injecting a small per-task proxy: other services call
  a bare discovery name (e.g. `http://mlflow:5000`, no namespace
  suffix) and the proxy transparently routes to wherever that service
  currently runs, regardless of which container instance. This is
  required because tasks are spread across ≥1 container instances and
  inter-service calls (e.g. `app` → `mlflow`) need a stable name
  independent of instance placement.
- **Task placement uses `spread` on `instanceId`** so the 5 services'
  combined memory footprint distributes across the fleet rather than
  stacking onto one instance.
- **Rolling deploys use `min=0% / max=100%`** healthy percent — a
  memory-constrained fleet can't afford to run two copies of a service
  simultaneously during a deploy.

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| `name_prefix` | `string` | required | Cluster/capacity-provider/log-group name prefix |
| `aws_region` | `string` | required | For the awslogs log driver |
| `vpc_id` | `string` | required | Cloud Map namespace VPC |
| `service_discovery_namespace` | `string` | required | e.g. `mlops-framework-prod.local`; Service Connect's backing Cloud Map namespace |
| `autoscaling_group_arn` | `string` | required | From `module.compute.autoscaling_group_arn` |
| `ecs_task_execution_role_arn` | `string` | required | From `module.iam.ecs_task_execution_role_arn` |
| `ecs_task_role_arn` | `string` | required | From `module.iam.ecs_task_role_arn` |
| `log_retention_days` | `number` | `7` | |
| `managed_termination_protection` | `string` | `"DISABLED"` | Requires ASG `protect_from_scale_in = true` if enabled |
| `managed_scaling_status` | `string` | `"DISABLED"` | Keeps the ASG a static fleet |
| `services` | `map(object)` | required | See below |

### `services` object fields

| Field | Type | Description |
|---|---|---|
| `image` | `string` | Full ECR image URI (repo URL + tag) |
| `container_port` | `number` | Port the container listens on |
| `host_port` | `number`, optional | Defaults to `container_port` |
| `cpu` | `number`, optional (128) | Container CPU units |
| `memory` | `number` | Hard memory limit (MiB) |
| `memory_reservation` | `number`, optional | Soft reservation for placement; defaults to `memory` |
| `command` | `list(string)`, optional | Container command override |
| `environment` | `map(string)`, optional | Plain env vars |
| `secrets` | `map(string)`, optional | Env var name -> SSM parameter ARN |
| `health_check_command` | `list(string)`, optional | e.g. `["CMD-SHELL", "curl -f http://localhost:5000/ || exit 1"]` |
| `health_check_interval` / `_timeout` / `_retries` / `_start_period` | `number` | Container health check tuning |
| `desired_count` | `number`, optional (1) | Task copies to run |
| `essential` | `bool`, optional (true) | |

## Outputs

| Name | Description |
|---|---|
| `cluster_name` / `cluster_arn` | |
| `capacity_provider_name` | |
| `service_discovery_namespace_id` / `_name` | |
| `service_names` | Map of service key -> ECS service name |
| `task_definition_arns` | Map of service key -> current task def ARN |
| `log_group_names` | Map of service key -> log group name |
| `service_connect_dns_names` | Map of service key -> Service Connect discovery name; other tasks call `http://<name>:<container_port>` directly |

## Example

```hcl
module "ecs" {
  source                       = "../../modules/ecs"
  name_prefix                  = local.name_prefix
  aws_region                   = var.aws_region
  vpc_id                       = module.network.vpc_id
  service_discovery_namespace  = "${local.name_prefix}.local"
  autoscaling_group_arn        = module.compute.autoscaling_group_arn
  ecs_task_execution_role_arn  = module.iam.ecs_task_execution_role_arn
  ecs_task_role_arn            = module.iam.ecs_task_role_arn

  services = {
    mlflow = {
      image          = "${module.ecr.repository_urls["mlflow"]}:${var.mlflow_image_tag}"
      container_port = 5000
      memory         = 300
      environment    = { POSTGRES_HOST = module.rds.address }
      secrets        = { POSTGRES_PASSWORD = module.ssm.parameter_arns["db/password"] }
    }
  }
}
```
