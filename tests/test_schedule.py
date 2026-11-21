"""The pure upkeep helpers and the option normaliser (no Home Assistant needed)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from custom_components.gc_tuning.const import DEFAULT_OPTIONS
from custom_components.gc_tuning.options import normalize_options, yaml_to_options
from custom_components.gc_tuning.schedule import (
    format_hms,
    next_daily,
    parse_hms,
    refreeze_min_gap,
    refreeze_slots,
    round_refreeze_interval,
)


@pytest.mark.parametrize(
    ("asked", "rounded"),
    [(-3, 0), (0, 0), (1, 1), (2, 2), (3, 3), (4, 4), (5, 4), (6, 6), (7, 6), (9, 8), (10, 8), (11, 12), (12, 12), (18, 12), (23, 24), (24, 24), (99, 24)],
)
def test_the_re_freeze_interval_is_rounded_to_a_divisor_of_24_and_ties_go_to_more_often(asked: int, rounded: int) -> None:
    assert round_refreeze_interval(asked) == rounded


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("04:30:00", (4, 30, 0)),
        ("04:30", (4, 30, 0)),
        ("4:5:9", (4, 5, 9)),
        (" 23:59:59 ", (23, 59, 59)),
        ("24:00:00", (4, 30, 0)),  # out of range: the default
        ("12:61", (4, 30, 0)),
        ("noon", (4, 30, 0)),
        ("", (4, 30, 0)),
        (None, (4, 30, 0)),
        (430, (4, 30, 0)),
        ("1:2:3:4", (4, 30, 0)),
    ],
)
def test_the_daily_time_is_parsed_or_falls_back_to_the_default(raw: object, expected: tuple[int, int, int]) -> None:
    assert parse_hms(raw) == expected


def test_format_hms_is_zero_padded() -> None:
    assert format_hms((4, 5, 0)) == "04:05:00"


def test_slots_are_a_grid_anchored_at_the_maintenance_time() -> None:
    assert refreeze_slots((4, 30, 0), 6, True) == [(10, 30, 0), (16, 30, 0), (22, 30, 0)]
    assert refreeze_slots((4, 30, 0), 6, False) == [(4, 30, 0), (10, 30, 0), (16, 30, 0), (22, 30, 0)]
    assert refreeze_slots((23, 0, 0), 12, True) == [(11, 0, 0)]
    assert refreeze_slots((4, 30, 0), 24, True) == []  # once a day: the maintenance itself
    assert refreeze_slots((4, 30, 0), 24, False) == [(4, 30, 0)]
    assert refreeze_slots((4, 30, 0), 0, True) == []
    assert len(refreeze_slots((4, 30, 0), 1, False)) == 24


def test_the_slots_follow_the_rounded_interval() -> None:
    assert refreeze_slots((4, 30, 0), 5, True) == refreeze_slots((4, 30, 0), 4, True)


def test_the_minimum_gap_is_half_the_rounded_interval() -> None:
    assert refreeze_min_gap(6) == timedelta(hours=3)
    assert refreeze_min_gap(5) == timedelta(hours=2)
    assert refreeze_min_gap(0) == timedelta(0)


def test_next_daily_is_strictly_after_now() -> None:
    now = datetime(2026, 11, 20, 4, 30, 0)
    assert next_daily(now, (4, 30, 0)) == datetime(2026, 11, 21, 4, 30, 0)
    assert next_daily(datetime(2026, 11, 20, 4, 29, 59), (4, 30, 0)) == now
    assert next_daily(datetime(2026, 11, 20, 23, 0, 0), (1, 0, 0)) == datetime(2026, 11, 21, 1, 0, 0)


def test_defaults_are_what_the_changelog_says() -> None:
    assert DEFAULT_OPTIONS == {
        "enabled": True,
        "start_delay": 300,
        "threshold0": 50_000,
        "threshold1": 10,
        "threshold2": 10,
        "freeze": True,
        "log_min_ms": 500,
        "daily_maintenance": True,
        "daily_time": "04:30:00",
        "refreeze_interval_hours": 6,
        "refreeze_pause_ms": 500,
        "full_collection_minutes": 45,
    }


def test_normalise_fills_every_missing_key_with_the_default() -> None:
    assert normalize_options(None) == DEFAULT_OPTIONS
    assert normalize_options({}) == DEFAULT_OPTIONS
    old = {"enabled": True, "start_delay": 300, "threshold0": 50000, "threshold1": 10, "threshold2": 10, "freeze": True, "log_min_ms": 500}
    assert normalize_options(old) == DEFAULT_OPTIONS  # a 1.0.0 entry reads as the 1.1.0 defaults


@pytest.mark.parametrize(
    ("raw", "key", "expected"),
    [
        ({"daily_time": "3:5"}, "daily_time", "03:05:00"),
        ({"daily_time": "nonsense"}, "daily_time", "04:30:00"),
        ({"refreeze_interval_hours": 5}, "refreeze_interval_hours", 4),
        ({"refreeze_interval_hours": 30}, "refreeze_interval_hours", 24),
        ({"refreeze_interval_hours": -2}, "refreeze_interval_hours", 0),
        ({"refreeze_pause_ms": 99_999}, "refreeze_pause_ms", 10_000),
        ({"refreeze_pause_ms": 0}, "refreeze_pause_ms", 0),
        ({"full_collection_minutes": 0}, "full_collection_minutes", 0),  # off stays off
        ({"full_collection_minutes": 3}, "full_collection_minutes", 5),  # too small to be useful: pulled up
        ({"full_collection_minutes": 5}, "full_collection_minutes", 5),
        ({"full_collection_minutes": 99_999}, "full_collection_minutes", 1440),
        ({"full_collection_minutes": "x"}, "full_collection_minutes", 45),
        ({"daily_maintenance": 0}, "daily_maintenance", False),
    ],
)
def test_normalise_bounds_each_new_option(raw: dict[str, object], key: str, expected: object) -> None:
    clamped: list[str] = []
    assert normalize_options(raw, clamped)[key] == expected


def test_normalise_reports_what_it_pulled_in() -> None:
    clamped: list[str] = []
    normalize_options({"full_collection_minutes": 2, "refreeze_pause_ms": 50_000, "start_delay": 5}, clamped)
    assert sorted(clamped) == ["full_collection_minutes", "refreeze_pause_ms", "start_delay"]


def test_the_old_yaml_block_gets_the_new_defaults() -> None:
    options, clamped = yaml_to_options({"auto_apply": True})
    assert clamped == []
    assert options == DEFAULT_OPTIONS
