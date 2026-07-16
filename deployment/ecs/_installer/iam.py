# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""IAM task roles + Secrets Manager app secret."""
from __future__ import annotations

import json
import secrets as pysecrets

from botocore.exceptions import ClientError

from .config import InstallConfig
from .state import State
from .util import account_id, client, dump_policy, log, tag_dict


ECS_TRUST = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "ecs-tasks.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }],
}


def _ensure_role(iam, name: str, description: str, tags: dict[str, str], dry: bool) -> str:
    if dry:
        log(f"[dry-run] IAM role {name}")
        return f"arn:aws:iam::000000000000:role/{name}"
    try:
        iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=dump_policy(ECS_TRUST),
            Description=description,
            Tags=[{"Key": k, "Value": v} for k, v in tags.items()],
        )
        log(f"IAM role created: {name}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "EntityAlreadyExists":
            log(f"IAM role reused: {name}")
        else:
            raise
    return iam.get_role(RoleName=name)["Role"]["Arn"]


def _put_inline(iam, role: str, policy_name: str, doc: dict, dry: bool) -> None:
    if dry:
        return
    iam.put_role_policy(
        RoleName=role,
        PolicyName=policy_name,
        PolicyDocument=dump_policy(doc),
    )


def ensure_roles(cfg: InstallConfig, state: State) -> dict[str, str]:
    iam = client("iam", cfg)
    acct = "000000000000" if cfg.dry_run else account_id(cfg)
    t = tag_dict(cfg)
    prefix = f"{cfg.project}-{cfg.environment}-ecs"

    exec_name = f"{prefix}-execution"
    gw_name = f"{prefix}-gateway-proxy"
    api_name = f"{prefix}-admin-api"
    worker_name = f"{prefix}-worker"

    exec_arn = _ensure_role(iam, exec_name, "ECS task execution", t, cfg.dry_run)
    gw_arn = _ensure_role(iam, gw_name, "gateway-proxy Bedrock/STS", t, cfg.dry_run)
    api_arn = _ensure_role(iam, api_name, "admin-api Cognito/STS", t, cfg.dry_run)
    worker_arn = _ensure_role(iam, worker_name, "workers/scheduler", t, cfg.dry_run)

    # Execution: ECR + logs + secrets
    _put_inline(iam, exec_name, "ecs-execution", {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "EcrPull",
                "Effect": "Allow",
                "Action": [
                    "ecr:GetAuthorizationToken",
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchGetImage",
                ],
                "Resource": "*",
            },
            {
                "Sid": "Logs",
                "Effect": "Allow",
                "Action": ["logs:CreateLogStream", "logs:PutLogEvents", "logs:CreateLogGroup"],
                "Resource": "*",
            },
            {
                "Sid": "Secrets",
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue"],
                "Resource": [
                    f"arn:aws:secretsmanager:{cfg.region}:{acct}:secret:/{cfg.project}/{cfg.environment}/*",
                    f"arn:aws:secretsmanager:{cfg.region}:{acct}:secret:rds!cluster-*",
                ],
            },
        ],
    }, cfg.dry_run)

    if not cfg.dry_run:
        try:
            iam.attach_role_policy(
                RoleName=exec_name,
                PolicyArn="arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
            )
        except ClientError:
            pass

    # Gateway proxy
    gw_stmts = [
        {
            "Sid": "BedrockInvoke",
            "Effect": "Allow",
            "Action": [
                "bedrock:InvokeModel",
                "bedrock:InvokeModelWithResponseStream",
                "bedrock:CountTokens",
            ],
            "Resource": cfg.bedrock_allowed_model_arns,
        },
        {
            "Sid": "BedrockList",
            "Effect": "Allow",
            "Action": [
                "bedrock:ListFoundationModels",
                "bedrock:GetFoundationModel",
                "bedrock:ListInferenceProfiles",
                "bedrock:GetInferenceProfile",
            ],
            "Resource": "*",
        },
        {
            "Sid": "Mantle",
            "Effect": "Allow",
            "Action": [
                "bedrock-mantle:CreateInference",
                "bedrock-mantle:GetInference",
                "bedrock-mantle:CallWithBearerToken",
            ],
            "Resource": "*",
        },
        {
            "Sid": "AgentCoreGateway",
            "Effect": "Allow",
            "Action": ["bedrock-agentcore:InvokeGateway"],
            "Resource": [f"arn:aws:bedrock-agentcore:us-east-1:{acct}:gateway/*"],
        },
    ]
    assume_resources = [r for r in (cfg.cowork_role_arn, cfg.claude_code_374_role_arn) if r]
    if assume_resources:
        gw_stmts.append({
            "Sid": "AssumeCrossAccount",
            "Effect": "Allow",
            "Action": ["sts:AssumeRole"],
            "Resource": assume_resources,
        })
    _put_inline(iam, gw_name, "bedrock-and-sts", {"Version": "2012-10-17", "Statement": gw_stmts}, cfg.dry_run)

    # Admin API
    api_stmts = [
        {
            "Sid": "Sts",
            "Effect": "Allow",
            "Action": ["sts:GetCallerIdentity"],
            "Resource": "*",
        },
        {
            "Sid": "PriceList",
            "Effect": "Allow",
            "Action": [
                "pricing:GetProducts",
                "pricing:DescribeServices",
                "pricing:GetAttributeValues",
            ],
            "Resource": "*",
        },
    ]
    pool_arn = cfg.cognito_user_pool_arn or (
        f"arn:aws:cognito-idp:{cfg.region}:{acct}:userpool/{cfg.cognito_user_pool_id}"
        if cfg.cognito_user_pool_id else ""
    )
    if pool_arn:
        api_stmts.append({
            "Sid": "Cognito",
            "Effect": "Allow",
            "Action": [
                "cognito-idp:ListGroups",
                "cognito-idp:ListUsersInGroup",
                "cognito-idp:ListUsers",
                "cognito-idp:AdminListGroupsForUser",
                "cognito-idp:AdminGetUser",
            ],
            "Resource": [pool_arn],
        })
    if cfg.agentcore_runtime_arn:
        api_stmts.append({
            "Sid": "AgentCoreRuntime",
            "Effect": "Allow",
            "Action": ["bedrock-agentcore:InvokeAgentRuntime"],
            "Resource": [cfg.agentcore_runtime_arn],
        })
    if cfg.chat_staging_bucket:
        bucket_arn = f"arn:aws:s3:::{cfg.chat_staging_bucket}"
        api_stmts.append({
            "Sid": "ChatStaging",
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"],
            "Resource": [bucket_arn, f"{bucket_arn}/*"],
        })
    _put_inline(iam, api_name, "admin-api", {"Version": "2012-10-17", "Statement": api_stmts}, cfg.dry_run)

    roles = {
        "execution_role_arn": exec_arn,
        "gateway_proxy_task_role_arn": gw_arn,
        "admin_api_task_role_arn": api_arn,
        "worker_task_role_arn": worker_arn,
    }
    state.update(roles)
    state.save()
    return roles


def ensure_app_secret(cfg: InstallConfig, state: State) -> str:
    sm = client("secretsmanager", cfg)
    name = cfg.app_secret_name
    if cfg.dry_run:
        log(f"[dry-run] Secrets Manager {name}")
        arn = f"arn:aws:secretsmanager:{cfg.region}:000000000000:secret:{name}"
        state.set("app_secret_arn", arn)
        state.save()
        return arn

    try:
        arn = sm.describe_secret(SecretId=name)["ARN"]
        log(f"App secret reused: {name}")
        # Ensure keys exist if empty
        try:
            val = sm.get_secret_value(SecretId=name)
            body = json.loads(val.get("SecretString") or "{}")
        except ClientError:
            body = {}
        if not body.get("virtual_key_encryption_key"):
            body["virtual_key_encryption_key"] = pysecrets.token_hex(32)
            body.setdefault("nextauth_secret", pysecrets.token_hex(32))
            body.setdefault("jwt_jwks_cache_key", pysecrets.token_hex(32))
            sm.put_secret_value(SecretId=name, SecretString=json.dumps(body))
            log("App secret keys seeded")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        body = {
            "virtual_key_encryption_key": pysecrets.token_hex(32),
            "nextauth_secret": pysecrets.token_hex(32),
            "jwt_jwks_cache_key": pysecrets.token_hex(32),
        }
        arn = sm.create_secret(
            Name=name,
            Description="LLM Gateway app secrets (ECS installer)",
            SecretString=json.dumps(body),
            Tags=[{"Key": k, "Value": v} for k, v in tag_dict(cfg).items()],
        )["ARN"]
        log(f"App secret created: {name}")

    state.set("app_secret_arn", arn)
    state.save()
    return arn


def ensure_ecr_repositories(cfg: InstallConfig, state: State) -> None:
    """Create ECR repos used by ECS services (idempotent)."""
    if cfg.dry_run:
        log("[dry-run] ECR repositories")
        return
    ecr = client("ecr", cfg)
    repos = [
        f"{cfg.project}/gateway-proxy",
        f"{cfg.project}/admin-api",
        f"{cfg.project}/admin-ui",
        f"{cfg.project}/cost-recorder-worker",
        f"{cfg.project}/notification-worker",
        f"{cfg.project}/migration",
        f"{cfg.project}/scheduler",  # often retag of admin-api; keep repo for clarity
    ]
    created = []
    for name in repos:
        try:
            ecr.create_repository(
                repositoryName=name,
                imageScanningConfiguration={"scanOnPush": True},
                imageTagMutability="MUTABLE",
                tags=[{"Key": k, "Value": v} for k, v in tag_dict(cfg).items()],
            )
            created.append(name)
        except ClientError as e:
            if e.response["Error"]["Code"] != "RepositoryAlreadyExistsException":
                raise
    if created:
        log(f"ECR repos created: {', '.join(created)}")
    else:
        log("ECR repos already present")
    if not cfg.ecr_registry:
        cfg.ecr_registry = f"{account_id(cfg)}.dkr.ecr.{cfg.region}.amazonaws.com"
    state.set("ecr_registry", cfg.ecr_registry)
    state.save()
    _check_images_exist(cfg, ecr)


def _check_images_exist(cfg: InstallConfig, ecr) -> None:
    """Warn (do not fail) when configured tags are missing — deploy can still create defs."""
    checks = [
        (f"{cfg.project}/gateway-proxy", cfg.image_tags.gateway_proxy),
        (f"{cfg.project}/admin-api", cfg.image_tags.admin_api),
        (f"{cfg.project}/admin-ui", cfg.image_tags.admin_ui),
        (f"{cfg.project}/cost-recorder-worker", cfg.image_tags.cost_recorder_worker),
        (f"{cfg.project}/notification-worker", cfg.image_tags.notification_worker),
        (f"{cfg.project}/migration", cfg.image_tags.migration),
    ]
    missing = []
    for repo, tag in checks:
        try:
            ecr.describe_images(repositoryName=repo, imageIds=[{"imageTag": tag}])
        except ClientError:
            missing.append(f"{repo}:{tag}")
    if missing:
        log("WARNING: ECR images missing — services will fail to start until pushed:")
        for m in missing:
            log(f"  - {cfg.ecr_registry}/{m}")
        log("Build & push, then: python3 installer.py deploy -c config.yaml")
