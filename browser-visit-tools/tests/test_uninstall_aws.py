"""Unit tests for uninstall_aws.py.

boto3 is faked; every AWS client is a recording fake and the single seam
is ``uninstall_aws.aws.client``.  No test reaches real AWS.
"""
import sys
import types
from pathlib import Path

import os

import pytest

_fake_boto3 = types.ModuleType('boto3')
_fake_boto3.client = lambda service, region_name=None: None
sys.modules.setdefault('boto3', _fake_boto3)

_fake_botocore = types.ModuleType('botocore')
_fake_bce = types.ModuleType('botocore.exceptions')


class BotoCoreError(Exception):
    pass


class NoCredentialsError(BotoCoreError):
    pass


class PartialCredentialsError(BotoCoreError):
    pass


class ClientError(Exception):
    def __init__(self, error_response, operation_name):
        super().__init__(str(error_response))
        self.response = error_response
        self.operation_name = operation_name


for _c in (BotoCoreError, NoCredentialsError, PartialCredentialsError,
           ClientError):
    setattr(_fake_bce, _c.__name__, _c)
_fake_botocore.exceptions = _fake_bce
sys.modules.setdefault('botocore', _fake_botocore)
sys.modules.setdefault('botocore.exceptions', _fake_bce)

sys.path.insert(0, str(Path(__file__).parent.parent))
import manage_vm as mv  # noqa: E402
import uninstall_aws as ua  # noqa: E402

ACCOUNT = '123456789012'
REGION = 'us-west-2'
BUCKET = f'bvl-sync-deploy-{ACCOUNT}-{REGION}'
MUTATING = ('terminate_', 'delete_', 'detach_', 'remove_')


class FakeError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {'Error': {'Code': code}}


class Rec:
    """Records calls (also into a shared ``log``); canned returns, which
    may be callables taking the kwargs, and per-method raises."""
    def __init__(self, returns=None, raises=None, log=None, tag=''):
        self.calls = []
        self._returns = returns or {}
        self._raises = raises or {}
        self._log = log
        self._tag = tag

    def __getattr__(self, name):
        def method(**kw):
            self.calls.append((name, kw))
            if self._log is not None:
                self._log.append(name)
            if name in self._raises:
                raise self._raises[name]
            val = self._returns.get(name)
            return val(kw) if callable(val) else val
        return method

    def names(self):
        return [c[0] for c in self.calls]

    def kw(self, name):
        return [kw for n, kw in self.calls if n == name]


def seq(*values):
    it = iter(values)
    last = []

    def f(_kw):
        if values:
            try:
                last[:] = [next(it)]
            except StopIteration:
                pass
        return last[0]
    return f


@pytest.fixture(autouse=True)
def safety(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith('AWS_'):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv('BVL_AWS_REGION', raising=False)
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('BVL_VM_STATE_FILE', str(tmp_path / 'vm.json'))
    def guard(*a, **k):
        pytest.fail('aws.client not patched')
    monkeypatch.setattr(ua.aws, 'client', guard)
    monkeypatch.setattr('builtins.input',
                        lambda *_: pytest.fail('unexpected prompt'))


def inst(iid='i-1', state='running'):
    return {'InstanceId': iid, 'State': {'Name': state}}


def res(*instances):
    return {'Reservations': [{'Instances': list(instances)}]}


def make(log=None, *, instances=(inst(),), sg=('sg-1',), role=True,
         profile=True, bucket=True, ours=True, ec2_raises=None,
         iam_raises=None, s3_returns=None, s3_raises=None,
         describe=None):
    """Build the four fake clients for a fully-deployed backend."""
    state = {'n': 0}

    def describe_instances(kw):
        if 'InstanceIds' in kw:
            return (describe or (lambda k: res(*[inst(i, 'terminated')
                    for i in kw['InstanceIds']])))(kw)
        return res(*instances)

    ec2 = Rec(returns={
        'describe_instances': describe_instances,
        'describe_security_groups':
            {'SecurityGroups': [{'GroupId': g} for g in sg]},
    }, raises=ec2_raises, log=log)
    iam_r = {}
    if not role:
        iam_r['get_role'] = FakeError('NoSuchEntity')
    if not profile:
        iam_r['get_instance_profile'] = FakeError('NoSuchEntity')
    # get_* raises only matter for plan; mutating raises merged below
    iam = Rec(raises={**iam_r, **(iam_raises or {})}, log=log)
    s3_ret = {
        'list_object_versions': {'Versions': [{'Key': 'a', 'VersionId': '1'}],
                                 'DeleteMarkers': [{'Key': 'b',
                                                    'VersionId': '2'}]},
        'get_bucket_lifecycle_configuration':
            {'Rules': [{'ID': ua.LIFECYCLE_RULE_ID}]} if ours else {'Rules': []},
        **(s3_returns or {}),
    }
    s3_rs = dict(s3_raises or {})
    if not bucket:
        s3_rs['head_bucket'] = FakeError('404')
    s3 = Rec(returns=s3_ret, raises=s3_rs, log=log)
    sts = Rec(returns={'get_caller_identity': {'Account': ACCOUNT}}, log=log)
    return types.SimpleNamespace(ec2=ec2, iam=iam, s3=s3, sts=sts, state=state)


def patch(monkeypatch, c):
    svc = {'ec2': c.ec2, 'iam': c.iam, 's3': c.s3, 'sts': c.sts}
    monkeypatch.setattr(ua.aws, 'client', lambda s, region=None: svc[s])
    monkeypatch.setattr(ua.time, 'sleep', lambda *_: None)


def run(argv=()):
    return ua.main(['--region', REGION, *argv])


def all_names(c):
    return c.ec2.names() + c.iam.names() + c.s3.names() + c.sts.names()


# ---------------------------------------------------------------- happy path

def test_full_teardown_order(monkeypatch, tmp_path):
    log = []
    c = make(log)
    patch(monkeypatch, c)
    (tmp_path / 'vm.json').write_text('{}')
    assert run(['--yes']) == 0
    muts = [n for n in log if n.startswith(MUTATING)]
    assert muts == [
        'terminate_instances', 'delete_security_group',
        'remove_role_from_instance_profile', 'delete_instance_profile',
        'delete_role_policy', 'detach_role_policy', 'delete_role',
        'delete_objects', 'delete_bucket']
    assert log.index('terminate_instances') < log.index('delete_security_group')
    assert not (tmp_path / 'vm.json').exists()
    assert c.ec2.kw('delete_security_group') == [{'GroupId': 'sg-1'}]
    assert c.iam.kw('delete_role_policy')[0]['PolicyName'] == \
        ua.INLINE_POLICY_NAME
    assert c.iam.kw('detach_role_policy')[0]['PolicyArn'] == \
        ua.SSM_CORE_POLICY_ARN
    objs = c.s3.kw('delete_objects')[0]['Delete']['Objects']
    assert objs == [{'Key': 'a', 'VersionId': '1'},
                    {'Key': 'b', 'VersionId': '2'}]
    assert c.s3.kw('delete_bucket') == [{'Bucket': BUCKET}]


def test_nothing_found(monkeypatch, capsys):
    c = make(instances=(), sg=(), role=False, profile=False, bucket=False)
    patch(monkeypatch, c)
    assert run() == 0
    assert 'nothing to remove' in capsys.readouterr().out
    assert not [n for n in all_names(c) if n.startswith(MUTATING)]


def test_instance_filter_has_both_tags_and_states(monkeypatch):
    c = make()
    patch(monkeypatch, c)
    run(['-n'])
    filters = c.ec2.kw('describe_instances')[0]['Filters']
    names = {f['Name']: f['Values'] for f in filters}
    assert names['tag:bvl:managed-by'] == ['browser-visit-tools']
    assert names['tag:bvl:role'] == ['sync-server']
    assert 'shutting-down' in names['instance-state-name']
    assert 'running' in names['instance-state-name']


def test_sg_filter_requires_tag(monkeypatch):
    c = make()
    patch(monkeypatch, c)
    run(['-n'])
    names = {f['Name']: f['Values']
             for f in c.ec2.kw('describe_security_groups')[0]['Filters']}
    assert names['group-name'] == ['bvl-sync-server-sg']
    assert names['tag:bvl:managed-by'] == ['browser-visit-tools']


def test_untagged_sg_not_deleted(monkeypatch):
    c = make(sg=())
    patch(monkeypatch, c)
    assert run(['--yes']) == 0
    assert 'delete_security_group' not in c.ec2.names()


def test_multiple_instances_all_terminated(monkeypatch):
    c = make(instances=(inst('i-1'), inst('i-2', 'stopped')))
    patch(monkeypatch, c)
    assert run(['--yes']) == 0
    assert c.ec2.kw('terminate_instances') == [{'InstanceIds': ['i-1', 'i-2']}]


# ------------------------------------------------------------ terminate wait

def test_terminate_wait_polls_until_terminated(monkeypatch):
    c = make(describe=seq(res(inst('i-1', 'shutting-down')),
                          res(inst('i-1', 'terminated'))))
    patch(monkeypatch, c)
    sleeps = []
    r = ua.Results()
    assert ua.terminate_instances(c.ec2, False, r, sleep=sleeps.append,
                                  monotonic=lambda: 0) is True
    assert sleeps == [10] and not r.failures


def test_terminate_wait_times_out_and_skips_sg(monkeypatch, capsys, tmp_path):
    c = make(describe=lambda kw: res(inst('i-1', 'shutting-down')))
    patch(monkeypatch, c)
    clock = iter(range(0, 10000, 400))
    monkeypatch.setattr(ua.time, 'monotonic', lambda: next(clock))
    # Module defaults bind at def time, so drive through terminate directly
    # for the timeout and through run() for the SG skip.
    r = ua.Results()
    assert ua.terminate_instances(c.ec2, False, r, sleep=lambda s: None,
                                  monotonic=lambda: next(clock)) is False
    assert 'not terminated' in r.failures[0]

    (tmp_path / 'vm.json').write_text('{}')
    monkeypatch.setattr(ua, 'terminate_instances',
                        lambda ec2, dry, results: results.fail('boom') or False)
    assert run(['--yes']) == 1
    assert 'delete_security_group' not in c.ec2.names()
    assert 'skipping security group' in capsys.readouterr().err
    assert (tmp_path / 'vm.json').exists()


# ------------------------------------------------------------ security group

def test_sg_dependency_violation(monkeypatch, capsys):
    c = make(ec2_raises={'delete_security_group':
                         FakeError('DependencyViolation')})
    patch(monkeypatch, c)
    assert run(['--yes']) == 1
    assert 'still in use' in capsys.readouterr().err
    assert 'delete_role' in c.iam.names()  # later steps still ran


def test_sg_other_error(monkeypatch, capsys):
    c = make(ec2_raises={'delete_security_group': FakeError('Boom')})
    patch(monkeypatch, c)
    assert run(['--yes']) == 1
    assert 'still in use' not in capsys.readouterr().err


def test_sg_already_gone(monkeypatch):
    c = make(ec2_raises={'delete_security_group':
                         FakeError('InvalidGroup.NotFound')})
    patch(monkeypatch, c)
    assert run(['--yes']) == 0


# ---------------------------------------------------------------------- IAM

IAM_CALLS = ['remove_role_from_instance_profile', 'delete_instance_profile',
             'delete_role_policy', 'detach_role_policy', 'delete_role']


@pytest.mark.parametrize('call', IAM_CALLS)
def test_iam_no_such_entity_is_success(monkeypatch, call):
    c = make(iam_raises={call: FakeError('NoSuchEntity')})
    patch(monkeypatch, c)
    assert run(['--yes']) == 0
    assert c.iam.names()[-1] == 'delete_role' or call == 'delete_role'


@pytest.mark.parametrize('call', IAM_CALLS)
def test_iam_other_error_recorded_and_continues(monkeypatch, call, capsys):
    c = make(iam_raises={call: FakeError('AccessDenied')})
    patch(monkeypatch, c)
    assert run(['--yes']) == 1
    assert 'delete_bucket' in c.s3.names()
    assert 'AccessDenied' in capsys.readouterr().err


def test_role_delete_conflict_not_forced(monkeypatch):
    c = make(iam_raises={'delete_role': FakeError('DeleteConflict')})
    patch(monkeypatch, c)
    assert run(['--yes']) == 1
    assert c.iam.names().count('delete_role') == 1
    assert not [n for n in c.iam.names() if n.startswith('list_')]


# ------------------------------------------------------------------- bucket

def test_bucket_absent(monkeypatch):
    c = make(bucket=False)
    patch(monkeypatch, c)
    assert run(['--yes']) == 0
    assert 'delete_bucket' not in c.s3.names()


def test_bucket_head_other_error_propagates(monkeypatch):
    c = make(s3_raises={'head_bucket': FakeError('AccessDenied')})
    patch(monkeypatch, c)
    with pytest.raises(FakeError):
        run(['--yes'])


def test_bucket_not_ours_skipped(monkeypatch, capsys):
    c = make(ours=False)
    patch(monkeypatch, c)
    assert run(['--yes']) == 1
    assert 'delete_objects' not in c.s3.names()
    assert 'delete_bucket' not in c.s3.names()
    assert 'not ours' in capsys.readouterr().err


def test_bucket_no_lifecycle_config_is_not_ours(monkeypatch):
    c = make(s3_raises={'get_bucket_lifecycle_configuration':
                        FakeError('NoSuchLifecycleConfiguration')})
    patch(monkeypatch, c)
    assert run(['--yes']) == 1
    assert 'delete_bucket' not in c.s3.names()


def test_bucket_lifecycle_other_error_is_recorded(monkeypatch, capsys):
    c = make(s3_raises={'get_bucket_lifecycle_configuration':
                        FakeError('AccessDenied')})
    patch(monkeypatch, c)
    assert run(['--yes']) == 1
    assert 'AccessDenied' in capsys.readouterr().err
    assert 'delete_bucket' not in c.s3.names()


def test_bucket_head_error_in_delete_bucket_is_recorded(capsys):
    s3 = Rec(raises={'head_bucket': FakeError('AccessDenied')})
    sts = Rec(returns={'get_caller_identity': {'Account': ACCOUNT}})
    results = ua.Results()
    ua.delete_bucket(s3, sts, REGION, False, results)
    assert len(results.failures) == 1
    assert 'AccessDenied' in capsys.readouterr().err


def test_terminate_error_recorded_and_later_steps_run(monkeypatch, capsys):
    c = make(ec2_raises={'terminate_instances':
                         FakeError('UnauthorizedOperation')})
    patch(monkeypatch, c)
    assert run(['--yes']) == 1
    assert 'UnauthorizedOperation' in capsys.readouterr().err
    assert 'delete_security_group' not in c.ec2.names()
    assert 'delete_role' in c.iam.names()
    assert 'delete_bucket' in c.s3.names()


def test_bucket_pagination(monkeypatch):
    pages = seq(
        {'Versions': [{'Key': 'a', 'VersionId': '1'}], 'IsTruncated': True,
         'NextKeyMarker': 'a', 'NextVersionIdMarker': '1'},
        {'DeleteMarkers': [{'Key': 'b', 'VersionId': '2'}],
         'IsTruncated': False})
    c = make(s3_returns={'list_object_versions': pages})
    patch(monkeypatch, c)
    assert run(['--yes']) == 0
    lists = c.s3.kw('list_object_versions')
    assert lists[1]['KeyMarker'] == 'a' and lists[1]['VersionIdMarker'] == '1'
    sent = [o for kw in c.s3.kw('delete_objects')
            for o in kw['Delete']['Objects']]
    assert sent == [{'Key': 'a', 'VersionId': '1'},
                    {'Key': 'b', 'VersionId': '2'}]


def test_bucket_batches_split_at_1000(monkeypatch):
    versions = [{'Key': f'k{i}', 'VersionId': 'v'} for i in range(2500)]
    c = make(s3_returns={'list_object_versions': {'Versions': versions}})
    patch(monkeypatch, c)
    assert run(['--yes']) == 0
    sizes = [len(kw['Delete']['Objects']) for kw in c.s3.kw('delete_objects')]
    assert sizes == [1000, 1000, 500]


def test_bucket_empty_has_no_delete_objects(monkeypatch):
    c = make(s3_returns={'list_object_versions': {}})
    patch(monkeypatch, c)
    assert run(['--yes']) == 0
    assert 'delete_objects' not in c.s3.names()
    assert 'delete_bucket' in c.s3.names()


def test_delete_objects_errors_block_bucket_delete(monkeypatch, capsys):
    c = make(s3_returns={'delete_objects':
                         {'Errors': [{'Key': 'a', 'Code': 'AccessDenied'}]}})
    patch(monkeypatch, c)
    assert run(['--yes']) == 1
    assert 'delete_bucket' not in c.s3.names()
    assert 'could not be deleted' in capsys.readouterr().err


# ------------------------------------------------------------- state / dry

def test_state_kept_on_failure(monkeypatch, tmp_path):
    c = make(iam_raises={'delete_role': FakeError('DeleteConflict')})
    patch(monkeypatch, c)
    (tmp_path / 'vm.json').write_text('{}')
    assert run(['--yes']) == 1
    assert (tmp_path / 'vm.json').exists()


def test_success_without_state_file(monkeypatch):
    c = make()
    patch(monkeypatch, c)
    assert run(['--yes']) == 0


def test_dry_run_is_read_only(monkeypatch, tmp_path, capsys):
    c = make()
    patch(monkeypatch, c)
    (tmp_path / 'vm.json').write_text('{}')
    assert run(['--dry-run']) == 0
    assert not [n for n in all_names(c) if n.startswith(MUTATING)]
    assert (tmp_path / 'vm.json').exists()
    out = capsys.readouterr().out
    assert 'would remove' in out and 'vm.json' in out
    assert f'account={ACCOUNT} region={REGION}' in out


def test_dry_run_reports_foreign_bucket(monkeypatch):
    c = make(ours=False)
    patch(monkeypatch, c)
    assert run(['-n']) == 1
    assert 'delete_bucket' not in c.s3.names()


def test_dry_run_timeout_branch_not_taken(monkeypatch):
    c = make()
    patch(monkeypatch, c)
    r = ua.Results()
    assert ua.terminate_instances(c.ec2, True, r) is True
    assert 'terminate_instances' not in c.ec2.names()


# ------------------------------------------------------------- confirmation

def test_confirm_correct_account(monkeypatch):
    c = make()
    patch(monkeypatch, c)
    prompts = []
    monkeypatch.setattr('builtins.input',
                        lambda p: prompts.append(p) or f' {ACCOUNT} ')
    assert run() == 0
    assert ACCOUNT in prompts[0]
    assert 'delete_bucket' in c.s3.names()


def test_confirm_wrong_account(monkeypatch):
    c = make()
    patch(monkeypatch, c)
    monkeypatch.setattr('builtins.input', lambda *_: 'yes')
    assert run() == 1
    assert not [n for n in all_names(c) if n.startswith(MUTATING)]


def test_confirm_eof(monkeypatch, capsys):
    c = make()
    patch(monkeypatch, c)

    def eof(*_):
        raise EOFError
    monkeypatch.setattr('builtins.input', eof)
    assert run() == 2
    assert not [n for n in all_names(c) if n.startswith(MUTATING)]
    assert 'non-interactive' in capsys.readouterr().err


# ------------------------------------------------------------------ region

def test_region_from_flag(monkeypatch):
    c = make(instances=(), sg=(), role=False, profile=False, bucket=False)
    seen = []
    svc = {'ec2': c.ec2, 'iam': c.iam, 's3': c.s3, 'sts': c.sts}
    monkeypatch.setattr(ua.aws, 'client',
                        lambda s, region=None: seen.append(region) or svc[s])
    assert ua.main(['--region', 'eu-west-1']) == 0
    assert set(seen) == {'eu-west-1'}


def test_region_from_env(monkeypatch):
    c = make(instances=(), sg=(), role=False, profile=False, bucket=False)
    seen = []
    svc = {'ec2': c.ec2, 'iam': c.iam, 's3': c.s3, 'sts': c.sts}
    monkeypatch.setattr(ua.aws, 'client',
                        lambda s, region=None: seen.append(region) or svc[s])
    monkeypatch.setenv('BVL_AWS_REGION', 'ap-south-1')
    assert ua.main([]) == 0
    assert set(seen) == {'ap-south-1'}


def test_region_from_state_file(monkeypatch, tmp_path):
    c = make(instances=(), sg=(), role=False, profile=False, bucket=False)
    seen = []
    svc = {'ec2': c.ec2, 'iam': c.iam, 's3': c.s3, 'sts': c.sts}
    monkeypatch.setattr(ua.aws, 'client',
                        lambda s, region=None: seen.append(region) or svc[s])
    (tmp_path / 'vm.json').write_text('{"region": "ca-central-1"}')
    assert ua.main(['-n']) == 0
    assert set(seen) == {'ca-central-1'}


def test_no_region_exits_2(capsys):
    assert ua.main([]) == 2
    assert 'no region' in capsys.readouterr().err


def test_runtime_error_exit_1(monkeypatch, capsys):
    monkeypatch.setattr(ua, 'run', lambda a: (_ for _ in ()).throw(
        RuntimeError('bad')))
    assert ua.main(['--region', REGION]) == 1
    assert 'error: bad' in capsys.readouterr().err


# ------------------------------------------------------------- drift guards

def test_drift_iam_constants_match_manage_vm():
    iam = Rec()
    mv._ensure_iam(iam, ACCOUNT, REGION)
    assert iam.kw('put_role_policy')[0]['PolicyName'] == ua.INLINE_POLICY_NAME
    assert iam.kw('attach_role_policy')[0]['PolicyArn'] == \
        ua.SSM_CORE_POLICY_ARN
    assert iam.kw('get_role')[0]['RoleName'] == ua.aws.ROLE_NAME
    assert iam.kw('get_instance_profile')[0]['InstanceProfileName'] == \
        ua.aws.PROFILE_NAME


def test_drift_lifecycle_rule_matches_ensure_bucket():
    s3 = Rec()
    ua.aws.ensure_bucket(s3, BUCKET, REGION)
    rules = s3.kw('put_bucket_lifecycle_configuration')[0][
        'LifecycleConfiguration']['Rules']
    assert [r['ID'] for r in rules] == [ua.LIFECYCLE_RULE_ID]


def test_drift_tags_match_manage_vm():
    tags = {t['Key']: t['Value'] for t in mv._tag_spec('instance')['Tags']}
    filt = {f['Name'][4:]: f['Values'][0] for f in ua._TAG_FILTERS}
    assert tags == filt


def test_iam_get_error_propagates(monkeypatch):
    c = make(iam_raises={'get_role': FakeError('AccessDenied')})
    patch(monkeypatch, c)
    with pytest.raises(FakeError):
        run(['--yes'])


def test_terminate_with_no_instances_is_noop(monkeypatch):
    c = make(instances=())
    patch(monkeypatch, c)
    assert ua.terminate_instances(c.ec2, False, ua.Results()) is True
    assert 'terminate_instances' not in c.ec2.names()


# ------------------------------------------------- unexpected AWS errors

def test_delete_bucket_error_is_reported_and_state_kept(monkeypatch, tmp_path,
                                                        capsys):
    # Requirement: a failure in one step is recorded (exit 1, state file
    # kept) rather than crashing with a traceback.
    c = make(s3_raises={'delete_bucket': FakeError('BucketNotEmpty')})
    patch(monkeypatch, c)
    (tmp_path / 'vm.json').write_text('{}')
    assert run(['--yes']) == 1
    assert (tmp_path / 'vm.json').exists()
    assert 'BucketNotEmpty' in capsys.readouterr().err


# ------------------------------------------------- AWS error handling (main)

def cerr(code, message='', op=None):
    return ua.bce.ClientError({'Error': {'Code': code, 'Message': message}},
                              op)


def assert_one_error_line(capsys):
    cap = capsys.readouterr()
    lines = [line for line in cap.err.splitlines() if line.strip()]
    assert len(lines) == 1 and lines[0].startswith('error:')
    assert 'Traceback' not in cap.err + cap.out
    return lines[0]


def test_no_credentials_from_account_lookup(monkeypatch, capsys):
    c = make()
    c.sts._raises['get_caller_identity'] = ua.bce.NoCredentialsError()
    patch(monkeypatch, c)
    assert run(['--yes']) == 1
    line = assert_one_error_line(capsys)
    assert 'aws sso login' in line and 'AWS_PROFILE' in line
    assert not [n for n in all_names(c) if n.startswith(MUTATING)]


class SSOTokenLoadError(ua.bce.BotoCoreError):
    pass


@pytest.mark.parametrize('err', [
    cerr('ExpiredToken', 'The security token included is expired', 'X'),
    ua.bce.PartialCredentialsError(),
    SSOTokenLoadError(),
])
def test_other_credential_triggers(monkeypatch, capsys, err):
    c = make()
    c.sts._raises['get_caller_identity'] = err
    patch(monkeypatch, c)
    assert run(['--yes']) == 1
    line = assert_one_error_line(capsys)
    assert 'aws sso login' in line and 'AWS_PROFILE' in line


def test_credentials_error_from_client_factory(monkeypatch, capsys):
    def factory(service, region=None):
        raise ua.bce.NoCredentialsError()
    monkeypatch.setattr(ua.aws, 'client', factory)
    assert run(['--yes']) == 1
    assert 'aws sso login' in assert_one_error_line(capsys)


def test_access_denied_names_action_from_message(monkeypatch, capsys):
    c = make(ec2_raises={'describe_instances': cerr(
        'AccessDenied', 'User: arn:aws:iam::1:user/x is not authorized to '
        'perform: ec2:DescribeInstances on resource: *', 'DescribeInstances')})
    patch(monkeypatch, c)
    assert run(['--yes']) == 1
    line = assert_one_error_line(capsys)
    assert 'ec2:DescribeInstances' in line and 'arn:' not in line


def test_access_denied_falls_back_to_operation(monkeypatch, capsys):
    c = make(ec2_raises={'describe_instances': cerr(
        'UnauthorizedOperation', 'You are not authorized', 'DescribeInstances')})
    patch(monkeypatch, c)
    assert run(['--yes']) == 1
    assert 'DescribeInstances' in assert_one_error_line(capsys)


def test_access_denied_unknown_action(monkeypatch, capsys):
    c = make(ec2_raises={'describe_instances': cerr('AccessDenied')})
    patch(monkeypatch, c)
    assert run(['--yes']) == 1
    assert 'unknown action' in assert_one_error_line(capsys)


def test_multiline_message_collapsed(monkeypatch, capsys):
    c = make(ec2_raises={'describe_instances': cerr(
        'Throttling', 'slow\ndown\n  please', 'DescribeInstances')})
    patch(monkeypatch, c)
    assert run(['--yes']) == 1
    line = assert_one_error_line(capsys)
    assert line == 'error: AWS call failed: Throttling: slow down please'


def test_generic_botocore_error(monkeypatch, capsys):
    c = make(ec2_raises={'describe_instances': ua.bce.BotoCoreError()})
    patch(monkeypatch, c)
    assert run(['--yes']) == 1
    assert 'AWS call failed' in assert_one_error_line(capsys)


def test_client_error_without_code(monkeypatch, capsys):
    c = make(ec2_raises={'describe_instances': ua.bce.ClientError({}, 'Op')})
    patch(monkeypatch, c)
    assert run(['--yes']) == 1
    assert 'AWS call failed' in assert_one_error_line(capsys)


def test_read_failure_in_delete_bucket_is_caught_by_main(monkeypatch, capsys):
    c = make()
    calls = {'n': 0}

    def ident(_kw):
        calls['n'] += 1
        if calls['n'] > 2:
            raise ua.bce.NoCredentialsError()
        return {'Account': ACCOUNT}
    c.sts._returns['get_caller_identity'] = ident
    patch(monkeypatch, c)
    assert run(['--yes']) == 1
    assert 'aws sso login' in assert_one_error_line(capsys)


def test_mutating_client_error_still_recorded(monkeypatch, capsys, tmp_path):
    c = make(iam_raises={'delete_role': cerr('AccessDenied', 'no', 'DeleteRole')})
    patch(monkeypatch, c)
    (tmp_path / 'vm.json').write_text('{}')
    assert run(['--yes']) == 1
    err = capsys.readouterr().err
    assert 'IAM delete role' in err and '1 step(s) failed' in err
    assert 'delete_bucket' in c.s3.names()
    assert (tmp_path / 'vm.json').exists()


# ------------------------------------------------------------- abbreviation

@pytest.mark.parametrize('argv', [['--purge'], ['--dr'], ['--ye'],
                                  ['--reg', 'us-west-2']])
def test_abbreviated_flags_rejected(argv):
    with pytest.raises(SystemExit) as ei:
        ua.main(argv)
    assert ei.value.code == 2


def test_parser_disallows_abbrev_and_exact_flags_work():
    p = ua._build_parser()
    assert p.allow_abbrev is False
    a = p.parse_args(['--region', 'r', '--dry-run', '--yes'])
    assert (a.region, a.dry_run, a.yes) == ('r', True, True)
    a = p.parse_args(['-n', '-y'])
    assert a.dry_run and a.yes


# --------------------------------------------------------------- plan wording

PENDING = (f'unverified: S3 bucket {BUCKET} - lifecycle rule '
           f'{ua.LIFECYCLE_RULE_ID} not yet checked; deleted only if present')


def test_dry_plan_does_not_claim_bucket_removal_before_check(monkeypatch,
                                                             capsys):
    c = make()
    patch(monkeypatch, c)
    assert run(['-n']) == 0
    out = capsys.readouterr().out
    first = out.split('lifecycle rule bvl-expire-staging found')[0]
    lines = [line for line in first.splitlines() if BUCKET in line]
    assert lines and all('remove' not in line for line in lines)


def test_pending_line_identical_in_dry_and_real(monkeypatch, capsys):
    c = make()
    patch(monkeypatch, c)
    run(['-n'])
    dry = set(capsys.readouterr().out.splitlines())
    c = make()
    patch(monkeypatch, c)
    run(['--yes'])
    real = set(capsys.readouterr().out.splitlines())
    assert f'  {PENDING}' in dry and f'  {PENDING}' in real
    assert not any('will remove: S3 bucket' in line for line in real)


def test_dry_run_ours_prints_found_then_would_remove(monkeypatch, capsys):
    c = make(ours=True)
    patch(monkeypatch, c)
    assert run(['-n']) == 0
    out = capsys.readouterr().out
    found = out.index('lifecycle rule bvl-expire-staging found')
    assert found < out.index('would remove: S3 bucket')


def test_not_ours_never_claims_removal(monkeypatch, capsys):
    c = make(ours=False)
    patch(monkeypatch, c)
    assert run(['-n']) == 1
    assert 'remove: S3 bucket' not in capsys.readouterr().out


def test_bucket_only_deployment_still_prompts(monkeypatch, capsys):
    c = make(instances=(), sg=(), role=False, profile=False)
    patch(monkeypatch, c)
    prompts = []
    monkeypatch.setattr('builtins.input',
                        lambda p: prompts.append(p) or ACCOUNT)
    assert run() == 0
    assert 'nothing to remove' not in capsys.readouterr().out
    assert prompts
