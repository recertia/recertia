"""ADR-0021 refusal and pairing tests. Does not establish a1."""

from contracts.eval import CausalLiftResult, ConfidenceInterval, ContributionCell, BinomialSample
from recertia.evals.contribution import (
    ContributionProtocol,
    PairRow,
    mask_bundle,
    pair_rows,
    paired_discordant_interval,
    reject_holdout,
    retirement_proposal,
    skill_claim_allowed,
)


def _protocol() -> ContributionProtocol:
    return ContributionProtocol(
        task_class="repo-chore",
        chore_ids=["c1", "c2", "c3", "c4", "c5"],
        holdout_ids=["h1"],
        skill_ids=["lint-fix"],
        shuffle_seeds=[7],
        model_pin="pin",
        criteria_hash="hash",
        tau=0.0,
        k=1,
    )


def test_secondary_cell_is_not_auto_banned() -> None:
    cell = ContributionCell(
        skill_id="lint-fix",
        stratum="repo-chore",
        order_arm="fixed",
        n_paired=8,
        n_discordant_help=6,
        n_discordant_hurt=0,
        status="established_positive",
        pathway="applied",
        estimand="itt",
        multiplicity="secondary",
    )
    assert cell.refuses_established() is False
    assert cell.multiplicity == "secondary"


def test_exploratory_cell_still_refuses() -> None:
    cell = ContributionCell(
        skill_id="unregistered",
        stratum="repo-chore",
        order_arm="fixed",
        n_paired=8,
        n_discordant_help=6,
        n_discordant_hurt=0,
        status="established_positive",
        pathway="applied",
        estimand="itt",
        multiplicity="exploratory",
    )
    assert cell.refuses_established() is True


def test_equal_discordance_includes_zero() -> None:
    interval = paired_discordant_interval(20, 4, 4)
    assert interval is not None
    assert interval.method == "newcombe_paired_discordant"
    assert interval.low <= 0 <= interval.high


def test_no_discordance_is_insufficient() -> None:
    assert paired_discordant_interval(10, 0, 0) is None


def test_holdout_blocked() -> None:
    assert reject_holdout(_protocol(), "h1") is not None
    assert reject_holdout(_protocol(), "c1") is None


def test_mask_does_not_substitute() -> None:
    masked = mask_bundle(["lint-fix", "format"], "lint-fix")
    assert masked == ["format"]
    assert "other" not in masked


def test_pairs_on_task_id() -> None:
    protocol = _protocol()
    rows = []
    for i in range(6):
        rows.append(PairRow(task_id=f"t{i}", arm="on", success=True, skill_id="lint-fix", order_arm="fixed"))
        rows.append(PairRow(task_id=f"t{i}", arm="masked", success=False, skill_id="lint-fix", order_arm="fixed"))
    cells = pair_rows(rows, protocol=protocol)
    assert cells[0].n_paired == 6
    assert cells[0].n_discordant_help == 6
    assert cells[0].protocol_hash == protocol.protocol_hash()


def test_retirement_emitter_off_while_a4_untested() -> None:
    cell = ContributionCell(
        skill_id="lint-fix",
        stratum="repo-chore",
        order_arm="fixed",
        n_paired=8,
        n_discordant_help=0,
        n_discordant_hurt=6,
        status="established_negative",
        interval=ConfidenceInterval(low=-0.5, high=-0.1, method="newcombe_paired_discordant"),
        pathway="applied",
        estimand="itt",
        multiplicity="secondary",
    )
    proposal = retirement_proposal(
        cell, other_order=None, a4_measured=False, false_pass_rate=None, tau=0.0
    )
    assert proposal is not None
    assert proposal.emitted is False
    assert proposal.writes_t3 is False


def test_class_constructor_still_parses() -> None:
    result = CausalLiftResult(
        task_class="repo-chore",
        treatment=BinomialSample(successes=1, trials=2),
        control=BinomialSample(successes=1, trials=2),
        estimate=0.0,
        interval=None,
        status="not_established",
    )
    assert result.contribution_cells == []
    assert result.library_claim_allowed() is False
    assert skill_claim_allowed(
        ContributionCell(
            skill_id="lint-fix",
            stratum="repo-chore",
            order_arm="fixed",
            n_paired=1,
            n_discordant_help=0,
            n_discordant_hurt=0,
            status="not_established",
            multiplicity="secondary",
        ),
        other_order=None,
        protocol_hash=None,
        holm_kept=set(),
    ) is False
