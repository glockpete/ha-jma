"""Hardening, lifecycle batch: Home Assistant's time zone is not Japan's, the earthquake connections follow the
entries that remain, `jma.simulate` is one action for all entries, a malformed area list or station table is an
abort (not "Unknown error"), and diagnostics hide the tsunami coast area."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import yaml
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.jma.const import DOMAIN, TSUNAMI_COAST_AREA, URL_AMEDAS_TABLE, URL_AREA
from custom_components.jma.diagnostics import REDACTED, async_get_config_entry_diagnostics

from .conftest import AREA, ENTRY_DATA, LAT, LON, mock_jma, setup
from .helpers import load_json

COMPONENT = Path(__file__).parent.parent / "custom_components" / "jma"

# ---------- L1: the weather entity's "today" is Japan's, whatever zone Home Assistant is set to ----------


@pytest.mark.parametrize(("zone", "instant", "jst_day"), [
    ("Asia/Tokyo", "2026-09-26T13:30:00+00:00", 26),           # 22:30 JST: the control, nothing changes
    ("Pacific/Auckland", "2026-09-26T13:30:00+00:00", 26),     # the same instant is 01:30 on the 27th there
    ("Pacific/Kiritimati", "2026-09-26T10:30:00+00:00", 26),   # 00:30 on the 27th there, 19:30 JST on the 26th
    ("America/Los_Angeles", "2026-09-26T07:05:00+00:00", 26),  # 00:05 PDT; the day's high (00:35 JST) is 08:35 on the 25th
    ("UTC", "2026-09-26T15:30:00+00:00", 27),                  # 00:30 JST on the 27th, still the 26th in UTC
])
async def test_weather_today_is_the_japanese_day(hass: HomeAssistant, freezer, aioclient_mock, entry,
                                                  zone: str, instant: str, jst_day: int) -> None:
    freezer.move_to(instant)
    hass.config.latitude, hass.config.longitude = LAT, LON
    await hass.config.async_set_time_zone(zone)
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    days = {d.date: d for d in entry.runtime_data.forecast.data.forecast.days}
    today = days[date(2026, 9, jst_day)]
    other = days[date(2026, 9, 27 if jst_day == 26 else 26)]
    assert (today.code, today.temp_max, today.temp_min) != (other.code, other.temp_max, other.temp_min)

    attrs = hass.states.get("weather.jma_chiyoda").attributes
    assert (attrs["jma_code"], attrs["temp_max"], attrs["temp_min"]) == (today.code, today.temp_max, today.temp_min)
    # The AMeDAS extremes in the fixture are those of the 26th (JST). They are "today's" on the 26th only.
    if jst_day == 26:
        assert attrs["temp_max_observed"] == 23.6
        assert attrs["temp_max_observed_time"].startswith("2026-09-26T00:35")
    else:
        assert attrs["temp_max_observed"] is None and attrs["gust_max_today"] is None

    forecast = await hass.services.async_call(
        "weather", "get_forecasts", {"entity_id": "weather.jma_chiyoda", "type": "daily"},
        blocking=True, return_response=True)
    first = forecast["weather.jma_chiyoda"]["forecast"][0]
    assert first["datetime"].startswith(f"2026-09-{jst_day}") and first["jma_code"] == today.code


# ---------- L2: a freed earthquake connection goes to an entry that has none ----------


def _three_entries() -> list[MockConfigEntry]:
    """The service allows two connections per IP address (MAX_QUAKE_SOCKETS): the third entry has none."""
    out = [MockConfigEntry(domain=DOMAIN, title="JMA Test", unique_id=AREA, data=ENTRY_DATA)]
    for area, title in (("2710000", "JMA Second"), ("2714000", "JMA Third")):
        out.append(MockConfigEntry(domain=DOMAIN, title=title, unique_id=area, data={**ENTRY_DATA, "area": area}))
    return out


def _sockets(hass: HomeAssistant) -> set[str]:
    return hass.data[DOMAIN]["quake_sockets"]


async def test_removing_an_entry_hands_its_connection_to_the_entry_without_one(tokyo: HomeAssistant,
                                                                                aioclient_mock) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    e1, e2, e3 = _three_entries()
    for e in (e1, e2, e3):
        await setup(hass, e)
    assert _sockets(hass) == {e1.entry_id, e2.entry_id}
    assert e1.runtime_data.quake.enabled and e2.runtime_data.quake.enabled and not e3.runtime_data.quake.enabled
    second = e2.runtime_data

    await hass.config_entries.async_remove(e1.entry_id)
    await hass.async_block_till_done()

    assert _sockets(hass) == {e2.entry_id, e3.entry_id}
    assert e3.runtime_data.quake.enabled
    assert e2.runtime_data is second  # the entry that kept its connection was not reloaded


async def test_disabling_an_entry_hands_its_connection_to_the_entry_without_one(tokyo: HomeAssistant,
                                                                                 aioclient_mock) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    e1, e2, e3 = _three_entries()
    for e in (e1, e2, e3):
        await setup(hass, e)

    assert await hass.config_entries.async_set_disabled_by(e1.entry_id, ConfigEntryDisabler.USER)
    await hass.async_block_till_done()

    assert _sockets(hass) == {e2.entry_id, e3.entry_id}
    assert e3.runtime_data.quake.enabled


async def test_reloading_an_entry_keeps_its_connection_and_reloads_no_other(tokyo: HomeAssistant,
                                                                             aioclient_mock) -> None:
    """A reload frees the connection for a moment only: the entry takes it again, the third entry stays as it was."""
    hass = tokyo
    mock_jma(aioclient_mock)
    e1, e2, e3 = _three_entries()
    for e in (e1, e2, e3):
        await setup(hass, e)
    third = e3.runtime_data
    for _ in range(2):
        assert await hass.config_entries.async_reload(e1.entry_id)
        await hass.async_block_till_done()
    assert _sockets(hass) == {e1.entry_id, e2.entry_id}
    assert e1.runtime_data.quake.enabled and not e3.runtime_data.quake.enabled
    assert e3.runtime_data is third


async def test_removing_the_entry_without_a_connection_reloads_nobody(tokyo: HomeAssistant, aioclient_mock) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    e1, e2, e3 = _three_entries()
    for e in (e1, e2, e3):
        await setup(hass, e)
    first, second = e1.runtime_data, e2.runtime_data
    await hass.config_entries.async_remove(e3.entry_id)
    await hass.async_block_till_done()
    assert _sockets(hass) == {e1.entry_id, e2.entry_id}
    assert e1.runtime_data is first and e2.runtime_data is second


# ---------- L3: jma.simulate is one action, registered once, for every entry with the debug option ----------


def _debug_entry(title: str, area: str) -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, title=title, unique_id=area, data={**ENTRY_DATA, "area": area},
                           options={"debug_simulate": True})


async def _two_debug_entries(hass: HomeAssistant, aioclient_mock) -> tuple[MockConfigEntry, MockConfigEntry]:
    mock_jma(aioclient_mock)
    e1, e2 = _debug_entry("JMA Test", "2710000"), _debug_entry("JMA Spare", "2714000")
    await setup(hass, e1)
    await setup(hass, e2)
    return e1, e2


def _last_event(hass: HomeAssistant, entity_id: str) -> str | None:
    return hass.states.get(entity_id).attributes.get("event_type")


async def test_simulate_without_an_entry_id_reaches_every_debug_entry(tokyo: HomeAssistant, aioclient_mock) -> None:
    hass = tokyo
    await _two_debug_entries(hass, aioclient_mock)
    await hass.services.async_call(DOMAIN, "simulate", {"event_type": "earthquake", "data": {}}, blocking=True)
    await hass.async_block_till_done()
    assert _last_event(hass, "event.jma_test_alerts") == "earthquake"
    assert _last_event(hass, "event.jma_spare_alerts") == "earthquake"
    assert hass.states.get("event.jma_test_alerts").attributes["simulated"] is True


async def test_simulate_with_an_entry_id_reaches_that_entry_only(tokyo: HomeAssistant, aioclient_mock) -> None:
    hass = tokyo
    e1, e2 = await _two_debug_entries(hass, aioclient_mock)
    await hass.services.async_call(DOMAIN, "simulate", {
        "event_type": "eew", "data": {"predicted_shindo": "5-"}, "config_entry_id": e2.entry_id}, blocking=True)
    await hass.async_block_till_done()
    assert _last_event(hass, "event.jma_spare_alerts") == "eew"
    assert hass.states.get("event.jma_spare_alerts").attributes["predicted_shindo"] == "5-"
    assert _last_event(hass, "event.jma_test_alerts") is None


async def test_simulate_survives_the_unload_of_one_debug_entry(tokyo: HomeAssistant, aioclient_mock) -> None:
    hass = tokyo
    e1, e2 = await _two_debug_entries(hass, aioclient_mock)
    assert await hass.config_entries.async_unload(e2.entry_id)
    await hass.async_block_till_done()
    assert hass.services.has_service(DOMAIN, "simulate")
    await hass.services.async_call(DOMAIN, "simulate", {"event_type": "eew"}, blocking=True)
    await hass.async_block_till_done()
    assert _last_event(hass, "event.jma_test_alerts") == "eew"
    # an entry that is not loaded is not a target, with or without its id
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(DOMAIN, "simulate", {
            "event_type": "eew", "config_entry_id": e2.entry_id}, blocking=True)
    assert err.value.translation_key == "simulate_entry_not_found"


async def test_simulate_errors_say_why(tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)  # no debug option
    other = MockConfigEntry(domain="other", title="Other")
    other.add_to_hass(hass)

    async def call(**extra) -> ServiceValidationError:
        with pytest.raises(ServiceValidationError) as err:
            await hass.services.async_call(DOMAIN, "simulate", {"event_type": "eew", **extra}, blocking=True)
        assert err.value.translation_domain == DOMAIN
        return err.value

    assert (await call()).translation_key == "simulate_no_debug_entry"
    assert (await call(config_entry_id=entry.entry_id)).translation_key == "simulate_not_enabled"
    assert (await call(config_entry_id="no-such-entry")).translation_key == "simulate_entry_not_found"
    assert (await call(config_entry_id=other.entry_id)).translation_key == "simulate_entry_not_found"
    assert _last_event(hass, "event.jma_chiyoda_alerts") is None


def test_simulate_texts_exist_in_both_languages_and_the_field_is_selectable() -> None:
    services = yaml.safe_load((COMPONENT / "services.yaml").read_text(encoding="utf-8"))
    field = services["simulate"]["fields"]["config_entry_id"]
    assert field["required"] is False and field["selector"] == {"config_entry": {"integration": DOMAIN}}
    for name in ("en", "ja"):
        strings = json.loads((COMPONENT / "translations" / f"{name}.json").read_text(encoding="utf-8"))
        assert strings["services"]["simulate"]["fields"]["config_entry_id"].keys() == {"name", "description"}, name
        for key in ("simulate_entry_not_found", "simulate_not_enabled", "simulate_no_debug_entry"):
            assert strings["exceptions"][key]["message"], (name, key)


# ---------- L4: a payload of the wrong shape ends the flow with "cannot_connect", not "Unknown error" ----------


@pytest.mark.parametrize("area_body", [
    [],
    {"centers": {}},
    "maintenance",
    {"offices": {}},
    {"offices": []},
    {"offices": {"130000": "Tokyo"}},
    {"offices": {"130000": {"enName": "Tokyo"}}},  # no name
])
async def test_flow_aborts_on_an_area_list_of_the_wrong_shape(tokyo: HomeAssistant, aioclient_mock,
                                                              area_body) -> None:
    hass = tokyo
    aioclient_mock.get(URL_AREA, json=area_body)  # the first registration wins
    mock_jma(aioclient_mock)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "cannot_connect"


@pytest.mark.parametrize("class20s", [[], {"1310100": "Chiyoda"}, {"1310100": {"name": "Chiyoda"}}])
async def test_flow_aborts_when_the_chosen_office_has_no_usable_area(tokyo: HomeAssistant, aioclient_mock,
                                                                      class20s) -> None:
    hass = tokyo
    area = load_json("area.json")
    area["class20s"] = class20s
    aioclient_mock.get(URL_AREA, json=area)
    mock_jma(aioclient_mock)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"office": "130000"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "cannot_connect"


STATION_TABLES = [
    [],
    {"44132": {"type": "A"}},                                    # no lat / lon
    {"44132": {"lat": [35], "lon": [139, 46.0], "type": "A"}},   # a coordinate without minutes
    {"44132": {"lat": [35, 41.0], "lon": [139, 46.0], "type": "C", "elems": "0"}},  # neither kind of station
    {"44132": {"lat": [35, 41.0], "lon": [139, 46.0], "type": "C", "elems": "11110000"}},  # no type A
    {"44132": {"lat": [35, 41.0], "lon": [139, 46.0], "type": "A", "elems": "0"}},  # no station with temperature
    {"44132": {"lat": [35, 41.0], "lon": [139, 46.0], "type": "A", "elems": "11110000"}},  # no name
]


@pytest.mark.parametrize("table", STATION_TABLES)
async def test_flow_aborts_on_a_station_table_of_the_wrong_shape(tokyo: HomeAssistant, aioclient_mock,
                                                                 table) -> None:
    hass = tokyo
    aioclient_mock.get(URL_AMEDAS_TABLE, json=table)
    mock_jma(aioclient_mock)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"office": "130000"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"area": AREA})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "cannot_connect"


# ---------- L5: the tsunami coast area is the class10 code in words ----------


async def test_diagnostics_hide_the_tsunami_coast_area(tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    tsunami = entry.runtime_data.quake.tsunami
    assert tsunami.home_area == TSUNAMI_COAST_AREA[ENTRY_DATA["class10"]]  # it is set for this place
    out = await async_get_config_entry_diagnostics(hass, entry)
    assert out["tsunami"]["home_area"] == REDACTED
    text = json.dumps(out, ensure_ascii=False, default=str)
    for name in set(TSUNAMI_COAST_AREA.values()):
        assert name not in text, name


@pytest.mark.parametrize("name", sorted(set(TSUNAMI_COAST_AREA.values())))
async def test_diagnostics_hide_every_coast_area_name(tokyo: HomeAssistant, aioclient_mock, entry, name) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    entry.runtime_data.quake.tsunami.home_area = name
    entry.runtime_data.quake.tsunami.memory["home_area"] = name  # the remembered state is written out too
    out = await async_get_config_entry_diagnostics(hass, entry)
    assert name not in json.dumps(out, ensure_ascii=False, default=str)
