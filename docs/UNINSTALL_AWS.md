# Uninstalling the AWS backend

`browser-visit-tools/uninstall_aws.py` removes the AWS resources created by
`manage_vm.py` and `manage_sync_server.py`, and nothing else. It needs
`boto3` and working AWS credentials. It is independent of the laptop
uninstaller (see [UNINSTALL_LAPTOP.md](UNINSTALL_LAPTOP.md)).

`enroll_machine.py` (SQLite only) and `fetch_vm_snapshot.py` (gRPC only)
create no AWS resources.

## What is removed

| Resource | Identity | Created by |
|---|---|---|
| EC2 instance(s) | tags `bvl:role=sync-server` **and** `bvl:managed-by=browser-visit-tools` | `manage_vm create` |
| Security group | name `bvl-sync-server-sg` **and** the managed-by tag | `manage_vm` |
| IAM role | `bvl-sync-server-role` (managed policy `AmazonSSMManagedInstanceCore`, inline policy `bvl-deploy-bucket-read`) | `manage_vm` |
| IAM instance profile | `bvl-sync-server-profile` | `manage_vm` |
| S3 staging bucket | `bvl-sync-deploy-{account}-{region}`, must carry lifecycle rule `bvl-expire-staging` | `manage_sync_server` |
| Local state file | `~/.browser-visit-logger/vm.json` (or `BVL_VM_STATE_FILE`) | `manage_vm` |

The root EBS volume is deleted with the instance. The IAM role, profile and
bucket are untagged, so their exact names are their only identity. An
untagged security group with the same name is **not** ours and is left alone.
A bucket without the `bvl-expire-staging` lifecycle rule is skipped and the
run exits 1.

### Staging bucket contents

The bucket holds copies of the server TLS private keys, the Drive
service-account key and deployed binaries. The script empties it (all object
versions and delete markers) and then deletes it. This is irreversible.

## Usage

```
python3 uninstall_aws.py --dry-run --region us-west-2   # look first
python3 uninstall_aws.py --region us-west-2
```

| Flag | Meaning |
|---|---|
| `--region R` | Region. Else `BVL_AWS_REGION`, else `region` in `vm.json`. Never guessed: no region means exit 2. |
| `-n`, `--dry-run` | Print the plan and make read-only calls only. |
| `-y`, `--yes` | Skip the confirmation prompt. |

Always run `--dry-run` first. The output starts with the account ID and
region the script is talking to.

### Confirmation

Unless `--yes` is given, you must type the exact AWS account ID. A mismatch
exits 1 with nothing changed. With a non-interactive stdin and no `--yes`
the script exits 2.

### Exit codes

- `0`: success, including "nothing to remove"
- `1`: a step failed, or you declined the prompt
- `2`: usage error (no region, or non-interactive without `--yes`)

## Order of operations

1. Terminate all matching instances and wait for `terminated` (10 min timeout;
   on timeout the security group step is skipped).
2. Delete the security group (`DependencyViolation` is reported as a failure).
3. IAM: remove role from profile, delete profile, delete inline policy,
   detach the SSM policy, delete role. Each step tolerates "already gone".
   Unknown extra policies are never force-detached; `DeleteConflict` is
   reported as a failure.
4. Empty and delete the S3 bucket.
5. Delete `vm.json`, only if every step succeeded.

Run this **before** `uninstall_laptop.py --purge-data`, which deletes
`vm.json` (the source of the region fallback).

## Re-running

Every step treats "already gone" as success, so after a partial failure fix
the cause and run the script again. Failures never abort later steps, and the
state file is kept until a run succeeds completely.

## Required IAM permissions

- `sts:GetCallerIdentity`
- `ec2:DescribeInstances`, `ec2:TerminateInstances`,
  `ec2:DescribeSecurityGroups`, `ec2:DeleteSecurityGroup`
- `iam:GetRole`, `iam:GetInstanceProfile`,
  `iam:RemoveRoleFromInstanceProfile`, `iam:DeleteInstanceProfile`,
  `iam:DeleteRolePolicy`, `iam:DetachRolePolicy`, `iam:DeleteRole`
- `s3:ListBucket`, `s3:ListBucketVersions`, `s3:GetLifecycleConfiguration`,
  `s3:DeleteObject`, `s3:DeleteObjectVersion`, `s3:DeleteBucket`

## Not AWS, not touched

The Google Drive snapshot archive is not an AWS resource and is untouched.
VM-side data (`/var/lib/browser-visit-sync`, enrolled machines, rclone
config) disappears with the instance.
