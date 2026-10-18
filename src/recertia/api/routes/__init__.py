"""Include every ARCH-5 router onto the FastAPI app."""

from __future__ import annotations

from fastapi import FastAPI

from recertia.api.deps import RouteState
from recertia.app.state import AppState


def include_routes(app: FastAPI, state: AppState) -> None:
    rs = RouteState(state)
    from recertia.api.routes.auth import register_auth_routes
    from recertia.api.routes.blobs import register_blobs_routes
    from recertia.api.routes.evals import register_evals_routes
    from recertia.api.routes.goals import register_goals_routes
    from recertia.api.routes.jobs import register_jobs_routes
    from recertia.api.routes.library import register_library_routes
    from recertia.api.routes.meta import register_meta_routes
    from recertia.api.routes.metrics import register_metrics_routes
    from recertia.api.routes.programs import register_programs_routes
    from recertia.api.routes.proposals import register_proposals_routes
    from recertia.api.routes.runs import register_runs_routes
    from recertia.api.routes.skills import register_skills_routes
    from recertia.api.routes.trajectories import register_trajectories_routes
    from recertia.api.routes.workspaces import register_workspaces_routes

    register_meta_routes(app, rs)
    register_runs_routes(app, rs)
    register_blobs_routes(app, rs)
    register_metrics_routes(app, rs)
    register_goals_routes(app, rs)
    register_skills_routes(app, rs)
    register_jobs_routes(app, rs)
    register_proposals_routes(app, rs)
    register_programs_routes(app, rs)
    register_workspaces_routes(app, rs)
    register_auth_routes(app, rs)
    register_library_routes(app, rs)
    register_trajectories_routes(app, rs)
    register_evals_routes(app, rs)
