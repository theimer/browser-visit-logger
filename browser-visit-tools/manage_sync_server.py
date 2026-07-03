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
    provision-drive
               Mount the Google Drive snapshot archive on the VM via
               rclone + a service-account key, and install the systemd
               mount unit.  Idempotent.  (See issue #47.)
    drive-status
               Report whether Drive is mounted, the mount service is up,
               and the canonical DB holds snapshot rows.  Read-only.
    start      systemctl start sync-server
    stop       systemctl stop sync-server
    restart    systemctl restart sync-server
    status     systemctl is-active + status
    logs       journalctl snapshot (NOT a live tail — for follow, use
               `aws ssm start-session`)
    health     service active + gRPC port answers + disk ok
    enroll     enrol / revoke / list a laptop in the server's
               enrolled_machines.db, in place over SSM (no restart — the
               server re-reads the allowlist on every request)

The VM itself (and its AWS infra) is owned by the sibling
``manage_vm.py``.  Build the binary first with
``make -C ../browser-visit-sync-server build-linux GOARCH=<amd64|arm64>``.

Exit codes: 0 success, 1 remote/AWS failure, 2 usage.
"""

import argparse
import hashlib
import os
import shlex
import sys
from pathlib import Path

import _bvl_aws as aws
import enroll_machine

_HERE = Path(__file__).resolve().parent
_DEPLOY = _HERE.parent / 'browser-visit-sync-server' / 'deploy'
_UNIT_FILE = _DEPLOY / 'sync-server.service'
_MOUNT_UNIT_FILE = _DEPLOY / 'gdrive-snapshots.service'

# state ``arch`` → Go GOARCH token expected in the built binary's name.
_GOARCH = {'x86_64': 'amd64', 'arm64': 'arm64'}

_DATA_DIR = '/var/lib/browser-visit-sync'
_ETC_DIR = '/etc/browser-visit-sync'
_ENROLLED_DB = f'{_DATA_DIR}/enrolled_machines.db'
_SNAPSHOTS_DB = f'{_DATA_DIR}/browser-visits.db'

# Google Drive snapshot mount (see issue #47).  rclone reaches Drive with a
# service-account key so the VM needs no interactive OAuth; the mounted tree is
# where the (future) VM-side verifier seals days and writes MANIFEST.tsv.
_GDRIVE_MOUNT = '/mnt/gdrive-snapshots'
_GDRIVE_SA = f'{_ETC_DIR}/gdrive-sa.json'
_RCLONE_CONF = f'{_ETC_DIR}/rclone.conf'

# Read-only DB probe for `drive-status`.  Uses the sqlite3 module (guaranteed on
# AL2023) rather than the sqlite3 CLI (not installed), mirroring _ENROLL_PY.
# argv: <db>.  Opened read-only so a concurrent write never blocks the probe.
_SNAPSHOT_SUMMARY_PY = '''\
import sqlite3, sys
try:
    c = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
    rows = c.execute("SELECT date, sealed FROM snapshots "
                     "ORDER BY date DESC LIMIT 5").fetchall()
    if not rows:
        print("  (snapshots table empty)")
    for d, s in rows:
        print("  %s  sealed=%s" % (d, s))
except Exception as e:
    print("  (query failed: %s)" % e)
'''


def _rclone_conf(root_folder_id):
    """rclone.conf body for the Drive remote, keyed off the service account.

    `scope = drive` is the token's capability (read/write), NOT which files
    it can see: a service account can only reach items explicitly shared with
    its address.  We share only the `snapshots` folder, so that is the whole
    universe the VM can touch.  (The narrower `drive.file` scope is unusable
    here — it restricts to files the app itself created, but the snapshots are
    created by the laptop, a different identity.)  `root_folder_id` pins the
    remote root to that shared folder so the mount lands directly on the
    date-directory tree.
    """
    return ('[gdrive]\n'
            'type = drive\n'
            'scope = drive\n'
            f'service_account_file = {_GDRIVE_SA}\n'
            f'root_folder_id = {root_folder_id}\n')

# Stdlib-only program run on the VM (Amazon Linux 2023 ships python3) to
# mutate the server's allowlist in place.  argv: <db> <op> [machine_id] [fp].
# The server re-reads enrolled_machines.db on every request, so no restart is
# needed; busy_timeout rides out the brief lock the live server may hold.
_ENROLL_PY = '''\
import sqlite3, sys, datetime
conn = sqlite3.connect(sys.argv[1])
conn.execute("PRAGMA busy_timeout=5000")
conn.execute("CREATE TABLE IF NOT EXISTS enrolled_machines ("
             "machine_id TEXT PRIMARY KEY, cert_sha256 TEXT NOT NULL, "
             "enrolled_at TEXT NOT NULL)")
op = sys.argv[2]
if op == "list":
    rows = conn.execute("SELECT machine_id, cert_sha256, enrolled_at FROM "
                        "enrolled_machines ORDER BY machine_id").fetchall()
    for r in rows:
        print("%-30s %s... %s" % (r[0], r[1][:16], r[2]))
    print("(%d enrolled)" % len(rows))
elif op == "revoke":
    n = conn.execute("DELETE FROM enrolled_machines WHERE machine_id=?",
                     (sys.argv[3],)).rowcount
    conn.commit()
    print("revoked %s (%d row(s))" % (sys.argv[3], n))
else:
    conn.execute("INSERT OR REPLACE INTO enrolled_machines VALUES (?,?,?)",
                 (sys.argv[3], sys.argv[4],
                  datetime.datetime.now(datetime.timezone.utc).isoformat()))
    conn.commit()
    print("enrolled %s" % sys.argv[3])
'''


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
    state = aws.load_state()
    # Fall back to the VM's recorded region so every client (ec2/ssm/s3)
    # targets where the VM actually lives.  A region-less run otherwise
    # leaves region=None, which poisons the staging-bucket name and makes
    # CreateBucket fail with IllegalLocationConstraintException against the
    # ambient (non-us-east-1) endpoint boto3 picks.
    region = aws.resolve_region(args.region) or state.get('region')
    ec2 = aws.client('ec2', region)
    instance_id = aws.resolve_instance_id(ec2, state)
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


def cmd_provision_drive(args):
    """Mount the Google Drive snapshot archive on the VM via rclone + a
    service-account key, and install the systemd mount unit.  Idempotent."""
    sa = Path(os.path.expanduser(args.service_account))
    if not sa.is_file():
        print(f"error: service account key not found: {sa}", file=sys.stderr)
        return 1
    region, ssm, instance_id = _ssm_target(args)
    s3, bucket = _stage_clients(region)
    aws.upload_file(s3, _MOUNT_UNIT_FILE, bucket,
                    'deploy/gdrive-snapshots.service')
    aws.upload_file(s3, sa, bucket, 'gdrive/gdrive-sa.json', encrypt=True)
    conf = _rclone_conf(args.root_folder_id)
    script = [
        'set -euo pipefail',
        'command -v rclone >/dev/null || dnf install -y rclone fuse3',
        # The mountpoint is made by root and handed to the service account, so
        # rclone (running as bvlsync) can mount onto a directory it owns.
        f'mkdir -p {_GDRIVE_MOUNT}',
        f'chown bvlsync:bvlsync {_GDRIVE_MOUNT}',
        f'aws s3 cp s3://{bucket}/gdrive/gdrive-sa.json {_GDRIVE_SA}',
        f'printf %s {shlex.quote(conf)} > {_RCLONE_CONF}',
        f'aws s3 cp s3://{bucket}/deploy/gdrive-snapshots.service '
        '/etc/systemd/system/gdrive-snapshots.service',
        # The key and remote config are secrets: owned by the service account,
        # unreadable to anyone else.
        f'chown bvlsync:bvlsync {_GDRIVE_SA} {_RCLONE_CONF}',
        f'chmod 400 {_GDRIVE_SA}',
        f'chmod 600 {_RCLONE_CONF}',
        'systemctl daemon-reload',
        'systemctl enable --now gdrive-snapshots',
    ]
    rc = _surface(aws.run_remote(ssm, instance_id, script,
                                 comment='bvl provision-drive'))
    if rc == 0:
        print(f"drive mount provisioned at {_GDRIVE_MOUNT}; "
              "run `drive-status` to verify")
    return rc


def cmd_drive_status(args):
    """Diagnostic: is Drive mounted, is the mount service up, and does the
    canonical DB show snapshot rows?  Always prints; never mutates."""
    _region, ssm, instance_id = _ssm_target(args)
    # The rclone FUSE mount is owner-only (no allow_other), so probe it AS the
    # mount owner (bvlsync).  SSM runs this script as root, and root touching a
    # non-allow_other FUSE mount gets EACCES — a plain `mountpoint`/`ls` would
    # falsely report "NOT mounted" even when the mount is live for bvlsync
    # (which is the account the verifier runs under, so owner-only is enough).
    script = [
        f'runuser -u bvlsync -- mountpoint -q {_GDRIVE_MOUNT} '
        f'&& echo "mount: mounted at {_GDRIVE_MOUNT}" '
        '|| echo "mount: NOT mounted"',
        'echo "service: $(systemctl is-active gdrive-snapshots.service '
        '2>/dev/null || echo absent)"',
        'echo "recent snapshot days on Drive:"; '
        f'days=$(runuser -u bvlsync -- ls -1 {_GDRIVE_MOUNT} 2>/dev/null '
        '| tail -5); echo "${days:-  (none yet)}"',
        'echo "snapshot rows in canonical DB:"; '
        f'python3 -c {shlex.quote(_SNAPSHOT_SUMMARY_PY)} {_SNAPSHOTS_DB}',
    ]
    return _surface(aws.run_remote(ssm, instance_id, script,
                                   comment='bvl drive-status'))


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


def _enroll_remote(op, machine_id=None, fp=None):
    """Shell lines that run the enrolled-DB ``op`` on the VM over SSM."""
    argv = [_ENROLLED_DB, op]
    if op == 'enroll':
        argv += [machine_id, fp]
    elif op == 'revoke':
        argv += [machine_id]
    quoted = ' '.join(shlex.quote(a) for a in argv)
    lines = ['set -euo pipefail',
             f'python3 -c {shlex.quote(_ENROLL_PY)} {quoted}']
    if op != 'list':
        # The write ran as root; keep the file owned by the service account.
        lines.append(f'chown bvlsync:bvlsync {_ENROLLED_DB}')
    return lines


def cmd_enroll(args):
    if args.list:
        op, machine_id, fp = 'list', None, None
    elif args.revoke:
        if not args.machine_id:
            print("error: --machine-id required to revoke", file=sys.stderr)
            return 2
        op, machine_id, fp = 'revoke', args.machine_id, None
    else:
        if not args.machine_id or not args.cert:
            print("error: --machine-id and --cert required to enroll",
                  file=sys.stderr)
            return 2
        op, machine_id = 'enroll', args.machine_id
        fp = enroll_machine.cert_fingerprint(
            Path(os.path.expanduser(args.cert)))
    _region, ssm, instance_id = _ssm_target(args)
    return _surface(aws.run_remote(
        ssm, instance_id, _enroll_remote(op, machine_id, fp),
        comment=f'bvl enroll {op}'))


_DISPATCH = {
    'provision': cmd_provision, 'deploy': cmd_deploy, 'start': cmd_start,
    'stop': cmd_stop, 'restart': cmd_restart, 'status': cmd_status,
    'logs': cmd_logs, 'health': cmd_health, 'enroll': cmd_enroll,
    'provision-drive': cmd_provision_drive, 'drive-status': cmd_drive_status,
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

    pdr = with_region(sub.add_parser(
        'provision-drive',
        help='mount the Google Drive snapshot archive on the VM (rclone)'))
    pdr.add_argument('--service-account', required=True,
                     help='path to the Google service-account JSON key')
    pdr.add_argument('--root-folder-id', required=True,
                     help='Drive folder id of the shared `snapshots` folder '
                          '(from its Drive URL)')

    with_region(sub.add_parser(
        'drive-status',
        help='report whether Drive is mounted and holds snapshot data'))

    pl = with_region(sub.add_parser('logs', help='journalctl snapshot'))
    pl.add_argument('--lines', type=int, default=100,
                    help='number of log lines (default 100)')
    pl.add_argument('--since', help="journalctl --since value, e.g. '1 hour ago'")

    pe = with_region(sub.add_parser(
        'enroll', help='enrol / revoke / list laptops on the VM'))
    pe.add_argument('--machine-id', help='laptop machine id (= its cert CN)')
    pe.add_argument('--cert', help='laptop client cert (PEM or DER) to enrol')
    pe.add_argument('--revoke', action='store_true',
                    help='revoke --machine-id instead of enrolling')
    pe.add_argument('--list', action='store_true',
                    help='list the enrolled machines')
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
