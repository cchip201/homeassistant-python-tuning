# pyright: reportTypedDictNotRequiredAccess=false, reportIndexIssue=false, reportOptionalSubscript=false, reportArgumentType=false, reportOperatorIssue=false, reportCallIssue=false, reportReturnType=false, reportOptionalMemberAccess=false, reportPrivateImportUsage=false
"""Config flow, YAML import and options flow."""

from __future__ import annotations

from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.gc_tuning.const import DEFAULT_OPTIONS, DOMAIN, ISSUE_DEPRECATED_YAML

from .conftest import FakeGc, make_entry, setup_entry


async def test_user_flow_creates_the_entry_with_the_proven_defaults(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "GC tuning"
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.options == DEFAULT_OPTIONS
    assert entry.data == {}


async def test_only_one_instance(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    await setup_entry(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_import_flow_maps_the_old_yaml_keys(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    yaml_block = {"auto_apply": True, "freeze": False, "threshold0": 40000, "threshold1": 12, "threshold2": 9, "delay": 120, "log_min_ms": 250}
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_IMPORT}, data=yaml_block)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert hass.config_entries.async_entries(DOMAIN)[0].options == {
        "enabled": True,
        "start_delay": 120,
        "threshold0": 40000,
        "threshold1": 12,
        "threshold2": 9,
        "freeze": False,
        "log_min_ms": 250,
    }


async def test_yaml_line_of_today_is_imported_without_a_manual_step(hass: HomeAssistant, fake_gc: FakeGc, setup_yaml: Any) -> None:
    """configuration.yaml today: `gc_tuning:` + `auto_apply: true`. The entry that results is the proven fix."""
    await setup_yaml({"auto_apply": True})

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    assert entries[0].source == config_entries.SOURCE_IMPORT
    assert entries[0].options == DEFAULT_OPTIONS  # enabled, 300 s, 50000/10/10, freeze on
    assert entries[0].state is config_entries.ConfigEntryState.LOADED
    # the YAML block is redundant now: a repair issue says so
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_DEPRECATED_YAML) is not None


async def test_yaml_without_auto_apply_keeps_its_old_meaning(hass: HomeAssistant, fake_gc: FakeGc, setup_yaml: Any) -> None:
    """The 0.1.0 default was auto_apply false = observer only; the import keeps that."""
    await setup_yaml({})
    assert hass.config_entries.async_entries(DOMAIN)[0].options["enabled"] is False


async def test_yaml_does_not_overwrite_options_changed_in_the_ui(hass: HomeAssistant, fake_gc: FakeGc, setup_yaml: Any) -> None:
    entry = make_entry({"enabled": False, "threshold0": 60000})
    entry.add_to_hass(hass)

    await setup_yaml({"auto_apply": True})

    assert hass.config_entries.async_entries(DOMAIN) == [entry]
    assert entry.options["enabled"] is False
    assert entry.options["threshold0"] == 60000
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_DEPRECATED_YAML) is not None


async def test_no_yaml_no_issue(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    ir.async_create_issue(hass, DOMAIN, ISSUE_DEPRECATED_YAML, is_fixable=False, severity=ir.IssueSeverity.WARNING, translation_key=ISSUE_DEPRECATED_YAML)
    await setup_entry(hass)
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_DEPRECATED_YAML) is None


@pytest.mark.parametrize(
    ("yaml_value", "expected"),
    [
        ({"threshold0": 900_000}, {"threshold0": 200_000}),
        ({"threshold0": 700}, {"threshold0": 700}),
        ({"delay": 0}, {"start_delay": 60}),
        ({"delay": 3600}, {"start_delay": 1800}),
        ({"threshold1": 1000}, {"threshold1": 100}),
        ({"log_min_ms": 1}, {"log_min_ms": 10}),
    ],
)
async def test_yaml_values_outside_the_new_bounds_are_pulled_in(
    hass: HomeAssistant, fake_gc: FakeGc, setup_yaml: Any, yaml_value: dict[str, int], expected: dict[str, int]
) -> None:
    await setup_yaml({"auto_apply": True, **yaml_value})
    options = hass.config_entries.async_entries(DOMAIN)[0].options
    for key, value in expected.items():
        assert options[key] == value


async def _open_settings(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["settings", "reset"]
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "settings"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "settings"
    return result


async def test_options_flow_saves_whole_numbers(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    entry = await setup_entry(hass)
    result = await _open_settings(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"enabled": True, "start_delay": 120.0, "threshold0": 60000.0, "threshold1": 20, "threshold2": 15, "freeze": True, "log_min_ms": 250},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        "enabled": True,
        "start_delay": 120,
        "threshold0": 60000,
        "threshold1": 20,
        "threshold2": 15,
        "freeze": True,
        "log_min_ms": 250,
    }
    assert all(type(v) in (int, bool) for v in entry.options.values())


@pytest.mark.parametrize(
    "bad",
    [
        {"start_delay": 59},
        {"start_delay": 1801},
        {"threshold0": 699},
        {"threshold0": 200_001},
        {"threshold1": 0},
        {"threshold1": 101},
        {"threshold2": 0},
        {"threshold2": 101},
    ],
)
async def test_options_flow_refuses_values_outside_the_bounds(hass: HomeAssistant, fake_gc: FakeGc, bad: dict[str, int]) -> None:
    entry = await setup_entry(hass)
    result = await _open_settings(hass, entry)
    values = {"enabled": True, "start_delay": 300, "threshold0": 50000, "threshold1": 10, "threshold2": 10, "freeze": True, "log_min_ms": 500}
    values.update(bad)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(result["flow_id"], values)
    assert entry.options == DEFAULT_OPTIONS


async def test_options_flow_form_shows_the_current_values(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    entry = await setup_entry(hass, {"threshold0": 61000})
    result = await _open_settings(hass, entry)
    suggested = {str(k): k.description["suggested_value"] for k in result["data_schema"].schema if k.description}
    assert suggested["threshold0"] == 61000
    assert suggested["start_delay"] == 300
    assert suggested["enabled"] is True


async def test_reset_to_the_proven_defaults(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    entry = await setup_entry(hass, {"enabled": False, "start_delay": 900, "threshold0": 3000, "threshold1": 3, "threshold2": 4, "freeze": False, "log_min_ms": 20})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "reset"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reset"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == DEFAULT_OPTIONS
