"""CLI: lint, search, and promote skills in the procedural library."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

skills_app = typer.Typer(help="Lint and search the skill library.")


def register_skills_commands(app: typer.Typer) -> None:
    app.add_typer(skills_app, name="skills")


@skills_app.command("lint")
def skills_lint(
    ctx: typer.Context,
    skills_root: Path = typer.Option(Path("skills"), "--skills-root"),
) -> None:
    """Structural + semantic lint of every skill version under ``skills_root``."""

    from recertia.app.authz import PolicyDecisionPoint
    from recertia.app.library import LibraryService
    from recertia.errors import RecertiaError
    from recertia.memory.procedural.store import SkillStore
    from recertia.ports.identity import Principal

    principal = (
        ctx.obj["principal"]
        if isinstance(ctx.obj, dict) and ctx.obj.get("principal") is not None
        else Principal.local_operator()
    )
    store = SkillStore(skills_root)
    try:
        report = LibraryService(pdp=PolicyDecisionPoint.load(surface="cli")).lint_skills(
            principal, store=store
        )
    except RecertiaError as exc:
        from recertia.cli.exit import raise_exit

        raise_exit(exc)
    failures = {k: v for k, v in report.items() if v}
    for key, violations in report.items():
        if violations:
            typer.echo(f"FAIL {key}")
            for v in violations:
                typer.echo(f"  - {v}")
        else:
            typer.echo(f"OK   {key}")
    if failures:
        raise typer.Exit(code=1)


@skills_app.command("search")
def skills_search(
    ctx: typer.Context,
    query: str = typer.Argument(...),
    skills_root: Path = typer.Option(Path("skills"), "--skills-root"),
    index_path: Path = typer.Option(Path(".recertia/skill_index.db"), "--index"),
    workdir: Path = typer.Option(Path("."), "--workdir"),
    explain: bool = typer.Option(False, "--explain", help="Print scores and drop reasons."),
    env: Optional[str] = typer.Option(
        None, "--env", help="JSON object of tool→version for fingerprint matching."
    ),
) -> None:
    """Retrieval debug endpoint (specs §9): scores and drop reasons without running a task."""

    from recertia.app.authz import PolicyDecisionPoint
    from recertia.app.library import LibraryService
    from recertia.errors import RecertiaError
    from recertia.memory.procedural.store import SkillStore
    from recertia.ports.identity import Principal
    from recertia.retrieval.index import SkillIndex

    principal = (
        ctx.obj["principal"]
        if isinstance(ctx.obj, dict) and ctx.obj.get("principal") is not None
        else Principal.local_operator()
    )
    store = SkillStore(skills_root)
    index = SkillIndex(index_path)
    env_fp = json.loads(env) if env else {}
    try:
        payload = LibraryService(pdp=PolicyDecisionPoint.load(surface="cli")).search_skills(
            principal,
            store=store,
            index=index,
            query=query,
            workdir=workdir,
            env_fingerprint=env_fp,
            explain=explain,
        )
    except RecertiaError as exc:
        from recertia.cli.exit import raise_exit

        raise_exit(exc)
    bundle = payload["bundle"]
    explanation = payload["explanation"]

    if explain:
        typer.echo(f"snapshot={explanation.snapshot_id}")
        typer.echo(
            f"lexical_hits={len(explanation.lexical_hits)} "
            f"vector_hits={len(explanation.vector_hits)}"
        )
        for d in explanation.dropped:
            typer.echo(f"  DROP {d.skill_id}@v{d.version} stage={d.stage} reason={d.reason}")
        for sid, ver, score, reason in explanation.demoted:
            typer.echo(f"  DEMOTE {sid}@v{ver} score={score:.3f} ({reason})")
    for c in bundle.skills:
        typer.echo(f"{c.skill_id}@v{c.version} score={c.score}")
    if not bundle.skills:
        typer.echo("(empty bundle)")


@skills_app.command("promote")
def skills_promote(
    ctx: typer.Context,
    skill_id: str = typer.Argument(..., help="Skill id to promote."),
    version: int = typer.Option(..., "--version", help="Immutable version number."),
    skills_root: Path = typer.Option(Path("skills"), "--skills-root"),
    golden_dir: Optional[Path] = typer.Option(
        None, "--golden-dir", help="Single-fixture golden directory (M1 path)."
    ),
    golden_root: Optional[Path] = typer.Option(
        None, "--golden-root", help="Task-class golden root (M4 path)."
    ),
    runs_root: Path = typer.Option(Path(".recertia/promote-runs"), "--runs-root"),
    log_dir: Path = typer.Option(Path("evals/golden/_promotion_logs"), "--log-dir"),
    require_task_class_gate: bool = typer.Option(
        False, "--require-task-class-gate", help="Require full task-class harness."
    ),
    model_validated_on: str = typer.Option("m1-seed", "--model-validated-on"),
    audit_root: Optional[Path] = typer.Option(
        None,
        "--audit-root",
        help="Operator-only audit root for refusal detail (default: RECERTIA_OPERATOR_AUDIT_ROOT).",
    ),
    operator_detail: bool = typer.Option(
        False,
        "--operator-detail",
        help="Operator only: print the audit record (fixture ids, paths) for a refusal.",
    ),
) -> None:
    """Promote a skill to approved after the golden regression gate (specs §8).

    A refusal prints the decision only. Fixture ids and paths go to the operator
    audit store; ``--operator-detail`` prints that record. Without a usable audit
    store the promotion is refused before the gate runs.
    """

    from recertia.app.authz import PolicyDecisionPoint
    from recertia.app.promotion import PromotionService
    from recertia.errors import RecertiaError
    from recertia.memory.procedural.promote import PromotionError
    from recertia.memory.procedural.store import SkillStore
    from recertia.ports.identity import Principal
    from recertia.review.promotion_audit import AuditSinkUnavailable, open_operator_audit

    if golden_dir is None and golden_root is None:
        typer.echo("provide --golden-dir and/or --golden-root", err=True)
        raise typer.Exit(code=2)

    principal = (
        ctx.obj["principal"]
        if isinstance(ctx.obj, dict) and ctx.obj.get("principal") is not None
        else Principal.local_operator()
    )
    store = SkillStore(skills_root)
    repo_root = Path.cwd()
    if audit_root is None:
        settings = ctx.obj.get("settings") if isinstance(ctx.obj, dict) else None
        if settings is None:
            from recertia.settings import load_settings

            settings = load_settings()
        root_setting: Path | str | None = settings.operator_audit_root
    else:
        root_setting = audit_root
    try:
        audit = open_operator_audit(
            root_setting, forbidden_roots=[runs_root, log_dir, skills_root]
        )
    except AuditSinkUnavailable:
        typer.echo(
            "PROMOTION REFUSED: operator audit store unavailable; the golden gate did not "
            "run. Set RECERTIA_OPERATOR_AUDIT_ROOT or --audit-root to a private directory "
            "outside any git checkout, the runs root, the log dir and the skills root.",
            err=True,
        )
        raise typer.Exit(code=1) from None
    service = PromotionService(pdp=PolicyDecisionPoint.load(surface="cli"))
    try:
        status = service.promote(
            principal,
            skill_id,
            version,
            store=store,
            golden_dir=golden_dir,
            golden_root=golden_root,
            runs_root=runs_root,
            log_dir=log_dir,
            model_validated_on=model_validated_on,
            repo_root=repo_root,
            require_task_class_gate=require_task_class_gate,
            audit=audit,
        )
    except RecertiaError as exc:
        from recertia.cli.exit import raise_exit

        raise_exit(exc)
    except PromotionError as exc:
        typer.echo(f"PROMOTION FAILED: {exc}", err=True)
        if operator_detail:
            record = audit.read(exc.audit_ref) if exc.audit_ref else None
            if record is None:
                typer.echo("operator detail unavailable: no audit record for this refusal", err=True)
            else:
                typer.echo(json.dumps(record, indent=2, default=str), err=True)
        raise typer.Exit(code=1) from None

    typer.echo(
        f"skill_id={status.skill_id} version={status.version} "
        f"lifecycle={status.lifecycle} active={status.active}"
    )
    from recertia.memory.procedural.live_mix import live_mix_reason

    ver = store.get_version(skill_id, version)
    stats = store.get_stats(skill_id, version)
    typer.echo(f"live_mix={live_mix_reason(ver, status, stats)}")
    if status.certification.golden_set_ref:
        typer.echo(f"golden_set_ref={status.certification.golden_set_ref}")


@skills_app.command("list")
def skills_list(
    ctx: typer.Context,
    skills_root: Path = typer.Option(Path("skills"), "--skills-root"),
    task_class: Optional[str] = typer.Option(None, "--task-class"),
    lifecycle: Optional[str] = typer.Option(None, "--lifecycle"),
) -> None:
    """List skill versions, optionally filtered by task class and lifecycle."""

    from recertia.app.authz import PolicyDecisionPoint
    from recertia.app.library import LibraryService
    from recertia.errors import RecertiaError
    from recertia.memory.procedural.store import SkillStore
    from recertia.ports.identity import Principal

    principal = (
        ctx.obj["principal"]
        if isinstance(ctx.obj, dict) and ctx.obj.get("principal") is not None
        else Principal.local_operator()
    )
    store = SkillStore(skills_root)
    try:
        items = LibraryService(pdp=PolicyDecisionPoint.load(surface="cli")).list_skills(
            principal, store=store, task_class=task_class, lifecycle=lifecycle
        )
    except RecertiaError as exc:
        from recertia.cli.exit import raise_exit

        raise_exit(exc)
    for item in items:
        typer.echo(
            f"{item['skill_id']}@{item['version']} lifecycle={item['lifecycle']} "
            f"task_class={item['task_class']} active={item['active']}"
        )


def _parse_skill_ref(ref: str) -> tuple[str, int]:
    if "@" not in ref:
        raise typer.BadParameter("expected skill_id@version (example: bump-python-dep@3)")
    skill_id, _, raw_ver = ref.rpartition("@")
    raw_ver = raw_ver.lstrip("vV")
    try:
        version = int(raw_ver)
    except ValueError as exc:
        raise typer.BadParameter("version must be an integer") from exc
    if not skill_id or version < 1:
        raise typer.BadParameter("expected skill_id@version with version >= 1")
    return skill_id, version


@skills_app.command("show")
def skills_show(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="skill_id@version, e.g. bump-python-dep@3"),
    skills_root: Path = typer.Option(Path("skills"), "--skills-root"),
) -> None:
    """Print one skill version, status, and stats as JSON."""

    from recertia.app.authz import PolicyDecisionPoint
    from recertia.app.library import LibraryService
    from recertia.errors import RecertiaError
    from recertia.memory.procedural.store import SkillStore
    from recertia.ports.identity import Principal

    skill_id, version = _parse_skill_ref(ref)
    store = SkillStore(skills_root)
    principal = (
        ctx.obj["principal"]
        if isinstance(ctx.obj, dict) and ctx.obj.get("principal") is not None
        else Principal.local_operator()
    )
    try:
        payload = LibraryService(pdp=PolicyDecisionPoint.load(surface="cli")).get_skill(
            principal, store=store, skill_id=skill_id, version=version
        )
    except RecertiaError as exc:
        from recertia.cli.exit import raise_exit

        raise_exit(exc, message=f"skill not found: {skill_id}@{version}")
    typer.echo(json.dumps(payload, indent=2))
