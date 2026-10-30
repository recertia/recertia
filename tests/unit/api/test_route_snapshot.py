"""ARCH-5 PR-5.5: registered paths stay the published HTTP surface."""

from __future__ import annotations

from pathlib import Path

from recertia.api import create_app

# Frozen from create_app on this slice. Methods are sorted; HEAD/OPTIONS omitted.
SNAPSHOT: list[tuple[str, str]] = [
    ("/console", "GET"),
    ("/console/", "GET"),
    ("/health", "GET"),
    ("/ready", "GET"),
    ("/v1/affordances", "GET"),
    ("/v1/auth/dev-login", "POST"),
    ("/v1/auth/logout", "POST"),
    ("/v1/auth/oidc/callback", "GET"),
    ("/v1/auth/oidc/login", "GET"),
    ("/v1/auth/switch-tenant", "POST"),
    ("/v1/blobs", "POST"),
    ("/v1/blobs/{digest}", "GET"),
    ("/v1/cases", "GET"),
    ("/v1/cases/{case_id}", "GET"),
    ("/v1/console/tower-summary", "GET"),
    ("/v1/evals/runs", "POST"),
    ("/v1/facts", "GET"),
    ("/v1/goals/preview", "POST"),
    ("/v1/goals/probe", "POST"),
    ("/v1/goals/suggest", "POST"),
    ("/v1/jobs", "GET"),
    ("/v1/jobs/{job_run_id}", "GET"),
    ("/v1/jobs/{job}/run", "POST"),
    ("/v1/ledger/verify", "GET"),
    ("/v1/me", "GET"),
    ("/v1/memory/query", "POST"),
    ("/v1/metrics/canary", "GET"),
    ("/v1/metrics/dashboard", "GET"),
    ("/v1/metrics/report", "GET"),
    ("/v1/models", "GET"),
    ("/v1/policy", "GET"),
    ("/v1/policy/proposals", "POST"),
    ("/v1/programs", "GET"),
    ("/v1/programs", "POST"),
    ("/v1/programs/from-pack", "POST"),
    ("/v1/programs/{program_id}", "GET"),
    ("/v1/programs/{program_id}", "PUT"),
    ("/v1/programs/{program_id}/abandon", "POST"),
    ("/v1/programs/{program_id}/accept", "POST"),
    ("/v1/programs/{program_id}/repo-binding", "POST"),
    ("/v1/programs/{program_id}/steps/{step_id}", "PATCH"),
    ("/v1/programs/{program_id}/steps/{step_id}/preview", "POST"),
    ("/v1/programs/{program_id}/steps/{step_id}/record-tip", "POST"),
    ("/v1/programs/{program_id}/steps/{step_id}/run", "POST"),
    ("/v1/programs/{program_id}/steps/{step_id}/seed-workdir", "POST"),
    ("/v1/programs/{program_id}/steps/{step_id}/skip", "POST"),
    ("/v1/proposals", "GET"),
    ("/v1/proposals/{proposal_id}", "GET"),
    ("/v1/proposals/{proposal_id}/decision", "POST"),
    ("/v1/reviews", "GET"),
    ("/v1/reviews/{decision_id}", "POST"),
    ("/v1/runs", "GET"),
    ("/v1/runs", "POST"),
    ("/v1/runs/{run_id}", "GET"),
    ("/v1/runs/{run_id}/cancel", "POST"),
    ("/v1/runs/{run_id}/events", "GET"),
    ("/v1/runs/{run_id}/resume", "POST"),
    ("/v1/runs/{run_id}/trajectory", "GET"),
    ("/v1/runs/{run_id}/transcript", "GET"),
    ("/v1/skills", "GET"),
    ("/v1/skills/search", "POST"),
    ("/v1/skills/{skill_id}/versions/{version}", "GET"),
    ("/v1/skills/{skill_id}/versions/{version}/promote", "POST"),
    ("/v1/templates", "GET"),
    ("/v1/templates/{template_id}", "GET"),
    ("/v1/trajectories/distill", "POST"),
    ("/v1/trajectories/import", "POST"),
    ("/v1/workspaces", "GET"),
    ("/v1/workspaces", "POST"),
    ("/v1/workspaces/{workspace_id}", "DELETE"),
    ("/v1/workspaces/{workspace_id}", "GET"),
    ("/v1/workspaces/{workspace_id}", "PATCH"),
]


_SKIP_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc"})


def _route_pairs(app) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path is None or not methods or path in _SKIP_PATHS:
            continue
        for method in methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            pairs.append((path, method))
    return sorted(set(pairs))


def test_route_set_matches_snapshot(tmp_path: Path) -> None:
    app = create_app(root=tmp_path / "root")
    assert _route_pairs(app) == SNAPSHOT
    assert hasattr(app.state, "recertia")
    assert not hasattr(app.state, "console_ctx")
    assert app.state.recertia.library_service is app.state.recertia.library
