# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""ElastiCache Valkey (Redis-compatible) + AUTH secret."""
from __future__ import annotations

import json
import secrets
import string
import time
from urllib.parse import quote

from botocore.exceptions import ClientError

from ..config import InstallConfig
from ..state import State
from ..util import client, log, tags


def ensure_redis(cfg: InstallConfig, state: State) -> None:
    cache = client("elasticache", cfg)
    ec2 = client("ec2", cfg)
    sm = client("secretsmanager", cfg)
    rg_id = f"{cfg.project}-{cfg.environment}"

    try:
        groups = cache.describe_replication_groups(ReplicationGroupId=rg_id).get("ReplicationGroups") or []
    except ClientError:
        groups = []

    if groups:
        cfg.redis_host = _endpoint(groups[0])
        cfg.redis_auth_secret_arn = _ensure_auth_secret(cfg, sm, create=False)
        cfg.redis_auth_secret_key = "auth_token"  # JSON secret
        _sync_redis_url_secret(cfg, sm)
        log(f"ElastiCache reused: {cfg.redis_host}")
        state.update({
            "redis_host": cfg.redis_host,
            "redis_auth_secret_arn": cfg.redis_auth_secret_arn,
            "redis_auth_secret_key": cfg.redis_auth_secret_key,
        })
        state.save()
        return

    cache_subnets = state.get("elasticache_subnet_ids") or []
    if len(cache_subnets) < 2:
        sns = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [cfg.vpc_id]}]).get("Subnets") or []
        cache_subnets = [
            s["SubnetId"] for s in sns
            if any(t.get("Key") == "Tier" and t.get("Value") == "elasticache" for t in (s.get("Tags") or []))
        ]
    if len(cache_subnets) < 2:
        cache_subnets = cfg.private_subnet_ids[:2]

    sg_name = f"{rg_id}-elasticache"
    sg_id = _ensure_sg(ec2, cfg, sg_name)

    subnet_group = f"{rg_id}-redis"
    try:
        cache.describe_cache_subnet_groups(CacheSubnetGroupName=subnet_group)
    except ClientError:
        cache.create_cache_subnet_group(
            CacheSubnetGroupName=subnet_group,
            CacheSubnetGroupDescription=f"{rg_id} redis",
            SubnetIds=cache_subnets,
            Tags=[{"Key": t["Key"], "Value": t["Value"]} for t in tags(cfg)],
        )

    auth_arn, auth_token = _ensure_auth_secret(cfg, sm, create=True, return_token=True)
    cfg.redis_auth_secret_arn = auth_arn
    cfg.redis_auth_secret_key = "auth_token"

    log(f"Creating ElastiCache Valkey {rg_id}...")
    cache.create_replication_group(
        ReplicationGroupId=rg_id,
        ReplicationGroupDescription=f"LLM Gateway Valkey {cfg.environment}",
        Engine="valkey",
        CacheNodeType="cache.t4g.small",
        NumCacheClusters=1,
        Port=6379,
        CacheSubnetGroupName=subnet_group,
        SecurityGroupIds=[sg_id],
        AtRestEncryptionEnabled=True,
        TransitEncryptionEnabled=True,
        AuthToken=auth_token,
        AutomaticFailoverEnabled=False,
        MultiAZEnabled=False,
        Tags=[{"Key": t["Key"], "Value": t["Value"]} for t in tags(cfg)],
    )
    log("Waiting for ElastiCache available...")
    for _ in range(60):
        time.sleep(20)
        g = cache.describe_replication_groups(ReplicationGroupId=rg_id)["ReplicationGroups"][0]
        if g.get("Status") == "available":
            cfg.redis_host = _endpoint(g)
            break
    else:
        raise RuntimeError("ElastiCache create timed out")

    _sync_redis_url_secret(cfg, sm, auth_token=auth_token)

    state.update({
        "redis_host": cfg.redis_host,
        "redis_auth_secret_arn": cfg.redis_auth_secret_arn,
        "redis_auth_secret_key": cfg.redis_auth_secret_key,
        "elasticache_sg_id": sg_id,
    })
    state.save()
    log(f"ElastiCache ready: {cfg.redis_host}")


def _endpoint(g: dict) -> str:
    ng = (g.get("NodeGroups") or [{}])[0]
    pe = ng.get("PrimaryEndpoint") or {}
    if pe.get("Address"):
        return pe["Address"]
    pe = g.get("PrimaryEndpoint") or {}
    if pe.get("Address"):
        return pe["Address"]
    cfg_ep = g.get("ConfigurationEndpoint") or {}
    return cfg_ep.get("Address") or ""


def _ensure_sg(ec2, cfg: InstallConfig, name: str) -> str:
    found = ec2.describe_security_groups(
        Filters=[
            {"Name": "group-name", "Values": [name]},
            {"Name": "vpc-id", "Values": [cfg.vpc_id]},
        ]
    )["SecurityGroups"]
    if found:
        return found[0]["GroupId"]
    sg = ec2.create_security_group(
        GroupName=name, Description="ElastiCache Valkey", VpcId=cfg.vpc_id,
        TagSpecifications=[{"ResourceType": "security-group", "Tags": tags(cfg, {"Name": name})}],
    )
    sg_id = sg["GroupId"]
    cidr = ec2.describe_vpcs(VpcIds=[cfg.vpc_id])["Vpcs"][0]["CidrBlock"]
    try:
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[{
                "IpProtocol": "tcp", "FromPort": 6379, "ToPort": 6379,
                "IpRanges": [{"CidrIp": cidr}],
            }],
        )
    except ClientError:
        pass
    return sg_id


def _parse_auth_token(raw: str) -> str:
    """Support legacy raw-string secrets and JSON {\"auth_token\":...}."""
    raw = (raw or "").strip()
    if raw.startswith("{"):
        try:
            body = json.loads(raw)
            return str(body.get("auth_token") or body.get("password") or "")
        except json.JSONDecodeError:
            pass
    return raw


def _redis_url(cfg: InstallConfig, token: str) -> str:
    scheme = "rediss" if cfg.redis_tls else "redis"
    return f"{scheme}://:{quote(token, safe='')}@{cfg.redis_host}:{cfg.redis_port}/0"


def _sync_redis_url_secret(cfg: InstallConfig, sm, auth_token: str | None = None) -> None:
    """Ensure secret is JSON with auth_token + redis_url (apps need password in REDIS_URL)."""
    name = f"/{cfg.project}/{cfg.environment}/redis/auth_token"
    if not cfg.redis_host:
        return
    try:
        raw = sm.get_secret_value(SecretId=name)["SecretString"]
        token = auth_token or _parse_auth_token(raw)
        if not token:
            log("Redis secret has no token — skip URL sync")
            return
        body = {"auth_token": token, "password": token, "redis_url": _redis_url(cfg, token)}
        sm.put_secret_value(SecretId=name, SecretString=json.dumps(body))
        cfg.redis_auth_secret_key = "auth_token"
        log("Redis secret URLs synced")
    except ClientError as e:
        log(f"Redis secret sync warn: {e}")


def _ensure_auth_secret(cfg, sm, create: bool = False, return_token: bool = False):
    name = f"/{cfg.project}/{cfg.environment}/redis/auth_token"
    try:
        arn = sm.describe_secret(SecretId=name)["ARN"]
        if return_token:
            token = _parse_auth_token(sm.get_secret_value(SecretId=name)["SecretString"])
            return arn, token
        return arn
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        if not create:
            raise
    alphabet = string.ascii_letters + string.digits
    token = "".join(secrets.choice(alphabet) for _ in range(64))
    # Store JSON from the start so ECS can mount redis_url / auth_token keys.
    body = {"auth_token": token, "password": token}
    arn = sm.create_secret(
        Name=name,
        Description="ElastiCache AUTH token + redis_url",
        SecretString=json.dumps(body),
        Tags=[{"Key": t["Key"], "Value": t["Value"]} for t in tags(cfg)],
    )["ARN"]
    log(f"Redis AUTH secret created: {name}")
    if return_token:
        return arn, token
    return arn


def destroy_redis(cfg: InstallConfig, state: State) -> None:
    cache = client("elasticache", cfg)
    ec2 = client("ec2", cfg)
    sm = client("secretsmanager", cfg)
    rg_id = f"{cfg.project}-{cfg.environment}"
    try:
        cache.delete_replication_group(ReplicationGroupId=rg_id, RetainPrimaryCluster=False)
        log(f"Deleting ElastiCache {rg_id}")
        for _ in range(60):
            time.sleep(15)
            try:
                cache.describe_replication_groups(ReplicationGroupId=rg_id)
            except ClientError:
                break
    except ClientError as e:
        log(f"ElastiCache: {e}")

    subnet_group = f"{rg_id}-redis"
    try:
        cache.delete_cache_subnet_group(CacheSubnetGroupName=subnet_group)
        log(f"Deleted cache subnet group {subnet_group}")
    except ClientError as e:
        log(f"Cache subnet group: {e}")

    sg_id = state.get("elasticache_sg_id")
    vpc_id = state.get("vpc_id") or cfg.vpc_id
    if not sg_id and vpc_id:
        try:
            found = ec2.describe_security_groups(
                Filters=[
                    {"Name": "group-name", "Values": [f"{rg_id}-elasticache"]},
                    {"Name": "vpc-id", "Values": [vpc_id]},
                ]
            )["SecurityGroups"]
            if found:
                sg_id = found[0]["GroupId"]
        except ClientError:
            pass
    if sg_id:
        try:
            ec2.delete_security_group(GroupId=sg_id)
            log(f"Deleted ElastiCache SG {sg_id}")
        except ClientError as e:
            log(f"ElastiCache SG: {e}")

    for secret_id in (
        state.get("redis_auth_secret_arn"),
        f"/{cfg.project}/{cfg.environment}/redis/auth_token",
    ):
        if not secret_id:
            continue
        try:
            sm.delete_secret(SecretId=secret_id, ForceDeleteWithoutRecovery=True)
            log(f"Deleted Redis secret {secret_id}")
            break
        except ClientError as e:
            log(f"Redis secret: {e}")
