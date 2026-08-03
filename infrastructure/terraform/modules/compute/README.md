# compute module

Wraps the ECS container instance fleet: AMI lookup (latest
ECS-optimized Amazon Linux 2023, via the public
`/aws/service/ecs/optimized-ami/...` SSM parameter), an optional key
pair, a launch template, and a fixed-size Auto Scaling Group
(`min = max = desired = var.instance_count`, no scaling policy — a
static fleet, not elastic autoscaling). The `ecs` module's capacity
provider attaches to this ASG so ECS can schedule tasks across however
many instances are running.

Public IPs are **not stable**: there is no Elastic IP or ALB in this
Free-Tier stack, so each instance keeps whatever public IP the subnet
assigns it at launch. If an instance is replaced (health-check
failure, manual termination), its IP changes — re-run `terraform
output` (or `aws ec2 describe-instances --filters
"Name=tag:aws:autoscaling:groupName,Values=<asg-name>"`) to get current
IPs.

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| `name_prefix` | `string` | required | |
| `instance_type` | `string` | required | |
| `instance_count` | `number` | `2` | Fixed ASG size; set to `1` for strict Free-Tier hours at the cost of some services being unschedulable |
| `root_device_name` | `string` | `"/dev/xvda"` | Matches the AL2023 ECS-optimized AMI |
| `ebs_size_gb` | `number` | required | |
| `ebs_volume_type` | `string` | `"gp3"` | |
| `ebs_encrypted` | `bool` | `true` | |
| `monitoring` | `bool` | `false` | |
| `associate_public_ip_address` | `bool` | `true` | |
| `subnet_ids` | `list(string)` | required | ASG spans these subnets |
| `vpc_security_group_ids` | `list(string)` | required | |
| `iam_instance_profile_name` | `string` | required | |
| `protect_from_scale_in` | `bool` | `false` | |
| `ssh_public_key` | `string` | `""` | Conditional keypair |
| `ecs_ami_ssm_parameter` | `string` | latest AL2023 ECS-optimized AMI | |
| `user_data_template_path` | `string` | module-bundled script | |
| `user_data_vars` | `map(any)` | `{}` | Templatefile vars (just `ecs_cluster_name`) |

## Outputs

| Name | Description |
|---|---|
| `autoscaling_group_name` | |
| `autoscaling_group_arn` | Consumed by the `ecs` module's capacity provider |
| `launch_template_id` | |
| `instance_ids` | Current instance IDs (list; changes across replacement) |
| `instance_public_ips` | Current public IPs (list; not stable across replacement) |
| `instance_private_ips` | |
| `key_pair_name` | `null` if no key pair |
| `ssh_commands` | One SSH command per current instance (empty list when no key pair) |

## Example

```hcl
module "compute" {
  source                    = "../../modules/compute"
  name_prefix               = local.name_prefix
  instance_type             = "t3.small"
  instance_count            = var.instance_count
  ebs_size_gb               = 20
  ssh_public_key            = var.ssh_public_key
  subnet_ids                = module.network.public_subnet_ids
  vpc_security_group_ids    = [module.security_groups.app_security_group_id]
  iam_instance_profile_name = module.iam.instance_profile_name
  user_data_vars = {
    ecs_cluster_name = local.name_prefix
  }
}
```
