# security_groups module

Creates the three security groups used by the stack:

- `sg-alb` — future ALB ingress
- `sg-app` — EC2 instance; SSH from `admin_cidr`, app ports from sg-alb
- `sg-rds` — RDS; only the app SG may connect

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| `name_prefix` | `string` | required | e.g. `mlops-framework-prod` |
| `vpc_id` | `string` | required | VPC ID |
| `admin_cidr` | `string` | `"0.0.0.0/0"` | SSH source CIDR |
| `ingress_cidr_internet` | `string` | `"0.0.0.0/0"` | Public ingress CIDR |
| `egress_cidr` | `string` | `"0.0.0.0/0"` | Egress CIDR |
| `alb_http_port` | `number` | `80` | |
| `alb_https_port` | `number` | `443` | |
| `ssh_port` | `number` | `22` | |
| `mlflow_port` | `number` | `5000` | |
| `airflow_port` | `number` | `8080` | |
| `app_port` | `number` | `8000` | |
| `serving_port` | `number` | `8001` | |
| `rds_port` | `number` | `5432` | |

## Outputs

| Name | Description |
|---|---|
| `alb_security_group_id` | |
| `app_security_group_id` | |
| `rds_security_group_id` | |

## Example

```hcl
module "security_groups" {
  source      = "../../modules/security_groups"
  name_prefix = local.name_prefix
  vpc_id      = module.network.vpc_id
  admin_cidr  = "203.0.113.7/32"
}
```
