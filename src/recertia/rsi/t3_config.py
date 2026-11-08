"""T3 surfaces. Improvement-plane modules must not import this module."""

ABLATION_RATE = 0.05
JUDGE_ISOLATION = True
ACTIVE_CAP_FINITENESS = True
TOOL_REGISTRY: dict[str, str] = {}


def set_ablation_rate(value: float) -> None:
    raise PermissionError("T3 ablation rate is not writable from the improvement plane")
