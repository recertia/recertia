"""ARCH-8: approved status is written by PromotionWriter."""

from __future__ import annotations

import inspect

from contracts.status import SkillStatus
from recertia.memory.procedural.promote import PromotionError, PromotionWriter, promote_to_approved


def test_promote_does_not_call_unchecked_write() -> None:
    source = inspect.getsource(promote_to_approved)
    assert "PromotionWriter" in source
    assert "_write_status_unchecked" not in source


def test_promotion_writer_refuses_other_lifecycles() -> None:
    status = SkillStatus(skill_id="demo", version=1, lifecycle="candidate", active=False)
    try:
        PromotionWriter(store=None).write(status)  # type: ignore[arg-type]
    except PromotionError as exc:
        assert "candidate" in str(exc)
    else:
        raise AssertionError("expected PromotionError")
