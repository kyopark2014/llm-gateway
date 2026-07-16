# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""ECS cluster, Cloud Map, security groups, log group."""
from __future__ import annotations

from botocore.exceptions import ClientError

from .config import InstallConfig
from .state import State
from .util import client, ecs_tags, log, tags


def ensure_log_group(cfg: InstallConfig, state: State) -> str:
    logs = client("logs", cfg)
    name = cfg.log_group
    if cfg.dry_run:
        log(f"[dry-run] log group {name}")
        state.set("log_group_name", name)
        state.save()
        return name
    try:
        logs.create_log_group(logGroupName=name, tags={t["Key"]: t["Value"] for t in tags(cfg)})
        logs.put_retention_policy(logGroupName=name, retentionInDays=7 if cfg.environment == "dev" else 30)
        log(f"Log group created: {name}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise
        log(f"Log group reused: {name}")
    state.set("log_group_name", name)
    state.save()
    return name


def ensure_cluster(cfg: InstallConfig, state: State) -> str:
    ecs = client("ecs", cfg)
    name = cfg.cluster_name
    if cfg.dry_run:
        log(f"[dry-run] ECS cluster {name}")
        state.set("cluster_name", name)
        state.set("cluster_arn", f"arn:aws:ecs:{cfg.region}:000000000000:cluster/{name}")
        state.save()
        return name

    existing = ecs.describe_clusters(clusters=[name]).get("clusters") or []
    if existing and existing[0].get("status") == "ACTIVE":
        log(f"ECS cluster reused: {name}")
        arn = existing[0]["clusterArn"]
    else:
        resp = ecs.create_cluster(
            clusterName=name,
            capacityProviders=["FARGATE", "FARGATE_SPOT"],
            defaultCapacityProviderStrategy=[{"capacityProvider": "FARGATE", "weight": 1, "base": 1}],
            settings=[{"name": "containerInsights", "value": "enabled"}],
            tags=ecs_tags(cfg),
        )
        arn = resp["cluster"]["clusterArn"]
        log(f"ECS cluster created: {name}")

    state.set("cluster_name", name)
    state.set("cluster_arn", arn)
    state.save()
    return name


def ensure_security_groups(cfg: InstallConfig, state: State) -> dict[str, str]:
    ec2 = client("ec2", cfg)
    if cfg.dry_run:
        fake = {
            "alb_sg_id": "sg-alb",
            "tasks_sg_id": "sg-tasks",
        }
        state.update(fake)
        state.save()
        return fake

    def find_or_create(name: str, description: str) -> str:
        found = ec2.describe_security_groups(
            Filters=[
                {"Name": "group-name", "Values": [name]},
                {"Name": "vpc-id", "Values": [cfg.vpc_id]},
            ]
        )["SecurityGroups"]
        if found:
            log(f"SG reused: {name}")
            return found[0]["GroupId"]
        sg = ec2.create_security_group(
            GroupName=name,
            Description=description,
            VpcId=cfg.vpc_id,
            TagSpecifications=[{
                "ResourceType": "security-group",
                "Tags": tags(cfg, {"Name": name}),
            }],
        )
        log(f"SG created: {name}")
        return sg["GroupId"]

    alb_sg = find_or_create(f"{cfg.name_prefix}-ecs-alb", "Internet-facing ALBs for LLM Gateway ECS")
    tasks_sg = find_or_create(f"{cfg.name_prefix}-ecs-tasks", "ECS Fargate tasks for LLM Gateway")

    # ALB ingress 80
    def allow_ingress(sg_id: str, **kwargs) -> None:
        try:
            ec2.authorize_security_group_ingress(GroupId=sg_id, **kwargs)
        except ClientError as e:
            if e.response["Error"]["Code"] != "InvalidPermission.Duplicate":
                raise

    allow_ingress(
        alb_sg,
        IpPermissions=[{
            "IpProtocol": "tcp",
            "FromPort": 80,
            "ToPort": 80,
            "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "HTTP"}],
        }],
    )

    # Tasks: from ALB all TCP; egress all
    allow_ingress(
        tasks_sg,
        IpPermissions=[{
            "IpProtocol": "tcp",
            "FromPort": 0,
            "ToPort": 65535,
            "UserIdGroupPairs": [{"GroupId": alb_sg, "Description": "from ALB"}],
        }],
    )
    # Tasks: allow Cloud Map / self communication (admin-ui → admin-api)
    allow_ingress(
        tasks_sg,
        IpPermissions=[{
            "IpProtocol": "tcp",
            "FromPort": 0,
            "ToPort": 65535,
            "UserIdGroupPairs": [{"GroupId": tasks_sg, "Description": "tasks self"}],
        }],
    )

    # Ensure egress on tasks (default VPC SG may already have it; create_sg has default egress)
    result = {"alb_sg_id": alb_sg, "tasks_sg_id": tasks_sg}
    state.update(result)
    state.save()
    return result


def ensure_cloudmap(cfg: InstallConfig, state: State) -> dict[str, str]:
    sd = client("servicediscovery", cfg)
    ns_name = cfg.discovery_namespace
    if cfg.dry_run:
        out = {
            "namespace_id": "ns-dry",
            "namespace_name": ns_name,
            "admin_api_service_arn": "arn:aws:servicediscovery:...:service/srv-dry",
        }
        state.update(out)
        state.save()
        return out

    ns_id = state.get("namespace_id")
    if not ns_id:
        # Find existing
        for page in sd.get_paginator("list_namespaces").paginate():
            for ns in page.get("Namespaces", []):
                if ns.get("Name") == ns_name:
                    ns_id = ns["Id"]
                    log(f"Cloud Map namespace reused: {ns_name}")
                    break
            if ns_id:
                break
    if not ns_id:
        op = sd.create_private_dns_namespace(
            Name=ns_name,
            Vpc=cfg.vpc_id,
            Description="LLM Gateway ECS service discovery",
            Tags=tags(cfg),
        )
        # Wait for operation
        op_id = op["OperationId"]
        waiter_ok = False
        for _ in range(60):
            import time
            time.sleep(2)
            status = sd.get_operation(OperationId=op_id)["Operation"]["Status"]
            if status == "SUCCESS":
                ns_id = sd.get_operation(OperationId=op_id)["Operation"]["Targets"]["NAMESPACE"]
                waiter_ok = True
                break
            if status in ("FAIL", "TIMEOUT"):
                raise RuntimeError(f"Cloud Map namespace create failed: {status}")
        if not waiter_ok:
            raise RuntimeError("Cloud Map namespace create timed out")
        log(f"Cloud Map namespace created: {ns_name}")

    # admin-api discovery service
    svc_arn = state.get("admin_api_service_arn")
    if not svc_arn:
        for page in sd.get_paginator("list_services").paginate(
            Filters=[{"Name": "NAMESPACE_ID", "Values": [ns_id], "Condition": "EQ"}]
        ):
            for svc in page.get("Services", []):
                if svc.get("Name") == "admin-api":
                    svc_arn = svc["Arn"]
                    log("Cloud Map service reused: admin-api")
                    break
            if svc_arn:
                break
    if not svc_arn:
        svc = sd.create_service(
            Name="admin-api",
            NamespaceId=ns_id,
            DnsConfig={
                "NamespaceId": ns_id,
                "RoutingPolicy": "MULTIVALUE",
                "DnsRecords": [{"Type": "A", "TTL": 10}],
            },
            HealthCheckCustomConfig={"FailureThreshold": 1},
            Tags=tags(cfg, {"Component": "admin-api"}),
        )
        svc_arn = svc["Service"]["Arn"]
        log("Cloud Map service created: admin-api")

    out = {
        "namespace_id": ns_id,
        "namespace_name": ns_name,
        "admin_api_service_arn": svc_arn,
    }
    state.update(out)
    state.save()
    return out
