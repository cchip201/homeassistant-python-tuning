# pyright: reportTypedDictNotRequiredAccess=false, reportIndexIssue=false, reportOptionalSubscript=false, reportArgumentType=false, reportOperatorIssue=false, reportCallIssue=false, reportReturnType=false, reportOptionalMemberAccess=false, reportPrivateImportUsage=false
"""Diagnostics download, the icon, the manifest and the English strings."""

from __future__ import annotations

import json
from pathlib import Path
import struct

from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from homeassistant.core import HomeAssistant

from .conftest import FakeGc, setup_entry

PKG = Path(__file__).resolve().parents[1] / "custom_components" / "gc_tuning"


async def test_diagnostics_download(hass: HomeAssistant, hass_client: ClientSessionGenerator, fake_gc: FakeGc) -> None:
    entry = await setup_entry(hass)
    fake_gc.run(2, 1.0)

    data = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert data["entry"]["options"] == dict(entry.options)
    assert data["entry"]["title"] == "GC tuning"
    assert data["runtime"]["apply_pending"] is True
    assert data["runtime"]["original_thresholds"] == [2000, 10, 10]
    assert set(data["stats"]) == {"thresholds", "count_pending", "frozen", "enabled", "python_gc_stats", "observed", "recent", "applied"}
    assert data["last_hour"]["full_collections"] == 1
    assert set(data["sensors"]) == {
        "full_collections_last_hour",
        "longest_pause_last_hour",
        "last_full_collection_seconds",
        "frozen_objects",
        "thresholds",
        "applied_at",
        "process_memory_mb",
    }
    assert "python" in data["runtime"]
    json.dumps(data)  # serialisable as downloaded


async def test_diagnostics_hold_no_secret_names(hass: HomeAssistant, hass_client: ClientSessionGenerator, fake_gc: FakeGc) -> None:
    entry = await setup_entry(hass)
    text = json.dumps(await get_diagnostics_for_config_entry(hass, hass_client, entry)).lower()
    for word in ("password", "token", "api_key", "secret"):
        assert word not in text


def _png_size(path: Path) -> tuple[int, int]:
    raw = path.read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", raw[16:24])


def test_icon_files_are_square_pngs() -> None:
    assert _png_size(PKG / "brand" / "icon.png") == (256, 256)
    assert _png_size(PKG / "brand" / "icon@2x.png") == (512, 512)


async def test_home_assistant_serves_the_local_brand_icon(hass: HomeAssistant, hass_client_no_auth: ClientSessionGenerator) -> None:
    """The brands proxy of Home Assistant (2026.3 and later) reads custom_components/<domain>/brand/ first."""
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "brands", {})
    await hass.async_block_till_done()
    client = await hass_client_no_auth()
    token = hass.data["brands"][0]
    for image, size in (("icon.png", 256), ("icon@2x.png", 512)):
        response = await client.get(f"/api/brands/integration/gc_tuning/{image}?token={token}")
        assert response.status == 200, image
        body = await response.read()
        assert struct.unpack(">II", body[16:24]) == (size, size)
    # an image that is not shipped falls back to the icon (HA's own fallback chain), it is not a 404
    response = await client.get(f"/api/brands/integration/gc_tuning/dark_icon.png?token={token}")
    assert response.status == 200


def test_manifest() -> None:
    manifest = json.loads((PKG / "manifest.json").read_text())
    assert manifest["domain"] == "gc_tuning"
    assert manifest["version"] == "1.0.0"
    assert manifest["config_flow"] is True
    assert manifest["single_config_entry"] is True
    assert manifest["integration_type"] == "service"
    assert manifest["iot_class"] == "calculated"
    assert manifest["requirements"] == []
    assert manifest["codeowners"] == ["@cchip201"]


def test_phase_two_placeholders_are_marked() -> None:
    """documentation and issue_tracker wait for the repository name; publishing replaces both and this test."""
    manifest = json.loads((PKG / "manifest.json").read_text())
    assert "TODO-PHASE2-REPO-NAME" in manifest["documentation"]
    assert "TODO-PHASE2-REPO-NAME" in manifest["issue_tracker"]


def _keys(node: object, prefix: str = "") -> set[str]:
    out: set[str] = set()
    if isinstance(node, dict):
        for k, v in node.items():
            out |= _keys(v, f"{prefix}{k}.")
            out.add(f"{prefix}{k}")
    return out


def test_every_string_the_code_uses_has_an_english_text() -> None:
    en = json.loads((PKG / "translations" / "en.json").read_text())
    keys = _keys(en)
    expected = {
        "config.step.user.description",
        "config.abort.single_instance_allowed",
        "options.step.init.menu_options.settings",
        "options.step.init.menu_options.reset",
        "options.step.settings.title",
        "options.step.reset.description",
        "issues.deprecated_yaml.title",
        "issues.deprecated_yaml.description",
        "exceptions.not_loaded.message",
    }
    for key in ("enabled", "start_delay", "threshold0", "threshold1", "threshold2", "freeze", "log_min_ms"):
        expected |= {f"options.step.settings.data.{key}", f"options.step.settings.data_description.{key}"}
    for key in ("full_collections_last_hour", "longest_pause_last_hour", "last_full_collection_duration", "frozen_objects", "thresholds", "applied_at", "process_memory"):
        expected.add(f"entity.sensor.{key}.name")
    for service in ("apply", "freeze", "unfreeze", "collect", "stats"):
        expected |= {f"services.{service}.name", f"services.{service}.description"}
    expected |= {"services.stats.fields.recent.name", "services.stats.fields.recent.description"}
    assert expected - keys == set()


def test_every_sensor_description_has_a_translation_and_an_icon() -> None:
    from custom_components.gc_tuning.sensor import SENSORS

    en = json.loads((PKG / "translations" / "en.json").read_text())
    icons = json.loads((PKG / "icons.json").read_text())
    for description in SENSORS:
        assert description.translation_key in en["entity"]["sensor"]
        assert description.translation_key in icons["entity"]["sensor"]
