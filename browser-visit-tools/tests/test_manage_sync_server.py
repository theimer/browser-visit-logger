"""Unit tests for manage_sync_server.py.

All AWS plumbing is faked: _bvl_aws.client returns recording fakes, and
the higher-level helpers (wait_for_ssm_online, run_remote, the S3
staging trio) are monkeypatched so the suite never touches AWS and the
~20 MB-binary path is exercised with a few dummy bytes.
"""
import argparse
import json
import sys
import types
from pathlib import Path

import pytest

_fake_boto3 = types.ModuleType('boto3')
_fake_boto3.client = lambda service, region_name=None: None
sys.modules.setdefault('boto3', _fake_boto3)

sys.path.insert(0, str(Path(__file__).parent.parent))
import _bvl_aws as aws  # noqa: E402
import manage_sync_server as mss  # noqa: E402


class Rec:
    def __init__(self, returns=None):
        self.calls = []
        self._returns = returns or {}

    def __getattr__(self, name):
        def method(**kw):
            self.calls.append((name, kw))
            return self._returns.get(name)
        return method


class RunRec:
    """Stand-in for aws.run_remote: records (instance_id, commands)."""
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, ssm, instance_id, commands, comment='', **kw):
        self.calls.append((instance_id, list(commands), comment))
        return self.result

    def last_commands(self):
        return self.calls[-1][1]


def _reservations(*instances):
    return {'Reservations': [{'Instances': [i]} for i in instances]}


def _inst(iid='i-1'):
    return {'InstanceId': iid, 'State': {'Name': 'running'}}


def _ok(out='ok\n', err=''):
    return aws.RemoteResult('Success', 0, out, err)


def _patch(monkeypatch, run_result=None, ec2=None):
    services = {
        'ec2': ec2 or Rec(returns={'describe_instances':
                                   _reservations(_inst('i-1'))}),
        'ssm': Rec(),
        's3': Rec(),
        'sts': Rec(returns={'get_caller_identity': {'Account': '1'}}),
    }
    monkeypatch.setattr(mss.aws, 'client',
                        lambda service, region=None: services[service])
    waited = []
    monkeypatch.setattr(mss.aws, 'wait_for_ssm_online',
                        lambda ssm, iid, *a, **k: waited.append(iid))
    _patch.waited = waited
    monkeypatch.setattr(mss.aws, 'staging_bucket_name',
                        lambda sts, region: 'bvl-bucket')
    monkeypatch.setattr(mss.aws, 'ensure_bucket', lambda *a, **k: 'bvl-bucket')
    uploads = []

    def _upload(s3, path, bucket, key, encrypt=False):
        uploads.append((str(path), key, encrypt))
        return f's3://{bucket}/{key}'
    monkeypatch.setattr(mss.aws, 'upload_file', _upload)
    runner = RunRec(run_result or _ok())
    monkeypatch.setattr(mss.aws, 'run_remote', runner)
    return runner, uploads


# --------------------------------------------------------------------------
# _surface
# --------------------------------------------------------------------------

def test_surface_success_adds_newline(capsys):
    assert mss._surface(aws.RemoteResult('Success', 0, 'no-nl', '')) == 0
    assert capsys.readouterr().out == 'no-nl\n'


def test_surface_success_with_stderr(capsys):
    assert mss._surface(aws.RemoteResult('Success', 0, 'out\n', 'warn')) == 0
    cap = capsys.readouterr()
    assert cap.out == 'out\n'
    assert cap.err == 'warn'


def test_surface_failure(capsys):
    assert mss._surface(aws.RemoteResult('Failed', 2, '', 'boom')) == 1
    assert 'remote command Failed' in capsys.readouterr().err


# --------------------------------------------------------------------------
# _check_arch
# --------------------------------------------------------------------------

def _write_state(monkeypatch, tmp_path, **state):
    p = tmp_path / 'vm.json'
    p.write_text(json.dumps(state))
    monkeypatch.setenv('BVL_VM_STATE_FILE', str(p))
    return p


def test_check_arch_no_state(monkeypatch, tmp_path):
    monkeypatch.setenv('BVL_VM_STATE_FILE', str(tmp_path / 'absent.json'))
    assert mss._check_arch('sync-server-linux-amd64') is None


def test_check_arch_match(monkeypatch, tmp_path):
    _write_state(monkeypatch, tmp_path, arch='x86_64')
    assert mss._check_arch('bin/sync-server-linux-amd64') is None


def test_check_arch_mismatch(monkeypatch, tmp_path):
    _write_state(monkeypatch, tmp_path, arch='arm64')
    msg = mss._check_arch('sync-server-linux-amd64')
    assert msg and 'arm64' in msg


# --------------------------------------------------------------------------
# provision
# --------------------------------------------------------------------------

def _provision_args(tmp_path, and_deploy=False, binary=None):
    return argparse.Namespace(
        action='provision', region=None,
        server_cert=str(tmp_path / 'server.crt'),
        server_key=str(tmp_path / 'server.key'),
        clients_ca=str(tmp_path / 'clients-ca.crt'),
        and_deploy=and_deploy, binary=binary, force=False)


def test_provision_success(monkeypatch, tmp_path, capsys):
    runner, uploads = _patch(monkeypatch)
    assert mss.cmd_provision(_provision_args(tmp_path)) == 0
    # unit file + 3 TLS files uploaded; the TLS ones (and ONLY those) are
    # written with SSE.
    keys = [u[1] for u in uploads]
    assert 'deploy/sync-server.service' in keys
    assert {'tls/server.crt', 'tls/server.key', 'tls/clients-ca.crt'} <= set(keys)
    encrypted = {u[1] for u in uploads if u[2]}
    assert encrypted == {'tls/server.crt', 'tls/server.key', 'tls/clients-ca.crt'}
    # the remote shell creates the service account, confines ownership, and
    # locks down the private key — the security-relevant steps.
    cmds = '\n'.join(runner.last_commands())
    assert 'useradd' in cmds and 'systemctl enable sync-server' in cmds
    assert 'chown -R bvlsync:bvlsync /var/lib/browser-visit-sync' in cmds
    assert 'chmod 600 /etc/browser-visit-sync/server.key' in cmds
    assert 'provisioned' in capsys.readouterr().out


def test_provision_failure_skips_deploy(monkeypatch, tmp_path):
    runner, _ = _patch(monkeypatch,
                       run_result=aws.RemoteResult('Failed', 1, '', 'x'))
    args = _provision_args(tmp_path, and_deploy=True,
                           binary=str(tmp_path / 'bin'))
    assert mss.cmd_provision(args) == 1
    # only the provision command ran; deploy never reached
    assert len(runner.calls) == 1


def test_provision_and_deploy(monkeypatch, tmp_path):
    binary = tmp_path / 'sync-server-linux-amd64'
    binary.write_bytes(b'ELF...')
    runner, uploads = _patch(monkeypatch)
    args = _provision_args(tmp_path, and_deploy=True, binary=str(binary))
    assert mss.cmd_provision(args) == 0
    # provision command + deploy command both ran
    assert len(runner.calls) == 2
    assert any(k.startswith('sync-server/') for _p, k, _e in uploads)


# --------------------------------------------------------------------------
# deploy
# --------------------------------------------------------------------------

def _deploy_args(binary, force=False):
    return argparse.Namespace(action='deploy', region=None,
                              binary=str(binary), force=force)


def test_deploy_missing_binary(monkeypatch, capsys):
    _patch(monkeypatch)
    assert mss.cmd_deploy(_deploy_args('/no/such/binary')) == 1
    assert 'binary not found' in capsys.readouterr().err


def test_deploy_arch_mismatch_refused(monkeypatch, tmp_path, capsys):
    _write_state(monkeypatch, tmp_path, arch='arm64')
    binary = tmp_path / 'sync-server-linux-amd64'
    binary.write_bytes(b'x')
    _patch(monkeypatch)
    assert mss.cmd_deploy(_deploy_args(binary)) == 1
    assert 'does not look like' in capsys.readouterr().err


def test_deploy_arch_mismatch_forced(monkeypatch, tmp_path):
    _write_state(monkeypatch, tmp_path, arch='arm64')
    binary = tmp_path / 'sync-server-linux-amd64'
    binary.write_bytes(b'x')
    runner, uploads = _patch(monkeypatch)
    assert mss.cmd_deploy(_deploy_args(binary, force=True)) == 0
    assert any(k.startswith('sync-server/') for _p, k, _e in uploads)


def test_deploy_happy(monkeypatch, tmp_path):
    import hashlib
    monkeypatch.setenv('BVL_VM_STATE_FILE', str(tmp_path / 'absent.json'))
    binary = tmp_path / 'sync-server-linux-amd64'
    binary.write_bytes(b'binarydata')
    runner, uploads = _patch(monkeypatch)
    assert mss.cmd_deploy(_deploy_args(binary)) == 0
    # the binary is staged under a content-addressed key (its sha256) ...
    digest = hashlib.sha256(b'binarydata').hexdigest()
    assert any(k == f'sync-server/{digest}' for _p, k, _e in uploads)
    # ... and the remote shell pulls exactly that key, installs it 0755, and
    # restarts the service.
    cmds = '\n'.join(runner.last_commands())
    assert f'sync-server/{digest}' in cmds
    assert 'install -m 0755' in cmds and '/usr/local/bin/sync-server' in cmds
    assert 'systemctl restart sync-server' in cmds


# --------------------------------------------------------------------------
# systemctl wrappers / status / logs / health
# --------------------------------------------------------------------------

def _args(action, **kw):
    return argparse.Namespace(action=action, region=None, **kw)


@pytest.mark.parametrize('action,fn', [
    ('start', mss.cmd_start), ('stop', mss.cmd_stop),
    ('restart', mss.cmd_restart)])
def test_systemctl_wrappers(monkeypatch, action, fn):
    runner, _ = _patch(monkeypatch)
    assert fn(_args(action)) == 0
    assert runner.last_commands() == [f'systemctl {action} sync-server']
    # the SSM agent must be confirmed Online before the command is sent,
    # else SendCommand fails with InvalidInstanceId.
    assert _patch.waited == ['i-1']


def test_cmd_status(monkeypatch):
    runner, _ = _patch(monkeypatch)
    assert mss.cmd_status(_args('status')) == 0
    assert any('is-active' in c for c in runner.last_commands())


def test_cmd_logs_default(monkeypatch):
    runner, _ = _patch(monkeypatch)
    assert mss.cmd_logs(_args('logs', lines=50, since=None)) == 0
    assert '-n 50' in runner.last_commands()[0]
    assert '--since' not in runner.last_commands()[0]


def test_cmd_logs_since(monkeypatch):
    runner, _ = _patch(monkeypatch)
    assert mss.cmd_logs(_args('logs', lines=10, since='1 hour ago')) == 0
    assert '--since' in runner.last_commands()[0]


def test_cmd_health(monkeypatch):
    runner, _ = _patch(monkeypatch)
    assert mss.cmd_health(_args('health')) == 0
    cmds = '\n'.join(runner.last_commands())
    # all three probes present, each able to fail the script (exit 1) so a
    # non-zero ResponseCode surfaces as a tool failure.
    assert 'is-active --quiet sync-server' in cmds
    assert '/dev/tcp/127.0.0.1/50443' in cmds
    assert 'df --output=pcent' in cmds
    assert cmds.count('exit 1') == 3
    assert '-lt 90' in cmds  # disk threshold


# --------------------------------------------------------------------------
# enroll / revoke / list
# --------------------------------------------------------------------------

def _enroll_args(**kw):
    base = dict(action='enroll', region=None, machine_id=None, cert=None,
                revoke=False, list=False)
    base.update(kw)
    return argparse.Namespace(**base)


def test_enroll_inserts_fingerprint(monkeypatch, tmp_path):
    import hashlib
    cert = tmp_path / 'laptop-a.crt'
    cert.write_bytes(b'DERBYTES')          # no PEM header → hashed as DER
    fp = hashlib.sha256(b'DERBYTES').hexdigest()
    runner, _ = _patch(monkeypatch)
    assert mss.cmd_enroll(
        _enroll_args(machine_id='laptop-a', cert=str(cert))) == 0
    cmds = '\n'.join(runner.last_commands())
    # the fingerprint is computed locally and the row written on the VM;
    # no restart line — the server re-reads the DB live.
    assert 'INSERT OR REPLACE' in cmds
    assert 'laptop-a' in cmds and fp in cmds
    assert f'chown bvlsync:bvlsync {mss._ENROLLED_DB}' in cmds
    assert 'systemctl restart' not in cmds


def test_enroll_requires_cert(monkeypatch, capsys):
    _patch(monkeypatch)
    assert mss.cmd_enroll(_enroll_args(machine_id='laptop-a')) == 2
    assert 'required to enroll' in capsys.readouterr().err


def test_enroll_revoke(monkeypatch):
    runner, _ = _patch(monkeypatch)
    assert mss.cmd_enroll(_enroll_args(revoke=True, machine_id='laptop-a')) == 0
    cmds = '\n'.join(runner.last_commands())
    assert 'DELETE FROM enrolled_machines' in cmds and 'laptop-a' in cmds
    assert 'chown bvlsync:bvlsync' in cmds


def test_enroll_revoke_requires_machine_id(monkeypatch, capsys):
    _patch(monkeypatch)
    assert mss.cmd_enroll(_enroll_args(revoke=True)) == 2
    assert 'required to revoke' in capsys.readouterr().err


def test_enroll_list_is_read_only(monkeypatch):
    runner, _ = _patch(monkeypatch)
    assert mss.cmd_enroll(_enroll_args(list=True)) == 0
    cmds = '\n'.join(runner.last_commands())
    assert 'SELECT machine_id' in cmds
    assert 'chown' not in cmds              # list never writes


# --------------------------------------------------------------------------
# main dispatch + error + parser
# --------------------------------------------------------------------------

def test_region_defaults_to_vm_state(monkeypatch, tmp_path, capsys):
    # No --region / BVL_AWS_REGION: fall back to the VM's recorded region so
    # the ec2/ssm/s3 clients target where the VM lives.  Without it, region
    # stays None and the staging bucket's CreateBucket fails with
    # IllegalLocationConstraintException.
    monkeypatch.delenv('BVL_AWS_REGION', raising=False)
    _write_state(monkeypatch, tmp_path, region='us-west-2', instance_id='i-1')
    _patch(monkeypatch)
    assert mss.cmd_start(_args('start')) == 0
    assert 'region=us-west-2' in capsys.readouterr().out


def test_main_dispatch_start(monkeypatch):
    _patch(monkeypatch)
    assert mss.main(['start']) == 0


def test_main_error_returns_1(monkeypatch, capsys):
    ec2 = Rec(returns={'describe_instances': {'Reservations': []}})
    _patch(monkeypatch, ec2=ec2)
    assert mss.main(['status']) == 1
    assert 'error:' in capsys.readouterr().err


def test_parser_deploy_requires_binary():
    with pytest.raises(SystemExit):
        mss._build_parser().parse_args(['deploy'])
