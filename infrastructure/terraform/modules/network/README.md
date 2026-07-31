# network module

Creates the Free-Tier-friendly VPC, public + private subnets across N AZs,
an internet gateway (for public subnets), route tables, and the DB subnet
group consumed by the RDS module.

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
