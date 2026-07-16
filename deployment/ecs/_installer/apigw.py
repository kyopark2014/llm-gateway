# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""API Gateway HTTP API for admin-api REST (non-SSE)."""
from __future__ import annotations

from botocore.exceptions import ClientError

from .config import InstallConfig
from .state import State
from .util import client, log, tags


def ensure_api_gateway(cfg: InstallConfig, state: State) -> dict[str, str]:
    apigw = client("apigatewayv2", cfg)
    api_name = f"{cfg.project}-{cfg.environment}-admin-api"
    listener_arn = state.get("admin_api_listener_arn")
    if not listener_arn and not cfg.dry_run:
        raise RuntimeError("admin_api_listener_arn missing")

    if cfg.dry_run:
        out = {
            "api_gateway_id": "api-dry",
            "api_gateway_endpoint": "https://example.execute-api.ap-northeast-2.amazonaws.com",
            "vpc_link_id": "link-dry",
        }
        state.update(out)
        state.save()
        return out

    # VPC Link (get_vpc_links is NOT pageable in apigatewayv2)
    vpc_link_id = state.get("vpc_link_id")
    if not vpc_link_id:
        try:
            items = apigw.get_vpc_links().get("Items") or []
        except ClientError:
            items = []
        for link in items:
            if link.get("Name") == f"{api_name}-vpc-link":
                vpc_link_id = link["VpcLinkId"]
                log(f"VPC Link reused: {vpc_link_id}")
                break
    if not vpc_link_id:
        link_kwargs = {
            "Name": f"{api_name}-vpc-link",
            "SubnetIds": cfg.private_subnet_ids,
            "Tags": {t["Key"]: t["Value"] for t in tags(cfg)},
        }
        if state.get("tasks_sg_id"):
            link_kwargs["SecurityGroupIds"] = [state.get("tasks_sg_id")]
        link = apigw.create_vpc_link(**link_kwargs)
        vpc_link_id = link["VpcLinkId"]
        log(f"VPC Link created: {vpc_link_id} (waiting AVAILABLE)...")
        import time
        for _ in range(60):
            time.sleep(5)
            st = apigw.get_vpc_link(VpcLinkId=vpc_link_id).get("VpcLinkStatus")
            if st == "AVAILABLE":
                break
            if st in ("FAILED", "DELETING", "DELETED"):
                raise RuntimeError(f"VPC Link failed: {st}")
        else:
            raise RuntimeError("VPC Link did not become AVAILABLE in time")
        log(f"VPC Link AVAILABLE: {vpc_link_id}")

    # HTTP API
    api_id = state.get("api_gateway_id")
    endpoint = state.get("api_gateway_endpoint")
    if not api_id:
        for page in apigw.get_paginator("get_apis").paginate():
            for api in page.get("Items", []):
                if api.get("Name") == api_name:
                    api_id = api["ApiId"]
                    endpoint = api["ApiEndpoint"]
                    log(f"HTTP API reused: {api_name}")
                    break
            if api_id:
                break
    if not api_id:
        api = apigw.create_api(
            Name=api_name,
            ProtocolType="HTTP",
            Description="LLM Gateway admin-api REST (non-SSE). SSE uses ALB directly.",
            CorsConfiguration={
                "AllowOrigins": ["*"],
                "AllowMethods": ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                "AllowHeaders": ["Authorization", "Content-Type", "X-Request-Id"],
                "MaxAge": 300,
            },
            Tags={t["Key"]: t["Value"] for t in tags(cfg)},
        )
        api_id = api["ApiId"]
        endpoint = api["ApiEndpoint"]
        log(f"HTTP API created: {api_name}")

    # Integration
    integration_id = state.get("api_integration_id")
    if not integration_id:
        integrations = apigw.get_integrations(ApiId=api_id).get("Items") or []
        if integrations:
            integration_id = integrations[0]["IntegrationId"]
            log("API integration reused")
        else:
            integ = apigw.create_integration(
                ApiId=api_id,
                IntegrationType="HTTP_PROXY",
                IntegrationMethod="ANY",
                IntegrationUri=listener_arn,
                ConnectionType="VPC_LINK",
                ConnectionId=vpc_link_id,
                PayloadFormatVersion="1.0",
                TimeoutInMillis=29000,
            )
            integration_id = integ["IntegrationId"]
            log("API integration created")

    routes = [
        "POST /v1/auth/exchange",
        "GET /v1/usage/me",
        "ANY /admin/{proxy+}",
        "ANY /cli/{proxy+}",
        "GET /health",
    ]
    existing_routes = {
        r["RouteKey"]: r["RouteId"]
        for r in (apigw.get_routes(ApiId=api_id).get("Items") or [])
    }
    for route_key in routes:
        if route_key in existing_routes:
            continue
        try:
            apigw.create_route(
                ApiId=api_id,
                RouteKey=route_key,
                Target=f"integrations/{integration_id}",
            )
            log(f"Route created: {route_key}")
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConflictException":
                raise

    # Default stage
    try:
        apigw.create_stage(ApiId=api_id, StageName="$default", AutoDeploy=True)
        log("Stage $default created")
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("ConflictException", "BadRequestException"):
            raise

    out = {
        "api_gateway_id": api_id,
        "api_gateway_endpoint": endpoint,
        "vpc_link_id": vpc_link_id,
        "api_integration_id": integration_id,
    }
    state.update(out)
    state.save()
    return out
