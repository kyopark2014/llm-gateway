# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""VPC + multi-tier subnets + NAT (dev: single NAT)."""
from __future__ import annotations

import ipaddress
import time

from botocore.exceptions import ClientError

from ..config import InstallConfig
from ..state import State
from ..util import client, fail, log, tags


def ensure_vpc(cfg: InstallConfig, state: State) -> None:
    ec2 = client("ec2", cfg)
    name = f"{cfg.project}-{cfg.environment}"

    # Reuse if exists
    existing = ec2.describe_vpcs(
        Filters=[
            {"Name": "tag:Name", "Values": [name]},
            {"Name": "tag:ManagedBy", "Values": ["installer.py"]},
        ]
    ).get("Vpcs") or []
    if not existing:
        existing = ec2.describe_vpcs(
            Filters=[
                {"Name": "tag:Name", "Values": [name]},
                {"Name": "tag:Project", "Values": [cfg.project]},
            ]
        ).get("Vpcs") or []

    if existing:
        cfg.vpc_id = existing[0]["VpcId"]
        log(f"VPC reused: {cfg.vpc_id}")
        _fill_subnets(cfg, ec2)
        state.update({
            "vpc_id": cfg.vpc_id,
            "private_subnet_ids": cfg.private_subnet_ids,
            "public_subnet_ids": cfg.public_subnet_ids,
        })
        state.save()
        return

    cidr = cfg.vpc_cidr
    log(f"Creating VPC {name} cidr={cidr}")
    vpc = ec2.create_vpc(
        CidrBlock=cidr,
        TagSpecifications=[{"ResourceType": "vpc", "Tags": tags(cfg, {
            "Name": name, "Module": "vpc", "ManagedBy": "installer.py",
        })}],
    )["Vpc"]
    vpc_id = vpc["VpcId"]
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})
    waiter = ec2.get_waiter("vpc_available")
    waiter.wait(VpcIds=[vpc_id])

    igw = ec2.create_internet_gateway(
        TagSpecifications=[{"ResourceType": "internet-gateway", "Tags": tags(cfg, {"Name": f"{name}-igw"})}],
    )["InternetGateway"]
    igw_id = igw["InternetGatewayId"]
    ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)

    # Derive /24s from VPC /16 (or larger)
    net = ipaddress.ip_network(cidr)
    # Take first 8 /24-equivalent subnets from the VPC
    if net.prefixlen > 24:
        fail(f"vpc_cidr too small for multi-tier layout: {cidr}")
    subs = list(net.subnets(new_prefix=24))
    if len(subs) < 8:
        # fall back to packing in /20 VPC
        subs = list(net.subnets(new_prefix=min(28, net.prefixlen + 4)))
    azs = cfg.azs or _default_azs(cfg)
    if len(azs) < 2:
        fail("Need at least 2 AZs in config.azs")

    public_ids: list[str] = []
    private_ids: list[str] = []
    db_ids: list[str] = []
    cache_ids: list[str] = []

    # Layout: [pub0, pub1, priv0, priv1, db0, db1, cache0, cache1]
    layout = [
        ("public", 0, azs[0], {"kubernetes.io/role/elb": "1"}),
        ("public", 1, azs[1], {"kubernetes.io/role/elb": "1"}),
        ("private", 2, azs[0], {"kubernetes.io/role/internal-elb": "1"}),
        ("private", 3, azs[1], {"kubernetes.io/role/internal-elb": "1"}),
        ("database", 4, azs[0], {}),
        ("database", 5, azs[1], {}),
        ("elasticache", 6, azs[0], {}),
        ("elasticache", 7, azs[1], {}),
    ]

    for kind, idx, az, extra_tags in layout:
        sn = ec2.create_subnet(
            VpcId=vpc_id,
            CidrBlock=str(subs[idx]),
            AvailabilityZone=az,
            TagSpecifications=[{
                "ResourceType": "subnet",
                "Tags": tags(cfg, {
                    "Name": f"{name}-{kind}-{az}",
                    "Tier": kind,
                    **extra_tags,
                }),
            }],
        )["Subnet"]
        sid = sn["SubnetId"]
        if kind == "public":
            public_ids.append(sid)
            ec2.modify_subnet_attribute(SubnetId=sid, MapPublicIpOnLaunch={"Value": True})
        elif kind == "private":
            private_ids.append(sid)
        elif kind == "database":
            db_ids.append(sid)
        else:
            cache_ids.append(sid)

    # Public RT → IGW
    pub_rt = ec2.create_route_table(
        VpcId=vpc_id,
        TagSpecifications=[{"ResourceType": "route-table", "Tags": tags(cfg, {"Name": f"{name}-public"})}],
    )["RouteTable"]["RouteTableId"]
    ec2.create_route(RouteTableId=pub_rt, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw_id)
    for sid in public_ids:
        ec2.associate_route_table(RouteTableId=pub_rt, SubnetId=sid)

    # EIP + NAT in first public subnet
    eip = ec2.allocate_address(Domain="vpc", TagSpecifications=[{
        "ResourceType": "elastic-ip", "Tags": tags(cfg, {"Name": f"{name}-nat"}),
    }])
    nat = ec2.create_nat_gateway(
        SubnetId=public_ids[0],
        AllocationId=eip["AllocationId"],
        TagSpecifications=[{"ResourceType": "natgateway", "Tags": tags(cfg, {"Name": f"{name}-nat"})}],
    )["NatGateway"]
    nat_id = nat["NatGatewayId"]
    log(f"Waiting for NAT {nat_id}...")
    ec2.get_waiter("nat_gateway_available").wait(NatGatewayIds=[nat_id])

    # Private (+ db/cache) RT → NAT
    priv_rt = ec2.create_route_table(
        VpcId=vpc_id,
        TagSpecifications=[{"ResourceType": "route-table", "Tags": tags(cfg, {"Name": f"{name}-private"})}],
    )["RouteTable"]["RouteTableId"]
    ec2.create_route(RouteTableId=priv_rt, DestinationCidrBlock="0.0.0.0/0", NatGatewayId=nat_id)
    for sid in private_ids + db_ids + cache_ids:
        ec2.associate_route_table(RouteTableId=priv_rt, SubnetId=sid)

    cfg.vpc_id = vpc_id
    cfg.private_subnet_ids = private_ids
    cfg.public_subnet_ids = public_ids
    state.update({
        "vpc_id": vpc_id,
        "private_subnet_ids": private_ids,
        "public_subnet_ids": public_ids,
        "database_subnet_ids": db_ids,
        "elasticache_subnet_ids": cache_ids,
        "igw_id": igw_id,
        "nat_gateway_id": nat_id,
        "eip_allocation_id": eip["AllocationId"],
    })
    state.save()
    log(f"VPC created: {vpc_id}")


def _fill_subnets(cfg: InstallConfig, ec2) -> None:
    from ..discover import _find_subnets
    if not cfg.private_subnet_ids:
        cfg.private_subnet_ids = _find_subnets(cfg, role="internal-elb")
    if not cfg.public_subnet_ids:
        cfg.public_subnet_ids = _find_subnets(cfg, role="elb")


def _default_azs(cfg: InstallConfig) -> list[str]:
    ec2 = client("ec2", cfg)
    azs = ec2.describe_availability_zones(
        Filters=[{"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]}]
    )["AvailabilityZones"]
    names = sorted(a["ZoneName"] for a in azs if a.get("State") == "available")
    return names[:2]


def destroy_vpc(cfg: InstallConfig, state: State) -> None:
    ec2 = client("ec2", cfg)
    vpc_id = state.get("vpc_id") or cfg.vpc_id
    if not vpc_id:
        log("No VPC to destroy")
        return

    # NAT
    nat_id = state.get("nat_gateway_id")
    if nat_id:
        try:
            ec2.delete_nat_gateway(NatGatewayId=nat_id)
            log(f"Deleting NAT {nat_id} (wait)...")
            for _ in range(60):
                time.sleep(5)
                nats = ec2.describe_nat_gateways(NatGatewayIds=[nat_id])["NatGateways"]
                if not nats or nats[0]["State"] == "deleted":
                    break
        except ClientError as e:
            log(f"NAT: {e}")

    eip = state.get("eip_allocation_id")
    if eip:
        try:
            ec2.release_address(AllocationId=eip)
        except ClientError as e:
            log(f"EIP: {e}")

    # Detach/delete IGW
    try:
        for igw in ec2.describe_internet_gateways(
            Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}]
        ).get("InternetGateways") or []:
            igw_id = igw["InternetGatewayId"]
            ec2.detach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
            ec2.delete_internet_gateway(InternetGatewayId=igw_id)
    except ClientError as e:
        log(f"IGW: {e}")

    # Purge available ENIs (Lambda VPC leftovers block subnet delete)
    for _ in range(24):
        enis = ec2.describe_network_interfaces(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        ).get("NetworkInterfaces") or []
        pending = False
        for eni in enis:
            eni_id = eni["NetworkInterfaceId"]
            status = eni.get("Status")
            if status == "in-use":
                pending = True
                continue
            try:
                ec2.delete_network_interface(NetworkInterfaceId=eni_id)
                log(f"Deleted ENI {eni_id}")
            except ClientError as e:
                log(f"ENI {eni_id}: {e}")
                pending = True
        if not pending and not enis:
            break
        if not pending:
            break
        time.sleep(5)

    for sn in ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]).get("Subnets") or []:
        for attempt in range(6):
            try:
                ec2.delete_subnet(SubnetId=sn["SubnetId"])
                log(f"Deleted subnet {sn['SubnetId']}")
                break
            except ClientError as e:
                if attempt == 5:
                    log(f"Subnet: {e}")
                else:
                    time.sleep(10)

    for rt in ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]).get("RouteTables") or []:
        # skip main
        if any(a.get("Main") for a in rt.get("Associations") or []):
            continue
        try:
            for a in rt.get("Associations") or []:
                if a.get("RouteTableAssociationId"):
                    ec2.disassociate_route_table(AssociationId=a["RouteTableAssociationId"])
            ec2.delete_route_table(RouteTableId=rt["RouteTableId"])
        except ClientError as e:
            log(f"RT: {e}")

    # Non-default SGs (Aurora/Redis/ECS leftovers) block VPC delete
    for sg in ec2.describe_security_groups(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
    ).get("SecurityGroups") or []:
        if sg.get("GroupName") == "default":
            continue
        sg_id = sg["GroupId"]
        try:
            if sg.get("IpPermissions"):
                ec2.revoke_security_group_ingress(
                    GroupId=sg_id, IpPermissions=sg["IpPermissions"]
                )
        except ClientError:
            pass
        for attempt in range(6):
            try:
                ec2.delete_security_group(GroupId=sg_id)
                log(f"Deleted SG {sg.get('GroupName')}={sg_id}")
                break
            except ClientError as e:
                if attempt == 5:
                    log(f"SG {sg_id}: {e}")
                else:
                    time.sleep(10)

    try:
        ec2.delete_vpc(VpcId=vpc_id)
        log(f"VPC deleted: {vpc_id}")
    except ClientError as e:
        log(f"VPC delete: {e}")
