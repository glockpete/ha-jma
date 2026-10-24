"""The heat index (WBGT) for every forecast office: the file of the office's prefecture, the point nearest to the home
of Home Assistant, and a quiet unknown outside the Ministry's season."""

from __future__ import annotations

import json
import logging
from datetime import date

import pytest
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.jma.api import JmaApi
from custom_components.jma.const import DOMAIN, URL_WBGT, WBGT_ATTRIBUTION
from custom_components.jma.coordinator import JmaConfig, WbgtCoordinator, WbgtState
from custom_components.jma.diagnostics import async_get_config_entry_diagnostics
from custom_components.jma.parse_wbgt import nearest_points
from custom_components.jma.sensor import WbgtSensor

from .conftest import mock_jma, setup
from .helpers import load_text, wbgt_on
from .test_wbgt_points import at

FIRST_DAY = date(2026, 9, 26)  # the day of conftest.NOW; the captures are moved to start on it
OFF_SEASON = "2026-11-15T03:00:00+00:00"  # 12:00 JST on 15 November
STATION_ID = "62016"  # the AMeDAS point the synthetic file of the tests answers for (the entry needs one)

# office, area, class10, prefecture file, nearest point (number, name), highest figure of the capture's first day
OFFICES = [
    pytest.param("270000", "2714000", "270000", "osaka", ("62078", "大阪"), 20.0, id="osaka"),
    pytest.param("474000", "4720700", "474010", "okinawa", ("94081", "石垣島"), 26.0, id="yaeyama-okinawa"),
    pytest.param("014030", "0120700", "014030", "hokkaido", ("20432", "帯広"), 10.0, id="tokachi-hokkaido"),
    pytest.param("460040", "4622200", "460040", "kagoshima", ("88837", "名瀬"), 25.0, id="amami-kagoshima"),
]


def _entry(office: str, area: str, class10: str) -> MockConfigEntry:
    data = {"office": office, "area": area, "area_name": "Test", "class10": class10,
            "station_primary": STATION_ID, "station_secondary": STATION_ID}
    return MockConfigEntry(domain=DOMAIN, title="JMA Test", unique_id=area, data=data)


def _wbgt_state(hass: HomeAssistant, area: str) -> State:
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{area}_wbgt")
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    return state


def _asked_for(aioclient_mock, pref: str) -> int:
    return sum(1 for call in aioclient_mock.mock_calls if str(call[1]) == URL_WBGT.format(pref=pref))


async def _start(hass: HomeAssistant, aioclient_mock, entry: MockConfigEntry, pref: str, text: str | None,
                 home: tuple[float, float], status: int = 200) -> None:
    """Set up an entry whose home is `home` and whose prefecture file answers `text` (or `status`)."""
    hass.config.latitude, hass.config.longitude = home
    if text is None:
        aioclient_mock.get(URL_WBGT.format(pref=pref), status=status)
    else:
        aioclient_mock.get(URL_WBGT.format(pref=pref), text=text)
    mock_jma(aioclient_mock)
    await setup(hass, entry)


@pytest.mark.parametrize(("office", "area", "class10", "pref", "point", "first_day_max"), OFFICES)
async def test_every_office_gets_the_wbgt_sensor_for_the_nearest_point(
    tokyo: HomeAssistant, aioclient_mock, office, area, class10, pref, point, first_day_max
) -> None:
    number, name = point
    await _start(tokyo, aioclient_mock, _entry(office, area, class10), pref,
                 wbgt_on(f"wbgt_yohou_{pref}.csv", FIRST_DAY), at(number))
    state = _wbgt_state(tokyo, area)
    assert float(state.state) == first_day_max
    attrs = state.attributes
    assert (attrs["station"], attrs["station_name"]) == (number, name)
    assert 1.2 < attrs["station_km"] < 1.6 and attrs["season"] is True
    assert attrs["issued"] == "2026-09-26T00:25:00+09:00" and len(attrs["series"]) == 24
    assert attrs["series"][0]["time"] == "2026-09-26T03:00:00+09:00"
    assert attrs["series"][-1]["time"] == "2026-09-29T00:00:00+09:00"  # 24 slots, 3-hourly, over three days
    assert attrs["attribution"] == WBGT_ATTRIBUTION and attrs["unit_of_measurement"] == "°C"
    assert "device_class" not in attrs
    assert _asked_for(aioclient_mock, pref) == 1  # the one file of the office's prefecture, once


async def test_the_sensor_is_registered_enabled_for_an_office_that_was_not_mapped_before(
    tokyo: HomeAssistant, aioclient_mock
) -> None:
    entry = _entry("270000", "2714000", "270000")
    await _start(tokyo, aioclient_mock, entry, "osaka", wbgt_on("wbgt_yohou_osaka.csv", FIRST_DAY), at("62078"))
    registry = er.async_get(tokyo)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, "2714000_wbgt")
    assert entity_id is not None and registry.async_get(entity_id).disabled_by is None
    assert registry.async_get(entity_id).translation_key == "wbgt"
    # the other sensors of the entry are there as well
    assert registry.async_get_entity_id("sensor", DOMAIN, "2714000_warning_level") is not None


async def test_a_home_far_from_the_prefecture_reads_the_nearest_point_and_says_how_far(
    tokyo: HomeAssistant, aioclient_mock
) -> None:
    """The home of Home Assistant is Tokyo Station (the tests' place) but the entry is Osaka's office."""
    entry = _entry("270000", "2714000", "270000")
    await _start(tokyo, aioclient_mock, entry, "osaka", wbgt_on("wbgt_yohou_osaka.csv", FIRST_DAY), (35.6812, 139.7671))
    attrs = _wbgt_state(tokyo, "2714000").attributes
    nearest = nearest_points("osaka", 35.6812, 139.7671)[0]
    assert attrs["station"] == nearest.number and attrs["station_km"] == nearest.km > 300


async def test_when_the_nearest_point_has_no_row_the_next_nearest_is_read(tokyo: HomeAssistant, aioclient_mock) -> None:
    lines = wbgt_on("wbgt_yohou_osaka.csv", FIRST_DAY).splitlines()
    text = "\n".join(line for line in lines if not line.startswith("62078,")) + "\n"
    assert len(text.splitlines()) == len(lines) - 1  # the row was there and is gone
    second = nearest_points("osaka", *at("62078"))[1]
    await _start(tokyo, aioclient_mock, _entry("270000", "2714000", "270000"), "osaka", text, at("62078"))
    state = _wbgt_state(tokyo, "2714000")
    assert state.attributes["station"] == second.number != "62078"
    assert state.attributes["station_name"] == "生駒山" and state.attributes["station_km"] == second.km
    assert float(state.state) == 16.0  # 62081, highest of the first day's eight slots (160)


async def test_a_file_with_no_row_for_the_nearest_points_is_unknown_not_unavailable(
    tokyo: HomeAssistant, aioclient_mock, caplog
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.jma")
    wanted = {p.number for p in nearest_points("osaka", *at("62078"))}
    lines = wbgt_on("wbgt_yohou_osaka.csv", FIRST_DAY).splitlines()
    text = "\n".join(line for line in lines if line.split(",")[0] not in wanted) + "\n"
    await _start(tokyo, aioclient_mock, _entry("270000", "2714000", "270000"), "osaka", text, at("62078"))
    state = _wbgt_state(tokyo, "2714000")
    assert state.state == "unknown"
    assert state.attributes["season"] is True and state.attributes["station"] == "62078"  # the one asked for first
    assert "series" not in state.attributes and "issued" not in state.attributes
    assert [r for r in caplog.records if r.levelno >= logging.WARNING and r.name.startswith("custom_components.jma")
            and "wbgt" in r.getMessage().lower()] == []


async def test_a_missing_file_in_season_is_a_quiet_unknown(tokyo: HomeAssistant, aioclient_mock, caplog) -> None:
    """The Ministry's file may not be out yet on the first days of the season (HTTP 404)."""
    caplog.set_level(logging.DEBUG, logger="custom_components.jma")
    await _start(tokyo, aioclient_mock, _entry("270000", "2714000", "270000"), "osaka", None, at("62078"), status=404)
    state = _wbgt_state(tokyo, "2714000")
    assert state.state == "unknown" and state.attributes["season"] is True and state.attributes["station"] == "62078"
    assert [r for r in caplog.records if r.levelno >= logging.WARNING and r.name.startswith("custom_components.jma")
            and "wbgt" in r.getMessage().lower()] == []


async def test_a_server_error_makes_the_sensor_unavailable_and_it_recovers(tokyo: HomeAssistant, aioclient_mock) -> None:
    entry = _entry("270000", "2714000", "270000")
    await _start(tokyo, aioclient_mock, entry, "osaka", None, at("62078"), status=500)
    assert _wbgt_state(tokyo, "2714000").state == "unavailable"
    assert entry.runtime_data.wbgt.data is None
    sensor = WbgtSensor(entry)  # what the sensor would say with no data at all
    assert sensor.native_value is None and sensor.extra_state_attributes == {}
    aioclient_mock.clear_requests()
    mock_jma(aioclient_mock)
    aioclient_mock.get(URL_WBGT.format(pref="osaka"), text=wbgt_on("wbgt_yohou_osaka.csv", FIRST_DAY))
    await entry.runtime_data.wbgt.async_refresh()
    await tokyo.async_block_till_done()
    assert float(_wbgt_state(tokyo, "2714000").state) == 20.0


async def test_out_of_season_nothing_is_requested_and_the_sensor_is_a_quiet_unknown(
    tokyo: HomeAssistant, aioclient_mock, freezer, caplog
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.jma")
    freezer.move_to(OFF_SEASON)
    entry = _entry("270000", "2714000", "270000")
    await _start(tokyo, aioclient_mock, entry, "osaka", load_text("wbgt_yohou_osaka.csv"), at("62078"))
    state = _wbgt_state(tokyo, "2714000")
    assert state.state == "unknown"  # not "unavailable"
    assert state.attributes["season"] is False
    assert (state.attributes["station"], state.attributes["station_name"]) == ("62078", "大阪")
    assert "series" not in state.attributes and "issued" not in state.attributes
    assert state.attributes["attribution"] == WBGT_ATTRIBUTION
    coordinator = entry.runtime_data.wbgt
    assert coordinator.last_update_success is True and coordinator.last_error is None
    # polls go on all winter and none of them asks the Ministry or says anything above debug
    for _ in range(3):
        freezer.tick(coordinator.update_interval)
        await coordinator.async_refresh()
    assert _asked_for(aioclient_mock, "osaka") == 0
    assert coordinator.last_update_success is True and _wbgt_state(tokyo, "2714000").state == "unknown"
    assert [r for r in caplog.records if r.levelno >= logging.INFO and r.name.startswith("custom_components.jma")
            and "wbgt" in r.getMessage().lower()] == []
    # and it is not a feed problem: the optional feeds never are
    problem = tokyo.states.get("binary_sensor.jma_test_feed_problem")
    assert problem is not None and "wbgt" not in problem.attributes


@pytest.mark.parametrize(("moment", "season"), [
    ("2026-04-21T14:59:00+00:00", False),  # 23:59 JST on 21 April: the 4th Wednesday is the 22nd
    ("2026-04-21T15:00:00+00:00", True),  # 00:00 on the first day
    ("2026-10-21T14:59:00+00:00", True),  # 23:59 on the last day, 21 October
    ("2026-10-21T15:00:00+00:00", False),  # 00:00 on 22 October
])
async def test_the_season_is_the_ministrys_in_japan_time(
    tokyo: HomeAssistant, aioclient_mock, freezer, moment: str, season: bool
) -> None:
    """The same season as the heat alerts (parse_heat_alert.in_season); the clock is read in JST."""
    entry = _entry("270000", "2714000", "270000")
    await _start(tokyo, aioclient_mock, entry, "osaka", wbgt_on("wbgt_yohou_osaka.csv", FIRST_DAY), at("62078"))
    asked = _asked_for(aioclient_mock, "osaka")
    freezer.move_to(moment)
    coordinator = entry.runtime_data.wbgt
    await coordinator.async_refresh()
    assert coordinator.data.season is season
    assert _asked_for(aioclient_mock, "osaka") == asked + (1 if season else 0)


async def test_an_office_without_a_file_asks_for_nothing(tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    """Not reachable through the config flow (every office of the area list has a prefecture); the coordinator
    answers 'no data' rather than guess a file."""
    entry.add_to_hass(tokyo)
    cfg = JmaConfig(office="990000", area="9900000", area_name="x", class10="990000", station_primary=STATION_ID,
                    station_secondary=STATION_ID, latitude=35.6812, longitude=139.7671)
    coordinator = WbgtCoordinator(tokyo, entry, JmaApi(async_get_clientsession(tokyo)), cfg)
    assert coordinator.pref is None and coordinator.points == ()
    state = await coordinator._fetch()
    assert state == WbgtState(None, True, ())
    assert aioclient_mock.call_count == 0


async def test_the_sensor_without_a_point_or_a_file_has_only_the_season(tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    mock_jma(aioclient_mock)
    await setup(tokyo, entry)
    entry.runtime_data.wbgt.async_set_updated_data(WbgtState(None, False, ()))
    await tokyo.async_block_till_done()
    state = tokyo.states.get("sensor.jma_chiyoda_wbgt")
    assert state.state == "unknown" and state.attributes["season"] is False
    assert not {"station", "station_name", "station_km", "series", "issued"} & set(state.attributes)


async def test_diagnostics_name_the_prefecture_file_but_not_the_point(tokyo: HomeAssistant, aioclient_mock) -> None:
    """A failed fetch keeps its URL in last_error: it names the prefecture (as the office code does), never the point
    nearest to the home."""
    entry = _entry("270000", "2714000", "270000")
    await _start(tokyo, aioclient_mock, entry, "osaka", None, at("62078"), status=500)
    out = await async_get_config_entry_diagnostics(tokyo, entry)
    text = json.dumps(out, ensure_ascii=False, default=str)
    assert "yohou_osaka.csv" in text and "HTTP 500" in text
    point = nearest_points("osaka", *at("62078"))[0]
    for value in (point.number, point.name):
        assert value not in text, value
