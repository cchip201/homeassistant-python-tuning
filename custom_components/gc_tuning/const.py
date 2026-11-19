"""Constants for gc_tuning: option keys, the proven defaults and the bounds the options flow enforces."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "gc_tuning"
TITLE: Final = "GC tuning"

# Option keys (config entry options). The YAML keys of the 0.1.0 module map onto these in `yaml_to_options`.
CONF_ENABLED: Final = "enabled"  # YAML: auto_apply
CONF_START_DELAY: Final = "start_delay"  # YAML: delay
CONF_THRESHOLD0: Final = "threshold0"
CONF_THRESHOLD1: Final = "threshold1"
CONF_THRESHOLD2: Final = "threshold2"
CONF_FREEZE: Final = "freeze"
CONF_LOG_MIN_MS: Final = "log_min_ms"

# YAML keys (import only).
YAML_AUTO_APPLY: Final = "auto_apply"
YAML_DELAY: Final = "delay"

# The proven fix: 300 s after start, collect, freeze, thresholds (50000, 10, 10).
DEFAULT_OPTIONS: Final[dict[str, bool | int]] = {
    CONF_ENABLED: True,
    CONF_START_DELAY: 300,
    CONF_THRESHOLD0: 50_000,
    CONF_THRESHOLD1: 10,
    CONF_THRESHOLD2: 10,
    CONF_FREEZE: True,
    CONF_LOG_MIN_MS: 500,
}

# Inclusive bounds for the integer options.
BOUNDS: Final[dict[str, tuple[int, int]]] = {
    CONF_START_DELAY: (60, 1800),
    CONF_THRESHOLD0: (700, 200_000),
    CONF_THRESHOLD1: (1, 100),
    CONF_THRESHOLD2: (1, 100),
    CONF_LOG_MIN_MS: (10, 60_000),
}

# How often the sensors read the counters (the gc callbacks themselves only record durations).
UPDATE_INTERVAL_SECONDS: Final = 60

# Services (registered once, in async_setup).
SERVICE_APPLY: Final = "apply"
SERVICE_FREEZE: Final = "freeze"
SERVICE_UNFREEZE: Final = "unfreeze"
SERVICE_COLLECT: Final = "collect"
SERVICE_STATS: Final = "stats"

ISSUE_DEPRECATED_YAML: Final = "deprecated_yaml"
