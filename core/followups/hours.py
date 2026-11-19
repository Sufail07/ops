"""Working-hours window. Default: Mon-Sat 09:30-18:30 IST. Alerts outside it are queued, never dropped."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from core.settings import Settings


def _t(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


@dataclass(frozen=True)
class WorkingHours:
    tz: ZoneInfo = ZoneInfo("Asia/Kolkata")
    start: time = time(9, 30)
    end: time = time(18, 30)
    days: tuple[int, ...] = (0, 1, 2, 3, 4, 5)  # Monday=0
    holidays: frozenset[date] = field(default_factory=frozenset)

    @classmethod
    def from_settings(cls, s: Settings, holidays: frozenset[date] = frozenset()) -> "WorkingHours":
        return cls(ZoneInfo(s.timezone), _t(s.work_start), _t(s.work_end), tuple(s.work_days), holidays)

    def local_today(self, now: datetime) -> date:
        return now.astimezone(self.tz).date()

    def _working_day(self, d: date) -> bool:
        return d.weekday() in self.days and d not in self.holidays

    def is_open(self, now: datetime) -> bool:
        loc = now.astimezone(self.tz)
        return self._working_day(loc.date()) and self.start <= loc.time() < self.end

    def next_open(self, now: datetime) -> datetime:
        """`now` if inside the window, else the start of the next working window."""
        loc = now.astimezone(self.tz)
        for offset in range(0, 15):
            d = loc.date() + timedelta(days=offset)
            if not self._working_day(d):
                continue
            start = datetime.combine(d, self.start, self.tz)
            end = datetime.combine(d, self.end, self.tz)
            if loc < start:
                return start
            if loc < end:
                return loc
        raise ValueError("no working day within 14 days; check working-hours config")
