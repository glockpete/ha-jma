"""icon-translations (Home Assistant Integration Quality Scale, Gold): every icon comes from icons.json, none from code.

Home Assistant's frontend, not the state machine, resolves an icon translation (helpers/icon.async_get_icons serves
the file through frontend/get_icons), so a state's `icon` attribute stays empty. The tests therefore pin the file
itself: its shape, its coverage of the entities the code creates, and what it resolves to for a live state.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.icon import async_get_icons

from custom_components.jma.const import DOMAIN

from .conftest import AREA, mock_jma, setup

COMPONENT = Path(__file__).parents[1] / "custom_components" / "jma"
ICONS = json.loads((COMPONENT / "icons.json").read_text(encoding="utf-8"))
EN = json.loads((COMPONENT / "translations" / "en.json").read_text(encoding="utf-8"))
PLATFORMS = ("sensor", "binary_sensor", "event")  # weather.jma_<place> takes its icon from the condition
# These three entities keep no icon of their own: their device class (precipitation intensity, problem) supplies it.
DEVICE_CLASS_ICON = {
    ("sensor", "rain_next_hour"), ("sensor", "rain_outlook"), ("binary_sensor", "feed_problem")}
ICON = re.compile(r"^mdi:[a-z0-9-]+$")


def _keys(platform: str) -> set[str]:
    """translation_key of every entity class, parsed the way test_version_and_readme.py parses the sensors."""
    code = (COMPONENT / f"{platform}.py").read_text(encoding="utf-8")
    return set(re.findall(r'super\(\).__init__\(entry, "([a-z_0-9]+)"', code))


def _strings(node: Any) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for value in node.values() for s in _strings(value)]
    return []


def _icon_for(section: dict[str, Any], state: str) -> str:
    """The frontend's rule (docs: entity icon translations): a state icon wins, then the range icon of the highest
    key at or below a numeric state, else the default."""
    if state in section.get("state", {}):
        return section["state"][state]
    try:
        number = float(state)
    except ValueError:
        return section["default"]
    below = [(float(k), icon) for k, icon in section.get("range", {}).items() if float(k) <= number]
    return max(below)[1] if below else section["default"]


def test_every_entity_key_has_an_icon_entry_or_a_device_class_icon() -> None:
    for platform in PLATFORMS:
        keys = _keys(platform)
        assert keys, platform
        entries = set(ICONS["entity"].get(platform, {}))
        without = {(platform, k) for k in keys - entries}
        assert without <= DEVICE_CLASS_ICON, without - DEVICE_CLASS_ICON
        assert entries <= keys, f"icons.json names {platform} keys no entity has: {entries - keys}"
    assert set(ICONS["entity"]) <= set(PLATFORMS)


def test_every_icon_is_a_material_design_icon_name() -> None:
    icons = _strings(ICONS)
    assert len(icons) >= 20
    for icon in icons:
        assert ICON.match(icon), icon


def test_icons_json_follows_the_core_rules() -> None:
    """The two checks hassfest makes beyond the shape: ascending numeric range keys, and a state icon that is not the
    default one."""
    for platform, sections in ICONS["entity"].items():
        for key, section in sections.items():
            assert set(section) <= {"default", "state", "range"}, (platform, key)
            assert section["default"], (platform, key)
            numbers = [float(k) for k in section.get("range", {})]
            assert numbers == sorted(numbers), (platform, key)
            for state, icon in section.get("state", {}).items():
                assert icon != section["default"], (platform, key, state)


@pytest.mark.parametrize("key", ["tsunami", "heat_alert"])
def test_state_icons_cover_exactly_the_states_the_sensor_is_translated_for(key: str) -> None:
    translated = set(EN["entity"]["sensor"][key]["state"])
    assert translated
    assert set(ICONS["entity"]["sensor"][key]["state"]) == translated


def test_the_simulate_service_has_an_icon_and_nothing_else_does() -> None:
    services = (COMPONENT / "services.yaml").read_text(encoding="utf-8")
    assert set(ICONS["services"]) == set(re.findall(r"^([a-z_]+):", services, re.M))
    assert ICONS["services"]["simulate"] == {"service": "mdi:test-tube"}


def test_no_entity_class_sets_an_icon_in_code() -> None:
    for platform in (*PLATFORMS, "weather"):
        code = (COMPONENT / f"{platform}.py").read_text(encoding="utf-8")
        assert "_attr_icon" not in code, platform
        assert not re.search(r"def icon\b", code), platform


def test_warning_level_icon_follows_the_level() -> None:
    section = ICONS["entity"]["sensor"]["warning_level"]
    assert [_icon_for(section, str(level)) for level in (0, 2, 3, 4, 5)] == [
        "mdi:shield-check-outline", "mdi:alert-outline", "mdi:alert", "mdi:alert-octagon", "mdi:alert-octagram"]
    assert _icon_for(section, "unavailable") == section["default"] == "mdi:alert"
    tsunami = ICONS["entity"]["sensor"]["tsunami"]
    assert [_icon_for(tsunami, s) for s in ("none", "watch", "warning", "major_warning", "unknown")] == [
        "mdi:waves", "mdi:waves-arrow-up", "mdi:home-flood", "mdi:alert-octagram", "mdi:tsunami"]


async def test_live_entities_take_their_icons_from_the_file(tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)

    # Through Home Assistant's own loader, as the frontend asks for it.
    loaded = (await async_get_icons(hass, "entity", [DOMAIN]))[DOMAIN]
    assert loaded["sensor"]["warning_level"] == ICONS["entity"]["sensor"]["warning_level"]
    assert (await async_get_icons(hass, "services", [DOMAIN]))[DOMAIN]["simulate"]["service"] == "mdi:test-tube"

    level = hass.states.get("sensor.jma_chiyoda_warning_level")
    assert level.state == "0"
    assert "icon" not in level.attributes  # nothing in code overrides the translation
    assert _icon_for(loaded["sensor"]["warning_level"], level.state) == "mdi:shield-check-outline"
    assert _icon_for(loaded["sensor"]["tsunami"], "major_warning") == "mdi:alert-octagram"

    # Every entity of the entry that is not a weather entity either has an icon entry or a device class.
    registry = er.async_get(hass)
    entries = [e for e in er.async_entries_for_config_entry(registry, entry.entry_id) if e.domain in PLATFORMS]
    assert len(entries) >= 15
    for e in entries:
        assert e.unique_id.startswith(f"{AREA}_")
        has_entry = e.translation_key in loaded.get(e.domain, {})
        assert has_entry or e.original_device_class, e.entity_id
        assert has_entry != ((e.domain, e.translation_key) in DEVICE_CLASS_ICON), e.entity_id
        state = hass.states.get(e.entity_id)
        assert state is None or "icon" not in state.attributes, e.entity_id
