# ssm module

Creates SecureString parameters in SSM Parameter Store. Supports:

- An optional `db/password` parameter fed from a sensitive input.
- A `for_each` map of generated secrets (`random_password` + parameter).

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| `ssm_prefix` | `string` | required | Prefix for all parameter names |
| `db_password` | `string` sensitive | `null` | If set, creates `<prefix>/db/password` |
| `db_password_description` | `string` | description text | |
| `db_password_name_suffix` | `string` | `"db/password"` | |
| `generated_secrets` | `map(object)` | `{}` | Map of `{length, special, description}` |

Each generated secret entry accepts:

| Field | Type | Description |
|---|---|---|
| `length` | `number` | Length of the generated password |
| `special` | `bool` | Whether to include special characters |
| `description` | `string` | SSM parameter description |

## Outputs

| Name | Description |
|---|---|
| `parameter_names` | Map of suffix -> full SSM parameter name |
| `generated_secret_values` | Map of suffix -> plaintext (sensitive) |

## Example

```hcl
module "ssm" {
  source      = "../../modules/ssm"
  ssm_prefix  = "/mlops-framework/prod"
  db_password = var.db_password
  generated_secrets = {
    "airflow/fernet-key" = { length = 44, special = false, description = "..." }
    "airflow/web-secret" = { length = 32, special = false, description = "..." }
    "airflow/admin-password" = { length = 24, special = true, description = "..." }
  }
}
```
