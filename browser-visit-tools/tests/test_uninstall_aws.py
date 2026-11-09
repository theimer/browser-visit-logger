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
