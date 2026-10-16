"""Named fault points. Active only when ``RECERTIA_ALLOW_FAULTS=1``."""

from __future__ import annotations


class FaultInjected(BaseException):
    """Raised by ``fault_point`` so ``except Exception`` handlers do not swallow it."""

    def __init__(self, name: str, action: str) -> None:
        super().__init__(f"fault {name}:{action}")
        self.name = name
        self.action = action


def fault_point(name: str) -> None:
    """No-op unless faults are explicitly enabled and this point is listed.

    A point of the form ``name:kill`` raises :class:`FaultInjected`.
    """

    from recertia.settings import load_settings

    settings = load_settings()
    if not settings.allow_faults or not settings.faults:
        return
    for spec in settings.faults.split(","):
        point, sep, action = spec.strip().partition(":")
        if not sep or point != name:
            continue
        if action == "kill":
            raise FaultInjected(name, action)
