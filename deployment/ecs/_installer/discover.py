# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Discover existing data-plane resources via boto3.

Naming: `{project}-{environment}` VPC/Aurora/ElastiCache/Cognito
(same conventions used by installer dataplane provisioners).
"""
from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError

from .config import InstallConfig
from .util import account_id, client, fail, log


def discover_and_fill(cfg: InstallConfig, *, force: bool = False) -> dict[str, Any]:
    """Fill empty InstallConfig infrastructure fields from AWS.

    If force=True, overwrite even when config already has values.
    Returns a dict of discovered values (for printing / writing YAML).
    """
    if cfg.dry_run:
        log("[dry-run] skip AWS discovery")
        return {}

    found: dict[str, Any] = {}
    acct = account_id(cfg)

    # ECR registry
    if force or not cfg.ecr_registry:
        cfg.ecr_registry = f"{acct}.dkr.ecr.{cfg.region}.amazonaws.com"
        found["ecrRegistry"] = cfg.ecr_registry

    # VPC
    if force or not cfg.vpc_id:
        cfg.vpc_id = _find_vpc(cfg)
        found["vpcId"] = cfg.vpc_id
    if not cfg.vpc_id:
        fail(
            f"VPC not found for {cfg.project}-{cfg.environment}. "
            "Set infrastructure.vpcId or enable provisionDataPlane."
        )

    # Subnets
    if force or not cfg.private_subnet_ids:
        cfg.private_subnet_ids = _find_subnets(cfg, role="internal-elb")
        found["privateSubnetIds"] = cfg.private_subnet_ids
    if force or not cfg.public_subnet_ids:
        cfg.public_subnet_ids = _find_subnets(cfg, role="elb")
        found["publicSubnetIds"] = cfg.public_subnet_ids

    # Aurora / RDS Proxy
    if force or not cfg.db_host:
        cfg.db_host = _find_db_host(cfg)
        found["dbHost"] = cfg.db_host
    if force or not cfg.db_secret_arn:
        cfg.db_secret_arn = _find_secret_arn(cfg, f"/{cfg.project}/{cfg.environment}/db")
        found["dbSecretArn"] = cfg.db_secret_arn
    if force or not cfg.db_master_secret_arn:
        master = _find_aurora_master_secret(cfg)
        if master:
            cfg.db_master_secret_arn = master
            found["dbMasterSecretArn"] = master

    # ElastiCache
    if force or not cfg.redis_host:
        cfg.redis_host = _find_redis_host(cfg)
        found["redisHost"] = cfg.redis_host
    if force or not cfg.redis_auth_secret_arn:
        # TF stores raw AUTH string at this path
        cfg.redis_auth_secret_arn = _find_secret_arn(
            cfg, f"/{cfg.project}/{cfg.environment}/redis/auth_token"
        )
        cfg.redis_auth_secret_key = ""  # raw string
        found["redisAuthSecretArn"] = cfg.redis_auth_secret_arn
        found["redisAuthSecretKey"] = ""

    # Cognito
    if force or not cfg.cognito_user_pool_id:
        pool_id, pool_arn = _find_cognito(cfg)
        cfg.cognito_user_pool_id = pool_id
        cfg.cognito_user_pool_arn = pool_arn
        cfg.cognito_issuer_url = (
            f"https://cognito-idp.{cfg.region}.amazonaws.com/{pool_id}"
        )
        found["cognitoUserPoolId"] = pool_id
        found["cognitoUserPoolArn"] = pool_arn
        found["cognitoIssuerUrl"] = cfg.cognito_issuer_url
    elif force or not cfg.cognito_issuer_url:
        cfg.cognito_issuer_url = (
            f"https://cognito-idp.{cfg.region}.amazonaws.com/{cfg.cognito_user_pool_id}"
        )
        found["cognitoIssuerUrl"] = cfg.cognito_issuer_url

    log(
        "Discovered: "
        + ", ".join(f"{k}={_short(v)}" for k, v in found.items())
    )
    return found


def _short(v: Any) -> str:
    s = str(v)
    return s if len(s) <= 60 else s[:57] + "..."


def _find_vpc(cfg: InstallConfig) -> str:
    ec2 = client("ec2", cfg)
    name = f"{cfg.project}-{cfg.environment}"
    # Prefer Name tag = {project}-{environment}
    resp = ec2.describe_vpcs(
        Filters=[
            {"Name": "tag:Name", "Values": [name]},
            {"Name": "tag:Project", "Values": [cfg.project]},
        ]
    )
    vpcs = resp.get("Vpcs") or []
    if not vpcs:
        resp = ec2.describe_vpcs(
            Filters=[
                {"Name": "tag:Project", "Values": [cfg.project]},
                {"Name": "tag:Environment", "Values": [cfg.environment]},
            ]
        )
        vpcs = resp.get("Vpcs") or []
    if not vpcs:
        return ""
    if len(vpcs) > 1:
        log(f"Multiple VPCs matched; using {vpcs[0]['VpcId']}")
    return vpcs[0]["VpcId"]


def _find_subnets(cfg: InstallConfig, *, role: str) -> list[str]:
    """role: 'elb' (public) or 'internal-elb' (private) — EKS subnet tags."""
    ec2 = client("ec2", cfg)
    tag = f"kubernetes.io/role/{role}"
    resp = ec2.describe_subnets(
        Filters=[
            {"Name": "vpc-id", "Values": [cfg.vpc_id]},
            {"Name": f"tag:{tag}", "Values": ["1"]},
        ]
    )
    ids = sorted(s["SubnetId"] for s in resp.get("Subnets") or [])
    if not ids:
        # Fallback: Name contains private/public
        key = "public" if role == "elb" else "private"
        resp = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [cfg.vpc_id]}])
        ids = sorted(
            s["SubnetId"]
            for s in (resp.get("Subnets") or [])
            if key in _subnet_name(s).lower()
            and "database" not in _subnet_name(s).lower()
            and "elasticache" not in _subnet_name(s).lower()
        )
    if not ids:
        fail(f"No {role} subnets found in VPC {cfg.vpc_id}")
    return ids


def _subnet_name(subnet: dict) -> str:
    for t in subnet.get("Tags") or []:
        if t.get("Key") == "Name":
            return t.get("Value") or ""
    return ""


def _find_db_host(cfg: InstallConfig) -> str:
    # Prefer RDS Proxy (application endpoint when enable_rds_proxy=true)
    rds = client("rds", cfg)
    proxy_name = f"{cfg.project}-{cfg.environment}"
    try:
        proxies = rds.describe_db_proxies(DBProxyName=proxy_name).get("DBProxies") or []
        if proxies and proxies[0].get("Endpoint"):
            log(f"Using RDS Proxy endpoint: {proxies[0]['Endpoint']}")
            return proxies[0]["Endpoint"]
    except ClientError as e:
        if e.response["Error"]["Code"] not in (
            "DBProxyNotFoundFault",
            "DBProxyNotFound",
        ):
            # Some SDKs use InvalidException — try list
            pass

    try:
        all_proxies = rds.describe_db_proxies().get("DBProxies") or []
        for p in all_proxies:
            if p.get("DBProxyName") == proxy_name and p.get("Endpoint"):
                log(f"Using RDS Proxy endpoint: {p['Endpoint']}")
                return p["Endpoint"]
    except ClientError:
        pass

    # Aurora cluster writer
    cluster_id = f"{cfg.project}-{cfg.environment}"
    try:
        clusters = rds.describe_db_clusters(DBClusterIdentifier=cluster_id).get("DBClusters") or []
        if clusters and clusters[0].get("Endpoint"):
            log(f"Using Aurora writer endpoint: {clusters[0]['Endpoint']}")
            return clusters[0]["Endpoint"]
    except ClientError as e:
        if e.response["Error"]["Code"] not in (
            "DBClusterNotFoundFault",
            "DBClusterNotFound",
        ):
            raise

    fail(f"Aurora/RDS Proxy not found: {cluster_id}")
    return ""


def _find_aurora_master_secret(cfg: InstallConfig) -> str:
    rds = client("rds", cfg)
    cluster_id = f"{cfg.project}-{cfg.environment}"
    try:
        clusters = rds.describe_db_clusters(DBClusterIdentifier=cluster_id).get("DBClusters") or []
    except ClientError:
        return ""
    if not clusters:
        return ""
    secret = (clusters[0].get("MasterUserSecret") or {}).get("SecretArn") or ""
    return secret


def _find_redis_host(cfg: InstallConfig) -> str:
    cache = client("elasticache", cfg)
    rg_id = f"{cfg.project}-{cfg.environment}"
    try:
        groups = cache.describe_replication_groups(ReplicationGroupId=rg_id).get(
            "ReplicationGroups"
        ) or []
    except ClientError as e:
        if e.response["Error"]["Code"] in (
            "ReplicationGroupNotFoundFault",
            "ReplicationGroupNotFound",
        ):
            fail(f"ElastiCache replication group not found: {rg_id}")
        raise
    if not groups:
        fail(f"ElastiCache replication group not found: {rg_id}")
    g = groups[0]
    # Non-cluster: PrimaryEndpoint; cluster mode: ConfigurationEndpoint
    endpoint = (g.get("NodeGroups") or [{}])[0].get("PrimaryEndpoint") or {}
    if endpoint.get("Address"):
        return endpoint["Address"]
    cfg_ep = g.get("ConfigurationEndpoint") or {}
    if cfg_ep.get("Address"):
        return cfg_ep["Address"]
    # Fallback older shape
    pe = g.get("PrimaryEndpoint") or {}
    if pe.get("Address"):
        return pe["Address"]
    fail(f"No endpoint on replication group {rg_id}")
    return ""


def _find_secret_arn(cfg: InstallConfig, name: str) -> str:
    sm = client("secretsmanager", cfg)
    try:
        return sm.describe_secret(SecretId=name)["ARN"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            fail(f"Secrets Manager secret not found: {name}")
        raise
    return ""


def _find_cognito(cfg: InstallConfig) -> tuple[str, str]:
    cognito = client("cognito-idp", cfg)
    want = f"{cfg.project}-{cfg.environment}-userpool"
    paginator = cognito.get_paginator("list_user_pools")
    for page in paginator.paginate(MaxResults=60):
        for pool in page.get("UserPools") or []:
            if pool.get("Name") == want:
                pool_id = pool["Id"]
                desc = cognito.describe_user_pool(UserPoolId=pool_id)["UserPool"]
                arn = desc.get("Arn") or (
                    f"arn:aws:cognito-idp:{cfg.region}:{account_id(cfg)}:userpool/{pool_id}"
                )
                return pool_id, arn
    fail(f"Cognito user pool not found: {want}")
    return "", ""


def format_discovered_yaml(found: dict[str, Any]) -> str:
    """Render discovered fields as a YAML infrastructure snippet."""
    lines = ["# Auto-discovered by: python3 installer.py discover", "infrastructure:"]
    order = [
        ("vpcId", "vpcId"),
        ("privateSubnetIds", "privateSubnetIds"),
        ("publicSubnetIds", "publicSubnetIds"),
        ("dbHost", "dbHost"),
        ("dbSecretArn", "dbSecretArn"),
        ("dbMasterSecretArn", "dbMasterSecretArn"),
        ("redisHost", "redisHost"),
        ("redisAuthSecretArn", "redisAuthSecretArn"),
        ("redisAuthSecretKey", "redisAuthSecretKey"),
        ("cognitoIssuerUrl", "cognitoIssuerUrl"),
        ("cognitoUserPoolId", "cognitoUserPoolId"),
        ("cognitoUserPoolArn", "cognitoUserPoolArn"),
    ]
    for key, yaml_key in order:
        if key not in found:
            continue
        val = found[key]
        if isinstance(val, list):
            lines.append(f"  {yaml_key}:")
            for item in val:
                lines.append(f"    - \"{item}\"")
        else:
            lines.append(f"  {yaml_key}: \"{val}\"")
    if "ecrRegistry" in found:
        lines.append("")
        lines.append(f"ecrRegistry: \"{found['ecrRegistry']}\"")
    return "\n".join(lines) + "\n"
