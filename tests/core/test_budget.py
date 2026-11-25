from datetime import date

import pytest

from twobells.core.budget import DailyBudget

TODAY = date(2026, 10, 8)
TOMORROW = date(2026, 10, 9)


def test_fresh_budget_has_full_allowance():
    assert DailyBudget(limit=28).remaining(TODAY) == 28


def test_spending_reduces_remaining():
    budget = DailyBudget(limit=28)
    assert budget.try_spend(TODAY)
    assert budget.remaining(TODAY) == 27


def test_spending_is_refused_once_exhausted():
    budget = DailyBudget(limit=2)
    assert budget.try_spend(TODAY)
    assert budget.try_spend(TODAY)
    assert not budget.try_spend(TODAY)
    assert budget.remaining(TODAY) == 0


def test_allowance_resets_on_a_new_day():
    budget = DailyBudget(limit=1)
    budget.try_spend(TODAY)
    assert budget.remaining(TOMORROW) == 1
    assert budget.try_spend(TOMORROW)


def test_spending_several_hits_at_once():
    budget = DailyBudget(limit=5)
    assert budget.try_spend(TODAY, amount=2)
    assert budget.remaining(TODAY) == 3


def test_multi_hit_spend_refused_when_it_would_overrun():
    budget = DailyBudget(limit=3)
    budget.try_spend(TODAY, amount=2)
    assert not budget.try_spend(TODAY, amount=2)
    assert budget.remaining(TODAY) == 1


def test_round_trips_through_dict():
    budget = DailyBudget(limit=28)
    budget.try_spend(TODAY)
    restored = DailyBudget.from_dict(budget.to_dict(), limit=28)
    assert restored.remaining(TODAY) == 27


def test_restoring_with_new_limit_keeps_usage():
    budget = DailyBudget(limit=28)
    budget.try_spend(TODAY)
    restored = DailyBudget.from_dict(budget.to_dict(), limit=10)
    assert restored.remaining(TODAY) == 9


def test_restoring_from_nothing_gives_fresh_budget():
    assert DailyBudget.from_dict(None, limit=5).remaining(TODAY) == 5
    assert DailyBudget.from_dict({"day": None, "used": 0}, limit=5).remaining(TODAY) == 5  # as a fresh one saves


@pytest.mark.parametrize("saved", [
    "x", ["2026-10-08", 1], 5,
    {"day": "2026-13-45", "used": 1}, {"day": 5, "used": 1}, {"day": "", "used": 1}, {"day": "soon"},
    {"day": "2026-10-08", "used": "NaN"}, {"day": "2026-10-08", "used": "3"}, {"day": "2026-10-08", "used": 1.5},
    {"day": "2026-10-08", "used": float("inf")}, {"day": "2026-10-08", "used": [1]},
    {"day": "2026-10-08", "used": -5}, {"day": "2026-10-08", "used": True},
])
def test_a_saved_budget_that_isnt_one_is_refused(saved):
    # A negative spend would give more than the allowance; anything else would stop the board.
    with pytest.raises(ValueError):
        DailyBudget.from_dict(saved, limit=28)
