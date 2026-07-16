# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Cognito User Pool + app client + groups."""
from __future__ import annotations

from botocore.exceptions import ClientError

from ..config import InstallConfig
from ..state import State
from ..util import account_id, client, log, tags


def ensure_cognito(cfg: InstallConfig, state: State) -> None:
    cognito = client("cognito-idp", cfg)
    pool_name = f"{cfg.project}-{cfg.environment}-userpool"

    pool_id = None
    for page in cognito.get_paginator("list_user_pools").paginate(MaxResults=60):
        for pool in page.get("UserPools") or []:
            if pool.get("Name") == pool_name:
                pool_id = pool["Id"]
                break
        if pool_id:
            break

    if pool_id:
        log(f"Cognito pool reused: {pool_id}")
    else:
        log(f"Creating Cognito user pool {pool_name}")
        resp = cognito.create_user_pool(
            PoolName=pool_name,
            AutoVerifiedAttributes=["email"],
            UsernameAttributes=["email"],
            Policies={
                "PasswordPolicy": {
                    "MinimumLength": 12,
                    "RequireUppercase": True,
                    "RequireLowercase": True,
                    "RequireNumbers": True,
                    "RequireSymbols": False,
                }
            },
            Schema=[
                {"Name": "email", "Required": True, "Mutable": True, "AttributeDataType": "String"},
                {"Name": "name", "Required": False, "Mutable": True, "AttributeDataType": "String"},
            ],
            UserPoolTags={t["Key"]: t["Value"] for t in tags(cfg, {"Name": pool_name})},
        )
        pool_id = resp["UserPool"]["Id"]

    cfg.cognito_user_pool_id = pool_id
    cfg.cognito_user_pool_arn = (
        f"arn:aws:cognito-idp:{cfg.region}:{account_id(cfg)}:userpool/{pool_id}"
    )
    cfg.cognito_issuer_url = (
        f"https://cognito-idp.{cfg.region}.amazonaws.com/{pool_id}"
    )

    # Domain (Hosted UI /oauth2/*). Without a domain, OIDC discovery still
    # advertises cognito-idp…/authorize, which returns BadRequest in the browser.
    # Prefer the pool's existing Domain (one prefix domain per pool).
    # describe_user_pool_domain returns empty DomainDescription (HTTP 200) when
    # the prefix is unused — do not treat that as "already exists".
    pool_meta = cognito.describe_user_pool(UserPoolId=pool_id).get("UserPool") or {}
    domain = (pool_meta.get("Domain") or "").strip()
    if not domain:
        suffix = cfg.cognito_domain_suffix or f"auth-{account_id(cfg)}"
        # Keep prefix short: long Hosted UI FQDNs break getaddrinfo on some macOS resolvers.
        domain = f"{cfg.project[:12]}-{cfg.environment}-{suffix}"[:63].rstrip("-")

    desc: dict = {}
    try:
        desc = cognito.describe_user_pool_domain(Domain=domain).get("DomainDescription") or {}
    except ClientError:
        desc = {}

    if desc.get("Domain") and desc.get("Status") in ("ACTIVE", "CREATING", "UPDATING"):
        log(f"Cognito domain reused: {domain} ({desc.get('Status')})")
    else:
        try:
            cognito.create_user_pool_domain(Domain=domain, UserPoolId=pool_id)
            log(f"Cognito domain created: {domain}")
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "InvalidParameterException" and "already associated" in str(e).lower():
                log(f"Cognito domain already associated: {domain}")
            else:
                log(f"Cognito domain create failed (OIDC browser login will break): {e}")

    # App client
    client_name = f"{cfg.project}-{cfg.environment}-cli"
    clients = cognito.list_user_pool_clients(UserPoolId=pool_id, MaxResults=60).get("UserPoolClients") or []
    client_id = next((c["ClientId"] for c in clients if c.get("ClientName") == client_name), None)
    if not client_id:
        resp = cognito.create_user_pool_client(
            UserPoolId=pool_id,
            ClientName=client_name,
            GenerateSecret=False,
            ExplicitAuthFlows=["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
            AllowedOAuthFlows=["code"],
            AllowedOAuthScopes=["openid", "email", "profile"],
            AllowedOAuthFlowsUserPoolClient=True,
            CallbackURLs=cfg.cognito_callback_urls or [
                "http://localhost:8090/callback",
                "http://localhost:8091/callback",
            ],
            LogoutURLs=cfg.cognito_logout_urls or [
                "http://localhost:8090/logout",
            ],
            SupportedIdentityProviders=["COGNITO"],
        )
        client_id = resp["UserPoolClient"]["ClientId"]
        log(f"Cognito client created: {client_id}")
    else:
        log(f"Cognito client reused: {client_id}")

    # Groups
    for g in cfg.cognito_groups or ["ClaudeAdmin"]:
        try:
            cognito.create_group(GroupName=g, UserPoolId=pool_id)
            log(f"Cognito group created: {g}")
        except ClientError as e:
            if e.response["Error"]["Code"] != "GroupExistsException":
                log(f"Group {g}: {e}")

    state.update({
        "cognito_user_pool_id": pool_id,
        "cognito_user_pool_arn": cfg.cognito_user_pool_arn,
        "cognito_issuer_url": cfg.cognito_issuer_url,
        "cognito_client_id": client_id,
        "cognito_domain": domain,
    })
    state.save()


def destroy_cognito(cfg: InstallConfig, state: State) -> None:
    cognito = client("cognito-idp", cfg)
    pool_id = state.get("cognito_user_pool_id") or cfg.cognito_user_pool_id
    if not pool_id:
        return
    domain = state.get("cognito_domain")
    if domain:
        try:
            cognito.delete_user_pool_domain(Domain=domain, UserPoolId=pool_id)
        except ClientError as e:
            log(f"Domain: {e}")
    try:
        cognito.delete_user_pool(UserPoolId=pool_id)
        log(f"Cognito pool deleted: {pool_id}")
    except ClientError as e:
        log(f"Cognito: {e}")
