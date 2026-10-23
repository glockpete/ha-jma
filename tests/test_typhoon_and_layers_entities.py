"""Integration layer for the optional feeds: warning outlook, typhoon, heat index and map layers."""

from __future__ import annotations

import json
import logging

import pytest
from homeassistant.core import HomeAssistant

from custom_components.jma.const import URL_TYPHOON_TARGET, URL_WBGT, WBGT_ATTRIBUTION

from .conftest import mock_jma, setup

EVENING = "2026-09-27T14:30:00+00:00"  # an evening moment after the sample files were issued


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_optional_entities_from_sample_files(tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    hass = tokyo
    freezer.move_to(EVENING)
    mock_jma(aioclient_mock)
    await setup(hass, entry)

    outlook = hass.states.get("sensor.jma_chiyoda_warning_outlook")
    assert outlook.state == "2"  # 雷 L2 from 06:00
    assert len(outlook.attributes["steps"]) == 8
    assert outlook.attributes["peak"] == {
        "level": 2, "phenomena": ["thunder"], "start": "2026-09-28T06:00:00+09:00"}
    assert outlook.attributes["report_time"] == "2026-09-27T23:00:00+09:00"

    typhoon = hass.states.get("sensor.jma_chiyoda_typhoon")
    assert typhoon.state == "1"
    storm = typhoon.attributes["storms"][0]
    assert storm["id"] == "TC2632" and storm["name_en"] == "Surigae"
    # closest point on the track line (+71..+95 h), not the nearest centre (+95 h)
    # 86.44 h is the closest point of the fixture polyline to the tests' place; 86.5 is the plane projection's value
    assert storm["closest"]["hours"] == 86.5 and storm["closest"]["between"] == [71, 95]
    assert storm["now"]["pressure_hpa"] == 945
    assert len(json.dumps(typhoon.attributes["storms"], ensure_ascii=False).encode()) < 12 * 1024

    wbgt = hass.states.get("sensor.jma_chiyoda_wbgt")
    assert float(wbgt.state) == 20.0  # 44132, the 27th's only slot left (24時)
    assert wbgt.attributes["station"] == "44132"
    assert wbgt.attributes["attribution"] == WBGT_ATTRIBUTION
    assert wbgt.attributes["unit_of_measurement"] == "°C"
    assert "device_class" not in wbgt.attributes

    kiki = hass.states.get("sensor.jma_chiyoda_kikikuru")
    assert kiki.state == "0"
    assert {k: kiki.attributes[k] for k in ("inund", "land", "flood", "basetime")} == {
        "inund": 0, "land": 0, "flood": 0, "basetime": "20260927140000"}

    rain = hass.states.get("sensor.jma_chiyoda_rain_outlook")
    assert float(rain.state) == 1.0  # band 1 (<1 mm/h) in every frame of the sample tile
    assert rain.attributes["first_rain"] is None
    assert len(rain.attributes["series"]) == 13
    assert rain.attributes["basetime"] == "20260927140000"

    frames = hass.states.get("sensor.jma_chiyoda_radar_frames")
    layers = frames.attributes["layers"]
    assert set(layers) == {"thns", "trns", "liden", "risk_inund", "risk_land", "risk_flood", "rasrf",
                           "himawari_ir", "flood_hazard"}
    assert len(layers["liden"]["frames"]) == 12 and layers["liden"]["format"] == "geojson"
    assert len(layers["risk_land"]["frames"]) == 7 and len(layers["himawari_ir"]["frames"]) == 12
    assert len(json.dumps(layers, ensure_ascii=False).encode()) < 16 * 1024
    assert len(frames.attributes["past"]) == 12  # the nowcast part is unchanged

    # optional feeds never become a feed-health reason
    problem = hass.states.get("binary_sensor.jma_chiyoda_feed_problem")
    assert set(problem.attributes) >= {"warnings", "amedas", "nowcast", "forecast", "quake"}
    assert not {"timeline", "typhoon", "wbgt", "layers", "japan"} & set(problem.attributes)


async def test_wbgt_out_of_season_is_quietly_unavailable(
    tokyo: HomeAssistant, aioclient_mock, entry, freezer, caplog
) -> None:
    hass = tokyo
    freezer.move_to(EVENING)
    caplog.set_level(logging.DEBUG, logger="custom_components.jma")
    aioclient_mock.get(URL_WBGT.format(pref="tokyo"), status=404)  # first registration wins
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    assert hass.states.get("sensor.jma_chiyoda_wbgt").state == "unavailable"
    # Only the integration's own loggers: asyncio's slow-task warning (a freezer artefact) names this test.
    loud = [r for r in caplog.records if r.levelno >= logging.WARNING
            and r.name.startswith("custom_components.jma") and "wbgt" in r.getMessage().lower()]
    assert loud == []
    assert hass.states.get("sensor.jma_chiyoda_typhoon").state == "1"  # the others unaffected


async def test_no_typhoon_file_means_none(tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    """If JMA drops targetTc.json (404) when no storm is active, that is 0 storms, not an outage."""
    hass = tokyo
    freezer.move_to(EVENING)
    aioclient_mock.get(URL_TYPHOON_TARGET, status=404)  # first registration wins
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    state = hass.states.get("sensor.jma_chiyoda_typhoon")
    assert state.state == "0" and state.attributes.get("storms") == []
