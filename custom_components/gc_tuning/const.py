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
# 1.1.0: upkeep after the start-up freeze (all of it needs the heap to be frozen by this integration).
CONF_DAILY_MAINTENANCE: Final = "daily_maintenance"  # unfreeze, collect, freeze once a day
CONF_DAILY_TIME: Final = "daily_time"  # local time of that run, "HH:MM:SS"
CONF_REFREEZE_HOURS: Final = "refreeze_interval_hours"  # collect + freeze every N hours (0 = off)
CONF_REFREEZE_PAUSE_MS: Final = "refreeze_pause_ms"  # re-freeze only after a full collection this long (0 = always)
CONF_FULL_MINUTES: Final = "full_collection_minutes"  # a full collection at least this often (0 = off)

# YAML keys (import only).
YAML_AUTO_APPLY: Final = "auto_apply"
YAML_DELAY: Final = "delay"

# Generation 0 threshold: unchanged from 1.0.0. 10000 was tried live on the instance this was built for (11-20 17:23 to
# 17:29): one allocation burst ran 13 full collections of 0.73-0.91 s in 110 s; one burst is no proof, 50000 stays (changelog).
THRESHOLD0_DEFAULT: Final = 50_000

# The proven defaults of 1.1.0: the 1.0.0 fix (300 s, freeze, 50000 / 10 / 10) plus the upkeep that keeps the frozen heap small.
DEFAULT_OPTIONS: Final[dict[str, bool | int | str]] = {
    CONF_ENABLED: True,
    CONF_START_DELAY: 300,
    CONF_THRESHOLD0: THRESHOLD0_DEFAULT,
    CONF_THRESHOLD1: 10,
    CONF_THRESHOLD2: 10,
    CONF_FREEZE: True,
    CONF_LOG_MIN_MS: 500,
    CONF_DAILY_MAINTENANCE: True,
    CONF_DAILY_TIME: "04:30:00",
    CONF_REFREEZE_HOURS: 6,
    CONF_REFREEZE_PAUSE_MS: 500,
    CONF_FULL_MINUTES: 45,
}

# Inclusive bounds for the integer options.
BOUNDS: Final[dict[str, tuple[int, int]]] = {
    CONF_START_DELAY: (60, 1800),
    CONF_THRESHOLD0: (700, 200_000),
    CONF_THRESHOLD1: (1, 100),
    CONF_THRESHOLD2: (1, 100),
    CONF_LOG_MIN_MS: (10, 60_000),
    CONF_REFREEZE_HOURS: (0, 24),  # rounded to a divisor of 24 (see schedule.round_refreeze_interval)
    CONF_REFREEZE_PAUSE_MS: (0, 10_000),
    CONF_FULL_MINUTES: (0, 1440),
}

# Options where 0 means "off" and a small non-zero value would only hurt: such a value is pulled up to this minimum.
ZERO_OR_AT_LEAST: Final[dict[str, int]] = {CONF_FULL_MINUTES: 5}

# Options that are text, not numbers or switches.
TIME_OPTIONS: Final = (CONF_DAILY_TIME,)
BOOLEAN_OPTIONS: Final = (CONF_ENABLED, CONF_FREEZE, CONF_DAILY_MAINTENANCE)

# How long a read of the frozen-object count is reused. gc.get_freeze_count() walks the whole permanent generation
# (about 20 ms per 2.6 million objects on a quiet machine, several times that on a busy one) and holds the GIL.
FROZEN_COUNT_MAX_AGE_SECONDS: Final = 300.0

# How often the sensors read the counters (the gc callbacks themselves only record durations).
UPDATE_INTERVAL_SECONDS: Final = 60

# Services (registered once, in async_setup).
SERVICE_APPLY: Final = "apply"
SERVICE_FREEZE: Final = "freeze"
SERVICE_UNFREEZE: Final = "unfreeze"
SERVICE_COLLECT: Final = "collect"
SERVICE_STATS: Final = "stats"
SERVICE_MAINTAIN: Final = "maintain"

# The event fired after every operation (sensors are updated first).
EVENT_OPERATION: Final = f"{DOMAIN}_operation"

# `action` values of that event and of the "Last operation" sensor.
ACTION_STARTUP_APPLY: Final = "startup_apply"
ACTION_APPLY_SERVICE: Final = "apply_service"
ACTION_FREEZE_SERVICE: Final = "freeze_service"
ACTION_UNFREEZE_SERVICE: Final = "unfreeze_service"
ACTION_COLLECT_SERVICE: Final = "collect_service"
ACTION_MAINTAIN_SERVICE: Final = "maintain_service"
ACTION_DAILY_MAINTENANCE: Final = "daily_maintenance"
ACTION_PERIODIC_MAINTENANCE: Final = "periodic_maintenance"  # the re-freeze slot when daily maintenance is off
ACTION_PERIODIC_REFREEZE: Final = "periodic_refreeze"
ACTION_PERIODIC_COLLECTION: Final = "periodic_collection"  # "a full collection at least every N minutes"
ACTION_RETUNE: Final = "retune"
ACTION_RESTORE: Final = "restore"
MAINTENANCE_ACTIONS: Final = (ACTION_DAILY_MAINTENANCE, ACTION_PERIODIC_MAINTENANCE, ACTION_MAINTAIN_SERVICE)

# The scheduled upkeep skips a run that follows another too closely.
MIN_RESET_GAP_SECONDS: Final = 3600.0  # daily maintenance after an apply or another maintenance
FULL_COLLECTION_TICK_SECONDS: Final = 60  # how often the "full collection at least every N minutes" check runs

ISSUE_DEPRECATED_YAML: Final = "deprecated_yaml"
