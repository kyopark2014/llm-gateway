# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Provision admin-chat-agent (BI Insight) AgentCore Runtime + VPC Lambdas.

Optional path — ECS stack alone leaves AGENTCORE_RUNTIME_ARN empty → BI messages 503.
Idempotent: reuses bucket / roles / runtime / lambdas when present.

Wired as:  python3 installer.py chat-agent -c config.yaml
Also runs from deploy when agentcore.enableChatAgent: true.
"""
from __future__ import annotations

import io
import json
import re
import secrets
import subprocess
import time
import zipfile
from pathlib import Path

from botocore.exceptions import ClientError

from .config import InstallConfig
from .state import State
from .util import account_id, client, fail, log, tag_dict

REPO_ROOT = Path(__file__).resolve().parents[3]
CHAT_AGENT_DIR = REPO_ROOT / "admin-chat-agent"
LAMBDAS_DIR = CHAT_AGENT_DIR / "lambdas"


def provision_chat_agent(cfg: InstallConfig, state: State, *, skip_image_build: bool = False) -> dict:
    """Create BI chat infra and write ARNs into state + cfg fields."""
    if cfg.dry_run:
        log("[dry-run] chat-agent provision skipped")
        return {}

    acct = account_id(cfg)
    prefix = cfg.name_prefix
    bucket = cfg.chat_staging_bucket or f"{prefix}-chat-staging-{acct}"
    image_tag = getattr(cfg.image_tags, "admin_chat_agent", None) or "0.1.1-arm64"
    runtime_name = f"llm_gateway_chat_agent_{cfg.environment}".replace("-", "_")
    role_name = f"{prefix}-chat-agent-execution"
    lambda_role_name = f"{prefix}-chat-agent-lambda"
    ecr_repo = f"{cfg.project}/admin-chat-agent"
    registry = cfg.ecr_registry or f"{acct}.dkr.ecr.{cfg.region}.amazonaws.com"
    container_uri = f"{registry}/{ecr_repo}:{image_tag}"

    query_fn = f"{prefix}-chat-agent-query-db"
    schema_fn = f"{prefix}-chat-agent-get-schema"

    _ensure_bucket(cfg, bucket)
    role_arn = _ensure_runtime_role(cfg, role_name, bucket, acct)
    lambda_role_arn = _ensure_lambda_role(cfg, lambda_role_name, bucket, acct)
    _ensure_ecr_repo(cfg, ecr_repo)

    if not skip_image_build:
        _ensure_image(cfg, registry, ecr_repo, image_tag)
    else:
        _require_image(cfg, ecr_repo, image_tag)

    reader_secret = _ensure_chat_reader_secret(cfg, state)
    _apply_chat_reader_grants(cfg, state, reader_secret)

    _build_and_deploy_lambdas(
        cfg,
        state,
        query_fn=query_fn,
        schema_fn=schema_fn,
        lambda_role_arn=lambda_role_arn,
        bucket=bucket,
        reader_secret_arn=reader_secret,
    )

    runtime_arn = _ensure_agent_runtime(
        cfg,
        name=runtime_name,
        role_arn=role_arn,
        container_uri=container_uri,
        bucket=bucket,
        query_fn=query_fn,
        schema_fn=schema_fn,
    )

    out = {
        "chat_staging_bucket": bucket,
        "agentcore_runtime_arn": runtime_arn,
        "chat_agent_execution_role_arn": role_arn,
        "chat_query_db_function": query_fn,
        "chat_get_schema_function": schema_fn,
        "chat_agent_image": container_uri,
    }
    state.update(out)
    state.save()

    cfg.chat_staging_bucket = bucket
    cfg.agentcore_runtime_arn = runtime_arn
    log(f"chat-agent ready: runtime={runtime_arn}")
    return out


def _ensure_bucket(cfg: InstallConfig, bucket: str) -> None:
    s3 = client("s3", cfg)
    try:
        s3.head_bucket(Bucket=bucket)
        log(f"S3 bucket reused: {bucket}")
    except ClientError:
        kwargs: dict = {"Bucket": bucket}
        if cfg.region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": cfg.region}
        s3.create_bucket(**kwargs)
        s3.put_bucket_encryption(
            Bucket=bucket,
            ServerSideEncryptionConfiguration={
                "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
            },
        )
        s3.put_public_access_block(
            Bucket=bucket,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )
        log(f"S3 bucket created: {bucket}")


def _ensure_runtime_role(cfg: InstallConfig, role_name: str, bucket: str, acct: str) -> str:
    iam = client("iam", cfg)
    arn = f"arn:aws:iam::{acct}:role/{role_name}"
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {
                "StringEquals": {"aws:SourceAccount": acct},
                "ArnLike": {
                    "aws:SourceArn": f"arn:aws:bedrock-agentcore:{cfg.region}:{acct}:*"
                },
            },
        }],
    }
    perms = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "EcrPull",
                "Effect": "Allow",
                "Action": [
                    "ecr:GetAuthorizationToken",
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchCheckLayerAvailability",
                ],
                "Resource": "*",
            },
            {
                "Sid": "Logs",
                "Effect": "Allow",
                "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": "*",
            },
            {
                "Sid": "Bedrock",
                "Effect": "Allow",
                "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                "Resource": "*",
            },
            {
                "Sid": "LambdaInvoke",
                "Effect": "Allow",
                "Action": ["lambda:InvokeFunction"],
                "Resource": [
                    f"arn:aws:lambda:{cfg.region}:{acct}:function:{cfg.name_prefix}-chat-agent-*"
                ],
            },
            {
                "Sid": "S3Staging",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"],
                "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
            },
            {
                "Sid": "CodeInterpreter",
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:CreateCodeInterpreter",
                    "bedrock-agentcore:InvokeCodeInterpreter",
                    "bedrock-agentcore:StopCodeInterpreterSession",
                    "bedrock-agentcore:ListCodeInterpreters",
                    "bedrock-agentcore:GetCodeInterpreter",
                ],
                "Resource": "*",
            },
        ],
    }
    try:
        iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="AgentCore Runtime execution role for admin-chat-agent",
            Tags=[{"Key": k, "Value": v} for k, v in tag_dict(cfg).items()],
        )
        log(f"IAM role created: {role_name}")
        time.sleep(8)
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
        log(f"IAM role reused: {role_name}")
        iam.update_assume_role_policy(RoleName=role_name, PolicyDocument=json.dumps(trust))
    iam.put_role_policy(
        RoleName=role_name, PolicyName="chat-agent-runtime", PolicyDocument=json.dumps(perms)
    )
    return arn


def _ensure_lambda_role(cfg: InstallConfig, role_name: str, bucket: str, acct: str) -> str:
    iam = client("iam", cfg)
    arn = f"arn:aws:iam::{acct}:role/{role_name}"
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }],
    }
    perms = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "ec2:CreateNetworkInterface",
                    "ec2:DescribeNetworkInterfaces",
                    "ec2:DeleteNetworkInterface",
                    "ec2:AssignPrivateIpAddresses",
                    "ec2:UnassignPrivateIpAddresses",
                ],
                "Resource": "*",
            },
            {
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue"],
                "Resource": [
                    cfg.db_secret_arn,
                    f"arn:aws:secretsmanager:{cfg.region}:{acct}:secret:/{cfg.project}/{cfg.environment}/chat-reader*",
                ],
            },
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"],
                "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
            },
        ],
    }
    try:
        iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="VPC Lambda role for chat-agent query_db/get_schema",
            Tags=[{"Key": k, "Value": v} for k, v in tag_dict(cfg).items()],
        )
        log(f"IAM role created: {role_name}")
        time.sleep(5)
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
        log(f"IAM role reused: {role_name}")
    iam.put_role_policy(
        RoleName=role_name, PolicyName="chat-agent-lambda", PolicyDocument=json.dumps(perms)
    )
    return arn


def _ensure_ecr_repo(cfg: InstallConfig, repo: str) -> None:
    ecr = client("ecr", cfg)
    try:
        ecr.create_repository(
            repositoryName=repo,
            imageScanningConfiguration={"scanOnPush": True},
            imageTagMutability="MUTABLE",
            tags=[{"Key": k, "Value": v} for k, v in tag_dict(cfg).items()],
        )
        log(f"ECR repo created: {repo}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "RepositoryAlreadyExistsException":
            raise
        log(f"ECR repo reused: {repo}")


def _require_image(cfg: InstallConfig, repo: str, tag: str) -> None:
    ecr = client("ecr", cfg)
    try:
        ecr.describe_images(repositoryName=repo, imageIds=[{"imageTag": tag}])
    except ClientError:
        fail(
            f"ECR image missing: {repo}:{tag}\n"
            "Build arm64 image and push, then re-run chat-agent."
        )


def _ensure_image(cfg: InstallConfig, registry: str, repo: str, tag: str) -> None:
    ecr = client("ecr", cfg)
    try:
        ecr.describe_images(repositoryName=repo, imageIds=[{"imageTag": tag}])
        log(f"ECR image present: {repo}:{tag}")
        return
    except ClientError:
        pass

    log(f"Building admin-chat-agent image ({tag}, linux/arm64)…")
    uri = f"{registry}/{repo}:{tag}"
    pw = subprocess.check_output(
        ["aws", "ecr", "get-login-password", "--region", cfg.region], text=True
    ).strip()
    subprocess.run(
        ["docker", "login", "--username", "AWS", "--password-stdin", registry],
        input=pw, text=True, check=True, capture_output=True,
    )
    subprocess.run(
        ["docker", "build", "--platform", "linux/arm64", "-t", uri, "."],
        cwd=str(CHAT_AGENT_DIR), check=True,
    )
    subprocess.run(["docker", "push", uri], check=True)
    log(f"Pushed {uri}")


def _ensure_chat_reader_secret(cfg: InstallConfig, state: State) -> str:
    sm = client("secretsmanager", cfg)
    name = f"/{cfg.project}/{cfg.environment}/chat-reader"
    existing = state.get("chat_reader_secret_arn")
    if existing:
        return existing
    password = secrets.token_urlsafe(24)
    try:
        resp = sm.create_secret(
            Name=name,
            SecretString=json.dumps({"username": "gateway_chat_reader", "password": password}),
            Tags=[{"Key": k, "Value": v} for k, v in tag_dict(cfg).items()],
        )
        arn = resp["ARN"]
        log(f"chat-reader secret created: {name}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceExistsException":
            raise
        arn = sm.describe_secret(SecretId=name)["ARN"]
        raw = json.loads(sm.get_secret_value(SecretId=arn)["SecretString"])
        password = raw.get("password") or password
        log(f"chat-reader secret reused: {name}")
    state.set("chat_reader_secret_arn", arn)
    state.set("chat_reader_password", password)
    state.save()
    return arn


def _apply_chat_reader_grants(cfg: InstallConfig, state: State, secret_arn: str) -> None:
    if state.get("chat_reader_grants_applied"):
        log("chat_reader grants already applied")
        return

    password = state.get("chat_reader_password")
    if not password:
        sm = client("secretsmanager", cfg)
        password = json.loads(sm.get_secret_value(SecretId=secret_arn)["SecretString"])["password"]

    pw_sql = password.replace("'", "''")
    sql = f"""
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gateway_chat_reader') THEN
    CREATE ROLE gateway_chat_reader LOGIN PASSWORD '{pw_sql}' NOINHERIT;
  ELSE
    ALTER ROLE gateway_chat_reader WITH LOGIN PASSWORD '{pw_sql}';
  END IF;
END $$;
GRANT CONNECT ON DATABASE gateway TO gateway_chat_reader;
GRANT USAGE ON SCHEMA auth, public, model, budget, usage, chat_agent TO gateway_chat_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA auth TO gateway_chat_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA model TO gateway_chat_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA budget TO gateway_chat_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA usage TO gateway_chat_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA chat_agent TO gateway_chat_reader;
ALTER ROLE gateway_chat_reader SET statement_timeout = '10s';
"""
    _run_psql_via_migration_task(cfg, state, sql)
    state.set("chat_reader_grants_applied", True)
    if "chat_reader_password" in state.data:
        del state.data["chat_reader_password"]
    state.save()
    log("chat_reader role + grants applied")


def _run_psql_via_migration_task(cfg: InstallConfig, state: State, sql: str) -> None:
    ecs = client("ecs", cfg)
    task_def = state.get("migration_task_def")
    if not task_def:
        fail("migration_task_def missing — run deploy first")
    cluster = state.get("cluster_name") or cfg.cluster_name
    sg = state.get("tasks_sg_id")
    cmd = f'''set -e
export PGPASSWORD="$DB_MASTER_PASSWORD"
HOST=$(echo "$DB_MASTER_URL" | sed -E "s|.*@([^:/]+).*|\\1|")
PORT=$(echo "$DB_MASTER_URL" | sed -E "s|.*:([0-9]+)/.*|\\1|")
DB=$(echo "$DB_MASTER_URL" | sed -E "s|.*/([^?]+).*|\\1|")
USER="${{DB_MASTER_USER:-postgres_admin}}"
URL="postgresql://${{USER}}@${{HOST}}:${{PORT}}/${{DB}}?sslmode=require"
psql "$URL" -v ON_ERROR_STOP=1 <<'SQL'
{sql}
SQL
echo CHAT_READER_OK
'''
    resp = ecs.run_task(
        cluster=cluster,
        taskDefinition=task_def,
        launchType="FARGATE",
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": cfg.private_subnet_ids,
                "securityGroups": [sg],
                "assignPublicIp": "DISABLED",
            }
        },
        overrides={"containerOverrides": [{"name": "migration", "command": ["sh", "-c", cmd]}]},
    )
    if resp.get("failures"):
        fail(f"chat_reader grant RunTask failed: {resp['failures']}")
    arn = resp["tasks"][0]["taskArn"]
    log(f"chat_reader grant task: {arn}")
    while True:
        desc = ecs.describe_tasks(cluster=cluster, tasks=[arn])["tasks"][0]
        if desc["lastStatus"] == "STOPPED":
            code = (desc.get("containers") or [{}])[0].get("exitCode", 1)
            if code != 0:
                fail(f"chat_reader grant failed exit={code} {desc.get('stoppedReason')}")
            return
        time.sleep(4)


def _zip_dir(src: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in src.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(src).as_posix())
    return buf.getvalue()


def _build_and_deploy_lambdas(
    cfg: InstallConfig,
    state: State,
    *,
    query_fn: str,
    schema_fn: str,
    lambda_role_arn: str,
    bucket: str,
    reader_secret_arn: str,
) -> None:
    log("Building Lambda packages (manylinux wheels)…")
    subprocess.run(["bash", str(LAMBDAS_DIR / "build-lambdas.sh")], check=True)
    guard = LAMBDAS_DIR / "query_db" / "sql_guard.py"
    if guard.is_file():
        (LAMBDAS_DIR / "build" / "query_db" / "sql_guard.py").write_bytes(guard.read_bytes())

    lam = client("lambda", cfg)
    subnet_ids = cfg.private_subnet_ids[:2]
    sg_ids = [state.get("tasks_sg_id")]
    if not all(sg_ids) or not subnet_ids:
        fail("private subnets / tasks_sg_id required for chat lambdas")

    common_env = {
        "DB_HOST": cfg.db_host,
        "DB_NAME": cfg.db_name,
        "DB_USER": "gateway_chat_reader",
        "DB_SECRET_ARN": reader_secret_arn,
        "S3_STAGING_BUCKET": bucket,
        "SCHEMA_WHITELIST_PATH": "/var/task/schema_whitelist.yaml",
    }
    vpc = {"SubnetIds": subnet_ids, "SecurityGroupIds": sg_ids}

    for name, build_subdir in ((query_fn, "query_db"), (schema_fn, "get_schema")):
        zipped = _zip_dir(LAMBDAS_DIR / "build" / build_subdir)
        if len(zipped) > 50_000_000:
            fail(f"Lambda zip too large: {name} ({len(zipped)} bytes)")
        _upsert_lambda(
            lam, cfg, name=name, role_arn=lambda_role_arn, zipped=zipped, env=common_env, vpc=vpc
        )


def _upsert_lambda(
    lam, cfg: InstallConfig, *, name: str, role_arn: str, zipped: bytes, env: dict, vpc: dict
) -> None:
    try:
        lam.get_function(FunctionName=name)
        lam.update_function_code(FunctionName=name, ZipFile=zipped)
        time.sleep(3)
        for attempt in range(5):
            try:
                lam.update_function_configuration(
                    FunctionName=name,
                    Role=role_arn,
                    Runtime="python3.12",
                    Handler="lambda_function.lambda_handler",
                    Timeout=30,
                    MemorySize=512,
                    Environment={"Variables": env},
                    VpcConfig=vpc,
                )
                break
            except ClientError as e:
                if e.response["Error"]["Code"] != "ResourceConflictException":
                    raise
                time.sleep(5 * (attempt + 1))
        log(f"Lambda updated: {name}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        lam.create_function(
            FunctionName=name,
            Runtime="python3.12",
            Role=role_arn,
            Handler="lambda_function.lambda_handler",
            Code={"ZipFile": zipped},
            Timeout=30,
            MemorySize=512,
            Environment={"Variables": env},
            VpcConfig=vpc,
            Tags=tag_dict(cfg),
        )
        log(f"Lambda created: {name}")


def _ensure_agent_runtime(
    cfg: InstallConfig,
    *,
    name: str,
    role_arn: str,
    container_uri: str,
    bucket: str,
    query_fn: str,
    schema_fn: str,
) -> str:
    ctrl = client("bedrock-agentcore-control", cfg)
    try:
        items = ctrl.list_agent_runtimes().get("agentRuntimes") or []
    except ClientError:
        items = []
    for item in items:
        item_name = item.get("agentRuntimeName") or item.get("name")
        if item_name == name:
            rid = item.get("agentRuntimeId") or item.get("agentRuntimeArn")
            try:
                detail = ctrl.get_agent_runtime(agentRuntimeId=rid)
                arn = detail.get("agentRuntimeArn") or rid
            except ClientError:
                arn = item.get("agentRuntimeArn") or rid
            log(f"AgentCore runtime reused: {arn}")
            return arn

    env = {
        "AWS_REGION": cfg.region,
        "CHAT_STAGING_BUCKET": bucket,
        "LAMBDA_QUERY_DB": query_fn,
        "LAMBDA_GET_SCHEMA": schema_fn,
        "MODEL_OPUS": "global.anthropic.claude-opus-4-7",
        "MODEL_SONNET": "global.anthropic.claude-sonnet-4-6",
        "MODEL_HAIKU": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    }
    log(f"Creating AgentCore runtime {name}…")
    resp = ctrl.create_agent_runtime(
        agentRuntimeName=name,
        agentRuntimeArtifact={"containerConfiguration": {"containerUri": container_uri}},
        roleArn=role_arn,
        networkConfiguration={"networkMode": "PUBLIC"},
        protocolConfiguration={"serverProtocol": "HTTP"},
        environmentVariables=env,
    )
    arn = resp.get("agentRuntimeArn")
    runtime_id = resp.get("agentRuntimeId") or arn
    for _ in range(90):
        try:
            d = ctrl.get_agent_runtime(agentRuntimeId=runtime_id)
        except TypeError:
            d = ctrl.get_agent_runtime(agentRuntimeArn=arn)
        except ClientError as e:
            log(f"  get_agent_runtime: {e}")
            time.sleep(10)
            continue
        status = d.get("status") or d.get("agentRuntimeStatus")
        log(f"  runtime status={status}")
        if status in ("READY", "ACTIVE", "CREATE_COMPLETE"):
            return d.get("agentRuntimeArn") or arn
        if status in ("CREATE_FAILED", "FAILED", "Deleted"):
            fail(f"AgentCore runtime failed: {d}")
        time.sleep(10)
    fail("AgentCore runtime did not become READY in time")


def patch_config_yaml(path: Path, *, runtime_arn: str, bucket: str) -> None:
    text = path.read_text()
    if "runtimeArn:" in text:
        text = re.sub(r"(runtimeArn:\s*).*", rf"\1{runtime_arn}", text, count=1)
    else:
        text = re.sub(r"(agentcore:\s*\n)", rf"\1  runtimeArn: {runtime_arn}\n", text, count=1)
    if "chatStagingBucket:" in text:
        text = re.sub(r"(chatStagingBucket:\s*).*", rf"\1{bucket}", text, count=1)
    else:
        text = re.sub(r"(agentcore:\s*\n)", rf"\1  chatStagingBucket: {bucket}\n", text, count=1)
    if "enableChatAgent:" in text:
        text = re.sub(r"(enableChatAgent:\s*).*", r"\1true", text, count=1)
    else:
        text = re.sub(r"(agentcore:\s*\n)", r"\1  enableChatAgent: true\n", text, count=1)
    path.write_text(text)
    log(f"Updated {path} agentcore fields")


def destroy_chat_agent(cfg: InstallConfig, state: State) -> None:
    """Best-effort teardown of BI chat AgentCore + Lambdas + staging bucket."""
    prefix = cfg.name_prefix
    acct = None
    try:
        acct = account_id(cfg)
    except Exception:  # noqa: BLE001
        pass

    runtime_arn = state.get("agentcore_runtime_arn") or cfg.agentcore_runtime_arn
    if runtime_arn:
        _delete_agent_runtime(cfg, runtime_arn)

    for fn in (
        state.get("chat_query_db_function") or f"{prefix}-chat-agent-query-db",
        state.get("chat_get_schema_function") or f"{prefix}-chat-agent-get-schema",
    ):
        _delete_lambda(cfg, fn)

    bucket = state.get("chat_staging_bucket") or cfg.chat_staging_bucket
    if not bucket and acct:
        bucket = f"{prefix}-chat-staging-{acct}"
    if bucket:
        _empty_and_delete_bucket(cfg, bucket)

    for role in (
        f"{prefix}-chat-agent-execution",
        f"{prefix}-chat-agent-lambda",
    ):
        _delete_iam_role(cfg, role)

    reader = state.get("chat_reader_secret_arn") or f"/{cfg.project}/{cfg.environment}/chat-reader"
    _delete_secret(cfg, reader)

    # ECR for chat-agent is removed with other ECR repos in uninstall


def _delete_agent_runtime(cfg: InstallConfig, runtime_arn: str) -> None:
    ctrl = client("bedrock-agentcore-control", cfg)
    runtime_id = runtime_arn.rsplit("/", 1)[-1]
    try:
        try:
            ctrl.delete_agent_runtime(agentRuntimeId=runtime_id)
        except TypeError:
            ctrl.delete_agent_runtime(agentRuntimeArn=runtime_arn)
        log(f"Deleted AgentCore runtime {runtime_id}")
        for _ in range(60):
            try:
                try:
                    d = ctrl.get_agent_runtime(agentRuntimeId=runtime_id)
                except TypeError:
                    d = ctrl.get_agent_runtime(agentRuntimeArn=runtime_arn)
                status = d.get("status") or d.get("agentRuntimeStatus") or ""
                if status in ("DELETED", "DELETE_COMPLETE", ""):
                    break
                if "NOT" in status.upper() and "FOUND" in status.upper():
                    break
            except ClientError as e:
                if e.response["Error"]["Code"] in (
                    "ResourceNotFoundException",
                    "ValidationException",
                ):
                    break
                log(f"  wait runtime delete: {e}")
            time.sleep(5)
    except ClientError as e:
        log(f"AgentCore runtime: {e}")


def _delete_lambda(cfg: InstallConfig, name: str) -> None:
    lam = client("lambda", cfg)
    try:
        lam.delete_function(FunctionName=name)
        log(f"Deleted Lambda {name}")
        # VPC ENIs linger briefly
        time.sleep(2)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            log(f"Lambda {name}: {e}")


def _empty_and_delete_bucket(cfg: InstallConfig, bucket: str) -> None:
    s3 = client("s3", cfg)
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError:
        return
    try:
        paginator = s3.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=bucket):
            to_delete = []
            for obj in page.get("Versions") or []:
                to_delete.append({"Key": obj["Key"], "VersionId": obj["VersionId"]})
            for obj in page.get("DeleteMarkers") or []:
                to_delete.append({"Key": obj["Key"], "VersionId": obj["VersionId"]})
            if to_delete:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": to_delete})
        # Non-versioned objects
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            keys = [{"Key": o["Key"]} for o in page.get("Contents") or []]
            if keys:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": keys})
        s3.delete_bucket(Bucket=bucket)
        log(f"Deleted S3 bucket {bucket}")
    except ClientError as e:
        log(f"S3 {bucket}: {e}")


def _delete_iam_role(cfg: InstallConfig, name: str) -> None:
    iam = client("iam", cfg)
    try:
        for p in iam.list_role_policies(RoleName=name).get("PolicyNames") or []:
            iam.delete_role_policy(RoleName=name, PolicyName=p)
        for p in iam.list_attached_role_policies(RoleName=name).get("AttachedPolicies") or []:
            iam.detach_role_policy(RoleName=name, PolicyArn=p["PolicyArn"])
        iam.delete_role(RoleName=name)
        log(f"Deleted IAM role {name}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            log(f"IAM {name}: {e}")


def _delete_secret(cfg: InstallConfig, secret_id: str) -> None:
    if not secret_id:
        return
    sm = client("secretsmanager", cfg)
    try:
        sm.delete_secret(SecretId=secret_id, ForceDeleteWithoutRecovery=True)
        log(f"Deleted secret {secret_id}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            log(f"Secret {secret_id}: {e}")
