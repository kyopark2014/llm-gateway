# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Aurora PostgreSQL Serverless v2 + app secrets."""
from __future__ import annotations

import json
import secrets
import string
import time

from botocore.exceptions import ClientError

from ..config import InstallConfig
from ..state import State
from ..util import client, log, tags


def ensure_aurora(cfg: InstallConfig, state: State) -> None:
    rds = client("rds", cfg)
    ec2 = client("ec2", cfg)
    sm = client("secretsmanager", cfg)
    cluster_id = f"{cfg.project}-{cfg.environment}"

    # Reuse cluster
    try:
        clusters = rds.describe_db_clusters(DBClusterIdentifier=cluster_id).get("DBClusters") or []
    except ClientError:
        clusters = []

    if clusters:
        c = clusters[0]
        cfg.db_host = c["Endpoint"]
        # Prefer proxy if exists
        proxy_host = _proxy_endpoint(rds, cluster_id)
        if proxy_host:
            cfg.db_host = proxy_host
            log(f"Aurora/Proxy reused: {cfg.db_host}")
        else:
            log(f"Aurora reused: {cfg.db_host}")
        master = (c.get("MasterUserSecret") or {}).get("SecretArn") or ""
        if master:
            cfg.db_master_secret_arn = master
        cfg.db_secret_arn = _ensure_db_secret(cfg, sm, state)
        state.update({
            "db_host": cfg.db_host,
            "db_secret_arn": cfg.db_secret_arn,
            "db_master_secret_arn": cfg.db_master_secret_arn,
            "aurora_cluster_id": cluster_id,
        })
        state.save()
        return

    db_subnets = state.get("database_subnet_ids") or cfg.private_subnet_ids
    if len(db_subnets) < 2:
        fail_subnets = state.get("database_subnet_ids")
        if not fail_subnets:
            # discover database tier by Name tag
            sns = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [cfg.vpc_id]}]).get("Subnets") or []
            db_subnets = [
                s["SubnetId"] for s in sns
                if any(t.get("Key") == "Tier" and t.get("Value") == "database" for t in (s.get("Tags") or []))
            ]
        if len(db_subnets) < 2:
            db_subnets = cfg.private_subnet_ids[:2]
    if len(db_subnets) < 2:
        raise RuntimeError("Need ≥2 database/private subnets for Aurora")

    sg_name = f"{cluster_id}-aurora"
    sg_id = _ensure_sg(ec2, cfg, sg_name, "Aurora PostgreSQL", [5432], cfg.private_subnet_ids)

    subnet_group = f"{cluster_id}-aurora"
    try:
        rds.describe_db_subnet_groups(DBSubnetGroupName=subnet_group)
        log(f"DB subnet group reused: {subnet_group}")
    except ClientError:
        rds.create_db_subnet_group(
            DBSubnetGroupName=subnet_group,
            DBSubnetGroupDescription=f"{cluster_id} aurora",
            SubnetIds=db_subnets,
            Tags=[{"Key": t["Key"], "Value": t["Value"]} for t in tags(cfg)],
        )
        log(f"DB subnet group created: {subnet_group}")

    master_user = "postgres_admin"
    log(f"Creating Aurora Serverless v2 cluster {cluster_id}...")
    rds.create_db_cluster(
        DBClusterIdentifier=cluster_id,
        Engine="aurora-postgresql",
        EngineVersion=cfg.aurora_engine_version,
        EngineMode="provisioned",
        DatabaseName=cfg.db_name,
        MasterUsername=master_user,
        ManageMasterUserPassword=True,
        DBSubnetGroupName=subnet_group,
        VpcSecurityGroupIds=[sg_id],
        StorageEncrypted=True,
        BackupRetentionPeriod=7,
        ServerlessV2ScalingConfiguration={"MinCapacity": 0.5, "MaxCapacity": 4.0},
        Tags=[{"Key": t["Key"], "Value": t["Value"]} for t in tags(cfg, {"Name": cluster_id})],
    )
    rds.create_db_instance(
        DBInstanceIdentifier=f"{cluster_id}-instance-1",
        DBClusterIdentifier=cluster_id,
        DBInstanceClass="db.serverless",
        Engine="aurora-postgresql",
        PubliclyAccessible=False,
        Tags=[{"Key": t["Key"], "Value": t["Value"]} for t in tags(cfg)],
    )
    log("Waiting for Aurora available...")
    rds.get_waiter("db_cluster_available").wait(
        DBClusterIdentifier=cluster_id,
        WaiterConfig={"Delay": 30, "MaxAttempts": 60},
    )
    c = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)["DBClusters"][0]
    cfg.db_host = c["Endpoint"]
    cfg.db_master_secret_arn = (c.get("MasterUserSecret") or {}).get("SecretArn") or ""
    cfg.db_secret_arn = _ensure_db_secret(cfg, sm, state, create_password=True)

    # Allow tasks SG later — for now allow private CIDR via SG already
    state.update({
        "db_host": cfg.db_host,
        "db_secret_arn": cfg.db_secret_arn,
        "db_master_secret_arn": cfg.db_master_secret_arn,
        "aurora_cluster_id": cluster_id,
        "aurora_sg_id": sg_id,
    })
    state.save()
    log(f"Aurora ready: {cfg.db_host}")


def _proxy_endpoint(rds, name: str) -> str:
    try:
        for p in rds.describe_db_proxies().get("DBProxies") or []:
            if p.get("DBProxyName") == name:
                return p.get("Endpoint") or ""
    except ClientError:
        pass
    return ""


def _ensure_sg(ec2, cfg: InstallConfig, name: str, desc: str, ports: list[int], _private_subnets: list[str]) -> str:
    found = ec2.describe_security_groups(
        Filters=[
            {"Name": "group-name", "Values": [name]},
            {"Name": "vpc-id", "Values": [cfg.vpc_id]},
        ]
    )["SecurityGroups"]
    if found:
        return found[0]["GroupId"]
    sg = ec2.create_security_group(
        GroupName=name, Description=desc, VpcId=cfg.vpc_id,
        TagSpecifications=[{"ResourceType": "security-group", "Tags": tags(cfg, {"Name": name})}],
    )
    sg_id = sg["GroupId"]
    # Ingress from VPC CIDR on ports
    vpc = ec2.describe_vpcs(VpcIds=[cfg.vpc_id])["Vpcs"][0]
    cidr = vpc["CidrBlock"]
    for port in ports:
        try:
            ec2.authorize_security_group_ingress(
                GroupId=sg_id,
                IpPermissions=[{
                    "IpProtocol": "tcp", "FromPort": port, "ToPort": port,
                    "IpRanges": [{"CidrIp": cidr, "Description": "VPC"}],
                }],
            )
        except ClientError:
            pass
    return sg_id


def _db_urls(cfg: InstallConfig, password: str) -> dict[str, str]:
    """Full connection URLs (apps expect password embedded; no separate DB_PASSWORD merge)."""
    from urllib.parse import quote

    pw = quote(password, safe="")
    host, port, name, user = cfg.db_host, cfg.db_port, cfg.db_name, cfg.db_user
    return {
        "database_url": f"postgresql+asyncpg://{user}:{pw}@{host}:{port}/{name}?ssl=require",
        "db_url": f"postgresql+asyncpg://{user}:{pw}@{host}:{port}/{name}?ssl=require",
        "database_url_sync": f"postgresql://{user}:{pw}@{host}:{port}/{name}?sslmode=require",
    }


def _ensure_db_secret(cfg: InstallConfig, sm, state: State, create_password: bool = False) -> str:
    name = f"/{cfg.project}/{cfg.environment}/db"
    try:
        arn = sm.describe_secret(SecretId=name)["ARN"]
        log(f"DB secret reused: {name}")
        # Refresh URL keys when host is known (password kept)
        if cfg.db_host:
            try:
                body = json.loads(sm.get_secret_value(SecretId=name)["SecretString"] or "{}")
                password = body.get("password") or ""
                if password:
                    body.update(_db_urls(cfg, password))
                    body.setdefault("username", cfg.db_user)
                    sm.put_secret_value(SecretId=name, SecretString=json.dumps(body))
                    log("DB secret URLs refreshed")
            except ClientError as e:
                log(f"DB secret refresh warn: {e}")
        return arn
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
    # Create gateway user password secret (app connects with this after migration grants)
    alphabet = string.ascii_letters + string.digits
    password = "".join(secrets.choice(alphabet) for _ in range(32))
    body = {"password": password, "username": cfg.db_user}
    if cfg.db_host:
        body.update(_db_urls(cfg, password))
    if cfg.db_master_secret_arn:
        try:
            master = json.loads(sm.get_secret_value(SecretId=cfg.db_master_secret_arn)["SecretString"])
            body["master_password"] = master.get("password", "")
        except ClientError:
            pass
    arn = sm.create_secret(
        Name=name,
        Description="LLM Gateway DB app credentials",
        SecretString=json.dumps(body),
        Tags=[{"Key": t["Key"], "Value": t["Value"]} for t in tags(cfg)],
    )["ARN"]
    log(f"DB secret created: {name}")
    return arn


def destroy_aurora(cfg: InstallConfig, state: State) -> None:
    rds = client("rds", cfg)
    ec2 = client("ec2", cfg)
    sm = client("secretsmanager", cfg)
    cluster_id = state.get("aurora_cluster_id") or f"{cfg.project}-{cfg.environment}"
    try:
        instances = rds.describe_db_instances().get("DBInstances") or []
        for inst in instances:
            if inst.get("DBClusterIdentifier") == cluster_id:
                rds.delete_db_instance(
                    DBInstanceIdentifier=inst["DBInstanceIdentifier"],
                    SkipFinalSnapshot=True,
                )
                log(f"Deleting instance {inst['DBInstanceIdentifier']}")
    except ClientError as e:
        log(f"Instances: {e}")
    try:
        rds.delete_db_cluster(
            DBClusterIdentifier=cluster_id,
            SkipFinalSnapshot=True,
        )
        log(f"Deleting cluster {cluster_id} (may take minutes)")
        for _ in range(60):
            time.sleep(15)
            try:
                rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
            except ClientError:
                break
    except ClientError as e:
        log(f"Cluster: {e}")

    subnet_group = f"{cluster_id}-aurora"
    try:
        rds.delete_db_subnet_group(DBSubnetGroupName=subnet_group)
        log(f"Deleted DB subnet group {subnet_group}")
    except ClientError as e:
        log(f"DB subnet group: {e}")

    sg_id = state.get("aurora_sg_id")
    if not sg_id and cfg.vpc_id:
        try:
            found = ec2.describe_security_groups(
                Filters=[
                    {"Name": "group-name", "Values": [f"{cluster_id}-aurora"]},
                    {"Name": "vpc-id", "Values": [cfg.vpc_id or state.get("vpc_id", "")]},
                ]
            )["SecurityGroups"]
            if found:
                sg_id = found[0]["GroupId"]
        except ClientError:
            pass
    if sg_id:
        try:
            ec2.delete_security_group(GroupId=sg_id)
            log(f"Deleted Aurora SG {sg_id}")
        except ClientError as e:
            log(f"Aurora SG: {e}")

    for secret_id in (
        state.get("db_secret_arn"),
        f"/{cfg.project}/{cfg.environment}/db",
    ):
        if not secret_id:
            continue
        try:
            sm.delete_secret(SecretId=secret_id, ForceDeleteWithoutRecovery=True)
            log(f"Deleted DB secret {secret_id}")
            break
        except ClientError as e:
            log(f"DB secret: {e}")
