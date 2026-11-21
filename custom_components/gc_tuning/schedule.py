"""Pure helpers for the upkeep schedule (no Home Assistant imports).

The wall-clock grid of the periodic re-freeze follows the design of GC Manager (bakerkj/ha-gc-manager, MIT): the
interval is rounded to a divisor of 24 hours and the slots are anchored to the daily maintenance time, so the runs
stay evenly spaced whenever Home Assistant was started. The code here is written for this integration.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

DEFAULT_DAILY_TIME = "04:30:00"
REFREEZE_DIVISORS_OF_24 = (1, 2, 3, 4, 6, 8, 12, 24)


def round_refreeze_interval(hours: int) -> int:
    """Round to a divisor of 24 so the slots tile the day. 0 or less is off, 24 or more is once a day, ties go down."""
    if hours <= 0:
        return 0
    if hours >= 24:
        return 24
    return min(REFREEZE_DIVISORS_OF_24, key=lambda d: (abs(d - hours), d))


def parse_hms(value: Any, default: str = DEFAULT_DAILY_TIME) -> tuple[int, int, int]:
    """`HH:MM` or `HH:MM:SS` to (h, m, s); anything unusable falls back to `default`."""
    for candidate in (value, default):
        try:
            parts = str(candidate or "").strip().split(":")
            if not 2 <= len(parts) <= 3:
                continue
            numbers = [int(p) for p in parts] + [0]
            hour, minute, second = numbers[0], numbers[1], numbers[2] if len(parts) == 3 else 0
        except ValueError:
            continue
        if 0 <= hour < 24 and 0 <= minute < 60 and 0 <= second < 60:
            return hour, minute, second
    return 0, 0, 0  # unreachable while `default` is valid


def format_hms(hms: tuple[int, int, int]) -> str:
    """(h, m, s) as `HH:MM:SS`."""
    return f"{hms[0]:02d}:{hms[1]:02d}:{hms[2]:02d}"


def refreeze_slots(base: tuple[int, int, int], interval_hours: int, maintenance_enabled: bool) -> list[tuple[int, int, int]]:
    """Local (h, m, s) of the re-freeze slots: a grid of the rounded interval anchored at the maintenance time.

    The anchor slot itself is left out while the daily maintenance runs there (it does the same work and more).
    """
    step = round_refreeze_interval(interval_hours)
    if step == 0:
        return []
    slots: list[tuple[int, int, int]] = []
    for k in range(24 // step):
        if k == 0 and maintenance_enabled:
            continue
        slots.append(((base[0] + k * step) % 24, base[1], base[2]))
    return slots


def refreeze_min_gap(interval_hours: int) -> timedelta:
    """Half the rounded interval: a re-freeze closer than that to the previous freeze is skipped."""
    return timedelta(hours=round_refreeze_interval(interval_hours) / 2)


def next_daily(now: datetime, hms: tuple[int, int, int]) -> datetime:
    """The next occurrence of local time `hms` strictly after `now` (naive wall clock of `now`'s zone)."""
    candidate = now.replace(hour=hms[0], minute=hms[1], second=hms[2], microsecond=0)
    return candidate if candidate > now else candidate + timedelta(days=1)
