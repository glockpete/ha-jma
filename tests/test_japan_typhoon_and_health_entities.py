"""Integration layer: nationwide forecast sensor, a typhoon listed for the first time, per-station AMeDAS and
Open-Meteo feed health, the キキクル `partial` attribute, and forecasts withheld after a failed update."""

from __future__ import annotations

import dataclasses
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.jma.binary_sensor import feed_conditions
from custom_components.jma.const import (
    DOMAIN,
    JMA_ATTRIBUTION,
    URL_AMEDAS_POINT,
    URL_JAPAN_FORECAST,
    URL_TYPHOON_FORECAST,
)
from custom_components.jma.parse_layers import Kikikuru

from .conftest import AREA, mock_jma, setup

EVENING = "2026-09-27T14:30:00+00:00"  # an evening moment after the sample files were issued
JAPAN_NOW = "2026-09-29T15:30:00+00:00"  # a moment after the newest issue in forecast_010000.json


def _japan_entity(hass: HomeAssistant) -> str:
    # Looked up by unique id: HA derives the entity id from the translated name.
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{AREA}_japan")
    assert entity_id is not None
    return entity_id


async def _tick(hass: HomeAssistant, freezer, delta: timedelta, times: int = 1) -> None:
    for _ in range(times):
        freezer.tick(delta)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


# ---------- nationwide forecast ----------

@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_japan_sensor_from_fixture(tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    hass = tokyo
    freezer.move_to(JAPAN_NOW)
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    state = hass.states.get(_japan_entity(hass))
    assert state.state == "2026-09-29T12:00:00+00:00"  # the issue time of 仙台's re-issue
    assert state.attributes["device_class"] == "timestamp"
    assert state.attributes["attribution"] == JMA_ATTRIBUTION
    cities = state.attributes["cities"]
    assert len(cities) == 22
    tokyo_city = next(c for c in cities if c["office"] == "130000")
    assert [d["date"] for d in tokyo_city["days"]] == ["2026-09-30", "2026-10-01"]
    assert len(json.dumps(cities, ensure_ascii=False).encode()) < 12_000
    assert "japan" not in hass.states.get("binary_sensor.jma_chiyoda_feed_problem").attributes


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_japan_failing_is_not_a_feed_problem(tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    hass = tokyo
    freezer.move_to(EVENING)
    aioclient_mock.get(URL_JAPAN_FORECAST, status=500)  # first registration wins
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    assert hass.states.get(_japan_entity(hass)).state == "unavailable"
    problem = hass.states.get("binary_sensor.jma_chiyoda_feed_problem")
    assert "japan" not in problem.attributes
    assert hass.states.get("sensor.jma_chiyoda_typhoon").state == "1"  # the others unaffected


# ---------- typhoon: first sighting whose files fail ----------

async def test_typhoon_first_sighting_failure_lists_the_storm_bare(
    tokyo: HomeAssistant, aioclient_mock, entry, freezer
) -> None:
    hass = tokyo
    freezer.move_to(EVENING)
    aioclient_mock.get(URL_TYPHOON_FORECAST.format(tc="TC2632"), status=500)  # first registration wins
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    state = hass.states.get("sensor.jma_chiyoda_typhoon")
    assert state.state == "1"  # counted
    assert state.attributes["storms"] == [{
        "id": "TC2632", "number": "2626", "category": "TY", "issue": "2026-09-27T22:45:00+09:00",
        "now": None, "track": [], "past_track": [], "distance_km": None, "closest": None,
        "incomplete": True,
    }]
    # the files arrive: the full entry replaces the bare one
    aioclient_mock.clear_requests()
    mock_jma(aioclient_mock)
    await entry.runtime_data.typhoon.async_refresh()
    await hass.async_block_till_done()
    storm = hass.states.get("sensor.jma_chiyoda_typhoon").attributes["storms"][0]
    assert storm["now"]["pressure_hpa"] == 945 and "incomplete" not in storm


# ---------- キキクル partial ----------

async def test_kikikuru_partial_lists_unread_elements(tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    hass = tokyo
    freezer.move_to(EVENING)
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    assert hass.states.get("sensor.jma_chiyoda_kikikuru").attributes["partial"] == []
    layers = entry.runtime_data.layers
    layers.async_set_updated_data(dataclasses.replace(
        layers.data, kikikuru=Kikikuru("20260927140000", 0, None, 1)))
    await hass.async_block_till_done()
    kiki = hass.states.get("sensor.jma_chiyoda_kikikuru")
    assert kiki.state == "1"  # a lower bound: land could not be read
    assert kiki.attributes["partial"] == ["land"]


# ---------- one AMeDAS station silent ----------

async def test_one_silent_amedas_station_becomes_a_health_reason(
    tokyo: HomeAssistant, aioclient_mock, entry_two_stations, freezer
) -> None:
    hass = tokyo
    entry = entry_two_stations
    aioclient_mock.get(URL_AMEDAS_POINT.format(station="62016", ymd="20260926", hh="12"), status=500)
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    amedas = entry.runtime_data.amedas
    assert amedas.station_ok["44132"] is not None and amedas.station_ok.get("62016") is None
    assert "500" in amedas.station_error["62016"]
    assert hass.states.get("weather.jma_chiyoda").attributes["temperature"] == 18.8  # 44132 fills in
    problem = hass.states.get("binary_sensor.jma_chiyoda_feed_problem")
    assert problem.attributes["amedas_station"]["reason"] is None  # under 40 min
    await _tick(hass, freezer, timedelta(minutes=10, seconds=5), times=5)  # margin past each poll
    reason = hass.states.get("binary_sensor.jma_chiyoda_feed_problem").attributes["amedas_station"]["reason"]
    assert reason and "62016" in reason and "44132" in reason


def _runtime(now, *, primary_ok, secondary_ok, first_try, hourly_ok, same_station=False):
    secondary = "44132" if same_station else "62016"
    return SimpleNamespace(
        warnings=SimpleNamespace(last_ok=now, last_error=None, data=None, xml_ok=now, xml_error=None),
        amedas=SimpleNamespace(
            data=None, first_try=first_try,
            cfg=SimpleNamespace(station_primary="44132", station_secondary=secondary),
            station_ok={"44132": primary_ok, secondary: secondary_ok} if not same_station else {"44132": primary_ok},
            station_error={"44132": None, "62016": "HTTP 500"},
        ),
        nowcast=SimpleNamespace(data=None),
        forecast=SimpleNamespace(data=None),
        quake=SimpleNamespace(enabled=True, connected=True, down_since=None),
        hourly=SimpleNamespace(last_ok=hourly_ok, first_try=first_try, last_error="timeout"),
    )


def test_feed_conditions_amedas_station_and_hourly() -> None:
    now = dt_util.utcnow()
    start = now - timedelta(hours=5)
    fresh, m = now - timedelta(minutes=5), timedelta(minutes=1)

    def cond(**kw):
        base = {"primary_ok": fresh, "secondary_ok": fresh, "first_try": start, "hourly_ok": fresh}
        return feed_conditions(_runtime(now, **{**base, **kw}), now)

    assert cond()["amedas_station"] is None and cond()["hourly"] is None
    assert "62016" in cond(secondary_ok=None)["amedas_station"]  # never answered in 5 h
    assert "62016" in cond(secondary_ok=now - timedelta(minutes=40) - m)["amedas_station"]
    assert cond(secondary_ok=now - timedelta(minutes=39))["amedas_station"] is None
    assert cond(secondary_ok=None, first_try=now - timedelta(minutes=30))["amedas_station"] is None
    assert cond(primary_ok=None, secondary_ok=None)["amedas_station"] is None  # both: `amedas` says so
    assert "44132" in cond(primary_ok=now - timedelta(hours=2))["amedas_station"]
    assert feed_conditions(_runtime(now, primary_ok=None, secondary_ok=None, first_try=start,
                                    hourly_ok=fresh, same_station=True), now)["amedas_station"] is None
    assert "Open-Meteo" in cond(hourly_ok=now - timedelta(hours=3) - m)["hourly"]
    assert cond(hourly_ok=now - timedelta(hours=2))["hourly"] is None
    assert cond(hourly_ok=None)["hourly"] is not None  # nothing in 5 h
    assert cond(hourly_ok=None, first_try=now - timedelta(hours=1))["hourly"] is None


# ---------- forecasts gated on the coordinator's success ----------

async def test_forecasts_not_served_after_the_coordinator_failed(
    tokyo: HomeAssistant, aioclient_mock, entry
) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    rt = entry.runtime_data
    rt.forecast.last_update_success = False  # as after STALE_FACTOR x interval of failures
    rt.hourly.last_update_success = False
    for kind in ("daily", "hourly"):
        out = await hass.services.async_call(
            "weather", "get_forecasts", {"entity_id": "weather.jma_chiyoda", "type": kind},
            blocking=True, return_response=True)
        assert out["weather.jma_chiyoda"]["forecast"] == [], kind
