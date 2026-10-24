"""Integration layer: config flow, entities, events, last-good, health, simulate, alert-memory load."""

from __future__ import annotations

import dataclasses
import json
from datetime import timedelta
from pathlib import Path

import aiohttp
import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.jma.const import DOMAIN, JMA_ATTRIBUTION, URL_AMEDAS_TABLE, URL_AREA, URL_WARNINGS
from custom_components.jma.memory import AlertMemory, empty_memory
from custom_components.jma.parse_forecast import condition_for

from .conftest import AREA, ENTRY_DATA, mock_jma, setup
from .helpers import clone, load_json
from .test_options_flow_http import FLOW, _client

COMPONENT = Path(__file__).parent.parent / "custom_components" / "jma"


async def test_config_flow_picks_stations(tokyo: HomeAssistant, aioclient_mock) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"office": "130000"})
    assert result["step_id"] == "area"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"area": AREA})
    assert result["step_id"] == "confirm"
    # The nearest stations to the tests' place in tests/fixtures/amedastable.json:
    # 東京 44132 at 1.9 km is both the nearest station with temperature and the nearest type-A station.
    assert "東京 44132 (1.9 km)" in result["description_placeholders"]["primary"]
    assert "東京 44132 (1.9 km)" in result["description_placeholders"]["secondary"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "JMA Chiyoda"
    assert result["data"] == ENTRY_DATA


async def test_config_flow_picks_two_different_stations_where_the_rules_differ(tokyo: HomeAssistant, aioclient_mock) -> None:
    """At Tokyo Station the two rules give one station, so the test above cannot tell them apart. With one
    synthetic type-C station with a temperature element added to the table 0.24 km from the place, they
    differ: the nearest station with temperature is the added one, the nearest type-A station is
    東京 44132 (1.93 km). A swap of the two rules fails here."""
    hass = tokyo
    table = load_json("amedastable.json")
    table["99999"] = {"type": "C", "elems": "11110000", "lat": [35, 41.0], "lon": [139, 46.0], "alt": 10,
                      "kjName": "試験", "knName": "テスト", "enName": "Test"}
    aioclient_mock.get(URL_AMEDAS_TABLE, json=table)  # the first registration wins
    mock_jma(aioclient_mock)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"office": "130000"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"area": AREA})
    assert result["step_id"] == "confirm"
    assert "試験 99999 (0.2 km)" in result["description_placeholders"]["primary"]
    assert "東京 44132 (1.9 km)" in result["description_placeholders"]["secondary"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["data"]["station_primary"] == "99999" and result["data"]["station_secondary"] == "44132"


@pytest.mark.parametrize(("url", "fault"), [
    (URL_AREA, {"status": 503}),                            # the agency's server is down
    (URL_AREA, {"exc": aiohttp.ClientError("no route")}),   # this machine has no network
    (URL_AREA, {"text": "<html>maintenance</html>"}),       # a maintenance page served with HTTP 200
    (URL_AMEDAS_TABLE, {"status": 503}),                    # the station table is the second download
])
async def test_config_flow_aborts_when_a_download_fails(tokyo: HomeAssistant, aioclient_mock, url, fault) -> None:
    """Any failure of the two downloads that open the flow ends it with `cannot_connect`; no entry is made."""
    hass = tokyo
    aioclient_mock.get(url, **fault)  # the first registration wins
    mock_jma(aioclient_mock)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "cannot_connect"
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_config_flow_aborts_when_the_area_is_already_configured(
        tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    """The area code is the entry's unique id: a second flow for the same area ends at the area step."""
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"office": "130000"})
    assert result["step_id"] == "area"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"area": AREA})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_a_second_flow_for_an_area_whose_setup_is_open_aborts_as_in_progress(
        tokyo: HomeAssistant, aioclient_mock) -> None:
    """async_set_unique_id (area step) refuses a second flow for the area while the first sits on the confirm form; the
    reason `already_in_progress` has a text in both languages (translations: config.abort)."""
    hass = tokyo
    mock_jma(aioclient_mock)
    first = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    first = await hass.config_entries.flow.async_configure(first["flow_id"], {"office": "130000"})
    first = await hass.config_entries.flow.async_configure(first["flow_id"], {"area": AREA})
    assert first["step_id"] == "confirm"  # the first flow is open, nothing is stored yet
    second = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    second = await hass.config_entries.flow.async_configure(second["flow_id"], {"office": "130000"})
    second = await hass.config_entries.flow.async_configure(second["flow_id"], {"area": AREA})
    assert second["type"] is FlowResultType.ABORT and second["reason"] == "already_in_progress"
    for language in ("en", "ja"):
        texts = json.loads((COMPONENT / "translations" / f"{language}.json").read_text(encoding="utf-8"))
        assert texts["config"]["abort"]["already_in_progress"], language
    # the first flow is not harmed by it
    first = await hass.config_entries.flow.async_configure(first["flow_id"], {})
    assert first["type"] is FlowResultType.CREATE_ENTRY and first["data"] == ENTRY_DATA


async def test_options_flow_recovers_in_the_same_flow_after_an_invalid_station_id(
        tokyo: HomeAssistant, aioclient_mock, entry, hass_client) -> None:
    """A refused id keeps the flow open on the form; valid ids sent to the same flow_id are cleaned (spaces
    stripped, full-width digits folded to ASCII) and saved."""
    client = await _client(tokyo, aioclient_mock, entry, hass_client)
    flow = await (await client.post(FLOW, json={"handler": entry.entry_id})).json()
    url = f"{FLOW}/{flow['flow_id']}"
    refused = await client.post(url, json={"station_primary": "12", "station_secondary": "44132",
                                           "debug_simulate": False})
    assert refused.status == 200, await refused.text()
    form = await refused.json()
    assert form["type"] == "form" and form["flow_id"] == flow["flow_id"]
    assert form["errors"] == {"station_primary": "invalid_station"}
    assert dict(entry.options) == {}  # nothing was saved yet
    saved = await client.post(url, json={"station_primary": " 62016 ",
                                         "station_secondary": "４４１３２",  # full-width, as a Japanese IME types it
                                         "debug_simulate": False})
    assert saved.status == 200, await saved.text()
    result = await saved.json()
    assert result["type"] == "create_entry" and result["flow_id"] == flow["flow_id"]
    await tokyo.async_block_till_done()
    assert dict(entry.options) == {"station_primary": "62016", "station_secondary": "44132", "debug_simulate": False}


async def test_the_weather_credit_has_the_link_open_meteo_asks_for(tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    """Open-Meteo's licence: "You must include a link next to any location Open-Meteo data are displayed".
    The weather entity shows its hourly data; its attribution is plain text, so the link is written out."""
    mock_jma(aioclient_mock)
    await setup(tokyo, entry)
    credit = tokyo.states.get("weather.jma_chiyoda").attributes["attribution"]
    assert "Weather data by Open-Meteo.com" in credit and "https://open-meteo.com/" in credit
    assert JMA_ATTRIBUTION in credit and "CC BY 4.0" in credit and "converted" in credit


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_every_entity_that_shows_agency_data_carries_the_credit(tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    """The agency's terms ask for the source and, for edited content, a statement that it was edited."""
    mock_jma(aioclient_mock)
    await setup(tokyo, entry)
    states = [s for s in tokyo.states.async_all() if s.entity_id.split(".")[0] in ("weather", "sensor", "event")]
    assert len(states) == 16
    for state in states:
        credit = state.attributes.get("attribution", "")
        assert "気象庁" in credit or "Ministry of the Environment" in credit, state.entity_id
    quake = tokyo.states.get("sensor.jma_chiyoda_last_earthquake")
    assert "P2P地震情報" in quake.attributes["attribution"]
    assert JMA_ATTRIBUTION in tokyo.states.get("sensor.jma_chiyoda_typhoon").attributes["attribution"]
    assert "加工" in JMA_ATTRIBUTION and "https://www.jma.go.jp/" in JMA_ATTRIBUTION


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_entities_from_sample_files(tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)

    weather = hass.states.get("weather.jma_chiyoda")
    assert weather is not None
    assert weather.state == "rainy"  # today's JMA code 300, nowcast dry at the configured place
    assert weather.attributes["temperature"] == 18.8
    assert weather.attributes["humidity"] == 91
    assert weather.attributes["pressure"] == 1013.9
    assert weather.attributes["jma_code"] == "300"
    assert weather.attributes["jma_icon"] == "300.svg"

    daily = await hass.services.async_call(
        "weather", "get_forecasts", {"entity_id": "weather.jma_chiyoda", "type": "daily"},
        blocking=True, return_response=True)
    rows = daily["weather.jma_chiyoda"]["forecast"]
    assert rows[0]["datetime"].startswith("2026-09-26") and rows[0]["temperature"] == 20
    assert rows[1]["templow"] == 18 and rows[1]["temperature"] == 26
    assert len(rows) == 8

    hourly = await hass.services.async_call(
        "weather", "get_forecasts", {"entity_id": "weather.jma_chiyoda", "type": "hourly"},
        blocking=True, return_response=True)
    hrows = hourly["weather.jma_chiyoda"]["forecast"]
    assert hrows and hrows[0]["datetime"].startswith("2026-09-26T13:00")
    assert "precipitation_probability" not in hrows[0]  # null for every hour in JMA MSM

    assert hass.states.get("sensor.jma_chiyoda_warning_level").state == "0"
    rain = hass.states.get("sensor.jma_chiyoda_rain_next_hour")
    assert rain.state == "0" and len(rain.attributes["series"]) == 13
    frames = hass.states.get("sensor.jma_chiyoda_radar_frames")
    assert len(frames.attributes["past"]) == 12 and len(frames.attributes["forecast"]) == 12
    assert hass.states.get("sensor.jma_chiyoda_overview").state.startswith("前線が")
    assert hass.states.get("binary_sensor.jma_chiyoda_feed_problem").state == "off"
    assert hass.states.get("event.jma_chiyoda_alerts") is not None
    assert hass.states.get("sensor.jma_chiyoda_last_earthquake") is not None


async def test_weather_state_follows_wet_hold_directly(tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    """weather.jma_chiyoda reads the nowcast wet hold, not the forecast, whenever nowcast says
    wet; when it does not, the forecast day's condition shows through. Pins the direct-data
    wiring, and (together with the coordinator) that an unreadable observed frame is never silently
    read as dry."""
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    runtime = entry.runtime_data
    nowcast = runtime.nowcast
    base = nowcast.data

    nowcast.async_set_updated_data(dataclasses.replace(base, wet=True))
    await hass.async_block_till_done()
    assert hass.states.get("weather.jma_chiyoda").state == "rainy"

    nowcast.async_set_updated_data(dataclasses.replace(base, wet=False))
    await hass.async_block_till_done()
    today = next(d for d in runtime.forecast.data.forecast.days if d.date >= dt_util.now().date())
    sun = hass.states.get("sun.sun")
    is_night = bool(sun and sun.state == "below_horizon")
    assert hass.states.get("weather.jma_chiyoda").state == condition_for(today.code, is_night)

    pouring_series = [dict(base.rain.series[0], band=5), *base.rain.series[1:]]
    pouring_rain = dataclasses.replace(base.rain, series=pouring_series)
    nowcast.async_set_updated_data(dataclasses.replace(base, wet=True, rain=pouring_rain))
    await hass.async_block_till_done()
    assert hass.states.get("weather.jma_chiyoda").state == "pouring"


def _r8_with_chiyoda(code: str) -> list:
    records = clone(load_json("warning_r8_130000.json"))
    for record in records:
        if record["dataTypeCode"] == "VPWW55":
            for item in record["warning"]["class20Items"]:
                if item["areaCode"] == AREA:
                    item["kinds"] = [{"code": code, "status": "発表"}]
    return records


async def test_warning_change_fires_one_event(tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    assert hass.states.get("event.jma_chiyoda_alerts").state == "unknown"

    aioclient_mock.clear_requests()
    mock_jma(aioclient_mock, warnings=_r8_with_chiyoda("43"))
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    level = hass.states.get("sensor.jma_chiyoda_warning_level")
    assert level.state == "4"
    assert level.attributes["warnings"][0]["name_ja"] == "レベル４大雨危険警報"
    ev = hass.states.get("event.jma_chiyoda_alerts")
    assert ev.attributes["event_type"] == "warning_issued"
    assert ev.attributes["new_level"] == 4 and ev.attributes["phenomenon"] == "rain"
    assert ev.attributes["area"] == "千代田区"


def _r8_with_chiyoda_and_own_headline(code: str) -> list:
    """VPWW55 (own) carries the Chiyoda rain warning and an earlier headline; VPWW56 (other) is a
    later-reportDatetime bulletin for a different phenomenon/area with its own headline -- the
    fired event must use VPWW55's own headline, not VPWW56's newer one."""
    records = clone(load_json("warning_r8_130000.json"))
    own, other = records[0], records[1]
    for item in own["warning"]["class20Items"]:
        if item["areaCode"] == AREA:
            item["kinds"] = [{"code": code, "status": "発表"}]
    own["headlineText"], own["reportDatetime"] = "千代田区に大雨警報", "2026-09-26T10:00:00+09:00"
    other["headlineText"], other["reportDatetime"] = "伊豆諸島に雷注意報", "2026-09-26T11:00:00+09:00"
    return records


async def test_warning_event_headline_is_its_own_bulletin(
    tokyo: HomeAssistant, aioclient_mock, entry, freezer
) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)

    aioclient_mock.clear_requests()
    mock_jma(aioclient_mock, warnings=_r8_with_chiyoda_and_own_headline("43"))
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    ev = hass.states.get("event.jma_chiyoda_alerts")
    assert ev.attributes["event_type"] == "warning_issued"
    assert ev.attributes["headline"] == "千代田区に大雨警報"


async def test_warning_level_sensor_headline_is_its_own_bulletin(
    tokyo: HomeAssistant, aioclient_mock, entry, freezer
) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)

    aioclient_mock.clear_requests()
    mock_jma(aioclient_mock, warnings=_r8_with_chiyoda_and_own_headline("43"))
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    level = hass.states.get("sensor.jma_chiyoda_warning_level")
    assert level.state == "4"
    assert level.attributes["headline"] == "千代田区に大雨警報"


async def test_last_good_then_unavailable(tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    # The mocker answers with the FIRST matching registration: a maintenance page for warnings.
    aioclient_mock.clear_requests()
    aioclient_mock.get(URL_WARNINGS.format(office="130000"), text="<html>maintenance</html>")
    mock_jma(aioclient_mock)

    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.jma_chiyoda_warning_level").state == "0"  # last good kept

    for _ in range(3):
        freezer.tick(timedelta(seconds=61))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert hass.states.get("sensor.jma_chiyoda_warning_level").state == "unavailable"


async def test_simulate_only_with_debug_option(tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    # the action is registered once for the integration; with no debug entry loaded it refuses
    assert hass.services.has_service(DOMAIN, "simulate")
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "simulate", {"event_type": "eew"}, blocking=True)

    debug = MockConfigEntry(domain=DOMAIN, title="JMA Test", unique_id="2710000",
                            data={**ENTRY_DATA, "area": "2710000"},
                            options={"debug_simulate": True})
    await setup(hass, debug)
    await hass.services.async_call(DOMAIN, "simulate", {
        "event_type": "eew", "data": {"predicted_shindo": "5-"}}, blocking=True)
    await hass.async_block_till_done()
    ev = hass.states.get("event.jma_test_alerts")
    assert ev.attributes["event_type"] == "eew" and ev.attributes["simulated"] is True
    assert hass.states.get("event.jma_chiyoda_alerts").attributes.get("event_type") is None  # no debug option there


async def test_feed_problem_after_thirty_minutes(tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    hass = tokyo
    lagging = clone(load_json("warning_r8_130000.json"))
    for record in lagging:
        if record["dataTypeCode"] == "VPWW61":
            record["controlDatetime"] = "2026-09-25T18:00:00Z"  # XML has 19:02:36Z
    mock_jma(aioclient_mock, warnings=lagging)
    await setup(hass, entry)
    problem = hass.states.get("binary_sensor.jma_chiyoda_feed_problem")
    assert problem.state == "off"
    assert "VPWW61" in problem.attributes["warnings"]["reason"]

    for _ in range(31):
        freezer.tick(timedelta(minutes=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    problem = hass.states.get("binary_sensor.jma_chiyoda_feed_problem")
    assert problem.state == "on"
    assert problem.attributes["warnings"]["problem"] is True


# ---------- the alert memory file is read defensively; a bad file is never a failed setup ----------

STORED_TSUNAMI = {"active": True, "home": "warning", "max": "major_warning", "id": "t-1",
                  "issued": "2026-09-26T12:00:00+09:00"}


def _memory_file(key: str, data: dict, version: int = 1) -> dict:
    return {"version": version, "minor_version": 1, "key": key, "data": data}


async def test_alert_memory_drops_a_typhoon_entry_that_is_not_an_object(hass: HomeAssistant, hass_storage) -> None:
    key = "jma.abc.alert_memory"
    hass_storage[key] = _memory_file(key, {"typhoon": {
        "TC2631": "notice",                     # a string where the entry belongs
        "TC2632": ["notice", 0],                # a list
        "TC2633": {"stage": "urgent", "quiet": 2},
    }})
    memory = AlertMemory(hass, "abc")
    await memory.async_load()
    assert memory.typhoon == {"TC2633": {"stage": "urgent", "quiet": 2}}
    assert memory.heat == [] and memory.tsunami == {}


@pytest.mark.parametrize("tsunami", [
    {**STORED_TSUNAMI, "active": "yes"},        # not a boolean
    {**STORED_TSUNAMI, "home": "tidal-wave"},   # not a grade of the integration
    {**STORED_TSUNAMI, "max": None},
    {**STORED_TSUNAMI, "id": 7},                # not a string
    {k: v for k, v in STORED_TSUNAMI.items() if k != "issued"},
    ["warning"],                                # not an object at all
])
async def test_alert_memory_drops_a_tsunami_section_of_the_wrong_shape(
        hass: HomeAssistant, hass_storage, tsunami) -> None:
    key = "jma.abc.alert_memory"
    hass_storage[key] = _memory_file(key, {"tsunami": tsunami})
    memory = AlertMemory(hass, "abc")
    await memory.async_load()
    assert memory.tsunami == {}


async def test_alert_memory_keeps_a_good_tsunami_section_and_only_its_five_keys(
        hass: HomeAssistant, hass_storage) -> None:
    key = "jma.abc.alert_memory"
    hass_storage[key] = _memory_file(key, {"tsunami": {**STORED_TSUNAMI, "home_area": "extra"}})
    memory = AlertMemory(hass, "abc")
    await memory.async_load()
    assert memory.tsunami == STORED_TSUNAMI


async def test_alert_memory_written_by_a_newer_release_starts_empty_and_setup_goes_on(
        tokyo: HomeAssistant, aioclient_mock, entry, hass_storage, caplog) -> None:
    """A downgrade finds a file with a newer storage version: Home Assistant refuses to read it. The memory
    starts empty (one alert may be repeated) and the entry still loads."""
    hass = tokyo
    mock_jma(aioclient_mock)
    key = f"jma.{entry.entry_id}.alert_memory"
    hass_storage[key] = _memory_file(key, {"heat": ["2026-09-26|alert|today"]}, version=2)
    await setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.memory.data == empty_memory()
    assert "alert memory could not be read; starting empty" in caplog.text
