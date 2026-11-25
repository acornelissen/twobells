"""Daily API hit budget, so the free TransportAPI allowance is never blown mid-commute."""

from __future__ import annotations

from datetime import date


class DailyBudget:
    def __init__(self, limit: int, day: date | None = None, used: int = 0) -> None:
        self.limit = limit
        self._day = day
        self._used = used

    def _used_on(self, today: date) -> int:
        return self._used if self._day == today else 0

    def remaining(self, today: date) -> int:
        return max(self.limit - self._used_on(today), 0)

    def try_spend(self, today: date, amount: int = 1) -> bool:
        if self.remaining(today) < amount:
            return False
        self._used = self._used_on(today) + amount
        self._day = today
        return True

    def to_dict(self) -> dict:
        return {"day": self._day.isoformat() if self._day else None, "used": self._used}

    @classmethod
    def from_dict(cls, data: object, limit: int) -> DailyBudget:
        """A budget as to_dict saved it, or a fresh one from nothing. Raises
        ValueError for one that isn't (hand-edited, or corrupt): a spend that
        isn't a whole number of at least 0 could give more than the allowance."""
        if data is None:
            return cls(limit)
        if not isinstance(data, dict):
            raise ValueError("a saved budget is not an object")
        day, used = data.get("day"), data.get("used", 0)
        if day is None:
            return cls(limit)
        if not isinstance(day, str):
            raise ValueError("a saved budget's day is not a date")
        if isinstance(used, bool) or not isinstance(used, int) or used < 0:
            raise ValueError("a saved budget's spend is not a whole number of at least 0")
        return cls(limit, date.fromisoformat(day), used)
