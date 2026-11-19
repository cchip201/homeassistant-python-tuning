"""Turn raw option or YAML values into a complete, bounded options dict (no Home Assistant imports)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .const import (
    BOUNDS,
    CONF_ENABLED,
    CONF_FREEZE,
    CONF_LOG_MIN_MS,
    CONF_START_DELAY,
    CONF_THRESHOLD0,
    CONF_THRESHOLD1,
    CONF_THRESHOLD2,
    DEFAULT_OPTIONS,
    YAML_AUTO_APPLY,
    YAML_DELAY,
)

Options = dict[str, bool | int]


def _clamp(key: str, value: Any, clamped: list[str] | None) -> int:
    """Return `value` as an int inside BOUNDS[key]; a value outside is pulled to the nearest bound and noted."""
    low, high = BOUNDS[key]
    try:
        number = int(value)
    except (TypeError, ValueError):
        if clamped is not None:
            clamped.append(key)
        return int(DEFAULT_OPTIONS[key])
    if number < low or number > high:
        if clamped is not None:
            clamped.append(key)
        return min(max(number, low), high)
    return number


def normalize_options(raw: Mapping[str, Any] | None, clamped: list[str] | None = None) -> Options:
    """Fill every missing key with the proven default and bound the integers."""
    raw = raw or {}
    out: Options = {}
    for key, default in DEFAULT_OPTIONS.items():
        value = raw.get(key, default)
        if key in BOUNDS:
            out[key] = _clamp(key, value, clamped)
        else:
            out[key] = bool(value)
    return out


def yaml_to_options(conf: Mapping[str, Any]) -> tuple[Options, list[str]]:
    """Map the 0.1.0 YAML keys (auto_apply, delay, threshold0..2, freeze, log_min_ms) to options.

    `auto_apply` (default false in the old YAML schema) keeps its meaning: it becomes `enabled`.
    Returns the options and the keys whose YAML value was outside the new bounds (pulled to the nearest bound).
    """
    clamped: list[str] = []
    raw: dict[str, Any] = {
        CONF_ENABLED: conf.get(YAML_AUTO_APPLY, False),
        CONF_START_DELAY: conf.get(YAML_DELAY, DEFAULT_OPTIONS[CONF_START_DELAY]),
        CONF_THRESHOLD0: conf.get(CONF_THRESHOLD0, DEFAULT_OPTIONS[CONF_THRESHOLD0]),
        CONF_THRESHOLD1: conf.get(CONF_THRESHOLD1, DEFAULT_OPTIONS[CONF_THRESHOLD1]),
        CONF_THRESHOLD2: conf.get(CONF_THRESHOLD2, DEFAULT_OPTIONS[CONF_THRESHOLD2]),
        CONF_FREEZE: conf.get(CONF_FREEZE, DEFAULT_OPTIONS[CONF_FREEZE]),
        CONF_LOG_MIN_MS: conf.get(CONF_LOG_MIN_MS, DEFAULT_OPTIONS[CONF_LOG_MIN_MS]),
    }
    return normalize_options(raw, clamped), clamped
