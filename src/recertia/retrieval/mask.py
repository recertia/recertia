"""Drop one registered member from a frozen bundle. Do not search again."""

from __future__ import annotations

from typing import Any

from recertia.evals.contribution import mask_bundle


def drop_registered_member(skills: list[Any], skill_id: str | None) -> list[Any]:
    if not skill_id:
        return list(skills)
    kept = set(mask_bundle([str(getattr(skill, "skill_id", skill)) for skill in skills], skill_id))
    return [skill for skill in skills if str(getattr(skill, "skill_id", skill)) in kept]
