# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Application Load Balancers for gateway / admin-ui / admin-api."""
from __future__ import annotations

from botocore.exceptions import ClientError

from .config import InstallConfig
from .state import State
from .util import client, log, tags


def _find_alb(elbv2, name: str):
    try:
        lbs = elbv2.describe_load_balancers(Names=[name])["LoadBalancers"]
        return lbs[0] if lbs else None
    except ClientError as e:
        if e.response["Error"]["Code"] in ("LoadBalancerNotFound", "LoadBalancerNotFoundException"):
            return None
        raise


def _find_tg(elbv2, name: str):
    try:
        tgs = elbv2.describe_target_groups(Names=[name])["TargetGroups"]
        return tgs[0] if tgs else None
    except ClientError as e:
        if e.response["Error"]["Code"] in ("TargetGroupNotFound", "TargetGroupNotFoundException"):
            return None
        raise


def _ensure_alb_stack(
    cfg: InstallConfig,
    *,
    short: str,
    port: int,
    health_path: str,
    idle_timeout: int,
    alb_sg_id: str,
) -> dict[str, str]:
    elbv2 = client("elbv2", cfg)
    alb_name = f"{cfg.name_prefix}-{short}"[:32]
    tg_name = f"{cfg.name_prefix}-{short}"[:32]

    if cfg.dry_run:
        return {
            "alb_arn": f"arn:aws:elasticloadbalancing:{cfg.region}:0:loadbalancer/app/{alb_name}/x",
            "alb_dns": f"{alb_name}.ap-northeast-2.elb.amazonaws.com",
            "tg_arn": f"arn:aws:elasticloadbalancing:{cfg.region}:0:targetgroup/{tg_name}/x",
            "listener_arn": f"arn:aws:elasticloadbalancing:{cfg.region}:0:listener/app/{alb_name}/x/y",
        }

    alb = _find_alb(elbv2, alb_name)
    if alb:
        log(f"ALB reused: {alb_name}")
    else:
        alb = elbv2.create_load_balancer(
            Name=alb_name,
            Subnets=cfg.public_subnet_ids,
            SecurityGroups=[alb_sg_id],
            Scheme="internet-facing",
            Type="application",
            IpAddressType="ipv4",
            Tags=tags(cfg, {"Component": short}),
        )["LoadBalancers"][0]
        log(f"ALB created: {alb_name}")

    elbv2.modify_load_balancer_attributes(
        LoadBalancerArn=alb["LoadBalancerArn"],
        Attributes=[{"Key": "idle_timeout.timeout_seconds", "Value": str(idle_timeout)}],
    )

    tg = _find_tg(elbv2, tg_name)
    if tg:
        log(f"TG reused: {tg_name}")
    else:
        tg = elbv2.create_target_group(
            Name=tg_name,
            Protocol="HTTP",
            Port=port,
            VpcId=cfg.vpc_id,
            TargetType="ip",
            HealthCheckProtocol="HTTP",
            HealthCheckPath=health_path,
            HealthCheckIntervalSeconds=30,
            HealthCheckTimeoutSeconds=5,
            HealthyThresholdCount=2,
            UnhealthyThresholdCount=3,
            Matcher={"HttpCode": "200"},
            Tags=tags(cfg, {"Component": short}),
        )["TargetGroups"][0]
        log(f"TG created: {tg_name}")
        elbv2.modify_target_group_attributes(
            TargetGroupArn=tg["TargetGroupArn"],
            Attributes=[{"Key": "deregistration_delay.timeout_seconds", "Value": "30"}],
        )

    listeners = elbv2.describe_listeners(LoadBalancerArn=alb["LoadBalancerArn"]).get("Listeners") or []
    http = next((l for l in listeners if l.get("Port") == 80), None)
    if http:
        listener_arn = http["ListenerArn"]
        log(f"Listener reused: {alb_name}:80")
    else:
        listener_arn = elbv2.create_listener(
            LoadBalancerArn=alb["LoadBalancerArn"],
            Protocol="HTTP",
            Port=80,
            DefaultActions=[{"Type": "forward", "TargetGroupArn": tg["TargetGroupArn"]}],
        )["Listeners"][0]["ListenerArn"]
        log(f"Listener created: {alb_name}:80")

    return {
        "alb_arn": alb["LoadBalancerArn"],
        "alb_dns": alb["DNSName"],
        "tg_arn": tg["TargetGroupArn"],
        "listener_arn": listener_arn,
    }


def ensure_albs(cfg: InstallConfig, state: State) -> dict[str, str]:
    alb_sg = state.get("alb_sg_id")
    if not alb_sg and not cfg.dry_run:
        raise RuntimeError("alb_sg_id missing — run platform.ensure_security_groups first")

    gw = _ensure_alb_stack(
        cfg, short="gw", port=8000, health_path="/health",
        idle_timeout=cfg.gateway_idle_timeout, alb_sg_id=alb_sg or "sg-dry",
    )
    ui = _ensure_alb_stack(
        cfg, short="ui", port=3000, health_path="/api/health",
        idle_timeout=120, alb_sg_id=alb_sg or "sg-dry",
    )
    api = _ensure_alb_stack(
        cfg, short="api", port=8080, health_path="/health",
        idle_timeout=cfg.admin_api_idle_timeout, alb_sg_id=alb_sg or "sg-dry",
    )

    out = {
        "gateway_alb_arn": gw["alb_arn"],
        "gateway_alb_dns": gw["alb_dns"],
        "gateway_tg_arn": gw["tg_arn"],
        "admin_ui_alb_arn": ui["alb_arn"],
        "admin_ui_alb_dns": ui["alb_dns"],
        "admin_ui_tg_arn": ui["tg_arn"],
        "admin_api_alb_arn": api["alb_arn"],
        "admin_api_alb_dns": api["alb_dns"],
        "admin_api_tg_arn": api["tg_arn"],
        "admin_api_listener_arn": api["listener_arn"],
    }
    state.update(out)
    state.save()
    return out
