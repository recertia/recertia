"""CLI: start/resume runs, inspect route logs, verify the ledger. Goal-aware (Variant B)."""

from __future__ import annotations

import json
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer
from pydantic import ValidationError

from contracts.budget import Budget
from contracts.criteria import TaskCriterion
from contracts.goal import Goal, compile_goal
from contracts.run import Task
from recertia.bootstrap import build_default_orchestrator, resolve_task_class
from recertia.cli.exit import raise_exit
from recertia.config import load_model_config
from recertia.errors import ConfigError, RecertiaError
from recertia.ids import InvalidIdError, validate_run_id
from recertia.ledger import HashChainLedger, LedgerVerificationError
from recertia.runtime.errors import LedgerUnavailable
from recertia.solver.container import ensure_execution_ready, execution_backend_override
from recertia.solver.factory import ModelConfigError
from recertia.solver.sandbox import SandboxError

runs_app = typer.Typer(help="Inspect runs.")
ledger_app = typer.Typer(help="Verify the provenance ledger.")
goal_app = typer.Typer(help="Goal helpers (Variant B).")


def _load_spec(spec_path: Path) -> dict:
    try:
        return json.loads(spec_path.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"spec {spec_path.name} is not valid JSON: line {exc.lineno} column {exc.colno}: {exc.msg}"
        ) from None


def _first_error(exc: ValidationError) -> str:
    first = exc.errors(include_url=False)[0]
    where = ".".join(str(part) for part in first["loc"]) or "(root)"
    return f"{exc.error_count()} error(s); first at {where}: {first['msg']}"


def _prepare_execution() -> None:
    """Fail fast if the configured backend cannot run.

    ``--local-exec`` is a context override applied by the caller. It does not
    write ``RECERTIA_EXECUTION_BACKEND``. A missing container runtime fails
    closed unless that override or an already-configured local backend is set.
    """

    try:
        ensure_execution_ready()
    except SandboxError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc


def register_run_commands(app: typer.Typer) -> None:
    """Attach ``run`` / ``resume`` to the root app and nest runs/ledger/goal typers."""

    app.add_typer(runs_app, name="runs")
    app.add_typer(ledger_app, name="ledger")
    app.add_typer(goal_app, name="goal")
    app.command("run")(run_cmd)
    app.command("resume")(resume_cmd)


def run_cmd(
    spec: Optional[Path] = typer.Option(
        None, "--spec", exists=True, help="Path to a run spec JSON file (legacy or goal-aware)."
    ),
    goal: Optional[Path] = typer.Option(
        None, "--goal", exists=True, help="Path to a Goal JSON file (Variant B preferred)."
    ),
    runs_root: Path = typer.Option(Path(".recertia"), "--runs-root", help="Where run state is persisted."),
    run_id: Optional[str] = typer.Option(None, "--run-id", help="Defaults to a fresh UUID."),
    workdir: Optional[Path] = typer.Option(None, "--workdir"),
    workspace_id: Optional[str] = typer.Option(
        None,
        "--workspace-id",
        help="Use a registered host workspace under --runs-root (RW2). Optional --workdir as subpath.",
    ),
    tenant: str = typer.Option(
        "default",
        "--tenant",
        help="Tenant for --workspace-id lookup.",
    ),
    skills_root: Path = typer.Option(
        Path("skills"), "--skills-root", help="Procedural skill library root."
    ),
    facts_root: Path = typer.Option(
        Path("facts"), "--facts-root", help="Semantic facts root."
    ),
    index: Path = typer.Option(
        Path(".recertia/skill_index.db"),
        "--index",
        help="Skill retrieval index (default: under --runs-root).",
    ),
    local_exec: bool = typer.Option(
        False,
        "--local-exec",
        help="Local executor for this invocation (dev; no Docker). Does not change process env.",
    ),
    ablation: bool = typer.Option(
        False, "--ablation", help="Assign control arm via T3 ablation sampler (outside nodes)."
    ),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        help="Model provider selection: stub | anthropic:<id> | openai:<id>.",
    ),
    verifier: Optional[str] = typer.Option(
        None,
        "--verifier",
        help="Optional verifier model id (same provider family), e.g. anthropic:claude-…",
    ),
) -> None:
    """Start a run and drive it to completion (or to the first unrecoverable stop)."""

    if goal is None and spec is None:
        typer.echo("Provide --goal or --spec", err=True)
        raise typer.Exit(code=2)

    scope = execution_backend_override("local") if local_exec else nullcontext()
    with scope:
        _prepare_execution()
        try:
            _execute_run(
                spec=spec,
                goal=goal,
                runs_root=runs_root,
                run_id=run_id,
                workdir=workdir,
                workspace_id=workspace_id,
                tenant=tenant,
                skills_root=skills_root,
                facts_root=facts_root,
                index=index,
                ablation=ablation,
                model=model,
                verifier=verifier,
            )
        except RecertiaError as exc:
            raise_exit(exc)


def _execute_run(
    *,
    spec: Optional[Path],
    goal: Optional[Path],
    runs_root: Path,
    run_id: Optional[str],
    workdir: Optional[Path],
    workspace_id: Optional[str],
    tenant: str,
    skills_root: Path,
    facts_root: Path,
    index: Path,
    ablation: bool,
    model: Optional[str],
    verifier: Optional[str],
) -> None:
    try:
        model_config = load_model_config(model=model, verifier=verifier)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    rid = run_id or uuid.uuid4().hex[:12]
    try:
        rid = validate_run_id(rid)
    except InvalidIdError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    data: dict = {}
    if spec is not None:
        data = _load_spec(spec)

    task_goal: Goal | None = None
    try:
        if goal is not None:
            task_goal = Goal.model_validate_json(goal.read_text())
        elif "goal" in data:
            task_goal = Goal.model_validate(data["goal"])
    except ValidationError as exc:
        source = f"goal {goal.name}" if goal is not None else "the spec's goal"
        raise ConfigError(f"{source} is not a valid goal: {_first_error(exc)}") from None

    task_data = data.get("task", {})
    request = task_data.get("request") or data.get("request")
    if task_goal is not None and task_goal.context and not request:
        request = task_goal.context

    task = Task(
        task_id=task_data.get("task_id", rid),
        goal=task_goal,
        request=request,
        task_class=resolve_task_class(
            explicit=task_data.get("task_class") or data.get("task_class"),
            goal_task_class=task_goal.task_class if task_goal else None,
            default="repo-chore",
        ),
        submitted_at=datetime.now(timezone.utc),
        is_eval_fixture=bool(task_data.get("is_eval_fixture", False)),
    )
    criteria = [TaskCriterion(**c) for c in data.get("criteria", [])]
    if not criteria and task_goal is not None:
        criteria = compile_goal(task_goal)
    budget = Budget(**data["budget"]) if "budget" in data else Budget()
    script = data.get("script")
    if workspace_id:
        from recertia.paths import HostRootError, resolve_under_host_root
        from recertia.workspaces.registry import WorkspaceRegistry

        registry = WorkspaceRegistry(runs_root / "workspaces_registry.sqlite")
        try:
            ws = registry.get(workspace_id, tenant_id=tenant, enabled_only=False)
            if ws is None:
                typer.echo(f"workspace not found: {workspace_id}", err=True)
                raise typer.Exit(code=2)
            if not ws.enabled:
                typer.echo(f"workspace disabled: {workspace_id}", err=True)
                raise typer.Exit(code=2)
            sub = str(workdir) if workdir is not None else ""
            try:
                wd = resolve_under_host_root(ws.host_root, sub)
            except HostRootError as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(code=2) from exc
        finally:
            registry.close()
    else:
        wd = workdir or (Path(data["workdir"]) if "workdir" in data else runs_root / "workspaces" / rid)
        wd.mkdir(parents=True, exist_ok=True)

    arm = data.get("arm", "treatment")
    if ablation:
        from recertia.evals.ablation import assign_arm

        decision = assign_arm(
            run_id=rid,
            task_class=task.task_class,
            is_eval_fixture=task.is_eval_fixture,
            has_external_effects=bool(task_data.get("has_external_effects", False)),
            explicit_skill_supplied=bool(task_data.get("explicit_skill")),
        )
        arm = decision.arm
        typer.echo(f"ablation arm={arm} ({decision.reason})")

    index_path = index
    if index == Path(".recertia/skill_index.db"):
        index_path = runs_root / "skill_index.db"

    try:
        bundle = build_default_orchestrator(
            runs_root,
            skills_root=skills_root,
            facts_root=facts_root,
            index_path=index_path,
            model_config=model_config,
        )
    except ModelConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    try:
        state = bundle.orchestrator.start(
            rid,
            task,
            criteria,
            budget=budget,
            workdir=wd,
            script=script,
            arm=arm,
            manifest=bundle.run_manifest(),
        )
    finally:
        bundle.close()

    typer.echo(f"run_id={rid} terminal={state.terminal} arm={state.arm}")
    if state.failure is not None:
        typer.echo(f"failure_class={state.failure.failure_class}")
    raise typer.Exit(code=0 if state.terminal in ("solved", "abstained") else 1)


def resume_cmd(
    run_id: str = typer.Argument(...),
    runs_root: Path = typer.Option(Path(".recertia"), "--runs-root"),
    spec: Optional[Path] = typer.Option(
        None, "--spec", help="Re-supply script/workdir if not resuming in place."
    ),
    skills_root: Path = typer.Option(Path("skills"), "--skills-root"),
    facts_root: Path = typer.Option(Path("facts"), "--facts-root"),
    local_exec: bool = typer.Option(
        False,
        "--local-exec",
        help="Local executor for this invocation (dev; no Docker). Does not change process env.",
    ),
) -> None:
    """Resume a run from its last checkpoint. Safe to call after a killed process."""

    scope = execution_backend_override("local") if local_exec else nullcontext()
    with scope:
        _prepare_execution()
        try:
            _execute_resume(
                run_id=run_id,
                runs_root=runs_root,
                spec=spec,
                skills_root=skills_root,
                facts_root=facts_root,
            )
        except RecertiaError as exc:
            raise_exit(exc)


def _execute_resume(
    *,
    run_id: str,
    runs_root: Path,
    spec: Optional[Path],
    skills_root: Path,
    facts_root: Path,
) -> None:
    workdir = runs_root / "workspaces" / run_id
    script = None
    if spec is not None:
        data = _load_spec(spec)
        script = data.get("script")
        if "workdir" in data:
            workdir = Path(data["workdir"])

    bundle = build_default_orchestrator(
        runs_root,
        skills_root=skills_root,
        facts_root=facts_root,
        index_path=runs_root / "skill_index.db",
    )
    try:
        state = bundle.orchestrator.resume(run_id, workdir=workdir, script=script)
    finally:
        bundle.close()

    typer.echo(f"run_id={run_id} terminal={state.terminal}")
    raise typer.Exit(code=0 if state.terminal in ("solved", "abstained") else 1)


@runs_app.command("show")
def runs_show(
    run_id: str = typer.Argument(...),
    runs_root: Path = typer.Option(Path(".recertia"), "--runs-root"),
    route_log: bool = typer.Option(False, "--route-log", help="Print the full route log."),
) -> None:
    from recertia.app.checkpoints import CheckpointReader

    latest = CheckpointReader().latest_row("default", run_id, runs_root=runs_root)
    if latest is None:
        typer.echo(f"no such run: {run_id}", err=True)
        raise typer.Exit(code=1)
    _, _, next_node, state = latest
    typer.echo(
        f"run_id={run_id} terminal={state.terminal} next_node={next_node} "
        f"attempt_no={state.attempt_no}"
    )
    if route_log:
        for entry in state.route_log:
            reason = entry.reason.replace("\u2192", "->")
            typer.echo(f"  [{entry.attempt_no}] {entry.node} --{entry.route}--> {reason}")


def _ledger_path(runs_root: Path, tenant: str | None) -> Path:
    if tenant:
        from recertia.persistence.layout import RuntimeLayout

        return RuntimeLayout(runs_root).tenant(tenant).ledger_path
    return runs_root / "ledger.jsonl"


@ledger_app.command("verify")
def ledger_verify(
    ctx: typer.Context,
    runs_root: Path = typer.Option(Path(".recertia"), "--runs-root"),
    tenant: Optional[str] = typer.Option(None, "--tenant"),
    check_sig: bool = typer.Option(
        False,
        "--check-sig",
        help="Verify HMAC signatures. Unsigned entries are allowed until the first signed one.",
    ),
    check_anchor: bool = typer.Option(
        False,
        "--check-anchor",
        help="Reserved. Skipped until a tip anchor is configured.",
    ),
) -> None:
    from recertia.app.authz import PolicyDecisionPoint
    from recertia.app.library import LibraryService
    from recertia.errors import RecertiaError
    from recertia.ports.identity import Principal

    principal = (
        ctx.obj["principal"]
        if isinstance(ctx.obj, dict) and ctx.obj.get("principal") is not None
        else Principal.local_operator()
    )
    ledger = HashChainLedger(_ledger_path(runs_root, tenant))
    try:
        report = LibraryService(pdp=PolicyDecisionPoint.load(surface="cli")).verify_ledger(
            principal, ledger=ledger, check_sig=check_sig
        )
    except LedgerVerificationError as exc:
        typer.echo(f"LEDGER INVALID: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except LedgerUnavailable as exc:
        typer.echo(f"LEDGER INVALID: {exc}", err=True)
        raise typer.Exit(code=exc.exit_code) from exc
    except RecertiaError as exc:
        from recertia.cli.exit import raise_exit

        raise_exit(exc)
    anchor = " (anchor check skipped)" if check_anchor else ""
    typer.echo(f"ledger OK: {report.entries} entries verified{anchor}")


@ledger_app.command("rekey")
def ledger_rekey(
    runs_root: Path = typer.Option(Path(".recertia"), "--runs-root"),
    tenant: Optional[str] = typer.Option(None, "--tenant"),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Count unsigned and signed lines. Does not rewrite the ledger.",
    ),
) -> None:
    """Report unsigned vs signed lines. Historical lines are not rewritten."""

    if not dry_run:
        typer.echo("pass --dry-run; rekey does not rewrite historical lines", err=True)
        raise typer.Exit(code=2)
    from recertia.ledger.signer import SIG_FIELD

    ledger = HashChainLedger(_ledger_path(runs_root, tenant))
    try:
        rows = ledger.entries()
    except LedgerVerificationError as exc:
        typer.echo(f"LEDGER INVALID: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except LedgerUnavailable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=exc.exit_code) from exc
    unsigned = sum(1 for entry in rows if entry.evidence.get(SIG_FIELD) is None)
    signed = len(rows) - unsigned
    typer.echo(f"unsigned={unsigned} signed={signed}")


@ledger_app.command("repair")
def ledger_repair(
    ctx: typer.Context,
    truncate_torn_tail: bool = typer.Option(False, "--truncate-torn-tail"),
    runs_root: Path = typer.Option(Path(".recertia"), "--runs-root"),
    tenant: Optional[str] = typer.Option(None, "--tenant"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Truncate a partial EOF and record ``ledger_repaired``. Mid-chain damage is refused."""

    principal = (ctx.obj or {}).get("principal") if isinstance(ctx.obj, dict) else None
    if principal is None:
        from recertia.ports.identity import Principal

        principal = Principal.local_operator()
    if not principal.has_role("platform_admin"):
        typer.echo("forbidden: ledger repair requires platform_admin", err=True)
        raise typer.Exit(code=3)
    if not truncate_torn_tail:
        typer.echo("pass --truncate-torn-tail to repair a torn tail", err=True)
        raise typer.Exit(code=2)
    ledger = HashChainLedger(_ledger_path(runs_root, tenant))
    try:
        report = ledger.repair_torn_tail(actor=principal.subject, dry_run=dry_run)
    except LedgerUnavailable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=exc.exit_code) from exc
    if report.status == "dry_run":
        typer.echo(f"would drop {report.dropped_bytes} bytes")
        return
    typer.echo(f"{report.status} dropped_bytes={report.dropped_bytes}")


@goal_app.command("compile")
def goal_compile(
    goal_path: Path = typer.Argument(..., exists=True, help="Goal JSON file"),
) -> None:
    """Compile a Goal to TaskCriterion[] and print JSON."""
    goal = Goal.model_validate_json(goal_path.read_text())
    criteria = compile_goal(goal)
    typer.echo(json.dumps([c.model_dump(mode="json") for c in criteria], indent=2))
