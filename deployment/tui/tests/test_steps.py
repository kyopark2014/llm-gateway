from deployment.tui import steps
from deployment.tui.config import BackendConfig

BE = BackendConfig(bucket="b", dynamodb_table="t")


def _names(wf):
    return [s.name for s in wf]


def test_llm_workflow_uses_installer():
    wf = steps.build_llm_workflow(env="dev", backend=BE, enable_chat_db_tools=False, flags={})
    assert _names(wf) == ["installer-deploy", "verify"]
    deploy = wf[0]
    assert "installer.py" in deploy.argv[1]
    assert deploy.argv[2] == "deploy"


def test_llm_workflow_ignores_legacy_chat_db_flag():
    # installer 경로에서는 build-lambdas / terraform 없음
    wf = steps.build_llm_workflow(env="dev", backend=BE, enable_chat_db_tools=True, flags={})
    assert "build-lambdas" not in _names(wf)
    assert "tf-apply" not in _names(wf)


def test_llm_teardown_uses_installer_destroy_all():
    wf = steps.build_llm_teardown(env="dev", backend=BE)
    assert _names(wf) == ["installer-destroy"]
    assert "--all" in wf[0].argv
    assert "--yes" in wf[0].argv
