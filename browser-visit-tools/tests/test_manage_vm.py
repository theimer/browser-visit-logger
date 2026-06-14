"""Unit tests for manage_vm.py.

boto3 is faked (injected via test_bvl_aws's import side effect isn't
guaranteed ordering-wise, so we inject here too) and every AWS client is
a recording fake.  The single seam is _bvl_aws.client, monkeypatched to
hand back per-service fakes.
"""
import argparse
import sys
import types
from pathlib import Path

import pytest

_fake_boto3 = types.ModuleType('boto3')
_fake_boto3.client = lambda service, region_name=None: None
sys.modules.setdefault('boto3', _fake_boto3)

sys.path.insert(0, str(Path(__file__).parent.parent))
import manage_vm as mv  # noqa: E402


class FakeError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {'Error': {'Code': code}}


class Rec:
    """Records every method call; returns canned values and/or raises
    per-method on demand."""
    def __init__(self, returns=None, raises=None):
        self.calls = []
        self._returns = returns or {}
        self._raises = raises or {}

    def __getattr__(self, name):
        def method(**kw):
            self.calls.append((name, kw))
            if name in self._raises:
                raise self._raises[name]
            return self._returns.get(name)
        return method

    def names(self):
        return [c[0] for c in self.calls]


def _patch_clients(monkeypatch, **services):
    monkeypatch.setattr(mv.aws, 'client',
                        lambda service, region=None: services[service])


def _reservations(*instances):
    return {'Reservations': [{'Instances': [i]} for i in instances]}


def _inst(iid='i-1', state='running', **extra):
    return {'InstanceId': iid, 'State': {'Name': state}, **extra}


# --------------------------------------------------------------------------
# _resolve_ami / _ensure_security_group / _ensure_iam (helpers)
# --------------------------------------------------------------------------

def test_resolve_ami():
    ssm = Rec(returns={'get_parameter': {'Parameter': {'Value': 'ami-9'}}})
    assert mv._resolve_ami(ssm, 'arm64') == 'ami-9'
    assert 'arm64' in ssm.calls[0][1]['Name']


def test_ensure_sg_existing():
    ec2 = Rec(returns={'describe_security_groups':
                       {'SecurityGroups': [{'GroupId': 'sg-x'}]}})
    assert mv._ensure_security_group(ec2, ['1.2.3.4/32']) == 'sg-x'
    assert 'create_security_group' not in ec2.names()


def test_ensure_sg_create():
    ec2 = Rec(returns={'describe_security_groups': {'SecurityGroups': []},
                       'create_security_group': {'GroupId': 'sg-new'}})
    assert mv._ensure_security_group(ec2, ['1.2.3.4/32']) == 'sg-new'
    # ingress must open exactly the gRPC port for the given CIDR — and
    # nothing for SSH (22).
    perm = [c for c in ec2.calls
            if c[0] == 'authorize_security_group_ingress'][0][1]
    rule = perm['IpPermissions'][0]
    assert rule['FromPort'] == 50443 and rule['ToPort'] == 50443
    assert rule['IpProtocol'] == 'tcp'
    assert rule['IpRanges'][0]['CidrIp'] == '1.2.3.4/32'


def test_ensure_sg_authorizes_every_cidr():
    ec2 = Rec(returns={'describe_security_groups': {'SecurityGroups': []},
                       'create_security_group': {'GroupId': 'sg-new'}})
    mv._ensure_security_group(ec2, ['1.1.1.1/32', '2.2.2.2/32'])
    cidrs = [c[1]['IpPermissions'][0]['IpRanges'][0]['CidrIp']
             for c in ec2.calls if c[0] == 'authorize_security_group_ingress']
    assert cidrs == ['1.1.1.1/32', '2.2.2.2/32']


def test_ensure_sg_duplicate_ingress_ignored():
    ec2 = Rec(returns={'describe_security_groups':
                       {'SecurityGroups': [{'GroupId': 'sg-x'}]}},
              raises={'authorize_security_group_ingress':
                      FakeError('InvalidPermission.Duplicate')})
    assert mv._ensure_security_group(ec2, ['1.2.3.4/32']) == 'sg-x'


def test_ensure_sg_ingress_other_error_reraises():
    ec2 = Rec(returns={'describe_security_groups':
                       {'SecurityGroups': [{'GroupId': 'sg-x'}]}},
              raises={'authorize_security_group_ingress':
                      FakeError('RulesPerGroupLimitExceeded')})
    with pytest.raises(FakeError):
        mv._ensure_security_group(ec2, ['1.2.3.4/32'])


def test_ensure_iam_all_exist():
    iam = Rec()
    assert mv._ensure_iam(iam, '123', 'us-east-1') == mv.aws.PROFILE_NAME
    assert 'create_role' not in iam.names()
    assert 'create_instance_profile' not in iam.names()


def test_ensure_iam_creates_role_and_profile():
    iam = Rec(raises={'get_role': FakeError('NoSuchEntity'),
                      'get_instance_profile': FakeError('NoSuchEntity')})
    mv._ensure_iam(iam, '123456789012', 'us-east-1')
    calls = {c[0]: c[1] for c in iam.calls}
    assert 'create_role' in calls and 'create_instance_profile' in calls
    # the SSM-core managed policy is what makes the VM reachable over SSM
    assert calls['attach_role_policy']['PolicyArn'].endswith(
        'AmazonSSMManagedInstanceCore')
    # the role is bound to the profile that RunInstances will pass
    assert calls['add_role_to_instance_profile']['RoleName'] == mv.aws.ROLE_NAME
    # inline policy lets the VM read its own deploy bucket
    inline = calls['put_role_policy']['PolicyDocument']
    assert 'bvl-sync-deploy-123456789012-us-east-1' in inline
    assert 's3:GetObject' in inline


def test_ensure_iam_get_role_other_error():
    iam = Rec(raises={'get_role': FakeError('AccessDenied')})
    with pytest.raises(FakeError):
        mv._ensure_iam(iam, '123', 'us-east-1')


def test_ensure_iam_get_profile_other_error():
    iam = Rec(raises={'get_instance_profile': FakeError('Throttling')})
    with pytest.raises(FakeError):
        mv._ensure_iam(iam, '123', 'us-east-1')


def test_ensure_iam_add_role_limit_exceeded_ignored():
    iam = Rec(raises={'add_role_to_instance_profile':
                      FakeError('LimitExceeded')})
    assert mv._ensure_iam(iam, '123', 'us-east-1') == mv.aws.PROFILE_NAME


def test_ensure_iam_add_role_other_error():
    iam = Rec(raises={'add_role_to_instance_profile': FakeError('Boom')})
    with pytest.raises(FakeError):
        mv._ensure_iam(iam, '123', 'us-east-1')


# --------------------------------------------------------------------------
# cmd_create
# --------------------------------------------------------------------------

def _create_args(**kw):
    base = dict(region='us-east-1', allow_cidr=['1.2.3.4/32'],
                instance_type=None, volume_size=20, arch='x86_64', ami=None)
    base.update(kw)
    return argparse.Namespace(action='create', **base)


def test_create_ambiguous_existing(monkeypatch, capsys):
    ec2 = Rec(returns={'describe_instances': _reservations(_inst('i-1'),
                                                           _inst('i-2'))})
    _patch_clients(monkeypatch, ec2=ec2)
    assert mv.cmd_create(_create_args()) == 1
    assert 'multiple tagged instances' in capsys.readouterr().err


def test_create_already_exists(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv('BVL_VM_STATE_FILE', str(tmp_path / 'vm.json'))
    ec2 = Rec(returns={'describe_instances': _reservations(_inst('i-1'))})
    _patch_clients(monkeypatch, ec2=ec2)
    assert mv.cmd_create(_create_args()) == 0
    assert 'already exists' in capsys.readouterr().out
    assert mv.aws.load_state()['instance_id'] == 'i-1'


def test_create_launches(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv('BVL_VM_STATE_FILE', str(tmp_path / 'vm.json'))
    ec2 = Rec(returns={
        'describe_instances': {'Reservations': []},
        'describe_security_groups': {'SecurityGroups': []},
        'create_security_group': {'GroupId': 'sg-new'},
        'run_instances': {'Instances': [{'InstanceId': 'i-new'}]},
    })
    sts = Rec(returns={'get_caller_identity': {'Account': '123456789012'}})
    iam = Rec()
    ssm = Rec(returns={'get_parameter': {'Parameter': {'Value': 'ami-1'}}})
    _patch_clients(monkeypatch, ec2=ec2, sts=sts, iam=iam, ssm=ssm)
    assert mv.cmd_create(_create_args()) == 0
    state = mv.aws.load_state()
    assert state['instance_id'] == 'i-new'
    # state captures enough to manage the VM later without re-discovery
    assert state['security_group_id'] == 'sg-new'
    assert state['iam_instance_profile'] == mv.aws.PROFILE_NAME
    assert state['arch'] == 'x86_64'
    run_kw = [c for c in ec2.calls if c[0] == 'run_instances'][0][1]
    # default instance type for x86_64, the resolved AMI, the SG + profile,
    # a gp3 root volume, and the discovery tags must all be wired up.
    assert run_kw['InstanceType'] == 't3.small'
    assert run_kw['ImageId'] == 'ami-1'
    assert run_kw['SecurityGroupIds'] == ['sg-new']
    assert run_kw['IamInstanceProfile'] == {'Name': mv.aws.PROFILE_NAME}
    ebs = run_kw['BlockDeviceMappings'][0]['Ebs']
    assert ebs['VolumeType'] == 'gp3' and ebs['VolumeSize'] == 20
    tags = run_kw['TagSpecifications'][0]['Tags']
    assert {'Key': 'bvl:role', 'Value': 'sync-server'} in tags
    # deterministic ClientToken guards against a double-launch on retry
    assert run_kw['ClientToken'] == 'bvl-sync-server-us-east-1'
    assert 'created i-new' in capsys.readouterr().out


def test_create_with_ami_and_type_override(monkeypatch, tmp_path):
    monkeypatch.setenv('BVL_VM_STATE_FILE', str(tmp_path / 'vm.json'))
    ec2 = Rec(returns={
        'describe_instances': {'Reservations': []},
        'describe_security_groups': {'SecurityGroups': [{'GroupId': 'sg-1'}]},
        'run_instances': {'Instances': [{'InstanceId': 'i-z'}]},
    })
    sts = Rec(returns={'get_caller_identity': {'Account': '1'}})
    iam = Rec()
    ssm = Rec()  # get_parameter must NOT be called (ami provided)
    _patch_clients(monkeypatch, ec2=ec2, sts=sts, iam=iam, ssm=ssm)
    args = _create_args(ami='ami-custom', instance_type='c7g.large',
                        arch='arm64')
    assert mv.cmd_create(args) == 0
    assert 'get_parameter' not in ssm.names()
    run_kw = [c for c in ec2.calls if c[0] == 'run_instances'][0][1]
    assert run_kw['InstanceType'] == 'c7g.large'
    assert run_kw['ImageId'] == 'ami-custom'


# --------------------------------------------------------------------------
# lifecycle subcommands
# --------------------------------------------------------------------------

def _lifecycle_args(action, **kw):
    return argparse.Namespace(action=action, region=None, **kw)


def test_cmd_start(monkeypatch, capsys):
    ec2 = Rec(returns={'describe_instances': _reservations(_inst('i-1'))})
    _patch_clients(monkeypatch, ec2=ec2)
    assert mv.cmd_start(_lifecycle_args('start')) == 0
    assert 'start_instances' in ec2.names()
    assert 'starting i-1' in capsys.readouterr().out


def test_cmd_stop(monkeypatch):
    ec2 = Rec(returns={'describe_instances': _reservations(_inst('i-1'))})
    _patch_clients(monkeypatch, ec2=ec2)
    assert mv.cmd_stop(_lifecycle_args('stop')) == 0
    assert 'stop_instances' in ec2.names()


def test_cmd_reboot(monkeypatch):
    ec2 = Rec(returns={'describe_instances': _reservations(_inst('i-1'))})
    _patch_clients(monkeypatch, ec2=ec2)
    assert mv.cmd_reboot(_lifecycle_args('reboot')) == 0
    assert 'reboot_instances' in ec2.names()


def test_cmd_status_with_public(monkeypatch, capsys):
    inst = _inst('i-1', InstanceType='t3.small',
                 PublicDnsName='ec2.example.com', PublicIpAddress='1.2.3.4')
    ec2 = Rec(returns={'describe_instances': _reservations(inst)})
    _patch_clients(monkeypatch, ec2=ec2)
    assert mv.cmd_status(_lifecycle_args('status')) == 0
    out = capsys.readouterr().out
    assert 'ec2.example.com' in out and '1.2.3.4' in out


def test_cmd_status_no_public(monkeypatch, capsys):
    ec2 = Rec(returns={'describe_instances': _reservations(_inst('i-1'))})
    _patch_clients(monkeypatch, ec2=ec2)
    assert mv.cmd_status(_lifecycle_args('status')) == 0
    assert '(none)' in capsys.readouterr().out


def test_cmd_terminate_plain(monkeypatch, capsys):
    ec2 = Rec(returns={'describe_instances': _reservations(_inst('i-1'))})
    _patch_clients(monkeypatch, ec2=ec2)
    args = _lifecycle_args('terminate', purge_infra=False)
    assert mv.cmd_terminate(args) == 0
    assert 'terminate_instances' in ec2.names()
    assert 'terminating i-1' in capsys.readouterr().out


def test_cmd_terminate_with_purge(monkeypatch):
    ec2 = Rec(returns={'describe_instances': _reservations(_inst('i-1'))})
    iam = Rec()
    _patch_clients(monkeypatch, ec2=ec2, iam=iam)
    args = _lifecycle_args('terminate', purge_infra=True)
    assert mv.cmd_terminate(args) == 0
    assert 'delete_security_group' in ec2.names()
    assert 'delete_role' in iam.names()


def test_purge_infra_warns_on_failure(monkeypatch, capsys):
    ec2 = Rec(raises={'delete_security_group': FakeError('DependencyViolation')})
    iam = Rec(raises={'remove_role_from_instance_profile':
                      FakeError('NoSuchEntity')})
    mv._purge_infra(ec2, iam)
    err = capsys.readouterr().err
    assert 'could not delete security group' in err
    assert 'could not fully delete IAM' in err


# --------------------------------------------------------------------------
# main dispatch + error surfacing + parser
# --------------------------------------------------------------------------

def test_main_runtime_error_returns_1(monkeypatch, capsys):
    ec2 = Rec(returns={'describe_instances': {'Reservations': []}})
    _patch_clients(monkeypatch, ec2=ec2)
    assert mv.main(['status']) == 1
    assert 'error:' in capsys.readouterr().err


def test_main_dispatch_create(monkeypatch, tmp_path):
    monkeypatch.setenv('BVL_VM_STATE_FILE', str(tmp_path / 'vm.json'))
    ec2 = Rec(returns={'describe_instances': _reservations(_inst('i-1'))})
    _patch_clients(monkeypatch, ec2=ec2)
    assert mv.main(['create', '--allow-cidr', '1.2.3.4/32']) == 0


def test_parser_requires_allow_cidr():
    with pytest.raises(SystemExit):
        mv._build_parser().parse_args(['create'])
