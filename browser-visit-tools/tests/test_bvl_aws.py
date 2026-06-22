"""Unit tests for _bvl_aws.py.

boto3 isn't installed in the test venv, so a fake module is injected
into sys.modules before importing the module under test (mirrors how
test_fetch_vm_snapshot fakes grpc).  Every AWS interaction is exercised
through hand-rolled fake clients — no network, no real boto3.
"""
import sys
import types
from pathlib import Path

import pytest

# Fake boto3 BEFORE importing the module (its `import boto3` is lazy, but
# client() needs it to resolve for the success-path coverage).
_fake_boto3 = types.ModuleType('boto3')
_fake_boto3.client = lambda service, region_name=None: (
    'client', service, region_name)
sys.modules.setdefault('boto3', _fake_boto3)

sys.path.insert(0, str(Path(__file__).parent.parent))
import _bvl_aws as aws  # noqa: E402


class FakeError(Exception):
    """A botocore ClientError look-alike (carries a .response)."""
    def __init__(self, code):
        super().__init__(code)
        self.response = {'Error': {'Code': code}}


# --------------------------------------------------------------------------
# region / client / error_code
# --------------------------------------------------------------------------

def test_resolve_region_arg_wins(monkeypatch):
    monkeypatch.setenv('BVL_AWS_REGION', 'eu-west-1')
    assert aws.resolve_region('us-east-1') == 'us-east-1'


def test_resolve_region_env(monkeypatch):
    monkeypatch.setenv('BVL_AWS_REGION', 'eu-west-1')
    assert aws.resolve_region(None) == 'eu-west-1'


def test_resolve_region_none(monkeypatch):
    monkeypatch.delenv('BVL_AWS_REGION', raising=False)
    assert aws.resolve_region(None) is None


def test_client_uses_boto3():
    assert aws.client('ec2', 'us-east-1') == ('client', 'ec2', 'us-east-1')


def test_error_code_present():
    assert aws.error_code(FakeError('NoSuchEntity')) == 'NoSuchEntity'


def test_error_code_absent():
    assert aws.error_code(ValueError('plain')) is None


# --------------------------------------------------------------------------
# state file
# --------------------------------------------------------------------------

def test_state_path_override(monkeypatch, tmp_path):
    monkeypatch.setenv('BVL_VM_STATE_FILE', str(tmp_path / 'x.json'))
    assert aws.state_path() == tmp_path / 'x.json'


def test_state_path_config_dir(monkeypatch, tmp_path):
    monkeypatch.delenv('BVL_VM_STATE_FILE', raising=False)
    monkeypatch.setenv('BVL_CONFIG_DIR', str(tmp_path))
    assert aws.state_path() == tmp_path / 'vm.json'


def test_state_path_home_default(monkeypatch):
    monkeypatch.delenv('BVL_VM_STATE_FILE', raising=False)
    monkeypatch.delenv('BVL_CONFIG_DIR', raising=False)
    monkeypatch.setenv('HOME', '/home/tester')
    assert aws.state_path() == Path('/home/tester/.browser-visit-logger/vm.json')


def test_load_state_missing(monkeypatch, tmp_path):
    monkeypatch.setenv('BVL_VM_STATE_FILE', str(tmp_path / 'absent.json'))
    assert aws.load_state() == {}


def test_save_then_load_roundtrip(monkeypatch, tmp_path):
    target = tmp_path / 'sub' / 'vm.json'
    monkeypatch.setenv('BVL_VM_STATE_FILE', str(target))
    out = aws.save_state({'instance_id': 'i-1', 'region': 'us-east-1'})
    assert out['instance_id'] == 'i-1'
    assert 'updated_at' in out
    loaded = aws.load_state()
    assert loaded['instance_id'] == 'i-1'
    assert loaded['region'] == 'us-east-1'


# --------------------------------------------------------------------------
# instance discovery
# --------------------------------------------------------------------------

class FakeEc2:
    def __init__(self, instances=()):
        self._instances = list(instances)
        self.describe_filters = None

    def describe_instances(self, Filters=None, **kw):
        self.describe_filters = Filters
        return {'Reservations': [
            {'Instances': [i]} for i in self._instances]}


def _inst(iid, state='running'):
    return {'InstanceId': iid, 'State': {'Name': state}}


def test_discover_instances_filters_and_flattens():
    ec2 = FakeEc2([_inst('i-1'), _inst('i-2')])
    found = aws.discover_instances(ec2)
    assert [i['InstanceId'] for i in found] == ['i-1', 'i-2']
    names = [f['Name'] for f in ec2.describe_filters]
    assert 'tag:bvl:role' in names
    assert 'instance-state-name' in names


def test_resolve_instance_id_state_hit():
    ec2 = FakeEc2([_inst('i-1'), _inst('i-2')])
    assert aws.resolve_instance_id(ec2, {'instance_id': 'i-2'}) == 'i-2'


def test_resolve_instance_id_fallback_single():
    ec2 = FakeEc2([_inst('i-9')])
    assert aws.resolve_instance_id(ec2, {}) == 'i-9'


def test_resolve_instance_id_none():
    with pytest.raises(RuntimeError, match='no sync-server VM'):
        aws.resolve_instance_id(FakeEc2([]), {})


def test_resolve_instance_id_ambiguous():
    ec2 = FakeEc2([_inst('i-1'), _inst('i-2')])
    with pytest.raises(RuntimeError, match='ambiguous'):
        aws.resolve_instance_id(ec2, {})


def test_resolve_instance_id_stale_state_falls_back():
    # state names a now-terminated instance; discovery returns a different one
    ec2 = FakeEc2([_inst('i-new')])
    assert aws.resolve_instance_id(ec2, {'instance_id': 'i-old'}) == 'i-new'


# --------------------------------------------------------------------------
# SSM wait + run
# --------------------------------------------------------------------------

class FakeSSM:
    def __init__(self, pings=(), invocations=()):
        self._pings = list(pings)
        self._invocations = list(invocations)
        self.sent = None

    def describe_instance_information(self, Filters=None, **kw):
        self.info_filters = Filters
        status = self._pings.pop(0)
        infos = [] if status is None else [{'PingStatus': status}]
        return {'InstanceInformationList': infos}

    def send_command(self, **kw):
        self.sent = kw
        return {'Command': {'CommandId': 'cmd-1'}}

    def get_command_invocation(self, **kw):
        item = self._invocations.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _clock(values):
    it = iter(values)
    return lambda: next(it)


def test_wait_for_ssm_online_immediate():
    ssm = FakeSSM(pings=['Online'])
    aws.wait_for_ssm_online(ssm, 'i-1', sleep=lambda _s: None,
                            monotonic=_clock([0]))
    # it polls for the specific instance, not all of them
    assert ssm.info_filters == [{'Key': 'InstanceIds', 'Values': ['i-1']}]


def test_wait_for_ssm_online_after_poll():
    ssm = FakeSSM(pings=[None, 'Online'])
    calls = []
    aws.wait_for_ssm_online(ssm, 'i-1', interval=1,
                            sleep=lambda s: calls.append(s),
                            monotonic=_clock([0, 1]))
    assert calls == [1]


def test_wait_for_ssm_online_timeout():
    ssm = FakeSSM(pings=['ConnectionLost'])
    with pytest.raises(TimeoutError):
        aws.wait_for_ssm_online(ssm, 'i-1', timeout=10,
                                sleep=lambda _s: None,
                                monotonic=_clock([0, 20]))


def _inv(status, code=0, out='', err=''):
    return {'Status': status, 'ResponseCode': code,
            'StandardOutputContent': out, 'StandardErrorContent': err}


def test_run_remote_success_immediate():
    ssm = FakeSSM(invocations=[_inv('Success', 0, 'ok\n')])
    res = aws.run_remote(ssm, 'i-1', ['echo hi'], comment='c',
                         sleep=lambda _s: None, monotonic=_clock([0]))
    assert res.status == 'Success'
    assert res.exit_code == 0
    assert res.stdout == 'ok\n'
    # the command must run the shell document against the target instance
    assert ssm.sent['DocumentName'] == 'AWS-RunShellScript'
    assert ssm.sent['InstanceIds'] == ['i-1']
    assert ssm.sent['Parameters'] == {'commands': ['echo hi']}


def test_run_remote_comment_truncated():
    # SSM caps the Comment field at 100 chars; run_remote must not exceed it.
    ssm = FakeSSM(invocations=[_inv('Success', 0)])
    aws.run_remote(ssm, 'i-1', ['x'], comment='c' * 250,
                   sleep=lambda _s: None, monotonic=_clock([0]))
    assert len(ssm.sent['Comment']) == 100


def test_run_remote_surfaces_failure_output():
    ssm = FakeSSM(invocations=[_inv('Failed', 7, 'partial', 'the error')])
    res = aws.run_remote(ssm, 'i-1', ['x'],
                         sleep=lambda _s: None, monotonic=_clock([0]))
    assert res.status == 'Failed'
    assert res.exit_code == 7
    assert res.stderr == 'the error'


def test_run_remote_polls_then_done():
    ssm = FakeSSM(invocations=[_inv('InProgress'), _inv('Success', 0, 'x')])
    res = aws.run_remote(ssm, 'i-1', ['c'], interval=2,
                         sleep=lambda _s: None, monotonic=_clock([0, 1]))
    assert res.stdout == 'x'


def test_run_remote_timeout():
    ssm = FakeSSM(invocations=[_inv('InProgress'), _inv('InProgress')])
    with pytest.raises(TimeoutError):
        aws.run_remote(ssm, 'i-1', ['c'], timeout=5,
                       sleep=lambda _s: None, monotonic=_clock([0, 99]))


def test_run_remote_tolerates_invocation_not_yet_registered():
    # Right after send_command, GetCommandInvocation raises
    # InvocationDoesNotExist until the invocation lands — run_remote must
    # keep polling, not crash.
    ssm = FakeSSM(invocations=[
        FakeError('InvocationDoesNotExist'), _inv('Success', 0, 'ok\n')])
    res = aws.run_remote(ssm, 'i-1', ['c'],
                         sleep=lambda _s: None, monotonic=_clock([0, 1]))
    assert res.status == 'Success' and res.stdout == 'ok\n'


def test_run_remote_reraises_other_invocation_error():
    # Any other SSM error propagates (not swallowed as the registration race).
    ssm = FakeSSM(invocations=[FakeError('AccessDeniedException')])
    with pytest.raises(FakeError):
        aws.run_remote(ssm, 'i-1', ['c'],
                       sleep=lambda _s: None, monotonic=_clock([0]))


# --------------------------------------------------------------------------
# S3 staging
# --------------------------------------------------------------------------

class FakeSTS:
    def get_caller_identity(self):
        return {'Account': '123456789012'}


class FakeS3:
    def __init__(self, head_error=None):
        self._head_error = head_error
        self.created = None
        self.lifecycle = None
        self.uploads = []

    def head_bucket(self, Bucket=None):
        if self._head_error is not None:
            raise self._head_error

    def create_bucket(self, **kw):
        self.created = kw

    def put_bucket_lifecycle_configuration(self, **kw):
        self.lifecycle = kw

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        self.uploads.append((path, bucket, key, ExtraArgs))


def test_staging_bucket_name():
    assert aws.staging_bucket_name(FakeSTS(), 'us-east-1') == \
        'bvl-sync-deploy-123456789012-us-east-1'


def test_ensure_bucket_exists():
    s3 = FakeS3(head_error=None)
    aws.ensure_bucket(s3, 'b', 'us-west-2')
    assert s3.created is None
    assert s3.lifecycle is not None


def test_ensure_bucket_create_us_east_1():
    s3 = FakeS3(head_error=FakeError('404'))
    aws.ensure_bucket(s3, 'b', 'us-east-1')
    assert s3.created == {'Bucket': 'b'}


def test_ensure_bucket_create_other_region():
    s3 = FakeS3(head_error=FakeError('NoSuchBucket'))
    aws.ensure_bucket(s3, 'b', 'eu-west-1')
    assert s3.created['CreateBucketConfiguration'] == \
        {'LocationConstraint': 'eu-west-1'}


def test_ensure_bucket_other_error_reraises():
    s3 = FakeS3(head_error=FakeError('AccessDenied'))
    with pytest.raises(FakeError):
        aws.ensure_bucket(s3, 'b', 'us-east-1')


def test_upload_file_plain():
    s3 = FakeS3()
    uri = aws.upload_file(s3, '/tmp/x', 'b', 'k')
    assert uri == 's3://b/k'
    assert s3.uploads == [('/tmp/x', 'b', 'k', {})]


def test_upload_file_encrypted():
    s3 = FakeS3()
    aws.upload_file(s3, '/tmp/x', 'b', 'k', encrypt=True)
    assert s3.uploads[0][3] == {'ServerSideEncryption': 'AES256'}
