"""A tenant listing must not return another tenant's observations."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from contracts.eval import EvalObservation
from recertia.evals.store import EvalStore


def _obs(run_id: str) -> EvalObservation:
    return EvalObservation(
        run_id=run_id,
        task_class="repo-chore",
        arm="treatment",
        snapshot_id="s1",
        first_attempt_success=True,
        is_eval_fixture=False,
        recorded_at=datetime.now(timezone.utc),
        evidence_hash=f"hash-{run_id}",
    )


def test_list_observations_hides_other_tenants(tmp_path: Path) -> None:
    store = EvalStore(tmp_path / "evals.db")
    store._append_observation(_obs("acme-run"), tenant_id="acme")
    store._append_observation(_obs("beta-run"), tenant_id="beta")
    listed = store.list_observations(tenant_id="acme")
    assert [row.run_id for row in listed] == ["acme-run"]
    store.close()
