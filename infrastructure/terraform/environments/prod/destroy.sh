#!/usr/bin/env bash
# Full teardown of this environment, in the order that actually works.
#
# `terraform destroy` alone hit three real, independently-discovered
# failure modes on this stack, all on the same run:
#
#   1. The `datasets`, `mlflow-artifacts` and `airflow-logs` S3 buckets
#      are deliberately `force_destroy = false` — a guardrail against
#      `terraform destroy` on the wrong workspace silently wiping real
#      data. Terraform therefore refuses to delete a non-empty bucket,
#      and `aws s3 rm --recursive` alone does not empty one: these
#      buckets had versioning enabled and later suspended, and a
#      suspended bucket still keeps every prior version and delete
#      marker until they're purged explicitly.
#
#   2. The compute module's EC2 fleet sits in a *public* subnet with
#      auto-assigned public IPs. Nothing in the Terraform graph forces
#      those instances to terminate before the Internet Gateway
#      detaches — compute doesn't reference the IGW and the IGW doesn't
#      reference compute, so they have no ordering relationship at all.
#      (Adding one — `depends_on = [module.compute]` on the IGW — was
#      tried and reverted: it fixes destroy order but also delays the
#      IGW's *creation* until after the fleet exists on a fresh apply,
#      which risks a boot-time race between instance launch and the
#      ECS agent's own bootstrap needing an internet route. See the
#      network module's README.) So the IGW detach fails with
#      `DependencyViolation: ... has some mapped public address(es)`
#      unless the ASG is destroyed first.
#
#   3. The 5 ECS services can finish deleting on AWS's side — confirmed
#      by `aws ecs list-services` returning empty — faster than AWS
#      reflects that in `DescribeServices`' own status field, which can
#      still read DRAINING well past Terraform's 20-minute waiter.
#      `force_delete_services = true` (this stack's default) only lets
#      the delete proceed despite non-zero running tasks; it does not
#      skip the provider's post-delete wait for the service to reach
#      INACTIVE, so the waiter can still time out even though the
#      service is, for every practical purpose, already gone.
#
# This script sequences around all three and is safe to re-run — every
# step either no-ops or picks up where a previous attempt left off.
set -euo pipefail
cd "$(dirname "$0")"

if [ -z "${TF_VAR_db_password:-}" ]; then
  echo "==> Reading db password from SSM (TF_VAR_db_password not set)"
  export TF_VAR_db_password
  TF_VAR_db_password=$(aws ssm get-parameter \
    --name /mlops-framework/prod/db/password --with-decryption \
    --query Parameter.Value --output text)
fi

echo "==> This will DESTROY the mlops-framework-prod environment,"
echo "    including the RDS instance (skip_final_snapshot = true — no"
echo "    backup) and the contents of every S3 bucket below. This"
echo "    cannot be undone."
read -r -p "    Type 'destroy' to continue: " confirm
if [ "$confirm" != "destroy" ]; then
  echo "Aborted."
  exit 1
fi

echo
echo "== 1/3: purging data buckets (force_destroy=false; Terraform can't) =="
# Every prior version and delete marker, not just the current object —
# a "Suspended" (not "Disabled") versioning state keeps both around.
# Bucket names carry a random suffix (module.s3's random_id), so they're
# discovered by prefix rather than hardcoded.
buckets=$(aws s3api list-buckets \
  --query "Buckets[?starts_with(Name, 'mlops-framework-')].Name" \
  --output text)
for bucket in $buckets; do
  echo "  -- ${bucket}"
  python3 - "$bucket" <<'PYEOF'
import json
import subprocess
import sys

bucket = sys.argv[1]
out = subprocess.run(
    ["aws", "s3api", "list-object-versions", "--bucket", bucket, "--output", "json"],
    capture_output=True, text=True, check=True,
)
data = json.loads(out.stdout)
objs = [{"Key": v["Key"], "VersionId": v["VersionId"]} for v in data.get("Versions", []) or []]
objs += [{"Key": m["Key"], "VersionId": m["VersionId"]} for m in data.get("DeleteMarkers", []) or []]
if not objs:
    print(f"     already empty")
    sys.exit(0)
print(f"     purging {len(objs)} version(s)/marker(s)")
payload = json.dumps({"Objects": objs, "Quiet": True})
subprocess.run(
    ["aws", "s3api", "delete-objects", "--bucket", bucket, "--delete", payload],
    check=True,
)
PYEOF
done

echo
echo "== 2/3: destroying the EC2 fleet first (releases the public IPs =="
echo "==      the Internet Gateway detach would otherwise fail on)     =="
terraform destroy -target=module.compute.aws_autoscaling_group.ecs \
  -var-file=terraform.tfvars -auto-approve

echo
echo "== 3/3: destroying everything else =="
set +e
terraform destroy -var-file=terraform.tfvars -auto-approve
status=$?
set -e

if [ $status -ne 0 ]; then
  echo
  echo "!! Full destroy hit an error — checking for the known ECS"
  echo "!! DRAINING/INACTIVE-timeout pattern before giving up..."
  cluster=mlops-framework-prod
  services="app mlflow airflow-webserver airflow-scheduler serving"
  # list-services only ever returns ACTIVE/DRAINING services, so an
  # empty result here means AWS has genuinely finished deleting them —
  # describe-services can still report a stale DRAINING for a service
  # that no longer exists in every way that matters.
  still_listed=$(aws ecs list-services --cluster "$cluster" --output text)
  if [ -z "$still_listed" ]; then
    echo "!! Confirmed: 0 services in 'aws ecs list-services' — they are"
    echo "!! actually gone. Removing them from state so Terraform stops"
    echo "!! waiting on a status field AWS just hasn't updated yet."
    for svc in $services; do
      terraform state rm "module.ecs.aws_ecs_service.this[\"${svc}\"]" 2>/dev/null || true
    done
    echo "==> Retrying the full destroy..."
    terraform destroy -var-file=terraform.tfvars -auto-approve
  else
    echo "!! Services still genuinely active: $still_listed"
    echo "!! This is not the known pattern — re-run this script, or"
    echo "!! investigate manually before retrying."
    exit $status
  fi
fi

echo
echo "Destroy complete."
