import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from time_decay import (  # noqa: E402
    TimeDecayError,
    calculate_ewma,
    update_ewma,
)


def test_recent_values_have_larger_influence():
    recent_high = calculate_ewma([0.5, 0.0, 1.0], 0.3)
    old_high = calculate_ewma([0.5, 1.0, 0.0], 0.3)
    assert recent_high > old_high


def test_same_input_is_deterministic():
    values = [700, 700, 3500, 650]
    assert calculate_ewma(values, 0.05) == calculate_ewma(values, 0.05)


def test_ewma_formula_and_bounds():
    assert update_ewma(10, 20, 0.3) == pytest.approx(13)
    assert update_ewma(None, 0.5, 0.3, lower_bound=0, upper_bound=1) == 0.5
    with pytest.raises(TimeDecayError, match="above"):
        update_ewma(0.5, 1.1, 0.3, lower_bound=0, upper_bound=1)


@pytest.mark.parametrize("alpha", [0, -0.1, 1.1])
def test_invalid_alpha_rejected(alpha):
    with pytest.raises(TimeDecayError, match="alpha"):
        update_ewma(1, 1, alpha)
