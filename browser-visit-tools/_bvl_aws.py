"""
_bvl_aws.py — Internal helpers shared by ``manage_vm.py`` and
``manage_sync_server.py``.  Not a CLI entrypoint (leading underscore).

Everything that talks to AWS routes through :func:`client`, the single
seam the unit tests monkeypatch so the suite never makes a real API
call (and ``boto3`` need not be installed in the test venv — it is
imported lazily, mirroring how ``fetch_vm_snapshot.py`` treats grpc).

Contents:
  * boto3 client factory + region resolution
  * resource tags + tag-based instance discovery
  * ``vm.json`` state-file load/save (under the existing config dir)
  * SSM: wait-for-online, send-command-and-wait
  * S3: staging-bucket ensure + upload
"""

import datetime
import json
import os
import sys
import time
from collections import namedtuple
from pathlib import Path

# ---------------------------------------------------------------------------
# boto3 client factory (the test seam) + region
# ---------------------------------------------------------------------------

# Tag set stamped on every resource this tooling creates, and the basis
# for idempotent discovery.
TAG_MANAGED_BY = ('bvl:managed-by', 'browser-visit-tools')
TAG_ROLE = ('bvl:role', 'sync-server')

# Names of the (singleton) shared infra resources.
SG_NAME = 'bvl-sync-server-sg'
ROLE_NAME = 'bvl-sync-server-role'
PROFILE_NAME = 'bvl-sync-server-profile'

GRPC_PORT = 50443


def resolve_region(arg_region):
    """Region from explicit flag, else ``BVL_AWS_REGION``, else None
    (let the ambient boto3 session decide)."""
    return arg_region or os.environ.get('BVL_AWS_REGION') or None


def client(service, region=None):
    """Construct a boto3 client.  The one place the whole package
    reaches AWS — unit tests monkeypatch this to hand back fakes."""
    try:
        import boto3
    except ImportError:  # pragma: no cover - import-time environment guard
        print("error: this tool requires boto3 (pip install boto3)",
              file=sys.stderr)
        sys.exit(2)
    return boto3.client(service, region_name=region)


def error_code(exc):
    """AWS error code from a botocore ClientError-shaped exception, or
    None.  Avoids importing botocore: we read the ``response`` dict that
    every ClientError carries (and that the test fakes mimic)."""
    resp = getattr(exc, 'response', None)
    if isinstance(resp, dict):
        return resp.get('Error', {}).get('Code')
    return None


# ---------------------------------------------------------------------------
# state file (~/.browser-visit-logger/vm.json)
# ---------------------------------------------------------------------------

def state_path():
    """Location of the persisted VM state.  ``BVL_VM_STATE_FILE`` wins
    (the test-sandbox seam); otherwise it lives in the same config dir
    ``install_laptop.sh`` already establishes."""
    override = os.environ.get('BVL_VM_STATE_FILE')
    if override:
        return Path(override)
    base = os.environ.get('BVL_CONFIG_DIR') or os.path.join(
        os.path.expanduser('~'), '.browser-visit-logger')
    return Path(base) / 'vm.json'


def load_state():
    """Return the parsed state dict, or ``{}`` if no file exists."""
    p = state_path()
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def save_state(state):
    """Persist ``state`` (a dict) atomically-ish, stamping updated_at."""
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    out = dict(state)
    out['updated_at'] = datetime.datetime.now(
        datetime.timezone.utc).isoformat()
    p.write_text(json.dumps(out, indent=2, sort_keys=True) + '\n')
    return out


# ---------------------------------------------------------------------------
# tag-based instance discovery
# ---------------------------------------------------------------------------

# Instance states we treat as "this VM still exists" — terminated and
# shutting-down are deliberately excluded so a fresh create can proceed.
LIVE_STATES = ('pending', 'running', 'stopping', 'stopped')


def discover_instances(ec2):
    """Every non-terminated instance carrying the sync-server role tag.
    Returns a list of instance dicts (possibly empty)."""
    resp = ec2.describe_instances(Filters=[
        {'Name': f'tag:{TAG_ROLE[0]}', 'Values': [TAG_ROLE[1]]},
        {'Name': 'instance-state-name', 'Values': list(LIVE_STATES)},
    ])
    instances = []
    for res in resp.get('Reservations', []):
        instances.extend(res.get('Instances', []))
    return instances


def resolve_instance_id(ec2, state):
    """Find the managed instance id: trust ``state`` if its instance is
    still live, else fall back to tag discovery.  Returns the id or
    raises RuntimeError (none / ambiguous)."""
    want = state.get('instance_id')
    found = discover_instances(ec2)
    by_id = {i['InstanceId']: i for i in found}
    if want and want in by_id:
        return want
    if not found:
        raise RuntimeError(
            "no sync-server VM found (state file empty and no instance "
            f"carries tag {TAG_ROLE[0]}={TAG_ROLE[1]}); run `create` first")
    if len(found) > 1:
        ids = ', '.join(sorted(by_id))
        raise RuntimeError(
            f"ambiguous: multiple sync-server instances found ({ids}); "
            "terminate the extras or fix the state file")
    return found[0]['InstanceId']


# ---------------------------------------------------------------------------
# SSM: wait-for-online + send-command-and-wait
# ---------------------------------------------------------------------------

RemoteResult = namedtuple('RemoteResult', 'status exit_code stdout stderr')

_SSM_TERMINAL = ('Success', 'Cancelled', 'Failed', 'TimedOut')


def wait_for_ssm_online(ssm, instance_id, timeout=180, interval=5,
                        sleep=time.sleep, monotonic=time.monotonic):
    """Block until the SSM agent on ``instance_id`` reports Online.

    Commands sent before registration fail with InvalidInstanceId, so
    every remote op calls this first.  Raises TimeoutError if the agent
    doesn't come online within ``timeout`` seconds."""
    deadline = monotonic() + timeout
    while True:
        resp = ssm.describe_instance_information(Filters=[
            {'Key': 'InstanceIds', 'Values': [instance_id]}])
        infos = resp.get('InstanceInformationList', [])
        if infos and infos[0].get('PingStatus') == 'Online':
            return
        if monotonic() >= deadline:
            raise TimeoutError(
                f"SSM agent on {instance_id} not Online after {timeout}s")
        sleep(interval)


def run_remote(ssm, instance_id, commands, comment='', timeout=300,
               interval=3, sleep=time.sleep, monotonic=time.monotonic):
    """Send a RunShellScript command and block for its result.

    ``commands`` is a list of shell lines.  Returns a RemoteResult.
    Raises TimeoutError if the invocation never reaches a terminal
    state within ``timeout`` seconds."""
    sent = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName='AWS-RunShellScript',
        Comment=comment[:100],
        Parameters={'commands': list(commands)},
    )
    command_id = sent['Command']['CommandId']
    deadline = monotonic() + timeout
    while True:
        inv = ssm.get_command_invocation(
            CommandId=command_id, InstanceId=instance_id)
        status = inv.get('Status')
        if status in _SSM_TERMINAL:
            return RemoteResult(
                status=status,
                exit_code=inv.get('ResponseCode', -1),
                stdout=inv.get('StandardOutputContent', ''),
                stderr=inv.get('StandardErrorContent', ''),
            )
        if monotonic() >= deadline:
            raise TimeoutError(
                f"SSM command {command_id} still {status} after {timeout}s")
        sleep(interval)


# ---------------------------------------------------------------------------
# S3 staging bucket
# ---------------------------------------------------------------------------

def staging_bucket_name(sts, region):
    """Deterministic per-account, per-region staging bucket name."""
    account = sts.get_caller_identity()['Account']
    return f'bvl-sync-deploy-{account}-{region}'


def ensure_bucket(s3, name, region):
    """Create the staging bucket if absent and attach a 7-day expiry
    lifecycle rule.  Idempotent."""
    try:
        s3.head_bucket(Bucket=name)
    except Exception as e:  # noqa: BLE001 - re-raised unless it's a 404
        if error_code(e) not in ('404', 'NoSuchBucket', 'NotFound'):
            raise
        # us-east-1 rejects an explicit LocationConstraint; every other
        # region requires it.
        if region == 'us-east-1':
            s3.create_bucket(Bucket=name)
        else:
            s3.create_bucket(
                Bucket=name,
                CreateBucketConfiguration={'LocationConstraint': region})
    s3.put_bucket_lifecycle_configuration(
        Bucket=name,
        LifecycleConfiguration={'Rules': [{
            'ID': 'bvl-expire-staging',
            'Status': 'Enabled',
            'Filter': {'Prefix': ''},
            'Expiration': {'Days': 7},
        }]})
    return name


def upload_file(s3, path, bucket, key, encrypt=False):
    """Upload a local file to S3.  ``encrypt`` requests SSE (used for
    the TLS material)."""
    extra = {'ServerSideEncryption': 'AES256'} if encrypt else {}
    s3.upload_file(str(path), bucket, key, ExtraArgs=extra)
    return f's3://{bucket}/{key}'
