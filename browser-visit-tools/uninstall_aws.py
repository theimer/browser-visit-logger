#!/usr/bin/env python3
"""uninstall_aws.py — tear down the AWS resources created by
``manage_vm.py`` and ``manage_sync_server.py``.

Removes, in order: sync-server EC2 instance(s) (matched by BOTH bvl tags),
the tagged security group, the IAM role/instance profile, the S3 staging
bucket (emptied first, only if it carries our lifecycle rule) and finally
the local ``vm.json`` state file (only after a fully successful run).

Region comes from --region, ``BVL_AWS_REGION`` or the state file; it is
never guessed.  The caller must type the AWS account ID to confirm
(or pass --yes).  --dry-run makes read-only calls only.

Exit codes: 0 success / nothing to remove, 1 a step failed or user
declined or an AWS error aborted the run (one 'error:' line, no
traceback), 2 usage error (no region, non-interactive without --yes, or an
unknown/abbreviated flag).
"""

import argparse
import re
import sys
import time

import _bvl_aws as aws
from botocore import exceptions as bce

SSM_CORE_POLICY_ARN = 'arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore'
INLINE_POLICY_NAME = 'bvl-deploy-bucket-read'
LIFECYCLE_RULE_ID = 'bvl-expire-staging'

_GONE_BUCKET = ('404', 'NoSuchBucket', 'NotFound')
_TAG_FILTERS = [
    {'Name': f'tag:{aws.TAG_MANAGED_BY[0]}', 'Values': [aws.TAG_MANAGED_BY[1]]},
    {'Name': f'tag:{aws.TAG_ROLE[0]}', 'Values': [aws.TAG_ROLE[1]]},
]
_DELETE_BATCH = 1000

# Credential problems are matched by class name over the MRO so that older
# botocore versions without some of these classes still import cleanly.
_CRED_ERRORS = ('NoCredentialsError', 'PartialCredentialsError',
                'CredentialRetrievalError', 'TokenRetrievalError',
                'SSOTokenLoadError', 'UnauthorizedSSOTokenError')
_CRED_CODES = ('ExpiredToken', 'ExpiredTokenException', 'RequestExpired',
               'InvalidClientTokenId', 'UnrecognizedClientException',
               'AuthFailure', 'InvalidAccessKeyId')
_DENIED_CODES = ('AccessDenied', 'AccessDeniedException',
                 'UnauthorizedOperation')
_ACTION_RE = re.compile(r'perform: ([A-Za-z0-9-]+:[A-Za-z0-9*]+)')


def _aws_error_message(e):
    """One-line 'error: ...' text for a botocore error."""
    code = aws.error_code(e)
    if (any(c.__name__ in _CRED_ERRORS for c in type(e).__mro__)
            or code in _CRED_CODES):
        return ("error: AWS credentials are missing or expired; run "
                "'aws sso login' and set AWS_PROFILE, then retry")
    if code in _DENIED_CODES:
        msg = e.response.get('Error', {}).get('Message') or ''
        m = _ACTION_RE.search(msg)
        action = (m.group(1) if m else getattr(e, 'operation_name', None)
                  or 'unknown action')
        text = (f"error: AWS access denied for {action}; "
                f"grant this permission and retry")
    elif code is not None:
        msg = e.response['Error'].get('Message', '')
        text = f"error: AWS call failed: {code}: {msg}"
    else:
        text = f"error: AWS call failed: {e}"
    return ' '.join(text.split())


class Results:
    """Collects failures so later steps still run."""
    def __init__(self):
        self.failures = []

    def fail(self, msg):
        self.failures.append(msg)
        print(f"error: {msg}", file=sys.stderr)


def _instances(ec2, states):
    resp = ec2.describe_instances(Filters=_TAG_FILTERS + [
        {'Name': 'instance-state-name', 'Values': list(states)}])
    return [i for r in resp.get('Reservations', [])
            for i in r.get('Instances', [])]


def _live_instance_ids(ec2):
    return [i['InstanceId']
            for i in _instances(ec2, aws.LIVE_STATES + ('shutting-down',))]


def _security_groups(ec2):
    resp = ec2.describe_security_groups(Filters=[
        {'Name': 'group-name', 'Values': [aws.SG_NAME]},
        {'Name': f'tag:{aws.TAG_MANAGED_BY[0]}',
         'Values': [aws.TAG_MANAGED_BY[1]]}])
    return [g['GroupId'] for g in resp.get('SecurityGroups', [])]


def _exists_iam(call, **kw):
    try:
        call(**kw)
    except Exception as e:  # noqa: BLE001
        if aws.error_code(e) != 'NoSuchEntity':
            raise
        return False
    return True


def _bucket_exists(s3, bucket):
    try:
        s3.head_bucket(Bucket=bucket)
    except Exception as e:  # noqa: BLE001
        if aws.error_code(e) not in _GONE_BUCKET:
            raise
        return False
    return True


def _is_ours(s3, bucket):
    """True if the bucket carries the staging lifecycle fingerprint."""
    try:
        resp = s3.get_bucket_lifecycle_configuration(Bucket=bucket)
    except Exception as e:  # noqa: BLE001
        if aws.error_code(e) != 'NoSuchLifecycleConfiguration':
            raise
        return False
    return any(r.get('ID') == LIFECYCLE_RULE_ID
               for r in resp.get('Rules', []))


def build_plan(ec2, iam, s3, sts, region):
    """Return (removals, pending): resources that exist, and the bucket,
    whose lifecycle rule is only verified later in delete_bucket."""
    plan = []
    pending = []
    for iid in _live_instance_ids(ec2):
        plan.append(f"EC2 instance {iid}")
    for gid in _security_groups(ec2):
        plan.append(f"security group {gid} ({aws.SG_NAME})")
    if _exists_iam(iam.get_role, RoleName=aws.ROLE_NAME):
        plan.append(f"IAM role {aws.ROLE_NAME} (+ policies)")
    if _exists_iam(iam.get_instance_profile,
                   InstanceProfileName=aws.PROFILE_NAME):
        plan.append(f"IAM instance profile {aws.PROFILE_NAME}")
    bucket = aws.staging_bucket_name(sts, region)
    if _bucket_exists(s3, bucket):
        pending.append(
            f"S3 bucket {bucket} - lifecycle rule {LIFECYCLE_RULE_ID} "
            f"not yet checked; deleted only if present")
    if aws.state_path().exists():
        plan.append(f"state file {aws.state_path()}")
    return plan, pending


def terminate_instances(ec2, dry_run, results, sleep=time.sleep,
                        monotonic=time.monotonic, timeout=600, interval=10):
    """Terminate every tagged instance and wait for 'terminated'.
    Returns False if the wait timed out (SG deletion must be skipped)."""
    ids = _live_instance_ids(ec2)
    if not ids:
        return True
    if dry_run:
        return True
    print(f"terminating {', '.join(ids)} ...")
    try:
        ec2.terminate_instances(InstanceIds=ids)
        deadline = monotonic() + timeout
        while True:
            states = [i['State']['Name'] for r in
                      ec2.describe_instances(InstanceIds=ids)
                      .get('Reservations', []) for i in r['Instances']]
            if all(s == 'terminated' for s in states):
                return True
            if monotonic() >= deadline:
                results.fail(f"instances {', '.join(ids)} not terminated "
                             f"after {timeout}s")
                return False
            sleep(interval)
    except Exception as e:  # noqa: BLE001 - record; SG step is skipped
        results.fail(f"terminate instances {', '.join(ids)}: "
                     f"{aws.error_code(e) or e}")
        return False


def delete_security_groups(ec2, dry_run, results):
    for gid in _security_groups(ec2):
        if dry_run:
            continue
        try:
            ec2.delete_security_group(GroupId=gid)
        except Exception as e:  # noqa: BLE001
            code = aws.error_code(e)
            if code == 'InvalidGroup.NotFound':
                continue
            hint = (" (still in use; terminate instances/ENIs using it)"
                    if code == 'DependencyViolation' else '')
            results.fail(f"delete security group {gid}: {e}{hint}")
        else:
            print(f"deleted security group {gid}")


def delete_iam(iam, dry_run, results):
    if dry_run:
        return
    steps = [
        ('remove role from instance profile',
         iam.remove_role_from_instance_profile,
         dict(InstanceProfileName=aws.PROFILE_NAME, RoleName=aws.ROLE_NAME)),
        ('delete instance profile', iam.delete_instance_profile,
         dict(InstanceProfileName=aws.PROFILE_NAME)),
        ('delete inline policy', iam.delete_role_policy,
         dict(RoleName=aws.ROLE_NAME, PolicyName=INLINE_POLICY_NAME)),
        ('detach SSM policy', iam.detach_role_policy,
         dict(RoleName=aws.ROLE_NAME, PolicyArn=SSM_CORE_POLICY_ARN)),
        ('delete role', iam.delete_role, dict(RoleName=aws.ROLE_NAME)),
    ]
    for label, call, kw in steps:
        try:
            call(**kw)
        except Exception as e:  # noqa: BLE001
            if aws.error_code(e) != 'NoSuchEntity':
                results.fail(f"IAM {label}: {e}")
        else:
            print(f"IAM: {label} ok")


def _version_batches(s3, bucket):
    """Yield lists of {Key, VersionId} of at most 1000 entries."""
    batch = []
    kwargs = {'Bucket': bucket}
    while True:
        page = s3.list_object_versions(**kwargs)
        for obj in page.get('Versions', []) + page.get('DeleteMarkers', []):
            batch.append({'Key': obj['Key'], 'VersionId': obj['VersionId']})
            if len(batch) == _DELETE_BATCH:
                yield batch
                batch = []
        if not page.get('IsTruncated'):
            break
        kwargs['KeyMarker'] = page.get('NextKeyMarker')
        kwargs['VersionIdMarker'] = page.get('NextVersionIdMarker')
    if batch:
        yield batch


def delete_bucket(s3, sts, region, dry_run, results):
    bucket = aws.staging_bucket_name(sts, region)
    try:
        if not _bucket_exists(s3, bucket):
            return
        ours = _is_ours(s3, bucket)
    except Exception as e:  # noqa: BLE001 - record and continue
        results.fail(f"bucket {bucket}: {aws.error_code(e) or e}")
        return
    if not ours:
        results.fail(f"bucket {bucket} lacks lifecycle rule "
                     f"{LIFECYCLE_RULE_ID}; it is not ours - SKIPPED")
        return
    print(f"bucket {bucket}: lifecycle rule {LIFECYCLE_RULE_ID} found")
    if dry_run:
        print(f"  would remove: S3 bucket {bucket} "
              f"(all objects and versions)")
        return
    try:
        for batch in _version_batches(s3, bucket):
            resp = s3.delete_objects(
                Bucket=bucket, Delete={'Objects': batch, 'Quiet': True}) or {}
            errors = resp.get('Errors', [])
            if errors:
                results.fail(f"bucket {bucket}: {len(errors)} object(s) "
                             f"could not be deleted (first: {errors[0]}); "
                             f"not deleting")
                return
        s3.delete_bucket(Bucket=bucket)
    except Exception as e:  # noqa: BLE001 - record and continue
        results.fail(f"bucket {bucket}: {aws.error_code(e) or e}")
        return
    print(f"deleted bucket {bucket}")


def _resolve_region(arg_region):
    region = aws.resolve_region(arg_region)
    if region:
        return region
    return aws.load_state().get('region')


def _build_parser():
    p = argparse.ArgumentParser(
        prog='uninstall_aws.py',
        description='Tear down the browser-visit-logger AWS infrastructure.',
        allow_abbrev=False)
    p.add_argument('--region', help='AWS region (else BVL_AWS_REGION, '
                   'else the state file)')
    p.add_argument('-n', '--dry-run', action='store_true',
                   help='list what would be removed; read-only AWS calls')
    p.add_argument('-y', '--yes', action='store_true',
                   help='skip the account-ID confirmation')
    return p


def run(args):
    region = _resolve_region(args.region)
    if not region:
        print("error: no region; pass --region, set BVL_AWS_REGION or "
              "create the state file first", file=sys.stderr)
        return 2
    sts = aws.client('sts', region)
    account = sts.get_caller_identity()['Account']
    print(f"account={account} region={region}")
    ec2 = aws.client('ec2', region)
    iam = aws.client('iam', region)
    s3 = aws.client('s3', region)

    plan, pending = build_plan(ec2, iam, s3, sts, region)
    if not plan and not pending:
        print("nothing to remove")
        return 0
    dry = args.dry_run
    prefix = 'would remove' if dry else 'will remove'
    for line in plan:
        print(f"  {prefix}: {line}")
    for line in pending:
        print(f"  unverified: {line}")

    if not dry and not args.yes:
        try:
            typed = input(f"Type the account ID ({account}) to confirm: ")
        except EOFError:
            print("error: non-interactive; pass --yes to proceed",
                  file=sys.stderr)
            return 2
        if typed.strip() != account:
            print("aborted; nothing changed")
            return 1

    # In dry-run the same steps run with dry=True: reads only, no mutations.
    results = Results()
    if terminate_instances(ec2, dry, results):
        delete_security_groups(ec2, dry, results)
    else:
        print("warn: skipping security group (instances still alive)",
              file=sys.stderr)
    delete_iam(iam, dry, results)
    delete_bucket(s3, sts, region, dry, results)
    if results.failures:
        print(f"{len(results.failures)} step(s) failed; state file kept",
              file=sys.stderr)
        return 1
    state = aws.state_path()
    if not dry and state.exists():
        state.unlink()
        print(f"removed {state}")
    print("dry run complete" if dry else "teardown complete")
    return 0


def main(argv):
    args = _build_parser().parse_args(argv)
    try:
        return run(args)
    except (RuntimeError, TimeoutError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except (bce.ClientError, bce.BotoCoreError,
            bce.NoCredentialsError) as e:
        print(_aws_error_message(e), file=sys.stderr)
        return 1


if __name__ == '__main__':  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
