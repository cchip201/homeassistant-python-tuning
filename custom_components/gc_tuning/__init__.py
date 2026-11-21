"""gc_tuning: observe and tune CPython's cyclic garbage collector inside Home Assistant Core.

Python 3.14's generational collector pauses the whole process for 2-3 s on every full (generation 2)
collection of a large instance. After start-up this integration freezes the start-up heap and raises the
generation-0 threshold, so full collections skip the objects that live as long as Home Assistant does.
Since 1.1.0 it also keeps that frozen heap small: a daily unfreeze + collect + freeze, a periodic re-freeze,
and a full collection at least every N minutes. Each operation fires a `gc_tuning_operation` event.

Set up from the UI (Settings > Devices & services > Add integration). A `gc_tuning:` YAML block of the 0.1.0
version is imported once; after that the entry holds the settings and the YAML block can be removed.
"""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, issue_registry as ir
from homeassistant.helpers.typing import ConfigType
from homeassistant.util.hass_dict import HassKey

from . import tuner as tuner_module
from .const import (
    CONF_ENABLED,
    CONF_FREEZE,
    CONF_LOG_MIN_MS,
    CONF_THRESHOLD0,
    CONF_THRESHOLD1,
    CONF_THRESHOLD2,
    DEFAULT_OPTIONS,
    DOMAIN,
    ISSUE_DEPRECATED_YAML,
    SERVICE_APPLY,
    SERVICE_COLLECT,
    SERVICE_FREEZE,
    SERVICE_MAINTAIN,
    SERVICE_STATS,
    SERVICE_UNFREEZE,
    YAML_AUTO_APPLY,
    YAML_DELAY,
)
from .coordinator import GcCoordinator
from .options import normalize_options
from .runtime import DomainData, GcRuntime
from .tuner import GcTuner

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]
DATA_GC: HassKey[DomainData] = HassKey(DOMAIN)

type GcTuningConfigEntry = ConfigEntry[GcRuntime]

# The 0.1.0 YAML schema, with its old ranges, so an existing block always validates; `yaml_to_options` bounds it on import.
CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(YAML_AUTO_APPLY, default=False): cv.boolean,
                vol.Optional(CONF_FREEZE, default=True): cv.boolean,
                vol.Optional(CONF_THRESHOLD0, default=DEFAULT_OPTIONS[CONF_THRESHOLD0]): vol.All(vol.Coerce(int), vol.Range(min=700, max=1_000_000)),
                vol.Optional(CONF_THRESHOLD1, default=DEFAULT_OPTIONS[CONF_THRESHOLD1]): vol.All(vol.Coerce(int), vol.Range(min=1, max=1000)),
                vol.Optional(CONF_THRESHOLD2, default=DEFAULT_OPTIONS[CONF_THRESHOLD2]): vol.All(vol.Coerce(int), vol.Range(min=1, max=1000)),
                vol.Optional(YAML_DELAY, default=300): vol.All(vol.Coerce(int), vol.Range(min=0, max=3600)),
                vol.Optional(CONF_LOG_MIN_MS, default=500): vol.All(vol.Coerce(int), vol.Range(min=1, max=60000)),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)

STATS_SCHEMA = vol.Schema({vol.Optional("recent", default=20): vol.Coerce(int)})


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the services; import a YAML block once; point at the block if it is still there."""
    hass.data.setdefault(DATA_GC, DomainData())
    _register_services(hass)

    if DOMAIN in config:
        if not hass.config_entries.async_entries(DOMAIN):
            hass.async_create_task(
                hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_IMPORT}, data=config[DOMAIN])
            )
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_DEPRECATED_YAML,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_DEPRECATED_YAML,
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_DEPRECATED_YAML)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: GcTuningConfigEntry) -> bool:
    """Install the observer now; the tuning itself waits for the start-up delay."""
    domain_data = hass.data.setdefault(DATA_GC, DomainData())
    if domain_data.original_thresholds is None:
        t0, t1, t2 = tuner_module.gc.get_threshold()
        domain_data.original_thresholds = (t0, t1, t2)

    opts = normalize_options(entry.options)
    tuner = GcTuner(
        _LOGGER,
        freeze=bool(opts[CONF_FREEZE]),
        thresholds=(int(opts[CONF_THRESHOLD0]), int(opts[CONF_THRESHOLD1]), int(opts[CONF_THRESHOLD2])),
        log_min_ms=float(opts[CONF_LOG_MIN_MS]),
    )
    tuner.install_callback()
    coordinator = GcCoordinator(hass, entry, tuner)
    runtime = GcRuntime(hass, entry, tuner, coordinator, domain_data)
    entry.runtime_data = runtime
    domain_data.runtime = runtime
    _LOGGER.info("gc observer installed; enabled=%s, thresholds now %s", opts[CONF_ENABLED], tuner_module.gc.get_threshold())

    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    entry.async_on_unload(runtime.start())
    return True


async def _async_options_updated(hass: HomeAssistant, entry: GcTuningConfigEntry) -> None:
    """New options take effect at once; the entry is not reloaded and Home Assistant is not restarted."""
    entry.runtime_data.reconcile()


async def async_unload_entry(hass: HomeAssistant, entry: GcTuningConfigEntry) -> bool:
    """Give the process its own collector settings back."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    entry.runtime_data.stop()
    domain_data = hass.data.get(DATA_GC)
    if domain_data is not None:
        domain_data.runtime = None
    return True


def _runtime(hass: HomeAssistant) -> GcRuntime:
    domain_data = hass.data.get(DATA_GC)
    if domain_data is None or domain_data.runtime is None:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_loaded")
    return domain_data.runtime


@callback
def _register_services(hass: HomeAssistant) -> None:
    """The five services of 0.1.0 and `maintain` (1.1.0), all returning a response; services.yaml describes them."""

    @callback
    def _svc_apply(call: ServiceCall) -> ServiceResponse:
        return _runtime(hass).apply_now()

    @callback
    def _svc_freeze(call: ServiceCall) -> ServiceResponse:
        runtime = _runtime(hass)
        result = runtime.tuner.freeze()
        runtime.publish()
        return result

    @callback
    def _svc_unfreeze(call: ServiceCall) -> ServiceResponse:
        runtime = _runtime(hass)
        result = runtime.tuner.unfreeze()
        runtime.publish()
        return result

    @callback
    def _svc_collect(call: ServiceCall) -> ServiceResponse:
        return _runtime(hass).tuner.collect()

    @callback
    def _svc_maintain(call: ServiceCall) -> ServiceResponse:
        runtime = _runtime(hass)
        result = runtime.tuner.maintain()
        runtime.publish()
        return result

    @callback
    def _svc_stats(call: ServiceCall) -> ServiceResponse:
        return _runtime(hass).tuner.stats(recent=int(call.data.get("recent", 20)))

    for name, fn, schema in (
        (SERVICE_APPLY, _svc_apply, None),
        (SERVICE_FREEZE, _svc_freeze, None),
        (SERVICE_UNFREEZE, _svc_unfreeze, None),
        (SERVICE_COLLECT, _svc_collect, None),
        (SERVICE_MAINTAIN, _svc_maintain, None),
        (SERVICE_STATS, _svc_stats, STATS_SCHEMA),
    ):
        hass.services.async_register(DOMAIN, name, fn, schema=schema, supports_response=SupportsResponse.OPTIONAL)  # pyright: ignore[reportArgumentType]
