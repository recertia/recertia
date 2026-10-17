"""Deliver eval observations queued beside the finalize checkpoint.

The outbox row commits with the checkpoint. Delivery is at-least-once.
``observation_key`` is ``tenant/run_id/attempt_no``. A second delivery of the
same run is a no-op once ``EvalStore`` already holds that run.
"""

from __future__ import annotations

from contracts.run import RunState
from recertia.evals.store import EvalStore, ObservationError
from recertia.graph.store import CheckpointStore

_MAX_ATTEMPTS = 8


def drain_eval_outbox(checkpoints: CheckpointStore, eval_store: EvalStore) -> int:
    """Move pending rows into ``EvalStore``. Returns how many were marked delivered."""

    delivered = 0
    for row in checkpoints.pending_eval_outbox():
        try:
            state = RunState.model_validate_json(row.payload_json)
            eval_store.append_run(state)
        except ObservationError as exc:
            if "already has an immutable observation" in str(exc):
                checkpoints.mark_eval_outbox(row.observation_key, "delivered", attempts=row.attempts)
                delivered += 1
                continue
            _retry(checkpoints, row.observation_key, row.attempts)
            continue
        except Exception:
            _retry(checkpoints, row.observation_key, row.attempts)
            continue
        checkpoints.mark_eval_outbox(row.observation_key, "delivered", attempts=row.attempts)
        delivered += 1
    return delivered


def _retry(checkpoints: CheckpointStore, observation_key: str, attempts: int) -> None:
    nxt = attempts + 1
    status = "dead" if nxt >= _MAX_ATTEMPTS else "pending"
    checkpoints.mark_eval_outbox(observation_key, status, attempts=nxt)
