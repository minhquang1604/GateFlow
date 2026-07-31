# compute module

Wraps the EC2 boot-time concerns: AMI lookup, optional key pair, the
instance itself, root EBS, and the Elastic IP. The userdata script is
a `templatefile()` call driven by `user_data_vars`.

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| `name_prefix` | `string` | required | |
| `instance_type` | `string` | required | |
| `ebs_size_gb` | `number` | required | |
| `ebs_volume_type` | `string` | `"gp3"` | |
| `ebs_encrypted` | `bool` | `true` | |
| `monitoring` | `bool` | `false` | |
| `associate_public_ip_address` | `bool` | `true` | |
| `subnet_id` | `string` | required | |
| `vpc_security_group_ids` | `list(string)` | required | |
| `iam_instance_profile_name` | `string` | required | |
| `ssh_public_key` | `string` | `""` | Conditional keypair |
| `ami_owners` | `list(string)` | `["137112412989"]` | Amazon's official account |
| `ami_name_filter` | `string` | `"al2023-ami-2023.*-x86_64"` | |
| `ami_virtualization_type` | `string` | `"hvm"` | |
| `user_data_template_path` | `string` | module-bundled script | |
| `user_data_vars` | `map(any)` | `{}` | Templatefile vars |
| `eip_depends_on` | `any` | `[]` | Pass `module.network` to ensure IGW exists |

## Outputs

| Name | Description |
|---|---|
| `instance_id` | |
| `public_ip` | |
| `private_ip` | |
| `elastic_ip_allocation_id` | |
| `key_pair_name` | null if no key pair |
| `ssh_command` | null if no key pair |

## Example

```hcl
module "compute" {
  source                    = "../../modules/compute"
  name_prefix               = local.name_prefix
  instance_type             = "t3.micro"
  ebs_size_gb               = 20
  ssh_public_key            = var.ssh_public_key
  subnet_id                 = module.network.public_subnet_ids[0]
  vpc_security_group_ids    = [module.security_groups.app_security_group_id]
  iam_instance_profile_name = module.iam.instance_profile_name
  user_data_vars = {
    ssm_prefix = local.ssm_prefix
    db_host    = module.rds.address
    # ... other vars consumed by userdata/ec2_init.sh.tftpl
  }
  eip_depends_on = [module.network]
}
```
