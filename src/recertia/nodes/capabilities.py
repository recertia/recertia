"""Fields a node is not granted. Warn mode reports them and leaves the context intact."""

from __future__ import annotations

_UNDECLARED: dict[str, frozenset[str]] = {
    "intake": frozenset({"tools", "model", "applicator", "index", "reviewer"}),
    "retrieve": frozenset({"tools", "reviewer", "index"}),
    "plan": frozenset({"index", "tools"}),
    "fan_out": frozenset({"tools", "reviewer", "index"}),
    "join": frozenset({"tools", "reviewer", "index"}),
    "solve": frozenset({"reviewer", "index"}),
    "validate": frozenset({"model", "index", "reviewer"}),
    "classify_failure": frozenset({"tools", "index"}),
    "evolve": frozenset({"tools", "index"}),
    "distill": frozenset({"tools", "index"}),
    "review": frozenset({"tools", "index"}),
    "store": frozenset({"tools", "model"}),
    "record_dead_end": frozenset({"tools", "index", "reviewer"}),
    "reject_draft": frozenset({"tools", "index"}),
    "finalize": frozenset({"tools", "model", "index"}),
}


def undeclared_capabilities(node: str, ctx: object) -> list[str]:
    """Capability names ``node`` can see that the grant table does not allow."""

    granted = _UNDECLARED.get(node, frozenset())
    return [name for name in sorted(granted) if getattr(ctx, name, None) is not None]
