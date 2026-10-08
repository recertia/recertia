"""Text for recertia lift. Does not change a1 status."""

from __future__ import annotations

from contracts.eval import CausalLiftResult
from recertia.evals.contribution import refused_sentences


def render_lift(result: CausalLiftResult, *, a4_measured: bool = False) -> str:
    lines = [
        f"task_class={result.task_class}",
        f"status={result.render_status()}",
        "a1=not established",
    ]
    if result.library_claim_allowed():
        lines.append("claim=class sentence allowed on this report only")
    else:
        lines.append("claim=not established")
    if not result.contribution_cells:
        lines.append("unavailable=no paired contribution cells")
    for cell in result.contribution_cells:
        lines.append(
            f"cell skill={cell.skill_id or 'class'} order={cell.order_arm} "
            f"status={cell.status} n={cell.n_paired} "
            f"help={cell.n_discordant_help} hurt={cell.n_discordant_hurt}"
        )
        other = next(
            (
                item
                for item in result.contribution_cells
                if item.skill_id == cell.skill_id and item.order_arm != cell.order_arm
            ),
            None,
        )
        for sentence in refused_sentences(
            cell, other_order=other, a4_measured=a4_measured, protocol_ok=True
        ):
            lines.append(f"refused: {sentence}")
    return "\n".join(lines) + "\n"
