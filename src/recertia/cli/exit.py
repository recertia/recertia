"""Map ``RecertiaError`` to Typer exits (ARCH-5 A2)."""

from __future__ import annotations

from typing import NoReturn

import typer

from recertia.errors import RecertiaError

_A2 = {
    "not_found": 1,
    "invalid_config": 2,
    "invalid_input": 2,
    "unauthorized": 3,
    "forbidden": 3,
    "quota_exceeded": 4,
    "rate_limited": 4,
    "conflict": 5,
    "run_id_exists": 5,
    "idempotency_mismatch": 5,
    "busy": 6,
    "run_busy": 6,
    "idempotency_in_progress": 6,
    "lease_lost": 6,
    "execution_unavailable": 7,
    "state_corrupt": 7,
    "unavailable": 7,
}


def exit_code_for(exc: BaseException) -> int:
    """A2 table first; ``RecertiaError.exit_code`` if the code is unlisted."""

    if isinstance(exc, RecertiaError):
        mapped = _A2.get(exc.code)
        if mapped is not None:
            return mapped
        return exc.exit_code
    return 7


def raise_exit(exc: BaseException, *, message: str | None = None) -> NoReturn:
    """Echo stderr and raise ``typer.Exit`` with the A2 code."""

    if isinstance(exc, RecertiaError):
        text = message if message is not None else f"{exc.code}: {exc.message}"
        code = exit_code_for(exc)
    else:
        text = message if message is not None else str(exc)
        code = exit_code_for(exc)
    typer.echo(text, err=True)
    raise typer.Exit(code=code) from exc
