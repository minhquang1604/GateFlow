# s3 module

Creates one or more S3 buckets with versioning, public-access-block, and
optional lifecycle rules. Each bucket is named
`${project_name}-${key}-${suffix}` where the suffix is auto-generated
unless `name_suffix` is specified.

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| `project_name` | `string` | required | Project name (used in bucket name) |
| `name_suffix` | `string` | `""` (auto-generated) | Suffix to append to each bucket name |
| `buckets` | `map(object)` | `{}` | Map of buckets to create |

Each bucket entry accepts:

| Field | Type | Default | Description |
|---|---|---|---|
| `purpose` | `string` | required | Free-form tag value |
| `force_destroy` | `bool` | `false` | Allow terraform destroy with non-empty bucket |
| `versioning` | `bool` | `true` | Enable bucket versioning |
| `noncurrent_expiration_days` | `number` | `null` | Days to keep noncurrent versions |
| `multipart_abort_days` | `number` | `null` | Days to abort incomplete uploads |

## Outputs

| Name | Description |
|---|---|
| `bucket_names` | List of bucket names |
| `bucket_names_by_key` | Map of input key -> bucket name |
| `bucket_arns` | Map of input key -> bucket ARN |
| `bucket_domain_names` | Map of input key -> bucket domain name |

## Example

```hcl
module "s3" {
  source       = "../../modules/s3"
  project_name = var.project_name
  buckets = {
    "mlflow-artifacts" = {
      purpose                    = "MLflow artifact store"
      noncurrent_expiration_days = 30
      multipart_abort_days       = 7
    }
    "airflow-logs" = {
      purpose = "Airflow log archive"
    }
  }
}
```
