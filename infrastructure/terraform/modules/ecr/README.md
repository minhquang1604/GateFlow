# ecr module

Creates one or more ECR repositories. Each repo name is
`${project_name}/${key}`.

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| `project_name` | `string` | required | Prefix for each repo name |
| `repositories` | `map(object({purpose}))` | `{}` | Repos to create |
| `image_tag_mutability` | `string` | `"MUTABLE"` | `MUTABLE` or `IMMUTABLE` |
| `force_delete` | `bool` | `true` | Allow destroy with images inside |
| `scan_on_push` | `bool` | `true` | Scan on image push |

Each repository entry accepts:

| Field | Type | Description |
|---|---|---|
| `purpose` | `string` | Tag value (e.g. "MLflow tracking server image") |

## Outputs

| Name | Description |
|---|---|
| `repository_names` | Map of key -> AWS repo name |
| `repository_arns` | Map of key -> repo ARN |
| `repository_urls` | Map of key -> full repo URL |
| `first_repository_url` | First repo URL (used to derive the registry hostname at the root) |

## Example

```hcl
module "ecr" {
  source       = "../../modules/ecr"
  project_name = var.project_name
  repositories = {
    mlflow = { purpose = "MLflow tracking server image" }
    app    = { purpose = "Framework FastAPI app / serving runtime" }
  }
}

# Derive the registry hostname at the root:
locals {
  ecr_registry = split("/", module.ecr.first_repository_url)[0]
}
```
