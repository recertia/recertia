import random

from contracts.eval import BinomialSample
from recertia.evals.statistics import anytime_valid_lift_interval, newcombe_wilson_difference


def _s(k, n):
    return BinomialSample(successes=k, trials=n)


def test_empty_arm_returns_none():
    assert anytime_valid_lift_interval(_s(0, 0), _s(1, 2)) is None


def test_wider_than_fixed_n_interval():
    t, c = _s(70, 100), _s(50, 100)
    av = anytime_valid_lift_interval(t, c)
    fx = newcombe_wilson_difference(t, c)
    assert av.low <= fx.low and av.high >= fx.high
    assert av.method == "anytime_normal_mixture"


def test_large_true_effect_is_eventually_established():
    av = anytime_valid_lift_interval(_s(4000, 5000), _s(2500, 5000))
    assert av.low > 0


def test_continuous_peeking_controls_false_positives():
    rng = random.Random(0)
    false_pos = 0
    for _ in range(200):
        kt = kc = 0
        for n in range(1, 801):
            kt += rng.random() < 0.5
            kc += rng.random() < 0.5
            if n % 10 == 0:
                iv = anytime_valid_lift_interval(_s(kt, n), _s(kc, n))
                if iv.low > 0 or iv.high < 0:
                    false_pos += 1
                    break
    assert false_pos / 200 <= 0.05
