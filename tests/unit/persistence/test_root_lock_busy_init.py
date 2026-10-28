"""``RootBusy`` initialises through ``RecertiaError`` and rejects unknown keywords (#145)."""

from __future__ import annotations

from typing import Any

import pytest

from recertia.errors import RecertiaError
from recertia.persistence.root_lock import RootBusy, RootLockBusy


def test_root_lock_busy_init_runs_the_recertia_error_base_init(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The base ``__init__`` is called once, with the message and the typed keywords."""

    calls: list[tuple[object, str, dict[str, Any]]] = []
    base_init = RecertiaError.__init__

    def spy(self: RecertiaError, message: str, **kwargs: Any) -> None:
        calls.append((self, message, kwargs))
        base_init(self, message, **kwargs)

    monkeypatch.setattr(RecertiaError, "__init__", spy)

    details = {"holder": "backup"}
    exc = RootBusy("root is busy", run_id="run-1", details=details)

    assert len(calls) == 1
    assert calls[0][0] is exc
    assert calls[0][1] == "root is busy"
    assert calls[0][2]["run_id"] == "run-1"
    assert calls[0][2]["details"] == details


def test_root_lock_busy_init_populates_every_recertia_error_field() -> None:
    details = {"holder": "backup"}
    exc = RootBusy("root is busy", run_id="run-1", details=details)

    assert exc.message == "root is busy"
    assert str(exc) == "root is busy"
    assert exc.args == ("root is busy",)
    assert exc.run_id == "run-1"
    assert exc.details == details
    assert exc.details is not details
    assert exc.error_body() == {
        "error": {
            "code": "root_busy",
            "message": "root is busy",
            "retryable": True,
            "run_id": "run-1",
            "details": {"holder": "backup"},
        }
    }


def test_root_lock_busy_init_defaults_match_the_base_class() -> None:
    exc = RootBusy("root is busy")
    base = RecertiaError("root is busy")

    assert (exc.message, exc.run_id, exc.details) == (base.message, base.run_id, base.details)
    assert exc.args == base.args


@pytest.mark.parametrize("keyword", ["bogus", "retry_after", "code", "http_status"])
def test_root_lock_busy_init_rejects_an_unknown_keyword(keyword: str) -> None:
    kwargs: dict[str, Any] = {keyword: 1}
    with pytest.raises(TypeError, match=keyword):
        RootBusy("root is busy", **kwargs)


def test_root_lock_busy_init_keeps_the_exception_hierarchy() -> None:
    """Every ``except RootLockBusy`` and ``except RootBusy`` site keeps matching."""

    assert [cls.__name__ for cls in RootBusy.__mro__[:5]] == [
        "RootBusy",
        "RootLockBusy",
        "RuntimeError",
        "RecertiaError",
        "Exception",
    ]
    exc = RootBusy("root is busy")
    assert isinstance(exc, (RootLockBusy, RuntimeError, RecertiaError))
    with pytest.raises(RootLockBusy):
        raise exc
    with pytest.raises(RuntimeError):
        raise exc
    with pytest.raises(RootBusy):
        raise exc
