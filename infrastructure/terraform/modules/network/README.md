# network module

Creates the Free-Tier-friendly VPC, public + private subnets across N AZs,
an internet gateway (for public subnets), route tables, and the DB subnet
group consumed by the RDS module.

**The Internet Gateway stays here, not in the environment root.** It
was moved out and given `depends_on = [module.compute]` once, to fix a
real `terraform destroy` failure (`DependencyViolation: ... has some
mapped public address(es)` — the compute module's EC2 fleet was still
running and holding public IPs when the IGW tried to detach). A live
`terraform plan` confirmed that change was cycle-free. It was reverted
anyway: `depends_on` orders both directions, so the same edge that
delays the IGW's *destruction* until after the fleet also delays its
*creation* until after the fleet on a fresh apply — and the fleet's own
boot script needs an internet route immediately to register with ECS
(see `modules/compute/userdata/ec2_init.sh.tftpl`). That trade wasn't
worth making without being able to verify the create-time path on a
real apply. See `environments/prod/destroy.sh` for how this is handled
instead — sequencing around it operationally rather than in the graph.

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| `name_prefix` | `string` | required | e.g. `mlops-framework-prod` |
| `vpc_cidr` | `string` | `"10.0.0.0/16"` | VPC CIDR |
| `az_count` | `number` | `2` | Number of AZs to span |
| `public_subnet_cidrs` | `list(string)` | `[]` (auto-derived) | Explicit CIDR overrides |
| `private_subnet_cidrs` | `list(string)` | `[]` (auto-derived) | Explicit CIDR overrides |
| `enable_dns_support` | `bool` | `true` | VPC DNS support |
| `enable_dns_hostnames` | `bool` | `true` | VPC DNS hostnames |
| `public_subnet_map_public_ip_on_launch` | `bool` | `true` | Auto-assign public IPs |
| `public_subnet_tag_tier` | `string` | `"public"` | Tier tag value for public subnets |
| `private_subnet_tag_tier` | `string` | `"private"` | Tier tag value for private subnets |
| `public_route_default_cidr` | `string` | `"0.0.0.0/0"` | Default route destination |

## Outputs

| Name | Description |
|---|---|
| `vpc_id` | VPC ID |
| `vpc_cidr` | VPC CIDR |
| `public_subnet_ids` | List of public subnet IDs |
| `private_subnet_ids` | List of private subnet IDs |
| `internet_gateway_id` | IGW ID |
| `db_subnet_group_name` | DB subnet group name |

## Example

```hcl
module "network" {
  source      = "../../modules/network"
  name_prefix = "${var.project_name}-${var.env}"
  vpc_cidr    = "10.0.0.0/16"
}
```
