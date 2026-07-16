# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Load and validate installer YAML config."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ImageTags:
    gateway_proxy: str = "latest"
    admin_api: str = "latest"
    admin_ui: str = "latest"
    cost_recorder_worker: str = "latest"
    notification_worker: str = "latest"
    migration: str = "latest"


@dataclass
class InstallConfig:
    project: str = "llm-gateway"
    environment: str = "dev"
    region: str = "ap-northeast-2"
    vpc_cidr: str = "10.50.0.0/16"
    azs: list[str] = field(default_factory=list)
    aurora_engine_version: str = "16.11"

    # True: create VPC/Aurora/Valkey/Cognito if missing (Terraform 대체)
    provision_data_plane: bool = True

    # Existing / discovered data plane
    vpc_id: str = ""
    private_subnet_ids: list[str] = field(default_factory=list)
    public_subnet_ids: list[str] = field(default_factory=list)

    db_host: str = ""
    db_port: int = 5432
    db_name: str = "gateway"
    db_user: str = "gateway"
    db_secret_arn: str = ""
    db_master_secret_arn: str = ""

    redis_host: str = ""
    redis_port: int = 6379
    redis_tls: bool = True
    redis_auth_secret_arn: str = ""
    # ElastiCache Terraform auth token is often a raw string — leave key empty.
    # If secret is JSON {"password":"..."}, set to "password".
    redis_auth_secret_key: str = ""

    cognito_issuer_url: str = ""
    cognito_user_pool_id: str = ""
    cognito_user_pool_arn: str = ""
    cognito_domain_suffix: str = ""
    cognito_callback_urls: list[str] = field(default_factory=list)
    cognito_logout_urls: list[str] = field(default_factory=list)
    cognito_groups: list[str] = field(default_factory=lambda: ["ClaudeAdmin"])

    ecr_registry: str = ""
    image_tags: ImageTags = field(default_factory=ImageTags)

    gateway_replicas: int = 1
    gateway_autoscaling_max: int = 3
    gateway_workers: int = 2
    gateway_idle_timeout: int = 600
    admin_api_idle_timeout: int = 600

    allowed_iam_roles: list[str] = field(default_factory=list)
    allowed_sts_regions: list[str] = field(default_factory=lambda: ["ap-northeast-2"])
    bedrock_allowed_model_arns: list[str] = field(default_factory=list)

    cowork_role_arn: str = ""
    claude_code_374_role_arn: str = ""

    admin_bootstrap_emails: list[str] = field(default_factory=list)
    admin_bootstrap_groups: list[str] = field(default_factory=lambda: ["ClaudeAdmin"])
    dev_login_enabled: bool = False

    agentcore_gateway_url: str = ""
    agentcore_runtime_arn: str = ""
    chat_staging_bucket: str = ""

    state_file: str = ""
    dry_run: bool = False
    # True: deploy/discover 시 비어 있는 infrastructure 필드를 boto3로 채움
    auto_discover: bool = True

    @property
    def name_prefix(self) -> str:
        return f"{self.project}-{self.environment}"

    @property
    def cluster_name(self) -> str:
        return f"{self.name_prefix}-ecs"

    @property
    def discovery_namespace(self) -> str:
        return f"{self.project}.local"

    @property
    def app_secret_name(self) -> str:
        return f"/{self.project}/{self.environment}/app"

    @property
    def log_group(self) -> str:
        return f"/ecs/{self.cluster_name}"

    def default_state_path(self) -> Path:
        if self.state_file:
            return Path(self.state_file)
        return Path(__file__).resolve().parent.parent / f".state-{self.environment}.json"

    def validate(self) -> list[str]:
        errors: list[str] = []
        required = {
            "vpc_id": self.vpc_id,
            "db_host": self.db_host,
            "db_secret_arn": self.db_secret_arn,
            "redis_host": self.redis_host,
            "redis_auth_secret_arn": self.redis_auth_secret_arn,
            "ecr_registry": self.ecr_registry,
            "cognito_issuer_url": self.cognito_issuer_url,
            "cognito_user_pool_id": self.cognito_user_pool_id,
        }
        for k, v in required.items():
            if not v:
                errors.append(f"missing required config: {k}")
        if len(self.private_subnet_ids) < 1:
            errors.append("private_subnet_ids must have at least 1 subnet")
        if len(self.public_subnet_ids) < 2:
            errors.append("public_subnet_ids must have at least 2 subnets (ALB)")
        return errors


def _tags(raw: dict[str, Any]) -> ImageTags:
    return ImageTags(
        gateway_proxy=str(raw.get("gatewayProxy") or raw.get("gateway_proxy") or "latest"),
        admin_api=str(raw.get("adminApi") or raw.get("admin_api") or "latest"),
        admin_ui=str(raw.get("adminUi") or raw.get("admin_ui") or "latest"),
        cost_recorder_worker=str(
            raw.get("costRecorderWorker") or raw.get("cost_recorder_worker") or "latest"
        ),
        notification_worker=str(
            raw.get("notificationWorker") or raw.get("notification_worker") or "latest"
        ),
        migration=str(raw.get("migration") or "latest"),
    )


def load_config(path: str | Path, *, dry_run: bool = False) -> InstallConfig:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    infra = data.get("infrastructure") or {}
    images = data.get("imageTags") or data.get("image_tags") or {}
    gw = data.get("gatewayProxy") or {}
    aws = data.get("aws") or {}
    agent = data.get("agentcore") or {}
    bootstrap = data.get("adminBootstrap") or {}
    global_ = data.get("global") or {}

    cfg = InstallConfig(
        project=str(data.get("project") or global_.get("project") or "llm-gateway"),
        environment=str(
            data.get("environment") or global_.get("environment") or "dev"
        ),
        region=str(aws.get("region") or data.get("region") or "ap-northeast-2"),
        vpc_cidr=str(infra.get("vpcCidr") or infra.get("vpc_cidr") or data.get("vpcCidr") or "10.50.0.0/16"),
        azs=list(infra.get("azs") or data.get("azs") or aws.get("azs") or []),
        aurora_engine_version=str(
            infra.get("auroraEngineVersion") or infra.get("aurora_engine_version") or "16.11"
        ),
        provision_data_plane=bool(
            data.get("provisionDataPlane", data.get("provision_data_plane", True))
        ),
        vpc_id=str(infra.get("vpcId") or infra.get("vpc_id") or ""),
        private_subnet_ids=list(
            infra.get("privateSubnetIds") or infra.get("private_subnet_ids") or []
        ),
        public_subnet_ids=list(
            infra.get("publicSubnetIds") or infra.get("public_subnet_ids") or []
        ),
        db_host=str(infra.get("dbHost") or infra.get("db_host") or ""),
        db_port=int(infra.get("dbPort") or infra.get("db_port") or 5432),
        db_name=str(infra.get("dbName") or infra.get("db_name") or "gateway"),
        db_user=str(infra.get("dbUser") or infra.get("db_user") or "gateway"),
        db_secret_arn=str(infra.get("dbSecretArn") or infra.get("db_secret_arn") or ""),
        db_master_secret_arn=str(
            infra.get("dbMasterSecretArn") or infra.get("db_master_secret_arn") or ""
        ),
        redis_host=str(infra.get("redisHost") or infra.get("redis_host") or ""),
        redis_port=int(infra.get("redisPort") or infra.get("redis_port") or 6379),
        redis_tls=bool(infra.get("redisTls", infra.get("redis_tls", True))),
        redis_auth_secret_arn=str(
            infra.get("redisAuthSecretArn") or infra.get("redis_auth_secret_arn") or ""
        ),
        redis_auth_secret_key=str(
            infra.get("redisAuthSecretKey") or infra.get("redis_auth_secret_key") or ""
        ),
        cognito_issuer_url=str(
            infra.get("cognitoIssuerUrl") or infra.get("cognito_issuer_url") or ""
        ),
        cognito_user_pool_id=str(
            infra.get("cognitoUserPoolId") or infra.get("cognito_user_pool_id") or ""
        ),
        cognito_user_pool_arn=str(
            infra.get("cognitoUserPoolArn") or infra.get("cognito_user_pool_arn") or ""
        ),
        cognito_domain_suffix=str(
            infra.get("cognitoDomainSuffix") or infra.get("cognito_domain_suffix") or ""
        ),
        cognito_callback_urls=list(
            infra.get("cognitoCallbackUrls") or infra.get("cognito_callback_urls") or []
        ),
        cognito_logout_urls=list(
            infra.get("cognitoLogoutUrls") or infra.get("cognito_logout_urls") or []
        ),
        cognito_groups=list(
            infra.get("cognitoGroups") or infra.get("cognito_groups") or ["ClaudeAdmin"]
        ),
        ecr_registry=str(
            data.get("ecrRegistry")
            or global_.get("imageRegistry")
            or infra.get("ecrRegistry")
            or ""
        ),
        image_tags=_tags(images),
        gateway_replicas=int(gw.get("replicas") or 1),
        gateway_autoscaling_max=int(gw.get("autoscalingMax") or gw.get("autoscaling_max") or 3),
        gateway_workers=int(gw.get("workers") or 2),
        allowed_iam_roles=list(aws.get("allowedIamRoles") or aws.get("allowed_iam_roles") or []),
        allowed_sts_regions=list(
            aws.get("allowedStsRegions") or aws.get("allowed_sts_regions") or ["ap-northeast-2"]
        ),
        bedrock_allowed_model_arns=list(
            aws.get("bedrockAllowedModelArns")
            or aws.get("bedrock_allowed_model_arns")
            or []
        ),
        cowork_role_arn=str(aws.get("coworkRoleArn") or aws.get("cowork_role_arn") or ""),
        claude_code_374_role_arn=str(
            aws.get("claudeCode374RoleArn") or aws.get("claude_code_374_role_arn") or ""
        ),
        admin_bootstrap_emails=list(bootstrap.get("emails") or []),
        admin_bootstrap_groups=list(bootstrap.get("groups") or ["ClaudeAdmin"]),
        dev_login_enabled=bool(
            (data.get("adminApi") or {}).get("devLoginEnabled")
            or (data.get("adminApi") or {}).get("dev_login_enabled")
            or False
        ),
        agentcore_gateway_url=str(agent.get("gatewayUrl") or agent.get("gateway_url") or ""),
        agentcore_runtime_arn=str(agent.get("runtimeArn") or agent.get("runtime_arn") or ""),
        chat_staging_bucket=str(
            agent.get("chatStagingBucket") or agent.get("chat_staging_bucket") or ""
        ),
        state_file=str(data.get("stateFile") or data.get("state_file") or ""),
        dry_run=dry_run,
        auto_discover=bool(data.get("autoDiscover", data.get("auto_discover", True))),
    )

    # Default Bedrock ARNs if omitted
    if not cfg.bedrock_allowed_model_arns:
        cfg.bedrock_allowed_model_arns = [
            "arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-4-*",
            "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-*",
            "arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-4-*",
            "arn:aws:bedrock:*::inference-profile/global.anthropic.claude-*",
            "arn:aws:bedrock:*:*:inference-profile/global.anthropic.claude-*",
        ]
    # Default redis auth key for JSON secret produced by dataplane.redis
    if not cfg.redis_auth_secret_key:
        cfg.redis_auth_secret_key = "auth_token"
    return cfg
