import json

from deployment.tui import postdeploy as pd


def test_endpoint_url_builds_http_when_hostname_present():
    ep = pd.Endpoint(role="gateway", ingress_name="gateway_alb_dns", hostname="abc.elb.amazonaws.com")
    assert ep.url == "http://abc.elb.amazonaws.com"


def test_endpoint_url_preserves_https():
    ep = pd.Endpoint(role="admin-api", ingress_name="api_gateway_endpoint",
                     hostname="https://abc.execute-api.ap-northeast-2.amazonaws.com")
    assert ep.url == "https://abc.execute-api.ap-northeast-2.amazonaws.com"


def test_endpoint_url_none_when_hostname_missing():
    ep = pd.Endpoint(role="gateway", ingress_name="gateway_alb_dns", hostname=None)
    assert ep.url is None


def test_endpoints_by_role_finds_and_misses():
    eps = pd.Endpoints(items=[pd.Endpoint("admin-ui", "admin_ui_alb_dns", "h")])
    assert eps.by_role("admin-ui").hostname == "h"
    assert eps.by_role("gateway") is None


def test_discover_endpoints_from_state(tmp_path):
    state = tmp_path / ".state-dev.json"
    state.write_text(json.dumps({
        "gateway_alb_dns": "g.elb.amazonaws.com",
        "admin_api_alb_dns": "a.elb.amazonaws.com",
        "admin_ui_alb_dns": "u.elb.amazonaws.com",
        "api_gateway_endpoint": "https://api.example.com",
    }))
    eps = pd.discover_endpoints(env="dev", state_path=state)
    assert eps.error is None
    assert eps.by_role("gateway").url == "http://g.elb.amazonaws.com"
    assert eps.by_role("admin-api").url == "https://api.example.com"
    assert eps.by_role("admin-ui").hostname == "u.elb.amazonaws.com"


def test_discover_endpoints_missing_file(tmp_path):
    eps = pd.discover_endpoints(state_path=tmp_path / "missing.json")
    assert eps.items == []
    assert eps.error is not None


def test_discover_endpoints_admin_api_falls_back_to_alb(tmp_path):
    state = tmp_path / "s.json"
    state.write_text(json.dumps({
        "gateway_alb_dns": "g.elb.amazonaws.com",
        "admin_api_alb_dns": "a.elb.amazonaws.com",
        "admin_ui_alb_dns": "u.elb.amazonaws.com",
    }))
    eps = pd.discover_endpoints(state_path=state)
    assert eps.by_role("admin-api").hostname == "a.elb.amazonaws.com"


def test_live_healthcheck_maps_curl_status(monkeypatch):
    eps = pd.Endpoints(items=[
        pd.Endpoint("gateway", "gateway_alb_dns", "g"),
        pd.Endpoint("admin-api", "api_gateway_endpoint", "a"),
        pd.Endpoint("admin-ui", "admin_ui_alb_dns", "u"),
    ])
    status = {"http://g/health": 200, "http://a/health": 200, "http://u/": 307}
    monkeypatch.setattr(pd, "_curl_status", lambda url: status.get(url))
    results = pd.live_healthcheck(eps)
    states = {r.label: r.state for r in results}
    assert states == {"gateway": "ok", "admin-api": "ok", "admin-ui": "ok"}


def test_live_healthcheck_pending_on_connection_failure(monkeypatch):
    eps = pd.Endpoints(items=[pd.Endpoint("gateway", "gateway_alb_dns", "g")])
    monkeypatch.setattr(pd, "_curl_status", lambda url: None)
    results = pd.live_healthcheck(eps)
    assert results[0].state == "pending"


def test_live_healthcheck_pending_when_hostname_missing(monkeypatch):
    eps = pd.Endpoints(items=[pd.Endpoint("gateway", "gateway_alb_dns", None)])

    def boom(url):
        raise AssertionError("curl should not be called for missing hostname")

    monkeypatch.setattr(pd, "_curl_status", boom)
    results = pd.live_healthcheck(eps)
    assert results[0].state == "pending"
