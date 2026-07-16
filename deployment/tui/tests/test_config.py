# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
from deployment.tui import config


def test_find_placeholders_flags_change_me():
    vals = {"cognito_domain_suffix": "CHANGE_ME"}
    assert config.find_placeholders(vals) == ["cognito_domain_suffix"]


def test_find_placeholders_empty_when_clean():
    assert config.find_placeholders({"a": "real", "b": "123456789012"}) == []


def test_backend_config_defaults():
    bc = config.BackendConfig(region="ap-northeast-2")
    assert bc.region == "ap-northeast-2"
    assert bc.bucket == ""
