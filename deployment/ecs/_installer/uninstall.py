# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Full teardown of installer.py-provisioned infrastructure."""
from __future__ import annotations

import time

from botocore.exceptions import ClientError

from . import chat_agent
from .config import InstallConfig
from .dataplane import destroy_data_plane
from .state import State
from .util import client, fail, log


def uninstall(
    cfg: InstallConfig,
    *,
    yes: bool = False,
    keep_ecr: bool = False,
    keep_state: bool = False,
) -> None:
    """Delete all resources created by installer.py (compute + data plane + leftovers)."""
    if not yes and not cfg.dry_run:
        fail("Pass --yes to confirm uninstall")

    state = State(cfg.default_state_path())
    log(f"Uninstall env={cfg.environment} region={cfg.region} state={state.path}")
    if cfg.dry_run:
        log("[dry-run] no AWS mutations")

    _destroy_chat_agent(cfg, state)
    _destroy_autoscaling(cfg, state)
    _destroy_ecs_services(cfg, state)
    _destroy_api_gateway(cfg, state)
    _destroy_albs_and_tgs(cfg, state)
    _destroy_ecs_cluster(cfg, state)
    _destroy_cloudmap(cfg, state)
    _destroy_log_group(cfg, state)
    # SGs deleted after data plane — Lambda ENIs / ALB ENIs must be gone first
    _destroy_iam_roles(cfg, state)
    _destroy_app_secret(cfg, state)

    if cfg.dry_run:
        log("[dry-run] destroy data plane (Cognito / Redis / Aurora / VPC)")
    else:
        # Ensure dataplane helpers see VPC/IDs from state when config.yaml is sparse
        if not cfg.vpc_id and state.get("vpc_id"):
            cfg.vpc_id = state.get("vpc_id")
        destroy_data_plane(cfg, state)
        # Retry ECS SGs after VPC ENI cleanup inside destroy_vpc
        _destroy_compute_security_groups(cfg, state)
        # Final sweep if VPC still blocked
        _final_vpc_sweep(cfg, state)

    if not keep_ecr:
        _destroy_ecr_repos(cfg)
    else:
        log("Keeping ECR repositories (--keep-ecr)")

    if not keep_state and not cfg.dry_run:
        if state.path.exists():
            state.path.unlink()
            log(f"Removed state file {state.path}")
    else:
        log("Keeping state file")

    log("Uninstall complete.")


def _safe(label: str, fn) -> None:
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        log(f"{label}: {e}")


def _destroy_chat_agent(cfg: InstallConfig, state: State) -> None:
    log("── chat-agent stack")
    if cfg.dry_run:
        log("[dry-run] destroy chat-agent")
        return
    chat_agent.destroy_chat_agent(cfg, state)


def _destroy_autoscaling(cfg: InstallConfig, state: State) -> None:
    log("── application autoscaling")
    if cfg.dry_run:
        return
    aa = client("application-autoscaling", cfg)
    cluster = state.get("cluster_name") or cfg.cluster_name
    resource_id = f"service/{cluster}/{cfg.name_prefix}-gateway-proxy"
    policy = f"{cfg.name_prefix}-gateway-cpu"

    def _run() -> None:
        try:
            aa.delete_scaling_policy(
                PolicyName=policy,
                ServiceNamespace="ecs",
                ResourceId=resource_id,
                ScalableDimension="ecs:service:DesiredCount",
            )
            log(f"Deleted scaling policy {policy}")
        except ClientError as e:
            if e.response["Error"]["Code"] not in ("ObjectNotFoundException", "ValidationException"):
                raise
        try:
            aa.deregister_scalable_target(
                ServiceNamespace="ecs",
                ResourceId=resource_id,
                ScalableDimension="ecs:service:DesiredCount",
            )
            log(f"Deregistered scalable target {resource_id}")
        except ClientError as e:
            if e.response["Error"]["Code"] not in ("ObjectNotFoundException", "ValidationException"):
                raise

    _safe("Autoscaling", _run)


def _destroy_ecs_services(cfg: InstallConfig, state: State) -> None:
    log("── ECS services")
    ecs = client("ecs", cfg)
    cluster = state.get("cluster_name") or cfg.cluster_name
    prefix = cfg.name_prefix
    for suffix in (
        "gateway-proxy",
        "admin-api",
        "admin-ui",
        "scheduler",
        "cost-recorder",
        "notification-worker",
    ):
        name = f"{prefix}-{suffix}"
        if cfg.dry_run:
            log(f"[dry-run] delete service {name}")
            continue

        def _del(svc: str = name) -> None:
            try:
                ecs.update_service(cluster=cluster, service=svc, desiredCount=0)
            except ClientError:
                pass
            ecs.delete_service(cluster=cluster, service=svc, force=True)
            log(f"Deleted service {svc}")

        _safe(f"Service {name}", _del)

    if cfg.dry_run or not cluster:
        return
    # Wait for tasks to stop so ENIs / ALB targets release
    log("Waiting for ECS tasks to drain…")
    for _ in range(60):
        try:
            arns = ecs.list_tasks(cluster=cluster).get("taskArns") or []
        except ClientError:
            break
        if not arns:
            break
        time.sleep(5)


def _destroy_api_gateway(cfg: InstallConfig, state: State) -> None:
    log("── API Gateway / VPC Link")
    if cfg.dry_run:
        return
    apigwv2 = client("apigatewayv2", cfg)
    api_id = state.get("api_gateway_id")
    if api_id:
        _safe("API GW", lambda: (apigwv2.delete_api(ApiId=api_id), log("Deleted API Gateway")))

    vpc_link_id = state.get("vpc_link_id")
    if not vpc_link_id:
        return

    def _del_link() -> None:
        apigwv2.delete_vpc_link(VpcLinkId=vpc_link_id)
        log(f"Deleting VPC Link {vpc_link_id}")
        for _ in range(60):
            try:
                links = apigwv2.get_vpc_links().get("Items") or []
                if not any(x.get("VpcLinkId") == vpc_link_id for x in links):
                    break
            except ClientError:
                break
            time.sleep(5)

    _safe("VPC Link", _del_link)


def _destroy_albs_and_tgs(cfg: InstallConfig, state: State) -> None:
    log("── ALBs / target groups")
    if cfg.dry_run:
        return
    elbv2 = client("elbv2", cfg)
    alb_arns = [
        state.get(k)
        for k in ("gateway_alb_arn", "admin_ui_alb_arn", "admin_api_alb_arn")
        if state.get(k)
    ]
    for arn in alb_arns:
        _safe(f"ALB {arn}", lambda a=arn: (elbv2.delete_load_balancer(LoadBalancerArn=a), log(f"Deleted ALB {a}")))

    # Target groups cannot be deleted while attached; wait for ALBs to go away
    if alb_arns:
        log("Waiting for ALBs to finish deleting…")
        for _ in range(90):
            remaining = []
            for arn in alb_arns:
                try:
                    elbv2.describe_load_balancers(LoadBalancerArns=[arn])
                    remaining.append(arn)
                except ClientError:
                    pass
            if not remaining:
                break
            time.sleep(5)
        # Extra settle time — listeners/rules can linger briefly after ALB vanishes
        time.sleep(15)

    for key in ("gateway_tg_arn", "admin_ui_tg_arn", "admin_api_tg_arn"):
        arn = state.get(key)
        if not arn:
            continue

        def _del_tg(a: str = arn, k: str = key) -> None:
            last_err: Exception | None = None
            for attempt in range(12):
                try:
                    elbv2.delete_target_group(TargetGroupArn=a)
                    log(f"Deleted TG {k}")
                    return
                except ClientError as e:
                    last_err = e
                    if e.response["Error"]["Code"] != "ResourceInUse":
                        raise
                    time.sleep(10)
            raise RuntimeError(f"TG still in use after retries: {k}: {last_err}")

        _safe(f"TG {key}", _del_tg)


def _destroy_ecs_cluster(cfg: InstallConfig, state: State) -> None:
    log("── ECS cluster")
    if cfg.dry_run:
        return
    ecs = client("ecs", cfg)
    cluster = state.get("cluster_name") or cfg.cluster_name
    if not cluster:
        return
    _safe(
        "Cluster",
        lambda: (ecs.delete_cluster(cluster=cluster), log(f"Deleted cluster {cluster}")),
    )


def _destroy_cloudmap(cfg: InstallConfig, state: State) -> None:
    log("── Cloud Map")
    if cfg.dry_run:
        return
    sd = client("servicediscovery", cfg)
    svc_arn = state.get("admin_api_service_arn")
    if svc_arn:
        svc_id = svc_arn.rsplit("/", 1)[-1]

        def _del_svc() -> None:
            # Drain instances first
            try:
                for page in sd.get_paginator("list_instances").paginate(ServiceId=svc_id):
                    for inst in page.get("Instances") or []:
                        sd.deregister_instance(ServiceId=svc_id, InstanceId=inst["Id"])
            except ClientError:
                pass
            sd.delete_service(Id=svc_id)
            log(f"Deleted Cloud Map service {svc_id}")

        _safe("Cloud Map service", _del_svc)

    ns_id = state.get("namespace_id")
    if ns_id:
        def _del_ns() -> None:
            op = sd.delete_namespace(Id=ns_id)
            op_id = op.get("OperationId")
            log(f"Deleting Cloud Map namespace {ns_id}")
            if not op_id:
                return
            for _ in range(60):
                status = sd.get_operation(OperationId=op_id)["Operation"]["Status"]
                if status in ("SUCCESS", "FAIL", "TIMEOUT"):
                    break
                time.sleep(2)

        _safe("Cloud Map namespace", _del_ns)


def _destroy_log_group(cfg: InstallConfig, state: State) -> None:
    log("── CloudWatch log group")
    if cfg.dry_run:
        return
    logs = client("logs", cfg)
    name = state.get("log_group_name") or cfg.log_group
    _safe(
        "Log group",
        lambda: (logs.delete_log_group(logGroupName=name), log(f"Deleted log group {name}")),
    )


def _destroy_compute_security_groups(cfg: InstallConfig, state: State) -> None:
    log("── ECS security groups")
    if cfg.dry_run:
        return
    ec2 = client("ec2", cfg)
    for key in ("alb_sg_id", "tasks_sg_id"):
        sg_id = state.get(key)
        if not sg_id:
            continue
        _safe(f"SG {key}", lambda s=sg_id, k=key: _delete_sg(ec2, s, k))


def _delete_sg(ec2, sg_id: str, label: str) -> None:
    # Clear self-references / cross rules that block delete
    try:
        sg = ec2.describe_security_groups(GroupIds=[sg_id])["SecurityGroups"][0]
        if sg.get("IpPermissions"):
            ec2.revoke_security_group_ingress(GroupId=sg_id, IpPermissions=sg["IpPermissions"])
        if sg.get("IpPermissionsEgress"):
            # Keep default egress revoke best-effort; some accounts reject
            try:
                ec2.revoke_security_group_egress(
                    GroupId=sg_id, IpPermissions=sg["IpPermissionsEgress"]
                )
            except ClientError:
                pass
    except ClientError:
        pass
    for attempt in range(12):
        try:
            ec2.delete_security_group(GroupId=sg_id)
            log(f"Deleted SG {label}={sg_id}")
            return
        except ClientError as e:
            if "DependencyViolation" not in str(e) and e.response["Error"]["Code"] != "DependencyViolation":
                raise
            time.sleep(10)
    raise RuntimeError(f"SG still in use: {sg_id}")


def _destroy_iam_roles(cfg: InstallConfig, state: State) -> None:
    log("── IAM roles")
    if cfg.dry_run:
        return
    iam = client("iam", cfg)
    prefix = f"{cfg.project}-{cfg.environment}-ecs"
    names = [
        f"{prefix}-execution",
        f"{prefix}-gateway-proxy",
        f"{prefix}-admin-api",
        f"{prefix}-worker",
    ]
    # Also from state ARNs if present
    for key in (
        "execution_role_arn",
        "gateway_proxy_task_role_arn",
        "admin_api_task_role_arn",
        "worker_task_role_arn",
    ):
        arn = state.get(key) or ""
        if arn and "/" in arn:
            names.append(arn.rsplit("/", 1)[-1])
    for name in sorted(set(names)):
        _safe(f"IAM {name}", lambda n=name: _delete_role(iam, n))


def _delete_role(iam, name: str) -> None:
    try:
        for p in iam.list_role_policies(RoleName=name).get("PolicyNames") or []:
            iam.delete_role_policy(RoleName=name, PolicyName=p)
        for p in iam.list_attached_role_policies(RoleName=name).get("AttachedPolicies") or []:
            iam.detach_role_policy(RoleName=name, PolicyArn=p["PolicyArn"])
        iam.delete_role(RoleName=name)
        log(f"Deleted IAM role {name}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchEntity":
            return
        raise


def _destroy_app_secret(cfg: InstallConfig, state: State) -> None:
    log("── app secret")
    if cfg.dry_run:
        return
    sm = client("secretsmanager", cfg)
    name = cfg.app_secret_name
    arn = state.get("app_secret_arn") or name

    def _del() -> None:
        sm.delete_secret(SecretId=arn, ForceDeleteWithoutRecovery=True)
        log(f"Deleted secret {name}")

    _safe("App secret", _del)


def _destroy_ecr_repos(cfg: InstallConfig) -> None:
    log("── ECR repositories")
    if cfg.dry_run:
        log("[dry-run] delete ECR repos")
        return
    ecr = client("ecr", cfg)
    repos = [
        f"{cfg.project}/gateway-proxy",
        f"{cfg.project}/admin-api",
        f"{cfg.project}/admin-ui",
        f"{cfg.project}/cost-recorder-worker",
        f"{cfg.project}/notification-worker",
        f"{cfg.project}/migration",
        f"{cfg.project}/scheduler",
        f"{cfg.project}/admin-chat-agent",
    ]
    for name in repos:
        def _del(repo: str = name) -> None:
            ecr.delete_repository(repositoryName=repo, force=True)
            log(f"Deleted ECR repo {repo}")

        _safe(f"ECR {name}", _del)


def _final_vpc_sweep(cfg: InstallConfig, state: State) -> None:
    """Last-chance cleanup for ENIs / TGs / subnets / VPC left after main destroy."""
    log("── final VPC sweep")
    vpc_id = state.get("vpc_id") or cfg.vpc_id
    if not vpc_id:
        return
    ec2 = client("ec2", cfg)
    elbv2 = client("elbv2", cfg)
    prefix = cfg.name_prefix

    # Orphan target groups by name
    try:
        tgs = elbv2.describe_target_groups().get("TargetGroups") or []
        for tg in tgs:
            name = tg.get("TargetGroupName") or ""
            if name.startswith(prefix):
                try:
                    elbv2.delete_target_group(TargetGroupArn=tg["TargetGroupArn"])
                    log(f"Deleted leftover TG {name}")
                except ClientError as e:
                    log(f"TG {name}: {e}")
    except ClientError as e:
        log(f"TG sweep: {e}")

    # Available ENIs in VPC (Lambda leftovers)
    try:
        enis = ec2.describe_network_interfaces(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        ).get("NetworkInterfaces") or []
        for eni in enis:
            if eni.get("Status") != "available":
                continue
            eni_id = eni["NetworkInterfaceId"]
            try:
                ec2.delete_network_interface(NetworkInterfaceId=eni_id)
                log(f"Deleted leftover ENI {eni_id}")
            except ClientError as e:
                log(f"ENI {eni_id}: {e}")
    except ClientError as e:
        log(f"ENI sweep: {e}")

    time.sleep(5)

    # Retry subnets / non-default SGs / VPC
    for sn in ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]).get("Subnets") or []:
        try:
            ec2.delete_subnet(SubnetId=sn["SubnetId"])
            log(f"Deleted leftover subnet {sn['SubnetId']}")
        except ClientError as e:
            log(f"Subnet {sn['SubnetId']}: {e}")

    for sg in ec2.describe_security_groups(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
    ).get("SecurityGroups") or []:
        if sg.get("GroupName") == "default":
            continue
        try:
            if sg.get("IpPermissions"):
                ec2.revoke_security_group_ingress(
                    GroupId=sg["GroupId"], IpPermissions=sg["IpPermissions"]
                )
        except ClientError:
            pass
        try:
            ec2.delete_security_group(GroupId=sg["GroupId"])
            log(f"Deleted leftover SG {sg.get('GroupName')}")
        except ClientError as e:
            log(f"SG {sg['GroupId']}: {e}")

    try:
        ec2.delete_vpc(VpcId=vpc_id)
        log(f"VPC deleted (sweep): {vpc_id}")
    except ClientError as e:
        log(f"VPC sweep: {e}")
