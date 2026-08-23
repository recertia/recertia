"""Three-layer MEA activation tests."""

from __future__ import annotations

from recertia.mea.activation import resolve_mea_activation


def test_default_inactive():
    a = resolve_mea_activation()
    assert a.active is False
    assert a.fallback_reason == "policy_mea_disabled"


def test_requires_all_three_layers():
    a = resolve_mea_activation(
        policy_mea_enabled=True, goal_mea_opt_in=True, runtime_strategy="mea"
    )
    assert a.active is True
    assert a.fallback_reason is None


def test_policy_off():
    a = resolve_mea_activation(
        policy_mea_enabled=False, goal_mea_opt_in=True, runtime_strategy="mea"
    )
    assert a.active is False
    assert a.fallback_reason == "policy_mea_disabled"


def test_goal_not_opted_in():
    a = resolve_mea_activation(
        policy_mea_enabled=True, goal_mea_opt_in=False, runtime_strategy="mea"
    )
    assert a.active is False
    assert a.fallback_reason == "goal_not_opted_in"


def test_runtime_not_mea():
    a = resolve_mea_activation(
        policy_mea_enabled=True, goal_mea_opt_in=True, runtime_strategy="single"
    )
    assert a.active is False
    assert a.fallback_reason == "runtime_strategy_not_mea"
