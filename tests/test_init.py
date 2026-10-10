"""Integration layer: config flow, entities, events, last-good, health, simulate."""

from __future__ import annotations

import dataclasses
from datetime import timedelta

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.jma.const import DOMAIN, JMA_ATTRIBUTION, URL_AMEDAS_TABLE, URL_WARNINGS
from custom_components.jma.parse_forecast import condition_for

from .conftest import AREA, ENTRY_DATA, mock_jma, setup
from .helpers import clone, load_json


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


async def test_the_weather_credit_has_the_link_open_meteo_asks_for(tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    """Open-Meteo's licence: "You must include a link next to any location Open-Meteo data are displayed".
    The weather entity shows its hourly data; its attribution is plain text, so the link is written out."""
    mock_jma(aioclient_mock)
    await setup(tokyo, entry)
    credit = tokyo.states.get("weather.jma_chiyoda").attributes["attribution"]
    assert "Weather data by Open-Meteo.com" in credit and "https://open-meteo.com/" in credit
    assert JMA_ATTRIBUTION in credit and "CC BY 4.0" in credit and "converted" in credit


async def test_every_entity_that_shows_agency_data_carries_the_credit(tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    """The agency's terms ask for the source and, for edited content, a statement that it was edited."""
    mock_jma(aioclient_mock)
    await setup(tokyo, entry)
    states = [s for s in tokyo.states.async_all() if s.entity_id.split(".")[0] in ("weather", "sensor", "event")]
    assert len(states) == 13
    for state in states:
        credit = state.attributes.get("attribution", "")
        assert "気象庁" in credit or "Ministry of the Environment" in credit, state.entity_id
    quake = tokyo.states.get("sensor.jma_chiyoda_last_earthquake")
    assert "P2P地震情報" in quake.attributes["attribution"]
    assert JMA_ATTRIBUTION in tokyo.states.get("sensor.jma_chiyoda_typhoon").attributes["attribution"]
    assert "加工" in JMA_ATTRIBUTION and "https://www.jma.go.jp/" in JMA_ATTRIBUTION


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
    assert not hass.services.has_service(DOMAIN, "simulate")

    debug = MockConfigEntry(domain=DOMAIN, title="JMA Test", unique_id="2710000",
                            data={**ENTRY_DATA, "area": "2710000"},
                            options={"debug_simulate": True})
    await setup(hass, debug)
    assert hass.services.has_service(DOMAIN, "simulate")
    await hass.services.async_call(DOMAIN, "simulate", {
        "event_type": "eew", "data": {"predicted_shindo": "5-"}}, blocking=True)
    await hass.async_block_till_done()
    ev = hass.states.get("event.jma_test_alerts")
    assert ev.attributes["event_type"] == "eew" and ev.attributes["simulated"] is True


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
