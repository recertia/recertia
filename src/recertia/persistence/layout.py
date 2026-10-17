"""Two layout classes. Paths only — this module does not move bytes."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from recertia.ports.identity import TenantId, tenant_id


@dataclass(frozen=True)
class RunsRootLayout:
    parent: RuntimeLayout
    tenant: TenantId | None
    runs_root: Path

    @property
    def runtime_db(self) -> Path:
        return self.runs_root / "runtime.db"

    @property
    def checkpoints_db(self) -> Path:
        return self.runs_root / "checkpoints.db"

    @property
    def operations_db(self) -> Path:
        return self.runs_root / "operations.db"

    @property
    def ledger_path(self) -> Path:
        return self.runs_root / "ledger.jsonl"

    @property
    def receipts_path(self) -> Path:
        return self.runs_root / "receipts.jsonl"

    @property
    def run_events_dir(self) -> Path:
        return self.runs_root / "run_events"

    @property
    def snapshots_dir(self) -> Path:
        return self.runs_root / "snapshots"

    @property
    def transcripts_dir(self) -> Path:
        return self.runs_root / "transcripts"

    @property
    def episodic_dir(self) -> Path:
        return self.runs_root / "episodic"

    @property
    def affordances_path(self) -> Path:
        return self.runs_root / "affordances.json"

    @property
    def skill_index_db(self) -> Path:
        return self.runs_root / "skill_index.db"

    @property
    def evals_db(self) -> Path:
        return self.runs_root / "evals.db"

    @property
    def lineage_dir(self) -> Path:
        return self.runs_root / "lineage"

    @property
    def trajectories_dir(self) -> Path:
        return self.runs_root / "trajectories"

    @property
    def audited_states_dir(self) -> Path:
        return self.runs_root / "audited_states"

    @property
    def job_quota_path(self) -> Path:
        return self.runs_root / "job_quota.json"

    @property
    def offload_dir(self) -> Path:
        return self.runs_root / "offload"

    @property
    def quarantine_dir(self) -> Path:
        return self.runs_root / "quarantine"

    def run_events_path(self, run_id: str) -> Path:
        return self.run_events_dir / f"{run_id}.jsonl"

    def jobs(self) -> RunsRootLayout:
        return RunsRootLayout(parent=self.parent, tenant=self.tenant, runs_root=self.runs_root / "jobs")


@dataclass(frozen=True)
class RuntimeLayout:
    root: Path

    @property
    def control_db(self) -> Path:
        return self.root / "control.db"

    @property
    def locks_dir(self) -> Path:
        return self.root / "locks"

    @property
    def root_lock(self) -> Path:
        return self.root / ".lock"

    @property
    def api_lock(self) -> Path:
        return self.root / "api.lock"

    @property
    def api_keys_db(self) -> Path:
        return self.root / "api_keys.sqlite"

    @property
    def quotas_db(self) -> Path:
        return self.root / "quotas.sqlite"

    @property
    def proposals_db(self) -> Path:
        return self.root / "proposals.sqlite"

    @property
    def programs_db(self) -> Path:
        return self.root / "programs.sqlite"

    @property
    def job_runs_db(self) -> Path:
        return self.root / "job_runs.sqlite"

    @property
    def workspaces_db(self) -> Path:
        return self.root / "workspaces_registry.sqlite"

    @property
    def api_key_pepper_path(self) -> Path:
        return self.root / "api_key_pepper"

    def tenant(self, tenant: TenantId | str) -> RunsRootLayout:
        ident = tenant if isinstance(tenant, TenantId) else tenant_id(str(tenant))
        return RunsRootLayout(parent=self, tenant=ident, runs_root=self.root / "runs" / str(ident))

    def local_runs(self) -> RunsRootLayout:
        """CLI legacy: the runs root is the process root."""

        return RunsRootLayout(parent=self, tenant=None, runs_root=self.root)

    def tenants(self) -> Iterator[TenantId]:
        runs = self.root / "runs"
        if not runs.is_dir():
            return
        for child in sorted(runs.iterdir()):
            if child.is_dir():
                try:
                    yield tenant_id(child.name)
                except ValueError:
                    continue

    def sqlite_files(self) -> list[Path]:
        return [
            self.control_db,
            self.api_keys_db,
            self.quotas_db,
            self.proposals_db,
            self.programs_db,
            self.job_runs_db,
            self.workspaces_db,
        ]

    def blobs_dir(self, tenant: TenantId | str) -> Path:
        ident = tenant if isinstance(tenant, TenantId) else tenant_id(str(tenant))
        return self.root / "blobs" / str(ident)
