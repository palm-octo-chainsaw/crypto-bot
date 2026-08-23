"""The arithmetic behind the collapse gate in data.database._is_collapse.

Pure policy, no Postgres: the caller supplies the baseline row.
"""
from datetime import datetime, timedelta, timezone

import pytest

from data.database import _is_collapse

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)


def _baseline(total: float, age_hours: float = 1.0) -> dict:
    return {"timestamp": NOW - timedelta(hours=age_hours), "total_value_usd": total}


def test_zeroed_portfolio_is_a_collapse():
    assert _is_collapse(2.64, NOW, _baseline(12000.0)) is True


def test_ordinary_drawdown_is_not():
    assert _is_collapse(9000.0, NOW, _baseline(12000.0)) is False


def test_exactly_at_the_ratio_is_allowed():
    """The gate is `<`, so a total landing exactly on the threshold passes."""
    assert _is_collapse(2400.0, NOW, _baseline(12000.0)) is False
    assert _is_collapse(2399.99, NOW, _baseline(12000.0)) is True


def test_gains_are_never_a_collapse():
    assert _is_collapse(50000.0, NOW, _baseline(1000.0)) is False


def test_no_baseline_lets_the_first_snapshot_through():
    assert _is_collapse(2.64, NOW, None) is False


def test_nonpositive_baseline_is_ignored():
    assert _is_collapse(2.64, NOW, _baseline(0.0)) is False


@pytest.mark.parametrize("age_hours, expected", [(47.9, True), (48.0, True), (48.1, False)])
def test_gate_disarms_once_the_baseline_ages_out(age_hours, expected):
    """A genuine collapse must not wedge writes forever behind an old high."""
    assert _is_collapse(100.0, NOW, _baseline(12000.0, age_hours)) is expected


def test_future_baseline_is_ignored_rather_than_wedging_writes():
    """A row dated ahead of now never ages out, so it would block writes forever."""
    assert _is_collapse(100.0, NOW, _baseline(12000.0, age_hours=-24)) is False


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_totals_are_always_rejected(bad):
    """NaN slips through every comparison and renders as '-$nan (-nan%)'."""
    assert _is_collapse(bad, NOW, _baseline(12000.0)) is True
    assert _is_collapse(bad, NOW, None) is True
