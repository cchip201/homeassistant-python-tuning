"""Config flow: one instance, YAML import of the 0.1.0 `gc_tuning:` block, options with a reset to the proven defaults."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

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
    DOMAIN,
    TITLE,
)
from .options import normalize_options, yaml_to_options

_LOGGER = logging.getLogger(__name__)


def _number(key: str, unit: str | None = None) -> NumberSelector:
    """A box input that accepts only whole numbers inside the bounds of `key`."""
    low, high = BOUNDS[key]
    config = NumberSelectorConfig(min=low, max=high, step=1, mode=NumberSelectorMode.BOX)
    if unit is not None:
        config["unit_of_measurement"] = unit
    return NumberSelector(config)


def settings_schema() -> vol.Schema:
    """The form of the options flow (values are filled in as suggestions)."""
    return vol.Schema(
        {
            vol.Required(CONF_ENABLED): BooleanSelector(),
            vol.Required(CONF_START_DELAY): _number(CONF_START_DELAY, "s"),
            vol.Required(CONF_THRESHOLD0): _number(CONF_THRESHOLD0),
            vol.Required(CONF_THRESHOLD1): _number(CONF_THRESHOLD1),
            vol.Required(CONF_THRESHOLD2): _number(CONF_THRESHOLD2),
            vol.Required(CONF_FREEZE): BooleanSelector(),
            vol.Required(CONF_LOG_MIN_MS): _number(CONF_LOG_MIN_MS, "ms"),
        }
    )


class GcTuningConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create the single entry, by hand or from the old YAML block."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """A confirmation only: the proven defaults are the starting point, every value is an option afterwards."""
        if user_input is not None:
            return self.async_create_entry(title=TITLE, data={}, options=dict(DEFAULT_OPTIONS))
        return self.async_show_form(step_id="user")

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        """Create the entry from the 0.1.0 YAML block (`auto_apply`, `delay`, `threshold0..2`, `freeze`, `log_min_ms`)."""
        options, clamped = yaml_to_options(import_data)
        if clamped:
            _LOGGER.warning("gc_tuning YAML values outside the allowed range were pulled to the nearest bound: %s", ", ".join(clamped))
        return self.async_create_entry(title=TITLE, data={}, options=options)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> GcTuningOptionsFlow:
        """Options: change the settings or reset them."""
        return GcTuningOptionsFlow()


class GcTuningOptionsFlow(OptionsFlow):
    """A menu: change the settings, or reset every value to the proven defaults. Applied live, no restart."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Menu."""
        return self.async_show_menu(step_id="init", menu_options=["settings", "reset"])

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The values. The selector already bounds each number; whole numbers are stored."""
        if user_input is not None:
            return self.async_create_entry(data=normalize_options(user_input))
        return self.async_show_form(
            step_id="settings",
            data_schema=self.add_suggested_values_to_schema(settings_schema(), normalize_options(self.config_entry.options)),  # pyright: ignore[reportArgumentType]
        )

    async def async_step_reset(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Confirm, then store the proven defaults."""
        if user_input is not None:
            return self.async_create_entry(data=dict(DEFAULT_OPTIONS))
        return self.async_show_form(
            step_id="reset",
            description_placeholders={
                "defaults": (
                    f"enabled, start delay {DEFAULT_OPTIONS[CONF_START_DELAY]} s, thresholds "
                    f"{DEFAULT_OPTIONS[CONF_THRESHOLD0]} / {DEFAULT_OPTIONS[CONF_THRESHOLD1]} / {DEFAULT_OPTIONS[CONF_THRESHOLD2]}, "
                    "freeze on"
                )
            },
        )
