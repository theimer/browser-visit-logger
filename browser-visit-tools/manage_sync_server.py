#!/usr/bin/env python3
"""
manage_sync_server.py — Remotely provision, deploy, and operate the
browser-visit sync-server running on the EC2 VM, all over AWS SSM (no
SSH, no inbound port 22).

Subcommands
-----------
    provision  First-boot setup: create the bvlsync user + data dirs,
               install the systemd unit and TLS material, enable the
               service.  Idempotent; safe to re-run.
    deploy     Cross-compiled binary → S3 → VM, install to
               /usr/local/bin/sync-server, restart the service.
    start      systemctl start sync-server
    stop       systemctl stop sync-server
    restart    systemctl restart sync-server
    status     systemctl is-active + status
    logs       journalctl snapshot (NOT a live tail — for follow, use
               `aws ssm start-session`)
    health     service active + gRPC port answers + disk ok

The VM itself (and its AWS infra) is owned by the sibling
``manage_vm.py``.  Build the binary first with
``make -C ../browser-visit-sync-server build-linux GOARCH=<amd64|arm64>``.

Exit codes: 0 success, 1 remote/AWS failure, 2 usage.
"""

import argparse
import hashlib
import sys
from pathlib import Path

import _bvl_aws as aws

_HERE = Path(__file__).resolve().parent
_UNIT_FILE = _HERE.parent / 'browser-visit-sync-server' / 'deploy' / \
    'sync-server.service'

# state ``arch`` → Go GOARCH token expected in the built binary's name.
_GOARCH = {'x86_64': 'amd64', 'arm64': 'arm64'}

_DATA_DIR = '/var/lib/browser-visit-sync'
_ETC_DIR = '/etc/browser-visit-sync'


def _surface(result):
    """Print a RemoteResult's output; return a tool exit code."""
    if result.stdout:
        sys.stdout.write(result.stdout)
        if not result.stdout.endswith('\n'):
            sys.stdout.write('\n')
    if result.stderr:
        sys.stderr.write(result.stderr)
    if result.status == 'Success' and result.exit_code == 0:
        return 0
    print(f"remote command {result.status} (exit {result.exit_code})",
          file=sys.stderr)
    return 1


def _ssm_target(args):
    """Resolve the instance, build an SSM client, and wait for the agent
    to be Online.  Returns (region, ssm, instance_id)."""
    region = aws.resolve_region(args.region)
    ec2 = aws.client('ec2', region)
    instance_id = aws.resolve_instance_id(ec2, aws.load_state())
    ssm = aws.client('ssm', region)
    print(f"region={region or 'default'} instance={instance_id}")
    aws.wait_for_ssm_online(ssm, instance_id)
    return region, ssm, instance_id


def _stage_clients(region):
    """(s3, bucket) for the staging bucket, ensuring it exists."""
    sts = aws.client('sts', region)
    s3 = aws.client('s3', region)
    bucket = aws.staging_bucket_name(sts, region)
    aws.ensure_bucket(s3, bucket, region or 'us-east-1')
    return s3, bucket


def cmd_provision(args):
    region, ssm, instance_id = _ssm_target(args)
    s3, bucket = _stage_clients(region)
    aws.upload_file(s3, _UNIT_FILE, bucket, 'deploy/sync-server.service')
    aws.upload_file(s3, args.server_cert, bucket, 'tls/server.crt',
                    encrypt=True)
    aws.upload_file(s3, args.server_key, bucket, 'tls/server.key',
                    encrypt=True)
    aws.upload_file(s3, args.clients_ca, bucket, 'tls/clients-ca.crt',
                    encrypt=True)
    script = [
        'set -euo pipefail',
        'id -u bvlsync &>/dev/null || useradd --system --no-create-home '
        '--shell /usr/sbin/nologin bvlsync',
        f'mkdir -p {_DATA_DIR}/logs {_ETC_DIR}',
        f'chown -R bvlsync:bvlsync {_DATA_DIR}',
        f'chmod 750 {_ETC_DIR}',
        f'aws s3 cp s3://{bucket}/deploy/sync-server.service '
        '/etc/systemd/system/sync-server.service',
        f'aws s3 cp s3://{bucket}/tls/server.crt {_ETC_DIR}/server.crt',
        f'aws s3 cp s3://{bucket}/tls/server.key {_ETC_DIR}/server.key',
        f'aws s3 cp s3://{bucket}/tls/clients-ca.crt '
        f'{_ETC_DIR}/clients-ca.crt',
        f'chown -R bvlsync:bvlsync {_ETC_DIR}',
        f'chmod 600 {_ETC_DIR}/server.key',
        'systemctl daemon-reload',
        'systemctl enable sync-server',
    ]
    rc = _surface(aws.run_remote(ssm, instance_id, script,
                                 comment='bvl provision'))
    if rc == 0:
        print("provisioned; run `deploy` to install the binary")
    if rc == 0 and args.and_deploy:
        return _deploy(region, ssm, instance_id, args)
    return rc


def _check_arch(binary):
    """Warn/refuse if the binary's arch token doesn't match the VM's
    recorded arch.  Returns an error string, or None if ok."""
    arch = aws.load_state().get('arch')
    if not arch:
        return None
    token = _GOARCH.get(arch)
    if token and token not in Path(binary).name:
        return (f"binary {Path(binary).name!r} does not look like a "
                f"{token} build but the VM arch is {arch}; "
                "rebuild with the matching GOARCH or pass --force")
    return None


def _deploy(region, ssm, instance_id, args):
    s3, bucket = _stage_clients(region)
    data = Path(args.binary).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    key = f'sync-server/{digest}'
    aws.upload_file(s3, args.binary, bucket, key)
    script = [
        'set -euo pipefail',
        f'aws s3 cp s3://{bucket}/{key} /tmp/sync-server.new',
        'install -m 0755 /tmp/sync-server.new /usr/local/bin/sync-server',
        'rm -f /tmp/sync-server.new',
        'systemctl restart sync-server',
    ]
    print(f"deploying {Path(args.binary).name} ({digest[:12]}) ...")
    return _surface(aws.run_remote(ssm, instance_id, script,
                                   comment='bvl deploy'))


def cmd_deploy(args):
    if not Path(args.binary).is_file():
        print(f"error: binary not found: {args.binary}", file=sys.stderr)
        return 1
    problem = _check_arch(args.binary)
    if problem and not args.force:
        print(f"error: {problem}", file=sys.stderr)
        return 1
    region, ssm, instance_id = _ssm_target(args)
    return _deploy(region, ssm, instance_id, args)


def _systemctl(args, action):
    _region, ssm, instance_id = _ssm_target(args)
    return _surface(aws.run_remote(
        ssm, instance_id, [f'systemctl {action} sync-server'],
        comment=f'bvl {action}'))


def cmd_start(args):
    return _systemctl(args, 'start')


def cmd_stop(args):
    return _systemctl(args, 'stop')


def cmd_restart(args):
    return _systemctl(args, 'restart')


def cmd_status(args):
    _region, ssm, instance_id = _ssm_target(args)
    return _surface(aws.run_remote(ssm, instance_id, [
        'systemctl is-active sync-server || true',
        'systemctl status sync-server --no-pager || true',
    ], comment='bvl status'))


def cmd_logs(args):
    _region, ssm, instance_id = _ssm_target(args)
    cmd = f'journalctl -u sync-server -n {args.lines} --no-pager'
    if args.since:
        cmd += f' --since {args.since!r}'
    return _surface(aws.run_remote(ssm, instance_id, [cmd],
                                   comment='bvl logs'))


def cmd_health(args):
    _region, ssm, instance_id = _ssm_target(args)
    script = [
        'set -e',
        'systemctl is-active --quiet sync-server && echo "service: active" '
        '|| { echo "service: INACTIVE"; exit 1; }',
        f'timeout 3 bash -c "</dev/tcp/127.0.0.1/{aws.GRPC_PORT}" '
        f'&& echo "port {aws.GRPC_PORT}: open" '
        f'|| {{ echo "port {aws.GRPC_PORT}: CLOSED"; exit 1; }}',
        f'use=$(df --output=pcent {_DATA_DIR} | tail -1 | tr -dc "0-9"); '
        'echo "disk: ${use}% used"; [ "$use" -lt 90 ] || '
        '{ echo "disk: OVER 90%"; exit 1; }',
    ]
    return _surface(aws.run_remote(ssm, instance_id, script,
                                   comment='bvl health'))


_DISPATCH = {
    'provision': cmd_provision, 'deploy': cmd_deploy, 'start': cmd_start,
    'stop': cmd_stop, 'restart': cmd_restart, 'status': cmd_status,
    'logs': cmd_logs, 'health': cmd_health,
}


def _build_parser():
    p = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    sub = p.add_subparsers(dest='action', required=True)

    def with_region(sp):
        sp.add_argument('--region', help='AWS region (else BVL_AWS_REGION)')
        return sp

    pp = with_region(sub.add_parser('provision', help='first-boot setup'))
    pp.add_argument('--server-cert', required=True, help='server cert PEM')
    pp.add_argument('--server-key', required=True, help='server key PEM')
    pp.add_argument('--clients-ca', required=True,
                    help='CA bundle that signed the laptop client certs')
    pp.add_argument('--and-deploy', action='store_true',
                    help='chain a deploy after provisioning (needs --binary)')
    pp.add_argument('--binary', help='binary to deploy when --and-deploy')
    pp.add_argument('--force', action='store_true',
                    help='skip the binary/VM arch check')

    pd = with_region(sub.add_parser('deploy', help='install/update binary'))
    pd.add_argument('--binary', required=True,
                    help='path to the cross-compiled linux sync-server')
    pd.add_argument('--force', action='store_true',
                    help='skip the binary/VM arch check')

    for name, helptext in (('start', 'start the service'),
                           ('stop', 'stop the service'),
                           ('restart', 'restart the service'),
                           ('status', 'service status'),
                           ('health', 'end-to-end health probe')):
        with_region(sub.add_parser(name, help=helptext))

    pl = with_region(sub.add_parser('logs', help='journalctl snapshot'))
    pl.add_argument('--lines', type=int, default=100,
                    help='number of log lines (default 100)')
    pl.add_argument('--since', help="journalctl --since value, e.g. '1 hour ago'")
    return p


def main(argv):
    args = _build_parser().parse_args(argv)
    try:
        return _DISPATCH[args.action](args)
    except (RuntimeError, TimeoutError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == '__main__':  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
