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

    def try_spend(self, today: date) -> bool:
        if self.remaining(today) == 0:
            return False
        self._used = self._used_on(today) + 1
        self._day = today
        return True

    def to_dict(self) -> dict:
        return {"day": self._day.isoformat() if self._day else None, "used": self._used}

    @classmethod
    def from_dict(cls, data: dict | None, limit: int) -> "DailyBudget":
        if not data or not data.get("day"):
            return cls(limit)
        return cls(limit, date.fromisoformat(data["day"]), int(data.get("used", 0)))
