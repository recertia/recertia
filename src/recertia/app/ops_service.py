"""Backup, restore, gc, soak, tabletop, and systems. Thin pass-through."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from recertia.app.authz import Resource
from recertia.ports.identity import Principal


class OpsService:
    """``pdp.require`` then ops/*. Routes and CLI do not call backup_tree themselves."""

    def __init__(self, *, pdp: Any | None = None) -> None:
        self._pdp = pdp

    def backup(self, principal: Principal, *, root: Path, output: Path | None = None) -> Path:
        self._require(principal, "ops.backup")
        from recertia.ops.backup import backup_tree, default_archive_name

        archive = output if output is not None else Path("backups") / default_archive_name()
        return backup_tree(root, archive)

    def restore(
        self,
        principal: Principal,
        *,
        archive: Path,
        dest: Path,
        overwrite: bool = False,
    ) -> Path:
        self._require(principal, "ops.restore")
        from recertia.ops.backup import restore_tree

        return restore_tree(archive, dest, overwrite=overwrite)

    def verify_backup(self, principal: Principal, *, archive: Path) -> dict[str, Any]:
        self._require(principal, "ops.backup")
        from recertia.ops.backup import verify_archive

        return verify_archive(archive)

    def gc(
        self,
        principal: Principal,
        *,
        root: Path,
        older_than_days: float = 14.0,
        dry_run: bool = False,
    ) -> Any:
        self._require(principal, "ops.gc")
        from recertia.retention import garbage_collect

        return garbage_collect(root, older_than_days=older_than_days, dry_run=dry_run)

    def soak_record(
        self,
        principal: Principal,
        *,
        metrics: Path,
        log_path: Path,
        week: str | None = None,
        probes: Path | None = None,
        canary: Path | None = None,
    ) -> Any:
        self._require(principal, "ops.soak")
        from recertia.ops.soak import classify_week, load_log, read_json, record_week, write_log

        payload = read_json(metrics)
        probe_payload = read_json(probes) if probes is not None else None
        canary_payload = read_json(canary) if canary is not None else None
        classified = classify_week(
            payload, week=week, probes=probe_payload, canary=canary_payload
        )
        log = record_week(load_log(log_path), classified)
        write_log(log_path, log)
        return classified

    def soak_status(
        self,
        principal: Principal,
        *,
        log_path: Path,
        tabletop: Path | None = None,
    ) -> dict[str, Any]:
        self._require(principal, "ops.soak")
        from recertia.ops.soak import load_log, read_json, status

        top = read_json(tabletop) if tabletop is not None else None
        return status(load_log(log_path), tabletop=top)

    def tabletop(
        self,
        principal: Principal,
        *,
        run_id: str,
        root: Path,
        tenant: str = "default",
        restore_from: Path | None = None,
        restore_dest: Path | None = None,
        follow_up: str = "",
    ) -> dict[str, Any]:
        self._require(principal, "ops.tabletop")
        from recertia.ops.tabletop import run_tabletop

        return run_tabletop(
            run_id,
            runs_root=root,
            tenant=tenant,
            restore_from=restore_from,
            restore_dest=restore_dest,
            follow_up=follow_up,
        )

    def systems(
        self,
        principal: Principal,
        *,
        spans: Path | None = None,
        brief: bool = False,
    ) -> dict[str, Any]:
        self._require(principal, "ops.systems")
        import json

        from recertia.observability.systems import snapshot_from_events
        from recertia.telemetry import get_telemetry

        if spans is not None:
            events: list[object] = []
            for line in spans.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                payload = json.loads(line)
                if payload.get("type") == "event":
                    events.append(payload)
        else:
            events = list(get_telemetry().events)
        snap = snapshot_from_events(events)
        if brief:
            from contracts.policy import COMPUTER_USE_TASK_CLASSES
            from recertia.ops.operator_brief import brief_from_events

            payload = brief_from_events(
                events, task_classes=list(COMPUTER_USE_TASK_CLASSES)
            ).as_dict()
            payload["six_properties"] = snap.as_dict()
            return payload
        return snap.as_dict()

    def _require(self, principal: Principal, action: str) -> None:
        pdp = self._pdp
        if pdp is None:
            return
        pdp.require(principal, action, Resource(tenant_id=principal.tenant_id))
