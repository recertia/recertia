"""Library reads and writes: skills, facts, proposals, policy, blobs, ledger."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from recertia.app.authz import Resource
from recertia.errors import ConfigError, Forbidden, NotFound
from recertia.ports.identity import Principal


class LibraryService:
    """``pdp.require`` then the existing stores. Routes do not call SkillStore."""

    def __init__(self, *, pdp: Any | None = None) -> None:
        self._pdp = pdp

    def list_skills(
        self,
        principal: Principal,
        *,
        store: Any,
        eval_store: Any | None = None,
        task_class: str | None = None,
        lifecycle: str | None = None,
        active: bool | None = None,
    ) -> list[dict[str, Any]]:
        self._require(principal, "skills.read")
        from recertia.memory.procedural.live_mix import live_mix_view

        streaks: dict[tuple[str, int], int] = {}
        if eval_store is not None:
            streaks = eval_store.field_failure_streaks()
        items: list[dict[str, Any]] = []
        for ver, status, stats in store.iter_loaded():
            if task_class and ver.task_class != task_class:
                continue
            if lifecycle and status.lifecycle != lifecycle:
                continue
            if active is not None and status.active != active:
                continue
            streak = streaks.get((ver.skill_id, ver.version), 0)
            items.append(
                {
                    "skill_id": ver.skill_id,
                    "version": ver.version,
                    "title": ver.title,
                    "task_class": ver.task_class,
                    "lifecycle": status.lifecycle,
                    "active": status.active,
                    "live_mix": live_mix_view(
                        ver, status, stats, consecutive_field_failures=streak
                    ),
                    "contribution": stats.contribution.model_dump(mode="json")
                    if stats.contribution
                    else None,
                }
            )
        return items

    def get_skill(
        self,
        principal: Principal,
        *,
        store: Any,
        skill_id: str,
        version: int,
        eval_store: Any | None = None,
        include_identity: bool = False,
    ) -> dict[str, Any]:
        self._require(principal, "skills.read")
        try:
            ver = store.get_version(skill_id, version)
            status = store.get_status(skill_id, version)
            stats = store.get_stats(skill_id, version)
        except FileNotFoundError as exc:
            raise NotFound(f"skill not found: {skill_id}@{version}") from exc
        except Exception as exc:
            raise NotFound("skill not found") from exc
        payload: dict[str, Any] = {
            "version": ver.model_dump(mode="json"),
            "status": status.model_dump(mode="json"),
            "stats": stats.model_dump(mode="json"),
        }
        if include_identity:
            from recertia.memory.procedural.apply_diversity import skill_identity
            from recertia.memory.procedural.live_mix import live_mix_view

            streak = 0
            if eval_store is not None:
                streak = eval_store.field_failure_streaks().get((skill_id, version), 0)
            payload["identity"] = skill_identity(ver, stats)
            payload["live_mix"] = live_mix_view(
                ver, status, stats, consecutive_field_failures=streak
            )
        return payload

    def search_skills(
        self,
        principal: Principal,
        *,
        store: Any,
        index: Any,
        query: str,
        limit: int = 5,
        workdir: Path | None = None,
        env_fingerprint: dict[str, str] | None = None,
        explain: bool = False,
    ) -> dict[str, Any]:
        self._require(principal, "skills.search")
        if workdir is None:
            try:
                entries = list(store.iter_loaded())
                index.rebuild(entries, library_fingerprint=store.library_fingerprint())
                hits = index.lexical_top_k(query, limit)
            finally:
                if hasattr(index, "close"):
                    index.close()
            return {
                "query": query,
                "hits": [
                    {"skill_id": sid, "version": ver, "score": score}
                    for sid, ver, score in hits
                ],
            }
        from recertia.retrieval.pipeline import Retriever

        try:
            fingerprint = store.library_fingerprint()
            if not index.is_fresh(fingerprint):
                index.rebuild(store.iter_loaded(), library_fingerprint=fingerprint)
            retriever = Retriever(index)
            bundle, explanation = retriever.search(
                query, workdir=workdir, env_fingerprint=env_fingerprint or {}
            )
        finally:
            if hasattr(index, "close"):
                index.close()
        return {
            "bundle": bundle,
            "explanation": explanation,
            "explain": explain,
        }

    def lint_skills(self, principal: Principal, *, store: Any) -> dict[str, list[str]]:
        self._require(principal, "skills.read")
        from recertia.memory.procedural.lint import lint_store

        return lint_store(store)

    def list_facts(self, principal: Principal, *, store: Any, scope: str | None = None) -> list[Any]:
        self._require(principal, "skills.read")
        return store.list_facts(scope=scope)

    def list_cases(self, principal: Principal, *, store: Any) -> list[Any]:
        self._require(principal, "skills.read")
        return store.list_index()

    def get_case(self, principal: Principal, *, store: Any, case_id: str) -> Any:
        self._require(principal, "skills.read")
        rec = store.get_by_case_id(case_id)
        if rec is None:
            raise NotFound("case not found")
        return rec

    def list_affordances(self, principal: Principal, *, store: Any) -> dict[str, Any]:
        self._require(principal, "skills.read")
        return {
            "tools": [
                {
                    "tool": t.tool,
                    "invocations": t.invocations,
                    "failures": t.failures,
                    "failure_rate": t.failure_rate,
                }
                for t in store.tools.values()
            ],
            "resources": [
                {
                    "kind": r.kind,
                    "id": r.id,
                    "conflicts": r.conflicts,
                    "timeouts": r.timeouts,
                }
                for r in store.resources.values()
            ],
        }

    def query_memory(
        self,
        principal: Principal,
        *,
        query: str,
        skills_root: Path,
        facts_root: Path,
        episodic_root: Path,
        index_path: Path,
        workdir: Path,
        env_fingerprint: dict[str, str] | None = None,
        limit: int = 8,
        affordance_path: Path | None = None,
        tenant_id: str | None = None,
        tenant_root: Path | None = None,
    ) -> dict[str, Any]:
        self._require(principal, "skills.search")
        from recertia.memory.query import federated_query

        kwargs: dict[str, Any] = {
            "skills_root": skills_root,
            "facts_root": facts_root,
            "episodic_root": episodic_root,
            "index_path": index_path,
            "workdir": workdir,
            "env_fingerprint": env_fingerprint,
            "limit": limit,
            "affordance_path": affordance_path,
        }
        if tenant_id is not None:
            kwargs["tenant_id"] = tenant_id
        if tenant_root is not None:
            kwargs["tenant_root"] = tenant_root
        return federated_query(query, **kwargs)

    def list_proposals(
        self,
        principal: Principal,
        *,
        store: Any,
        tenant_id: str,
        status: str | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> list[Any]:
        self._require(principal, "proposals.read")
        return store.list(tenant_id=tenant_id, status=status, kind=kind, limit=limit)

    def get_proposal(
        self, principal: Principal, *, store: Any, proposal_id: str, tenant_id: str
    ) -> Any:
        self._require(principal, "proposals.read")
        rec = store.get(proposal_id, tenant_id=tenant_id)
        if rec is None:
            raise NotFound("proposal not found")
        return rec

    def decide_proposal(
        self,
        principal: Principal,
        *,
        store: Any,
        proposal_id: str,
        tenant_id: str,
        decision: str,
        actor: str,
        note: str = "",
        ledger: Any | None = None,
        console_may_review: bool | None = None,
        kind_label: str = "proposal_decision",
    ) -> Any:
        self._require(principal, "proposals.decide")
        rec = store.get(proposal_id, tenant_id=tenant_id)
        if rec is None:
            raise NotFound("proposal not found")
        pdp = self._pdp
        if pdp is not None and not pdp.may_decide_proposal(
            principal,
            kind=rec.kind,
            payload=rec.payload,
            console_may_review=console_may_review,
        ):
            if console_may_review is not None:
                raise Forbidden("T2 requires reviewer")
            raise Forbidden("T2 requires admin key or reviewer")
        try:
            updated = store.decide(
                proposal_id,
                tenant_id=tenant_id,
                decision=decision,
                actor=actor,
                note=note,
            )
        except KeyError as exc:  # the row vanished after the read above
            raise NotFound("proposal not found") from exc
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        if ledger is not None:
            ledger.append(
                actor=actor,
                action="policy_change",
                target=f"proposal:{proposal_id}",
                evidence={
                    "kind": kind_label,
                    "decision": decision,
                    "note": note,
                    "proposal_kind": rec.kind,
                },
            )
        return updated

    def list_reviews(
        self,
        principal: Principal,
        *,
        store: Any,
        tenant_id: str,
        status: str = "pending",
        limit: int = 50,
    ) -> dict[str, Any]:
        items = self.list_proposals(
            principal, store=store, tenant_id=tenant_id, status=status, limit=limit
        )
        return {"alias_of": "proposals", "items": [p.to_dict() for p in items]}

    def decide_review(
        self,
        principal: Principal,
        *,
        store: Any,
        decision_id: str,
        tenant_id: str,
        decision: str,
        actor: str,
        note: str = "",
        ledger: Any | None = None,
        console_may_review: bool | None = None,
    ) -> Any:
        if decision not in {"approve", "reject", "request_changes"}:
            raise ConfigError("approve|reject|request_changes")
        rec = self.get_proposal(
            principal, store=store, proposal_id=decision_id, tenant_id=tenant_id
        )
        pdp = self._pdp
        self._require(principal, "proposals.decide")
        if pdp is not None and not pdp.may_decide_proposal(
            principal,
            kind=rec.kind,
            payload=rec.payload,
            console_may_review=console_may_review,
        ):
            if console_may_review is not None:
                raise Forbidden("T2 requires reviewer")
            raise Forbidden("T2 requires admin key or reviewer")
        try:
            updated = store.decide(
                decision_id,
                tenant_id=tenant_id,
                decision=decision,
                actor=actor,
                note=note,
            )
        except KeyError as exc:  # the row vanished after the read above
            raise NotFound("proposal not found") from exc
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        if ledger is not None:
            ledger.append(
                actor=actor,
                action="policy_change",
                target=f"review:{decision_id}",
                evidence={
                    "kind": "review_decision",
                    "decision": decision,
                    "lifecycle_approved": False,
                },
            )
        return updated

    def get_policy(self, principal: Principal, *, path: Path | None = None) -> dict[str, Any]:
        self._require(principal, "policy.read")
        from recertia.policy_load import load_policy

        return load_policy(path).model_dump(mode="json")

    def propose_policy(
        self,
        principal: Principal,
        *,
        store: Any,
        tenant_id: str,
        policy_diff: dict[str, Any],
        rationale: str,
        actor: str,
        ledger: Any | None = None,
        created_by_job: str = "policy-http",
        extra_payload: dict[str, Any] | None = None,
    ) -> Any:
        self._require(principal, "policy.propose")
        from recertia.proposals.store import ProposalRecord

        payload: dict[str, Any] = {
            "policy_diff": policy_diff,
            "tier": "T2",
            "applied": False,
        }
        if extra_payload:
            payload.update(extra_payload)
        rec = ProposalRecord(
            proposal_id=uuid4().hex[:12],
            kind="policy",
            skill_id="policy",
            version=0,
            rationale=rationale or "T2 policy proposal",
            payload=payload,
            tenant_id=tenant_id,
            created_by_job=created_by_job,
        )
        store.add(rec)
        if ledger is not None:
            ledger.append(
                actor=actor,
                action="policy_change",
                target=f"proposal:{rec.proposal_id}",
                evidence={"kind": "policy_proposal", "applied": False},
            )
        return rec

    def verify_ledger(
        self,
        principal: Principal,
        *,
        ledger: Any,
        check_sig: bool = False,
        full: bool | None = None,
    ) -> Any:
        self._require(principal, "ledger.verify")
        kwargs: dict[str, Any] = {"check_sig": check_sig}
        if full is not None:
            kwargs["full"] = full
        return ledger.verify(**kwargs)

    def preview_goal(self, principal: Principal, *, goal: Any) -> dict[str, Any]:
        self._require(principal, "skills.read")
        from contracts.goal import compile_goal

        criteria = compile_goal(goal)
        return {
            "goal": goal.model_dump(mode="json"),
            "criteria": [c.model_dump(mode="json") for c in criteria],
        }

    def suggest_goal(
        self,
        principal: Principal,
        *,
        context: str,
        task_class: str = "repo-chore",
        use_model: bool = True,
    ) -> dict[str, Any]:
        self._require(principal, "skills.read")
        from recertia.console_compose import suggest_criteria

        result = suggest_criteria(
            context=context, task_class=task_class, use_model=use_model
        )
        payload = result.to_dict()
        payload["blocked"] = any(w["severity"] == "block" for w in payload["warnings"])
        return payload

    def probe_goal(self, principal: Principal, *, workdir: Path) -> dict[str, Any]:
        self._require(principal, "skills.read")
        from recertia.programs.probe import probe_workdir

        return {"probe": probe_workdir(workdir), "locked": False}

    def list_templates(self, principal: Principal) -> dict[str, Any]:
        self._require(principal, "skills.read")
        from recertia.console_templates import list_templates

        return {"templates": list_templates()}

    def get_template(self, principal: Principal, template_id: str) -> dict[str, Any]:
        self._require(principal, "skills.read")
        from recertia.console_templates import get_template_goal

        try:
            goal = get_template_goal(template_id)
        except KeyError as exc:
            raise NotFound("template not found") from exc
        return {"id": template_id, "goal": goal.model_dump(mode="json")}

    def tower_summary(
        self,
        principal: Principal,
        *,
        skill_store: Any,
        eval_store: Any | None,
        proposals: Any,
        tenant_id: str,
    ) -> dict[str, Any]:
        self._require(principal, "skills.read")
        from recertia.evals.metrics import build_metric_report
        from recertia.memory.procedural.active_set import recompute_active_set
        from recertia.memory.procedural.composition import mean_composition_depth
        from recertia.review.autonomy_config import DEFAULT_AUTONOMY

        _u, pressure = recompute_active_set(skill_store, config=DEFAULT_AUTONOMY)
        mean_pressure = sum(pressure.values()) / len(pressure) if pressure else 0.0
        practice_conversion = None
        unavailable = None
        if eval_store is not None:
            rows = eval_store.metric_rows()
            report = build_metric_report(
                rows,
                snapshot_id="tower",
                active_cap_pressure=mean_pressure,
                mean_composition_depth=mean_composition_depth(skill_store),
            )
            practice_conversion = report.practice_conversion
            unavailable = report.unavailable.get("practice_conversion")
        pending = proposals.list(tenant_id=tenant_id, status="pending", limit=100)
        return {
            "active_cap_pressure": mean_pressure,
            "pressure_by_class": pressure,
            "mean_composition_depth": mean_composition_depth(skill_store),
            "practice_conversion": practice_conversion,
            "practice_conversion_unavailable": unavailable,
            "pending_proposals": len(pending),
        }

    def list_models(self, principal: Principal) -> dict[str, Any]:
        self._require(principal, "skills.read")
        from recertia.solver.model_allowlist import load_model_allowlist

        return {"models": list(load_model_allowlist())}

    def import_trajectory(
        self,
        principal: Principal,
        *,
        body: dict[str, Any],
        runs_root: Path,
        tenant_id: str,
        actor: str,
    ) -> dict[str, Any]:
        self._require(principal, "skills.read")
        from recertia.trajectory.import_store import ImportRejected, ingest_trajectory

        try:
            result = ingest_trajectory(
                body, runs_root=runs_root, tenant_id=tenant_id, actor=actor
            )
        except (ImportRejected, ValueError) as exc:
            raise ConfigError(str(exc)) from exc
        return result.as_public_dict()

    def distill_trajectory(
        self,
        principal: Principal,
        *,
        store: Any,
        trajectory: Any,
        actor: str,
        task_class: str,
        from_file: Path | None = None,
    ) -> dict[str, Any]:
        self._require(principal, "skills.read")
        from pydantic import ValidationError

        from recertia.distill.imported import DistillRejected, as_public_dict, distill_imported

        try:
            if from_file is not None:
                from recertia.distill.imported import distill_imported_file

                version = distill_imported_file(
                    from_file, store, actor=actor, task_class=task_class
                )
            else:
                from contracts.trajectory_import import TrajectoryImport

                imported = (
                    trajectory
                    if hasattr(trajectory, "events")
                    else TrajectoryImport.model_validate(trajectory)
                )
                version = distill_imported(
                    imported, store, actor=actor, task_class=task_class
                )
        except (DistillRejected, ValidationError, ValueError) as exc:
            raise ConfigError(str(exc)) from exc
        return as_public_dict(version, store)

    def put_blob(
        self,
        principal: Principal,
        *,
        store: Any,
        data: bytes,
        content_type: str = "text/plain",
        max_bytes: int | None = None,
    ) -> dict[str, str]:
        self._require(principal, "blobs.write")
        if max_bytes is not None and len(data) > max_bytes:
            raise ConfigError(f"blob exceeds max size of {max_bytes} bytes")
        digest = store.put(data, content_type=content_type)
        return {"digest": digest}

    def get_blob(self, principal: Principal, *, store: Any, digest: str) -> dict[str, Any]:
        self._require(principal, "blobs.read")
        from recertia.store.blobs import normalize_blob_digest

        try:
            key = normalize_blob_digest(digest)
            data = store.get(key)
        except (FileNotFoundError, ValueError) as exc:
            raise NotFound("blob not found") from exc
        return {
            "digest": key,
            "size": len(data),
            "text": data.decode(errors="replace")[:8000],
        }

    def _require(self, principal: Principal, action: str) -> None:
        pdp = self._pdp
        if pdp is None:
            return
        pdp.require(principal, action, Resource(tenant_id=principal.tenant_id))
