# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""ECS task definitions and services."""
from __future__ import annotations

import json
import time

from botocore.exceptions import ClientError

from .config import InstallConfig
from .state import State
from .util import client, ecs_tags, log, tags


def _secret_ref(arn: str, key: str | None = None) -> str:
    """ECS secrets valueFrom. Plain string secrets omit :key:: suffix."""
    if key:
        return f"{arn}:{key}::"
    return arn


def _common_env(cfg: InstallConfig) -> list[dict[str, str]]:
    """Non-secret env. DB/Redis URLs with credentials come from Secrets Manager."""
    return [
        {"name": "APP_ENV", "value": cfg.environment},
        {"name": "REDIS_CLUSTER_MODE", "value": "false"},
        {"name": "REDIS_TLS_ENABLED", "value": "true" if cfg.redis_tls else "false"},
        {"name": "AWS_REGION", "value": cfg.region},
        {"name": "AWS_DEFAULT_REGION", "value": cfg.region},
        {"name": "JWT_ALGORITHM", "value": "RS256"},
        {"name": "JWT_AUDIENCE", "value": "llm-gateway"},
        {"name": "ALLOWED_STS_REGIONS", "value": ",".join(cfg.allowed_sts_regions)},
        {"name": "ALLOWED_IAM_ROLES", "value": ",".join(cfg.allowed_iam_roles)},
        {"name": "DB_SSL_MODE", "value": "require"},
        {"name": "DB_STATEMENT_CACHE_SIZE", "value": "100"},
    ]


def _common_secrets(cfg: InstallConfig, app_secret_arn: str) -> list[dict[str, str]]:
    """Mount full connection URLs from SM (apps do not merge DB_PASSWORD into URL)."""
    return [
        # admin-api
        {"name": "DATABASE_URL", "valueFrom": _secret_ref(cfg.db_secret_arn, "database_url")},
        # gateway-proxy / workers (pydantic db_url ← DB_URL)
        {"name": "DB_URL", "valueFrom": _secret_ref(cfg.db_secret_arn, "db_url")},
        # redis_url includes AUTH token
        {"name": "REDIS_URL", "valueFrom": _secret_ref(cfg.redis_auth_secret_arn, "redis_url")},
        {
            "name": "VIRTUAL_KEY_ENCRYPTION_KEY",
            "valueFrom": _secret_ref(app_secret_arn, "virtual_key_encryption_key"),
        },
        {
            "name": "JWT_JWKS_CACHE_KEY",
            "valueFrom": _secret_ref(app_secret_arn, "jwt_jwks_cache_key"),
        },
    ]


def _log_config(cfg: InstallConfig, prefix: str) -> dict:
    return {
        "logDriver": "awslogs",
        "options": {
            "awslogs-group": cfg.log_group,
            "awslogs-region": cfg.region,
            "awslogs-stream-prefix": prefix,
        },
    }


def _register_task(
    ecs,
    cfg: InstallConfig,
    *,
    family: str,
    cpu: str,
    memory: str,
    task_role_arn: str,
    execution_role_arn: str,
    container: dict,
) -> str:
    if cfg.dry_run:
        log(f"[dry-run] task def {family}")
        return f"arn:aws:ecs:{cfg.region}:0:task-definition/{family}:1"

    resp = ecs.register_task_definition(
        family=family,
        networkMode="awsvpc",
        requiresCompatibilities=["FARGATE"],
        cpu=cpu,
        memory=memory,
        executionRoleArn=execution_role_arn,
        taskRoleArn=task_role_arn,
        containerDefinitions=[container],
        tags=ecs_tags(cfg),
    )
    arn = resp["taskDefinition"]["taskDefinitionArn"]
    log(f"Task definition: {arn}")
    return arn


def _ensure_service(
    ecs,
    cfg: InstallConfig,
    *,
    service_name: str,
    cluster: str,
    task_def_arn: str,
    desired: int,
    subnet_ids: list[str],
    sg_id: str,
    tg_arn: str | None = None,
    container_name: str | None = None,
    container_port: int | None = None,
    registry_arn: str | None = None,
) -> str:
    if cfg.dry_run:
        log(f"[dry-run] service {service_name}")
        return service_name

    existing = ecs.describe_services(cluster=cluster, services=[service_name]).get("services") or []
    active = [s for s in existing if s.get("status") == "ACTIVE"]
    net = {
        "awsvpcConfiguration": {
            "subnets": subnet_ids,
            "securityGroups": [sg_id],
            "assignPublicIp": "DISABLED",
        }
    }
    lbs = []
    if tg_arn and container_name and container_port:
        lbs = [{
            "targetGroupArn": tg_arn,
            "containerName": container_name,
            "containerPort": container_port,
        }]
    registries = [{"registryArn": registry_arn}] if registry_arn else []

    if active:
        ecs.update_service(
            cluster=cluster,
            service=service_name,
            taskDefinition=task_def_arn,
            desiredCount=desired,
            forceNewDeployment=True,
        )
        log(f"Service updated: {service_name}")
    else:
        kwargs = {
            "cluster": cluster,
            "serviceName": service_name,
            "taskDefinition": task_def_arn,
            "desiredCount": desired,
            "launchType": "FARGATE",
            "networkConfiguration": net,
            "deploymentConfiguration": {
                "minimumHealthyPercent": 100,
                "maximumPercent": 200,
            },
            "tags": ecs_tags(cfg),
        }
        if lbs:
            kwargs["loadBalancers"] = lbs
        if registries:
            kwargs["serviceRegistries"] = registries
        ecs.create_service(**kwargs)
        log(f"Service created: {service_name}")
    return service_name


def ensure_services(cfg: InstallConfig, state: State) -> dict[str, str]:
    ecs = client("ecs", cfg)
    app_secret = state.get("app_secret_arn")
    exec_role = state.get("execution_role_arn")
    gw_role = state.get("gateway_proxy_task_role_arn")
    api_role = state.get("admin_api_task_role_arn")
    worker_role = state.get("worker_task_role_arn")
    cluster = state.get("cluster_name") or cfg.cluster_name
    tasks_sg = state.get("tasks_sg_id")
    ns = state.get("namespace_name") or cfg.discovery_namespace
    admin_api_internal = f"http://admin-api.{ns}:8080"
    nextauth_url = f"http://{state.get('admin_ui_alb_dns', '')}"

    common = _common_env(cfg)
    secrets = _common_secrets(cfg, app_secret)
    reg = cfg.ecr_registry
    tags_img = cfg.image_tags
    prefix = cfg.name_prefix

    # --- gateway-proxy ---
    gw_family = f"{prefix}-gateway-proxy"
    gw_td = _register_task(
        ecs, cfg,
        family=gw_family, cpu="1024", memory="2048",
        task_role_arn=gw_role, execution_role_arn=exec_role,
        container={
            "name": "gateway-proxy",
            "image": f"{reg}/{cfg.project}/gateway-proxy:{tags_img.gateway_proxy}",
            "essential": True,
            "portMappings": [{"containerPort": 8000, "protocol": "tcp"}],
            "environment": common + [
                {"name": "WORKERS", "value": str(cfg.gateway_workers)},
                {"name": "WEB_SEARCH_ENABLED", "value": "true" if cfg.agentcore_gateway_url else "false"},
                {"name": "AGENTCORE_GATEWAY_URL", "value": cfg.agentcore_gateway_url},
                {"name": "AGENTCORE_REGION", "value": "us-east-1"},
                {"name": "AGENTCORE_TARGET_ID", "value": "web-search-tool"},
            ],
            "secrets": secrets,
            "logConfiguration": _log_config(cfg, "gateway-proxy"),
            "healthCheck": {
                "command": ["CMD-SHELL", "curl -f http://localhost:8000/health || exit 1"],
                "interval": 30, "timeout": 5, "retries": 3, "startPeriod": 60,
            },
        },
    )
    _ensure_service(
        ecs, cfg, service_name=f"{prefix}-gateway-proxy", cluster=cluster,
        task_def_arn=gw_td, desired=cfg.gateway_replicas,
        subnet_ids=cfg.private_subnet_ids, sg_id=tasks_sg,
        tg_arn=state.get("gateway_tg_arn"), container_name="gateway-proxy", container_port=8000,
    )

    # Autoscaling
    if not cfg.dry_run and cfg.gateway_autoscaling_max > cfg.gateway_replicas:
        aa = client("application-autoscaling", cfg)
        resource_id = f"service/{cluster}/{prefix}-gateway-proxy"
        try:
            aa.register_scalable_target(
                ServiceNamespace="ecs",
                ResourceId=resource_id,
                ScalableDimension="ecs:service:DesiredCount",
                MinCapacity=cfg.gateway_replicas,
                MaxCapacity=cfg.gateway_autoscaling_max,
            )
            aa.put_scaling_policy(
                PolicyName=f"{prefix}-gateway-cpu",
                ServiceNamespace="ecs",
                ResourceId=resource_id,
                ScalableDimension="ecs:service:DesiredCount",
                PolicyType="TargetTrackingScaling",
                TargetTrackingScalingPolicyConfiguration={
                    "TargetValue": 70.0,
                    "PredefinedMetricSpecification": {
                        "PredefinedMetricType": "ECSServiceAverageCPUUtilization",
                    },
                    "ScaleInCooldown": 300,
                    "ScaleOutCooldown": 60,
                },
            )
            log("Gateway autoscaling configured")
        except ClientError as e:
            log(f"Autoscaling warn: {e}")

    # --- admin-api ---
    api_family = f"{prefix}-admin-api"
    api_td = _register_task(
        ecs, cfg,
        family=api_family, cpu="1024", memory="2048",
        task_role_arn=api_role, execution_role_arn=exec_role,
        container={
            "name": "admin-api",
            "image": f"{reg}/{cfg.project}/admin-api:{tags_img.admin_api}",
            "essential": True,
            "portMappings": [{"containerPort": 8080, "protocol": "tcp"}],
            "environment": common + [
                {"name": "DEV_LOGIN_ENABLED", "value": "true" if cfg.dev_login_enabled else "false"},
                {"name": "COGNITO_USER_POOL_ID", "value": cfg.cognito_user_pool_id},
                {"name": "COGNITO_REGION", "value": cfg.region},
                {"name": "COGNITO_SYNC_DEACTIVATE_MISSING", "value": "true"},
                {"name": "OIDC_ISSUER_URL", "value": cfg.cognito_issuer_url},
                {"name": "OIDC_AUDIENCE", "value": ""},
                {"name": "OIDC_PROVIDER_NAME", "value": "oidc:cognito"},
                {"name": "OIDC_GROUPS_CLAIM", "value": "cognito:groups"},
                {"name": "OIDC_GROUP_PREFIX", "value": "Claude_"},
                {"name": "OIDC_REJECT_UNMATCHED_GROUPS", "value": "true"},
                {"name": "OIDC_VK_TTL_HOURS", "value": "1"},
                {"name": "ADMIN_EMAILS", "value": ",".join(cfg.admin_bootstrap_emails)},
                {"name": "ADMIN_GROUPS", "value": ",".join(cfg.admin_bootstrap_groups)},
                {"name": "AGENTCORE_REGION", "value": cfg.region},
                {"name": "AGENTCORE_RUNTIME_ARN", "value": cfg.agentcore_runtime_arn},
                {"name": "CHAT_STAGING_BUCKET", "value": cfg.chat_staging_bucket},
            ],
            "secrets": secrets,
            "logConfiguration": _log_config(cfg, "admin-api"),
            "healthCheck": {
                "command": ["CMD-SHELL", "curl -f http://localhost:8080/health || exit 1"],
                "interval": 30, "timeout": 5, "retries": 3, "startPeriod": 60,
            },
        },
    )
    _ensure_service(
        ecs, cfg, service_name=f"{prefix}-admin-api", cluster=cluster,
        task_def_arn=api_td, desired=1,
        subnet_ids=cfg.private_subnet_ids, sg_id=tasks_sg,
        tg_arn=state.get("admin_api_tg_arn"), container_name="admin-api", container_port=8080,
        registry_arn=state.get("admin_api_service_arn"),
    )

    # --- admin-ui ---
    ui_family = f"{prefix}-admin-ui"
    ui_td = _register_task(
        ecs, cfg,
        family=ui_family, cpu="512", memory="1024",
        task_role_arn=worker_role, execution_role_arn=exec_role,
        container={
            "name": "admin-ui",
            "image": f"{reg}/{cfg.project}/admin-ui:{tags_img.admin_ui}",
            "essential": True,
            "portMappings": [{"containerPort": 3000, "protocol": "tcp"}],
            "environment": [
                {"name": "APP_ENV", "value": cfg.environment},
                {"name": "ADMIN_API_URL", "value": admin_api_internal},
                {"name": "DEV_LOGIN_ENABLED", "value": "true" if cfg.dev_login_enabled else "false"},
                {"name": "NEXTAUTH_URL", "value": nextauth_url},
            ],
            "secrets": [
                {
                    "name": "NEXTAUTH_SECRET",
                    "valueFrom": _secret_ref(app_secret, "nextauth_secret"),
                },
            ],
            "logConfiguration": _log_config(cfg, "admin-ui"),
            "healthCheck": {
                "command": ["CMD-SHELL", "curl -f http://localhost:3000/api/health || exit 1"],
                "interval": 30, "timeout": 5, "retries": 3, "startPeriod": 90,
            },
        },
    )
    _ensure_service(
        ecs, cfg, service_name=f"{prefix}-admin-ui", cluster=cluster,
        task_def_arn=ui_td, desired=1,
        subnet_ids=cfg.private_subnet_ids, sg_id=tasks_sg,
        tg_arn=state.get("admin_ui_tg_arn"), container_name="admin-ui", container_port=3000,
    )

    # --- scheduler ---
    sch_td = _register_task(
        ecs, cfg,
        family=f"{prefix}-scheduler", cpu="512", memory="1024",
        task_role_arn=api_role, execution_role_arn=exec_role,
        container={
            "name": "scheduler",
            "image": f"{reg}/{cfg.project}/admin-api:{tags_img.admin_api}",
            "essential": True,
            "command": ["python", "-m", "app.scheduler.main"],
            "environment": common + [
                {"name": "COGNITO_USER_POOL_ID", "value": cfg.cognito_user_pool_id},
                {"name": "COGNITO_REGION", "value": cfg.region},
            ],
            "secrets": secrets,
            "logConfiguration": _log_config(cfg, "scheduler"),
        },
    )
    _ensure_service(
        ecs, cfg, service_name=f"{prefix}-scheduler", cluster=cluster,
        task_def_arn=sch_td, desired=1,
        subnet_ids=cfg.private_subnet_ids, sg_id=tasks_sg,
    )

    # --- cost-recorder ---
    cr_td = _register_task(
        ecs, cfg,
        family=f"{prefix}-cost-recorder", cpu="512", memory="1024",
        task_role_arn=worker_role, execution_role_arn=exec_role,
        container={
            "name": "cost-recorder-worker",
            "image": f"{reg}/{cfg.project}/cost-recorder-worker:{tags_img.cost_recorder_worker}",
            "essential": True,
            "environment": common,
            "secrets": secrets,
            "logConfiguration": _log_config(cfg, "cost-recorder"),
        },
    )
    _ensure_service(
        ecs, cfg, service_name=f"{prefix}-cost-recorder", cluster=cluster,
        task_def_arn=cr_td, desired=1,
        subnet_ids=cfg.private_subnet_ids, sg_id=tasks_sg,
    )

    # --- notification-worker ---
    nw_td = _register_task(
        ecs, cfg,
        family=f"{prefix}-notification-worker", cpu="512", memory="1024",
        task_role_arn=worker_role, execution_role_arn=exec_role,
        container={
            "name": "notification-worker",
            "image": f"{reg}/{cfg.project}/notification-worker:{tags_img.notification_worker}",
            "essential": True,
            "environment": common + [{"name": "EMAIL_PROVIDER", "value": "mock"}],
            "secrets": secrets,
            "logConfiguration": _log_config(cfg, "notification-worker"),
        },
    )
    _ensure_service(
        ecs, cfg, service_name=f"{prefix}-notification-worker", cluster=cluster,
        task_def_arn=nw_td, desired=1,
        subnet_ids=cfg.private_subnet_ids, sg_id=tasks_sg,
    )

    # --- migration task definition (entrypoint = run_migration.sh) ---
    master_url = (
        f"postgresql://postgres_admin:PLACEHOLDER@{cfg.db_host}:{cfg.db_port}/"
        f"{cfg.db_name}?sslmode=require"
    )
    mig_env = [
        {"name": "APP_ENV", "value": cfg.environment},
        {"name": "AWS_REGION", "value": cfg.region},
        {"name": "DB_MASTER_URL", "value": master_url},
        {"name": "DB_MASTER_USER", "value": "postgres_admin"},
        {"name": "APP_DB_USER", "value": cfg.db_user},
    ]
    mig_secrets = [
        {
            "name": "APP_DB_PASSWORD",
            "valueFrom": _secret_ref(cfg.db_secret_arn, "password"),
        },
    ]
    if cfg.db_master_secret_arn:
        mig_secrets.append({
            "name": "DB_MASTER_PASSWORD",
            "valueFrom": _secret_ref(cfg.db_master_secret_arn, "password"),
        })
    # Do not override CMD — image default is ./run_migration.sh (init SQL + grants + alembic)
    mig_td = _register_task(
        ecs, cfg,
        family=f"{prefix}-migration", cpu="512", memory="1024",
        task_role_arn=worker_role, execution_role_arn=exec_role,
        container={
            "name": "migration",
            "image": f"{reg}/{cfg.project}/migration:{tags_img.migration}",
            "essential": True,
            "environment": mig_env,
            "secrets": mig_secrets,
            "logConfiguration": _log_config(cfg, "migration"),
        },
    )

    out = {
        "gateway_proxy_task_def": gw_td,
        "admin_api_task_def": api_td,
        "admin_ui_task_def": ui_td,
        "migration_task_def": mig_td,
        "admin_api_internal_url": admin_api_internal,
    }
    state.update(out)
    state.save()
    return out


def run_migration(cfg: InstallConfig, state: State) -> None:
    if cfg.dry_run:
        log("[dry-run] migration RunTask")
        return
    ecs = client("ecs", cfg)
    cluster = state.get("cluster_name")
    task_def = state.get("migration_task_def")
    if not task_def:
        raise RuntimeError("migration_task_def missing — run deploy first")
    tasks_sg = state.get("tasks_sg_id")
    log("Starting migration RunTask...")
    resp = ecs.run_task(
        cluster=cluster,
        taskDefinition=task_def,
        launchType="FARGATE",
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": cfg.private_subnet_ids,
                "securityGroups": [tasks_sg],
                "assignPublicIp": "DISABLED",
            }
        },
    )
    failures = resp.get("failures") or []
    if failures:
        raise RuntimeError(f"migration RunTask failed: {json.dumps(failures)}")
    task_arn = resp["tasks"][0]["taskArn"]
    log(f"Migration task: {task_arn}")
    while True:
        desc = ecs.describe_tasks(cluster=cluster, tasks=[task_arn])["tasks"][0]
        if desc["lastStatus"] == "STOPPED":
            exit_code = (desc.get("containers") or [{}])[0].get("exitCode", 1)
            if exit_code != 0:
                reason = desc.get("stoppedReason", "")
                raise RuntimeError(f"migration failed exit={exit_code} reason={reason}")
            log("Migration succeeded")
            return
        time.sleep(5)


def wait_services(cfg: InstallConfig, state: State) -> None:
    if cfg.dry_run:
        return
    ecs = client("ecs", cfg)
    cluster = state.get("cluster_name")
    prefix = cfg.name_prefix
    for suffix in ("gateway-proxy", "admin-api", "admin-ui"):
        name = f"{prefix}-{suffix}"
        log(f"Waiting for {name}...")
        ok = False
        for _ in range(36):  # ~3 minutes
            time.sleep(5)
            desc = (ecs.describe_services(cluster=cluster, services=[name]).get("services") or [{}])[0]
            running = int(desc.get("runningCount") or 0)
            desired = int(desc.get("desiredCount") or 0)
            if desired > 0 and running >= desired:
                log(f"{name} running ({running}/{desired})")
                ok = True
                break
            for ev in (desc.get("events") or [])[:5]:
                msg = ev.get("message") or ""
                if "CannotPullContainerError" in msg or "ImagePull" in msg:
                    log(f"{name}: image missing — push to ECR then re-run deploy")
                    log(f"  {msg[:220]}")
                    ok = True  # stop waiting; not a hang
                    break
            if ok:
                break
        if not ok:
            log(f"{name} not stable yet — check ECS console / CloudWatch")
