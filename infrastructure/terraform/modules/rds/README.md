# rds module

Creates a single PostgreSQL RDS instance plus a parameter group. All
RDS-specific knobs (engine version, storage type, backup windows, etc.)
are tunable; the Free-Tier-safe defaults are wired in.

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| `name_prefix` | `string` | required | Used in resource names |
| `identifier` | `string` | `${name_prefix}-postgres` | |
| `engine_version` | `string` | `"15.7"` | |
| `parameter_group_family` | `string` | `"postgres15"` | |
| `parameter_group_description` | `string` | description text | |
| `parameters` | `list(object)` | `log_min_duration_statement=1000`, `max_connections=100` | |
| `instance_class` | `string` | required | e.g. `db.t3.micro` |
| `allocated_storage_gb` | `number` | required | |
| `max_allocated_storage_gb` | `number` | `0` | 0 = disable autoscaling |
| `storage_type` | `string` | `"gp2"` | |
| `storage_encrypted` | `bool` | `true` | |
| `kms_key_id` | `string` | `null` | default KMS key |
| `db_name` | `string` | required | |
| `db_username` | `string` | required | |
| `db_password` | `string` sensitive | required | |
| `db_subnet_group_name` | `string` | required | |
| `vpc_security_group_ids` | `list(string)` | required | |
| `multi_az` | `bool` | `false` | |
| `publicly_accessible` | `bool` | `false` | |
| `skip_final_snapshot` | `bool` | `true` | |
| `copy_tags_to_snapshot` | `bool` | `true` | |
| `deletion_protection` | `bool` | `false` | |
| `backup_retention_period` | `number` | `1` | |
| `backup_window` | `string` | `"07:00-09:00"` | UTC |
| `maintenance_window` | `string` | `"Sun:09:30-Sun:11:00"` | UTC |
| `enabled_cloudwatch_logs_exports` | `list(string)` | `["postgresql","upgrade"]` | |
| `performance_insights_enabled` | `bool` | `false` | |
| `monitoring_interval` | `number` | `0` | 0 disables |

## Outputs

| Name | Description |
|---|---|
| `endpoint` | `host:port` |
| `address` | Hostname only |
| `port` | Port |
| `db_name` | Echo of input |
| `db_username` | Echo of input |
| `arn` | RDS ARN |

## Example

```hcl
module "rds" {
  source                 = "../../modules/rds"
  name_prefix            = local.name_prefix
  instance_class         = "db.t3.micro"
  allocated_storage_gb   = 20
  db_name                = "mlops_framework"
  db_username            = "mlops_admin"
  db_password            = var.db_password
  db_subnet_group_name   = module.network.db_subnet_group_name
  vpc_security_group_ids = [module.security_groups.rds_security_group_id]
}
```
