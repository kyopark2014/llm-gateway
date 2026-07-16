# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""인라인 콘솔 UI — Claude Code처럼 터미널에 흘려쓰는 가벼운 배포 오케스트레이터.

Textual 풀스크린 대신 rich로 append-only 출력을 낸다. 전체 화면을 점유하지
않고 일반 셸 스크롤버퍼에 남으므로, 로그를 그대로 복사/스크롤할 수 있다.

UI 조립만 담당하고 배포 로직은 config/steps/preflight/runner를 그대로 재사용한다."""
from __future__ import annotations

import questionary
from questionary import Choice
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import config, paths, postdeploy, preflight
from .config import BackendConfig
from .steps import (
    Step,
    build_llm_teardown,
    build_llm_workflow,
)

console = Console()


# --------------------------------------------------------------------------- #
# 입력 래퍼 — questionary 화살표/체크박스. 여기만 인터랙션을 캡슐화한다.
# .ask()는 Ctrl-C 시 None을 반환하므로 취소로 처리한다.
# --------------------------------------------------------------------------- #
class Cancelled(Exception):
    """사용자가 프롬프트를 Ctrl-C/Esc로 취소."""


def _unwrap(value):
    if value is None:
        raise Cancelled
    return value


def ask_select(message: str, choices) -> str:
    """화살표 단일선택. choices: [(label, value), ...] 또는 [str, ...]."""
    opts = [c if isinstance(c, str) else Choice(title=c[0], value=c[1]) for c in choices]
    return _unwrap(questionary.select(message, choices=opts).ask())


def ask_checkbox(message: str, choices) -> list:
    """스페이스 토글 다중선택. choices: [(label, value, checked), ...]."""
    opts = [Choice(title=c[0], value=c[1], checked=c[2]) for c in choices]
    return _unwrap(questionary.checkbox(message, choices=opts).ask())


def ask_text(message: str, default: str = "") -> str:
    return _unwrap(questionary.text(message, default=default).ask())


def ask_confirm(message: str, default: bool = False) -> bool:
    return _unwrap(questionary.confirm(message, default=default).ask())

# 다음 단계 가이드에서 가리키는 리포 내 문서 경로(실제 위치라 상수).
NEXT_STEPS_DOCS = {
    "post_deploy": "deployment/ecs/installer.md",
    "cognito": "deployment/ecs/installer.md",
}

_STATE_MARK = {
    "ok": "[green]✓[/green]",
    "pending": "[yellow]⏳[/yellow]",
    "check": "[yellow]⚠[/yellow]",
}


def render_endpoints_panel(endpoints) -> None:
    """엔드포인트 URL 표. 비어있으면 프로비저닝 안내."""
    if endpoints.error:
        console.print(f"[yellow]엔드포인트 조회 실패[/yellow] — {endpoints.error}")
        console.print("[dim]deployment/ecs/.state-<env>.json 이 있는지 확인하세요.[/dim]")
        return
    if not endpoints.items or all(e.hostname is None for e in endpoints.items):
        console.print(
            "[yellow]엔드포인트 미준비[/yellow] — ALB/API GW DNS 가 아직 없습니다.\n"
            "[dim]1~2분 뒤 메뉴 → '배포 검증'에서 다시 확인하세요.[/dim]"
        )
        return
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("서비스")
    table.add_column("URL", style="cyan")
    for ep in endpoints.items:
        table.add_row(ep.role, ep.url or "[dim]프로비저닝 중[/dim]")
    console.print(Panel(table, title="접속 엔드포인트", border_style="cyan"))


def render_next_steps(env: str) -> None:
    """핵심 액션 + 문서 링크."""
    body = Text()
    body.append("다음 단계:\n", style="bold")
    body.append("  1. python3 installer.py status -c config.yaml 로 엔드포인트 확인\n")
    body.append("  2. 준비되면 메뉴 → '배포 검증'으로 ALB/API GW 헬스체크\n")
    body.append("  3. Admin UI 접속 → Cognito admin 온보딩 (첫 사용자 + 팀 그룹)\n")
    body.append("  4. 팀 budget 활성화 (기본 $0 + HARD_BLOCK → 활성화 전 모든 요청 429, 버그 아님)\n")
    console.print(Panel(body, title=f"배포 후 가이드 ({env})", border_style="green"))
    console.print(f"[dim]상세 가이드: {NEXT_STEPS_DOCS['post_deploy']}[/dim]")
    console.print(f"[dim]Cognito 온보딩: {NEXT_STEPS_DOCS['cognito']}[/dim]")


def render_health_table(results) -> None:
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("항목")
    table.add_column("상태")
    table.add_column("detail", style="dim")
    for r in results:
        table.add_row(r.label, _STATE_MARK.get(r.state, r.state), r.detail)
    console.print(table)

# --------------------------------------------------------------------------- #
# 표시 헬퍼
# --------------------------------------------------------------------------- #
def banner() -> None:
    console.print(
        Panel(
            Text("awsome-ai-gateway 배포 오케스트레이터", justify="center", style="bold cyan"),
            border_style="cyan",
        )
    )


def preflight_table(checks) -> bool:
    """CheckResult 리스트를 표로 출력하고 전부 통과했는지 반환."""
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail", style="dim")
    all_ok = True
    for c in checks:
        mark = "[green]✓[/green]" if c.ok else "[red]✗[/red]"
        table.add_row(c.name, mark, c.detail)
        all_ok = all_ok and c.ok
    console.print(table)
    return all_ok


def run_preflight(tools) -> bool:
    checks = preflight.check_tools(tools)
    checks.append(preflight.check_aws_auth())
    return preflight_table(checks)


def aws_account_id() -> str | None:
    """현재 자격증명의 AWS account ID (best-effort). 실패 시 None."""
    import subprocess

    try:
        proc = subprocess.run(
            ["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return None
    acct = proc.stdout.strip()
    return acct if proc.returncode == 0 and acct else None


# --------------------------------------------------------------------------- #
# 워크플로우 실행 — 스텝 단위 append-only 스트리밍
# --------------------------------------------------------------------------- #
def run_and_report(wf, title: str) -> bool:
    """runner.run_workflow를 인라인 스트리밍으로 구동. 전체 성공 여부 반환."""
    # 실행 지점에서 지연 import — runner는 subprocess를 돌리므로 테스트에서 격리
    from .runner import run_workflow

    console.rule(f"[bold]{title}[/bold]")

    def on_step_start(step) -> None:
        skip = " [dim](skippable)[/dim]" if step.skippable else ""
        console.print(f"[cyan]▶[/cyan] {step.name}{skip}")

    def on_line(line: str) -> None:
        console.print(f"  [dim]│[/dim] {line}", highlight=False)

    def on_step_done(result) -> None:
        if result.ok:
            console.print(f"[green]✓[/green] {result.step.name}")
        elif result.step.skippable:
            # best-effort 스텝 실패는 경고로만 — 워크플로우는 계속 진행된다
            console.print(f"[yellow]⚠[/yellow] {result.step.name} (exit {result.returncode}, skippable — 계속 진행)")
        else:
            console.print(f"[red]✗[/red] {result.step.name} (exit {result.returncode})")

    results = run_workflow(
        wf, on_line=on_line, on_step_done=on_step_done, on_step_start=on_step_start
    )
    # skippable 스텝 실패는 전체 성공 판정에서 제외(best-effort). 필수 스텝만 성공하면 완료.
    ok = bool(results) and all(r.ok or r.step.skippable for r in results)
    if ok:
        console.print(f"\n[bold green]완료[/bold green] — {title}")
    else:
        # 정지 사유는 필수(non-skippable) 스텝 실패 — 그걸 가리킨다
        failed = next((r for r in results if not r.ok and not r.step.skippable), None)
        where = f" ({failed.step.name})" if failed else ""
        console.print(f"\n[bold red]실패[/bold red]{where} — 위 로그를 확인하세요")
    return ok


# --------------------------------------------------------------------------- #
# Post-deploy 훅 — 배포 직후 엔드포인트 조회 + 가이드 (curl 없음)
# --------------------------------------------------------------------------- #
def _show_postdeploy_summary(env: str) -> None:
    """배포 직후: 엔드포인트 조회(curl 없음) + 다음 단계 가이드. 검증은 부수기능이라
    어떤 실패도 배포 성공 메시지를 덮지 않도록 예외를 삼킨다."""
    try:
        eps = postdeploy.discover_endpoints(env=env)
        render_endpoints_panel(eps)
    except Exception as exc:  # noqa: BLE001 - 배포 성공 흐름 보호가 우선
        console.print(f"[dim]엔드포인트 조회 건너뜀: {exc}[/dim]")
    render_next_steps(env)


def _maybe_postdeploy(deploy_ok: bool, env: str) -> None:
    if deploy_ok:
        _show_postdeploy_summary(env)


# --------------------------------------------------------------------------- #
# 워크플로우 A — LLM Gateway
# --------------------------------------------------------------------------- #
def flow_llm() -> bool:
    """LLM Gateway 배포 — boto3 installer.py (Terraform 대체)."""
    console.rule("[bold]LLM Gateway 배포[/bold]")
    if not run_preflight(preflight.LLM_TOOLS):
        console.print("[red]사전검증 실패[/red] — 누락 도구/인증을 해결한 뒤 다시 실행하세요.")
        return False

    env = ask_select("환경", ["dev", "prod"])
    region = ask_text("aws_region", default="ap-northeast-2")
    console.print(
        f"[dim]installer.py deploy (env={env} region={region}) — "
        f"config: deployment/ecs/config.yaml[/dim]"
    )

    backend = BackendConfig(bucket="unused", dynamodb_table="unused", region=region)
    wf = build_llm_workflow(
        env=env, backend=backend, enable_chat_db_tools=False, flags={}
    )

    _preview_steps(wf)
    if not ask_confirm("위 스텝을 실행합니다 (실제 배포) — 계속?", default=False):
        console.print("[dim]취소됨[/dim]")
        return False
    ok = run_and_report(wf, f"LLM Gateway {env}")
    _maybe_postdeploy(ok, env)
    return ok


def _preview_steps(wf) -> None:
    console.print("\n[bold]실행 스텝:[/bold]")
    for i, step in enumerate(wf, 1):
        skip = " [dim](skippable)[/dim]" if step.skippable else ""
        console.print(f"  {i}. {step.name}{skip}")
    console.print()


# --------------------------------------------------------------------------- #
# 워크플로우 D — 배포 검증 (Health Check)
# --------------------------------------------------------------------------- #
def flow_verify() -> bool:
    """배포 검증: installer state 엔드포인트 + 라이브 헬스체크 + installer status."""
    console.rule("[bold]배포 검증 (Health Check)[/bold]")
    if not run_preflight(preflight.LLM_TOOLS):
        console.print("[red]사전검증 실패[/red] — 누락 도구/인증을 해결하세요.")
        return False
    env = ask_select("환경", ["dev", "prod"])

    eps = postdeploy.discover_endpoints(env=env)
    render_endpoints_panel(eps)

    console.rule("[bold]라이브 헬스체크[/bold]")
    render_health_table(postdeploy.live_healthcheck(eps))

    cfg = paths.ECS_DIR / "config.yaml"
    if not cfg.is_file():
        cfg = paths.ECS_DIR / "config.example.yaml"
    status = [Step(
        "installer-status",
        ["python3", str(paths.installer_py()), "status", "-c", str(cfg)],
        cwd=paths.ECS_DIR,
        skippable=True,
    )]
    ok = run_and_report(status, f"installer status {env}")

    render_next_steps(env)
    return ok


# --------------------------------------------------------------------------- #
# 워크플로우 E — 스택 삭제 (Teardown)
# --------------------------------------------------------------------------- #
def flow_teardown() -> bool:
    """배포된 스택 삭제. 파괴적 작업이라 대상 요약 + 이중 확인(타이핑) 필수."""
    console.rule("[bold red]스택 삭제 (Teardown)[/bold red]")
    target = ask_select(
        "무엇을 삭제할까요?",
        [
            ("LLM Gateway", "llm"),
            ("취소", "__cancel__"),
        ],
    )
    if target == "__cancel__":
        console.print("[dim]취소됨[/dim]")
        return False

    if not run_preflight(preflight.LLM_TOOLS):
        console.print("[red]사전검증 실패[/red] — 누락 도구/인증을 해결하세요.")
        return False
    env = ask_select("환경", ["dev", "prod"])
    region = ask_text("aws_region", default="ap-northeast-2")
    backend = BackendConfig(bucket="unused", dynamodb_table="unused", region=region)
    wf = build_llm_teardown(env=env, backend=backend)
    summary = (
        f"llm-gateway-{env} 의 [bold]installer 리소스[/bold]"
        " (ECS, ALB, API GW, VPC, Aurora, Valkey, Cognito)"
    )
    confirm_token = f"delete llm-gateway-{env}"
    title = f"Teardown LLM Gateway {env}"

    _preview_steps(wf)
    console.print(
        f"[bold red]⚠ 파괴적 작업[/bold red] — 삭제 대상: {summary}\n"
        "[red]이 작업은 되돌릴 수 없습니다.[/red]"
    )
    # 이중 확인: 정확한 문구를 타이핑해야 진행
    typed = ask_text(f'확인을 위해 [bold]{confirm_token}[/bold] 를 그대로 입력하세요')
    if typed.strip() != confirm_token:
        console.print("[dim]문구 불일치 — 삭제를 취소합니다[/dim]")
        return False
    return run_and_report(wf, title)


# --------------------------------------------------------------------------- #
# 메인 루프
# --------------------------------------------------------------------------- #
# (label, handler)
MENU = [
    ("LLM Gateway 배포", flow_llm),
    ("배포 검증 (Health Check)", flow_verify),
    ("스택 삭제 (Teardown)", flow_teardown),
]


def main_menu() -> None:
    banner()
    while True:
        console.print()
        # 화살표 단일선택 — 각 워크플로우 핸들러를 value로.
        # questionary.Choice는 value=None을 title로 대체하므로 종료는 센티널 문자열 사용.
        choices = [(label, handler) for label, handler in MENU]
        choices.append(("종료", "__exit__"))
        handler = ask_select("워크플로우 선택 (↑↓ 이동, Enter 선택)", choices)
        if handler == "__exit__":
            console.print("[dim]bye[/dim]")
            return
        try:
            handler()
        except Cancelled:
            console.print("\n[dim]취소됨 — 메뉴로 돌아갑니다[/dim]")


def main() -> None:
    try:
        main_menu()
    except (Cancelled, KeyboardInterrupt, EOFError):
        console.print("\n[dim]bye[/dim]")


if __name__ == "__main__":
    main()
