#!/usr/bin/env python3
"""
manage_vm.py — Create and manage the EC2 VM that hosts the
browser-visit sync-server.

Subcommands
-----------
    create     Idempotently create the VM (and its security group + IAM
               instance profile).  Re-running is a no-op if the tagged
               instance already exists.
    start      Start a stopped instance.
    stop       Stop a running instance.
    reboot     Reboot the instance.
    status     Print instance id, state, and public address.
    terminate  Terminate the instance (optionally purge shared infra).

Remote management of the sync-server *process* lives in the sibling
``manage_sync_server.py`` — this tool only owns the VM and its AWS
infrastructure.

Idempotency keys on the tags ``bvl:role=sync-server`` /
``bvl:managed-by=browser-visit-tools``; ``create`` looks for an existing
tagged, non-terminated instance before launching a new one.  AWS
credentials come from the ambient boto3 chain (env / shared config /
SSO).  See the README for the IAM permissions the operator needs
(notably ``iam:PassRole``).

Exit codes: 0 success, 1 AWS/operation failure, 2 usage.
"""

import argparse
import json
import sys

import _bvl_aws as aws

# AMI arch → (SSM public parameter suffix, default instance type).
_ARCH = {
    'x86_64': ('x86_64', 't3.small'),
    'arm64': ('arm64', 't4g.small'),
}

_TRUST_POLICY = json.dumps({
    'Version': '2012-10-17',
    'Statement': [{
        'Effect': 'Allow',
        'Principal': {'Service': 'ec2.amazonaws.com'},
        'Action': 'sts:AssumeRole',
    }],
})

_SSM_CORE_POLICY = 'arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore'


def _tag_spec(resource):
    """A TagSpecification applying the managed-by + role tags."""
    return {
        'ResourceType': resource,
        'Tags': [
            {'Key': aws.TAG_MANAGED_BY[0], 'Value': aws.TAG_MANAGED_BY[1]},
            {'Key': aws.TAG_ROLE[0], 'Value': aws.TAG_ROLE[1]},
        ],
    }


def _resolve_ami(ssm, arch):
    """Latest Amazon Linux 2023 AMI id for ``arch`` via the SSM public
    parameter (so we never hardcode a region-specific id)."""
    suffix = _ARCH[arch][0]
    name = (f'/aws/service/ami-amazon-linux-latest/'
            f'al2023-ami-kernel-default-{suffix}')
    return ssm.get_parameter(Name=name)['Parameter']['Value']


def _ensure_security_group(ec2, allow_cidrs):
    """Find-or-create the sync-server security group; ensure ingress for
    the gRPC port from each ``allow_cidrs`` entry.  Returns the sg id."""
    resp = ec2.describe_security_groups(Filters=[
        {'Name': 'group-name', 'Values': [aws.SG_NAME]}])
    groups = resp.get('SecurityGroups', [])
    if groups:
        sg_id = groups[0]['GroupId']
    else:
        created = ec2.create_security_group(
            GroupName=aws.SG_NAME,
            Description='browser-visit sync-server gRPC ingress',
            TagSpecifications=[_tag_spec('security-group')])
        sg_id = created['GroupId']
    for cidr in allow_cidrs:
        try:
            ec2.authorize_security_group_ingress(
                GroupId=sg_id,
                IpPermissions=[{
                    'IpProtocol': 'tcp',
                    'FromPort': aws.GRPC_PORT,
                    'ToPort': aws.GRPC_PORT,
                    'IpRanges': [{'CidrIp': cidr}],
                }])
        except Exception as e:  # noqa: BLE001
            if aws.error_code(e) != 'InvalidPermission.Duplicate':
                raise
    return sg_id


def _ensure_iam(iam, account, region):
    """Find-or-create the instance role + profile.  Attaches the SSM
    core managed policy and an inline policy letting the VM read the
    deploy staging bucket.  Returns the instance-profile name."""
    try:
        iam.get_role(RoleName=aws.ROLE_NAME)
    except Exception as e:  # noqa: BLE001
        if aws.error_code(e) != 'NoSuchEntity':
            raise
        iam.create_role(RoleName=aws.ROLE_NAME,
                        AssumeRolePolicyDocument=_TRUST_POLICY)
    iam.attach_role_policy(RoleName=aws.ROLE_NAME, PolicyArn=_SSM_CORE_POLICY)
    bucket = f'bvl-sync-deploy-{account}-{region}'
    iam.put_role_policy(
        RoleName=aws.ROLE_NAME,
        PolicyName='bvl-deploy-bucket-read',
        PolicyDocument=json.dumps({
            'Version': '2012-10-17',
            'Statement': [{
                'Effect': 'Allow',
                'Action': 's3:GetObject',
                'Resource': f'arn:aws:s3:::{bucket}/*',
            }],
        }))
    try:
        iam.get_instance_profile(InstanceProfileName=aws.PROFILE_NAME)
    except Exception as e:  # noqa: BLE001
        if aws.error_code(e) != 'NoSuchEntity':
            raise
        iam.create_instance_profile(InstanceProfileName=aws.PROFILE_NAME)
    try:
        iam.add_role_to_instance_profile(
            InstanceProfileName=aws.PROFILE_NAME, RoleName=aws.ROLE_NAME)
    except Exception as e:  # noqa: BLE001
        # LimitExceeded means the profile already carries its role.
        if aws.error_code(e) != 'LimitExceeded':
            raise
    return aws.PROFILE_NAME


def cmd_create(args):
    region = aws.resolve_region(args.region)
    ec2 = aws.client('ec2', region)
    existing = aws.discover_instances(ec2)
    if len(existing) > 1:
        ids = ', '.join(sorted(i['InstanceId'] for i in existing))
        print(f"error: multiple tagged instances already exist ({ids}); "
              "refusing to create another", file=sys.stderr)
        return 1
    if len(existing) == 1:
        inst = existing[0]
        print(f"VM already exists: {inst['InstanceId']} "
              f"({inst['State']['Name']}) — nothing to do")
        aws.save_state({**aws.load_state(), 'region': region,
                        'instance_id': inst['InstanceId'], 'arch': args.arch})
        return 0

    sts = aws.client('sts', region)
    account = sts.get_caller_identity()['Account']
    iam = aws.client('iam', region)
    ssm = aws.client('ssm', region)

    sg_id = _ensure_security_group(ec2, args.allow_cidr)
    profile = _ensure_iam(iam, account, region)
    ami = args.ami or _resolve_ami(ssm, args.arch)
    instance_type = args.instance_type or _ARCH[args.arch][1]

    print(f"launching {instance_type} ({args.arch}) from {ami} "
          f"in {region or 'default region'} ...")
    run = ec2.run_instances(
        ImageId=ami,
        InstanceType=instance_type,
        MinCount=1, MaxCount=1,
        SecurityGroupIds=[sg_id],
        IamInstanceProfile={'Name': profile},
        BlockDeviceMappings=[{
            'DeviceName': '/dev/xvda',
            'Ebs': {'VolumeSize': args.volume_size, 'VolumeType': 'gp3'},
        }],
        TagSpecifications=[_tag_spec('instance')],
        ClientToken=f'bvl-sync-server-{region or "default"}',
    )
    instance_id = run['Instances'][0]['InstanceId']
    aws.save_state({'region': region, 'instance_id': instance_id,
                    'arch': args.arch, 'security_group_id': sg_id,
                    'iam_instance_profile': profile})
    print(f"created {instance_id}; run `manage_sync_server.py provision` "
          "once it boots")
    return 0


def _resolve(args):
    """Shared setup for the lifecycle subcommands: (ec2, instance_id)."""
    region = aws.resolve_region(args.region)
    ec2 = aws.client('ec2', region)
    instance_id = aws.resolve_instance_id(ec2, aws.load_state())
    print(f"region={region or 'default'} instance={instance_id}")
    return ec2, instance_id


def cmd_start(args):
    ec2, instance_id = _resolve(args)
    ec2.start_instances(InstanceIds=[instance_id])
    print(f"starting {instance_id}")
    return 0


def cmd_stop(args):
    ec2, instance_id = _resolve(args)
    ec2.stop_instances(InstanceIds=[instance_id])
    print(f"stopping {instance_id}")
    return 0


def cmd_reboot(args):
    ec2, instance_id = _resolve(args)
    ec2.reboot_instances(InstanceIds=[instance_id])
    print(f"rebooting {instance_id}")
    return 0


def cmd_status(args):
    ec2, instance_id = _resolve(args)
    resp = ec2.describe_instances(InstanceIds=[instance_id])
    inst = resp['Reservations'][0]['Instances'][0]
    print(f"state:   {inst['State']['Name']}")
    print(f"type:    {inst.get('InstanceType', '?')}")
    print(f"public:  {inst.get('PublicDnsName') or '(none)'} "
          f"{inst.get('PublicIpAddress') or ''}".rstrip())
    return 0


def _purge_infra(ec2, iam):
    """Best-effort teardown of the shared SG / role / profile."""
    try:
        ec2.delete_security_group(GroupName=aws.SG_NAME)
    except Exception as e:  # noqa: BLE001
        print(f"warn: could not delete security group: {e}", file=sys.stderr)
    try:
        iam.remove_role_from_instance_profile(
            InstanceProfileName=aws.PROFILE_NAME, RoleName=aws.ROLE_NAME)
        iam.delete_instance_profile(InstanceProfileName=aws.PROFILE_NAME)
        iam.delete_role_policy(RoleName=aws.ROLE_NAME,
                               PolicyName='bvl-deploy-bucket-read')
        iam.detach_role_policy(RoleName=aws.ROLE_NAME,
                               PolicyArn=_SSM_CORE_POLICY)
        iam.delete_role(RoleName=aws.ROLE_NAME)
    except Exception as e:  # noqa: BLE001
        print(f"warn: could not fully delete IAM role/profile: {e}",
              file=sys.stderr)


def cmd_terminate(args):
    region = aws.resolve_region(args.region)
    ec2 = aws.client('ec2', region)
    instance_id = aws.resolve_instance_id(ec2, aws.load_state())
    print(f"region={region or 'default'} instance={instance_id}")
    ec2.terminate_instances(InstanceIds=[instance_id])
    print(f"terminating {instance_id}")
    if args.purge_infra:
        _purge_infra(ec2, aws.client('iam', region))
    return 0


_DISPATCH = {
    'create': cmd_create, 'start': cmd_start, 'stop': cmd_stop,
    'reboot': cmd_reboot, 'status': cmd_status, 'terminate': cmd_terminate,
}


def _build_parser():
    p = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    sub = p.add_subparsers(dest='action', required=True)

    pc = sub.add_parser('create', help='idempotently create the VM')
    pc.add_argument('--region', help='AWS region (else BVL_AWS_REGION)')
    pc.add_argument('--allow-cidr', action='append', required=True,
                    metavar='CIDR',
                    help='CIDR allowed to reach the gRPC port (repeatable)')
    pc.add_argument('--instance-type',
                    help='override the arch default (t3.small / t4g.small)')
    pc.add_argument('--volume-size', type=int, default=20,
                    help='root EBS size in GiB (default 20)')
    pc.add_argument('--arch', choices=sorted(_ARCH), default='x86_64',
                    help='instance/AMI arch; must match the deployed binary')
    pc.add_argument('--ami', help='override the resolved AL2023 AMI id')

    for name, helptext in (('start', 'start a stopped VM'),
                           ('stop', 'stop a running VM'),
                           ('reboot', 'reboot the VM'),
                           ('status', 'print VM state + address')):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument('--region', help='AWS region (else BVL_AWS_REGION)')

    pt = sub.add_parser('terminate', help='terminate the VM')
    pt.add_argument('--region', help='AWS region (else BVL_AWS_REGION)')
    pt.add_argument('--purge-infra', action='store_true',
                    help='also delete the shared security group + IAM role')
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
