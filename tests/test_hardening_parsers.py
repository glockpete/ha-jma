"""Hardening, poll-side parsers and coordinators: hostile or garbled feed files must not wedge a
coordinator, must not raise past its keep-last-good handling, and must not forge an alert.

Each test asserts the FIXED behaviour (H1, H1b, H6, H6b, H7, H8, H9 of the hostile-feed review).
"""

from __future__ import annotations

import csv
import json
import logging
from datetime import date, datetime, timedelta, timezone

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.jma import parse_japan as parse_japan_module
from custom_components.jma.api import JmaApi
from custom_components.jma.const import STALE_FACTOR, URL_FORECAST, URL_TYPHOON_FORECAST
from custom_components.jma.coordinator import HeatAlertCoordinator, JmaConfig
from custom_components.jma.errors import JmaParseError
from custom_components.jma.parse_amedas import extreme_time, parse_point
from custom_components.jma.parse_forecast import parse_forecast
from custom_components.jma.parse_heat_alert import file_status, parse_heat_alert
from custom_components.jma.parse_japan import parse_japan
from custom_components.jma.parse_typhoon import ApproachTracker, approach_stage, parse_storm
from custom_components.jma.parse_warnings import parse_area_warnings, suspect_reason
from custom_components.jma.parse_wbgt import parse_wbgt

from .conftest import AREA, ENTRY_DATA, mock_jma, setup
from .helpers import clone, load_json, load_text
from .test_heat_alert import _url

JST = timezone(timedelta(hours=9))
LEVEL = "sensor.jma_chiyoda_warning_level"
ALERTS = "event.jma_chiyoda_alerts"
COORDINATOR_LOG = "custom_components.jma.coordinator"

# What a hostile or broken feed can put where a number belongs: JSON `1e400` parses to inf, `NaN` and
# `Infinity` parse to floats, a long digit string to an int too big for a float.
NOT_FINITE = [float("inf"), float("-inf"), float("nan"), 10**400, "1e999", "nan", "inf"]
NOT_FINITE_IDS = ["inf", "-inf", "nan", "10**400", "str-1e999", "str-nan", "str-inf"]


# ---------- warnings: an unreadable controlDatetime (H1, H1b) ----------

def _r8(code: str, times: dict[str, str] | None = None, extra: list | None = None) -> list:
    records = clone(load_json("warning_r8_130000.json"))
    for record in records:
        if times and record["dataTypeCode"] in times:
            record["controlDatetime"] = times[record["dataTypeCode"]]
        if record["dataTypeCode"] == "VPWW55":
            for item in record["warning"]["class20Items"]:
                if item["areaCode"] == AREA:
                    item["kinds"] = [{"code": code, "status": "発表"}]
    return records + (extra or [])


async def _poll(hass, freezer, aioclient_mock, warnings, n=1):
    for _ in range(n):
        aioclient_mock.clear_requests()
        mock_jma(aioclient_mock, warnings=warnings)
        freezer.tick(timedelta(seconds=61))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


async def test_h1_bad_control_time_accepted_on_first_poll_does_not_wedge(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    mock_jma(aioclient_mock, warnings=_r8("03", {"VPWW58": "2026-09-22 19:03:59Zjunk"}))
    await setup(tokyo, entry)
    assert tokyo.states.get(LEVEL).state == "3"
    # the feed is correct again and Chiyoda goes to an L5 special warning
    await _poll(tokyo, freezer, aioclient_mock, _r8("33"))
    assert entry.runtime_data.warnings.last_error is None
    assert tokyo.states.get(LEVEL).state == "5"
    assert tokyo.states.get(ALERTS).attributes["event_type"] == "warning_raised"


async def test_h1b_new_record_with_bad_time_mid_run_does_not_wedge(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    mock_jma(aioclient_mock, warnings=_r8("03"))
    await setup(tokyo, entry)
    assert tokyo.states.get(LEVEL).state == "3"
    new = clone(load_json("warning_r8_130000.json")[0])
    new["dataTypeCode"] = "VPWW60"  # a type this file did not have
    new["controlDatetime"] = "not-a-time"
    await _poll(tokyo, freezer, aioclient_mock, _r8("03", extra=[new]))
    good = clone(new)
    good["controlDatetime"] = "2026-09-26T03:00:00Z"
    await _poll(tokyo, freezer, aioclient_mock, _r8("33", extra=[good]))
    assert entry.runtime_data.warnings.last_error is None
    assert tokyo.states.get(LEVEL).state == "5"


async def test_unreadable_time_on_a_known_record_is_held_a_few_polls_then_believed(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    mock_jma(aioclient_mock, warnings=_r8("03"))
    await setup(tokyo, entry)
    assert tokyo.states.get(LEVEL).state == "3"
    garbage = _r8("33", {"VPWW58": "not-a-time"})  # VPWW58 was readable on the first poll
    co = entry.runtime_data.warnings
    await _poll(tokyo, freezer, aioclient_mock, garbage, n=2)
    assert co.last_error and "r8 held" in co.last_error  # the existing partial / older file hold
    assert tokyo.states.get(LEVEL).state == "3"
    await _poll(tokyo, freezer, aioclient_mock, garbage)  # still there: believed, as for any held file
    assert co.last_error is None
    assert tokyo.states.get(LEVEL).state == "5"


def test_suspect_reason_ignores_an_unreadable_previous_time_and_flags_an_unreadable_new_one():
    area = parse_area_warnings(load_json("warning_r8_130000.json"), AREA)
    good = dict(area.control_times)
    assert suspect_reason(good, area) is None
    broken_prev = {**good, "VPWW55": "garbage"}  # accepted earlier: no usable previous value
    assert suspect_reason(broken_prev, area) is None
    broken_new = parse_area_warnings(_r8("03", {"VPWW55": "garbage"}), AREA)
    reason = suspect_reason(good, broken_new)
    assert reason is not None and "VPWW55" in reason and "time" in reason
    # both unreadable: nothing to compare
    assert suspect_reason(broken_prev, broken_new) is None
    # valid times keep their meaning: an older copy is still reported, whatever else is unreadable
    older = parse_area_warnings(_r8("03", {"VPWW55": "2000-01-01T00:00:00Z", "VPWW58": "garbage"}), AREA)
    assert "older copy of: VPWW55" in suspect_reason(good, older)


# ---------- non-finite and absurd numbers (H6, H6b) ----------

def _poison(obj, keys: set[str], bad) -> None:
    """Replace every element of every list found under one of `keys`, at any depth, with `bad`."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in keys and isinstance(value, list):
                value[:] = [bad] * len(value)
            else:
                _poison(value, keys, bad)
    elif isinstance(obj, list):
        for item in obj:
            _poison(item, keys, bad)


@pytest.mark.parametrize("bad", NOT_FINITE, ids=NOT_FINITE_IDS)
def test_forecast_non_finite_numbers_read_as_missing(bad):
    data = clone(load_json("forecast_130000.json"))
    _poison(data, {"pops", "temps", "tempsMin", "tempsMax", "tempsMinUpper", "tempsMaxUpper",
                   "tempsMinLower", "tempsMaxLower"}, bad)
    for area in (data[1].get("tempAverage") or {}).get("areas") or []:
        area["min"] = area["max"] = bad
    parsed = parse_forecast(data, "130010", "44132")
    assert parsed.days
    for day in parsed.days:
        assert day.pop is None and day.temp_max is None and day.temp_min is None
        assert day.temp_max_upper is None and day.temp_min_lower is None
    assert all(p is None for _t, p in parsed.pops_6h)
    assert parsed.normal_temp_max is None and parsed.normal_temp_min is None


async def test_h6_forecast_overflow_keeps_the_poll_good(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer, caplog) -> None:
    mock_jma(aioclient_mock)
    await setup(tokyo, entry)
    co = entry.runtime_data.forecast
    assert co.last_update_success
    bad = clone(load_json("forecast_130000.json"))
    bad[0]["timeSeries"][1]["areas"][0]["pops"][0] = "1e999"
    aioclient_mock.clear_requests()
    aioclient_mock.get(URL_FORECAST.format(office="130000"), json=bad)
    mock_jma(aioclient_mock)
    caplog.clear()
    await co.async_refresh()
    assert co.last_update_success and co.data.forecast.days
    assert "Unexpected error" not in caplog.text and "OverflowError" not in caplog.text


@pytest.mark.parametrize("bad", NOT_FINITE, ids=NOT_FINITE_IDS)
def test_japan_non_finite_numbers_do_not_drop_the_cities(bad):
    data = clone(load_json("forecast_010000.json"))
    _poison(data, {"pops", "temps", "tempsMin", "tempsMax"}, bad)
    result = parse_japan(data, date(2026, 9, 29))
    assert len(result.cities) == len(clone(load_json("forecast_010000.json")))
    assert all(d["temp_max"] is None and d["temp_min"] is None and d["pop"] is None
               for city in result.cities for d in city["days"])


def test_japan_one_office_overflowing_is_skipped_not_fatal(monkeypatch):
    data = load_json("forecast_010000.json")
    real = parse_japan_module.parse_forecast
    calls = []

    def flaky(blocks, class10, station):
        calls.append(1)
        if len(calls) == 1:
            raise OverflowError("int too large to convert to float")
        return real(blocks, class10, station)

    monkeypatch.setattr(parse_japan_module, "parse_forecast", flaky)
    result = parse_japan(data, date(2026, 9, 29))
    assert len(result.cities) == len(data) - 1


def test_amedas_non_finite_readings_read_as_missing():
    for bad in (float("inf"), float("-inf"), float("nan"), 10**400):
        data = clone(load_json("point_44132_12.json"))
        for row in data.values():
            for element in ("temp", "gust", "wind"):
                if element in row:
                    row[element] = [bad, 0]
        obs = parse_point(data, "44132")
        assert obs.temperature is None and obs.gust_max is None and obs.wind_speed is None
        assert obs.humidity is not None  # the rest of the file is still read


@pytest.mark.parametrize("hm", [{"hour": 3, "minute": 10**400}, {"hour": 10**400, "minute": 3},
                                {"hour": 99, "minute": 0}, {"hour": 3, "minute": -5}, {"hour": True, "minute": 0}])
def test_amedas_absurd_extreme_time_is_none(hm):
    row_time = datetime(2026, 9, 26, 12, 50, tzinfo=JST)
    assert extreme_time(row_time, hm) is None
    data = clone(load_json("point_44132_12.json"))
    for row in data.values():
        row["gustTime"] = hm
    assert parse_point(data, "44132").gust_max_time is None


def test_amedas_good_extreme_time_is_unchanged():
    row_time = datetime(2026, 9, 26, 12, 50, tzinfo=JST)
    assert extreme_time(row_time, {"hour": 3, "minute": 10}) == datetime(2026, 9, 26, 12, 10, tzinfo=JST)
    assert extreme_time(row_time, {"hour": 0, "minute": 0}) == datetime(2026, 9, 26, 9, 0, tzinfo=JST)


def test_wbgt_huge_cell_is_missing_not_an_overflow():
    text = ",,2026092703,2026092706,2026092709\n44132,2026/09/26 22:25, 200," + "9" * 400 + ", 210\n"
    forecast = parse_wbgt(text, ("44132",), date(2026, 9, 27))
    assert forecast is not None
    assert [s["wbgt"] for s in forecast.series] == [20.0, None, 21.0]
    assert forecast.today_max == 21.0


# ---------- the heat-alert CSV (H9) ----------

HOSTILE_CSV = '<html>,"' + "x" * 140000  # an unclosed quote: csv.Error "field larger than field limit"


def test_heat_alert_csv_error_is_an_unusable_file():
    with pytest.raises(csv.Error):  # the premise: the stdlib really raises on this body
        list(csv.reader(HOSTILE_CSV.splitlines()))
    assert parse_heat_alert(HOSTILE_CSV, "130000") is None
    assert file_status(HOSTILE_CSV) is None


def _heat_cfg() -> JmaConfig:
    return JmaConfig(office=ENTRY_DATA["office"], area=ENTRY_DATA["area"], area_name=ENTRY_DATA["area_name"],
                     class10=ENTRY_DATA["class10"], station_primary=ENTRY_DATA["station_primary"],
                     station_secondary=ENTRY_DATA["station_secondary"], latitude=35.6812, longitude=139.7671)


def _heat(hass: HomeAssistant, entry, fire=None, memory=None) -> HeatAlertCoordinator:
    entry.add_to_hass(hass)
    co = HeatAlertCoordinator(hass, entry, JmaApi(async_get_clientsession(hass)), _heat_cfg(),
                              fire or (lambda _t, _d: None), memory if memory is not None else [], lambda: None)
    co.station_names = ("東京", "東京")
    return co


def _quiet(aioclient_mock, day: int, hours=(5, 10, 14, 17)) -> None:
    """HTTP 404 for the files of one issue day (the answer when a file is not out yet)."""
    for hour in hours:
        aioclient_mock.get(_url(day, hour), status=404)


def _coordinator_warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == COORDINATOR_LOG and r.levelno >= logging.WARNING]


async def test_h9_heat_csv_error_keeps_last_good_and_falls_back(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer, caplog) -> None:
    freezer.move_to("2026-07-20T08:10:00+00:00")  # 17:10 JST
    aioclient_mock.get(_url(20260720, 17), text=HOSTILE_CSV)
    aioclient_mock.get(_url(20260720, 14), text=load_text("alert_20260720_17.csv"))
    co = _heat(tokyo, entry)
    caplog.clear()
    await co.async_refresh()
    assert co.last_update_success and co.data.bulletin is not None  # the older file answered
    assert co.data.bulletin.flag1 == "1"
    warned = _coordinator_warnings(caplog)
    assert len(warned) == 1 and warned[0].exc_info is None and "alert_20260720_17.csv" in warned[0].getMessage()
    await co.async_refresh()  # the hostile file is asked for again, but named only once
    assert co.last_update_success and len(_coordinator_warnings(caplog)) == 1


async def test_h9_heat_csv_error_with_nothing_older_is_a_quiet_poll(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer, caplog) -> None:
    freezer.move_to("2026-07-20T08:10:00+00:00")
    aioclient_mock.get(_url(20260720, 17), text=HOSTILE_CSV)
    aioclient_mock.get(_url(20260720, 14), status=404)
    co = _heat(tokyo, entry)
    caplog.clear()
    await co.async_refresh()
    assert co.last_update_success and co.data.bulletin is None
    assert "Unexpected error" not in caplog.text and "Traceback" not in caplog.text


# ---------- the keep-last-good tuple takes OverflowError and csv.Error ----------

COORDINATORS = ["forecast", "hourly", "amedas", "warnings", "nowcast", "timeline", "typhoon", "wbgt",
                "layers", "japan", "heat_alert"]


@pytest.mark.parametrize("error", [OverflowError("int too large to convert to float"),
                                   csv.Error("field larger than field limit (131072)")],
                         ids=["OverflowError", "csv.Error"])
@pytest.mark.parametrize("name", COORDINATORS)
async def test_overflow_and_csv_error_keep_last_good_then_fail_at_the_threshold(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer, caplog, monkeypatch, name, error) -> None:
    mock_jma(aioclient_mock)
    await setup(tokyo, entry)
    co = getattr(entry.runtime_data, name)
    assert co.last_update_success and co.data is not None
    good = co.data

    async def boom():
        raise error

    monkeypatch.setattr(co, "_fetch", boom)
    caplog.clear()
    await co.async_refresh()
    assert co.last_update_success and co.data is good  # kept
    warned = _coordinator_warnings(caplog)
    assert len(warned) == 1 and warned[0].exc_info is None and type(error).__name__ in warned[0].getMessage()
    assert "Unexpected error" not in caplog.text
    freezer.tick(co.update_interval * STALE_FACTOR + timedelta(seconds=1))  # the existing threshold
    await co.async_refresh()
    assert not co.last_update_success


# ---------- the typhoon parser (H6b, H7) ----------

HOME = (35.68, 139.76)
NOW = datetime(2026, 9, 27, 22, 50, tzinfo=JST)


def _typhoon_files():
    return clone(load_json("typhoon_TC2632_forecast.json")), clone(load_json("typhoon_TC2632_specs.json"))


async def test_h6b_typhoon_huge_integer_does_not_fail_the_poll(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer, caplog) -> None:
    mock_jma(aioclient_mock)
    await setup(tokyo, entry)
    co = entry.runtime_data.typhoon
    assert co.last_update_success
    bad, _ = _typhoon_files()
    bad[1]["stormWarningArea"]["arc"][0][1] = 10**400  # a JSON integer with 401 digits
    aioclient_mock.clear_requests()
    aioclient_mock.get(URL_TYPHOON_FORECAST.format(tc="TC2632"), text=json.dumps(bad))  # first match wins
    mock_jma(aioclient_mock)
    caplog.clear()
    await co.async_refresh()
    assert co.last_update_success
    assert "Unexpected error" not in caplog.text and "OverflowError" not in caplog.text
    storm = co.data.storms[0]
    assert storm["id"] == "TC2632" and not storm.get("incomplete") and storm["now"] is not None


@pytest.mark.parametrize("radius", [float("inf"), float("-inf"), float("nan"), -1, -83340, 0, 1e300, 10**400,
                                    "inf", "nan", None, True],
                         ids=["inf", "-inf", "nan", "-1", "-83340", "0", "1e300", "10**400", "str-inf", "str-nan",
                              "none", "true"])
def test_h7_unusable_zone_radius_is_ignored_not_urgent_everywhere(radius):
    fc, sp = _typhoon_files()
    good = parse_storm({"id": "TC2632"}, clone(fc), sp, HOME)
    assert fc[3]["advancedHours"] == 2
    fc[3]["stormWarningArea"]["arc"][0][1] = radius
    bad = parse_storm({"id": "TC2632"}, fc, sp, HOME)
    assert approach_stage(good, NOW) == "none" and good["distance_km"] > 1000
    assert approach_stage(bad, NOW) == "none"
    assert ApproachTracker({}).update([{**bad, "stage": approach_stage(bad, NOW)}], NOW) == []
    zone = bad["storm_zone"]
    assert zone is not None and all(0 < step["radius_km"] < 2000 for step in zone["steps"])
    assert 2 not in {step["hours"] for step in zone["steps"]}  # the step with the unusable radius is out
    assert zone["margin_km"] is not None and zone["margin_km"] > 0  # nowhere near inside


def test_h7_the_reviewers_infinite_radius_made_a_typhoon_approach_event():
    fc, sp = _typhoon_files()
    fc[3]["stormWarningArea"]["arc"][0][1] = float("inf")
    bad = parse_storm({"id": "TC2632"}, fc, sp, HOME)
    assert approach_stage(bad, NOW) != "urgent"
    assert ApproachTracker({}).update([{**bad, "stage": approach_stage(bad, NOW)}], NOW) == []


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan"), 10**400, 999, -999, "nan"],
                         ids=["inf", "-inf", "nan", "10**400", "999", "-999", "str-nan"])
def test_typhoon_centre_must_be_a_real_place(bad):
    for index in (0, 1):  # latitude, longitude of the analysis position
        fc, sp = _typhoon_files()
        fc[1]["center"][index] = bad
        with pytest.raises(JmaParseError):
            parse_storm({"id": "TC2632"}, fc, sp, HOME)
    fc, sp = _typhoon_files()
    fc[4]["center"][0] = bad  # a forecast point
    with pytest.raises(JmaParseError):
        parse_storm({"id": "TC2632"}, fc, sp, HOME)
    fc, sp = _typhoon_files()
    fc[1]["track"] = {"typhoon": [[bad, 129.0]]}  # a past-track point
    with pytest.raises(JmaParseError):
        parse_storm({"id": "TC2632"}, fc, sp, HOME)


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan"), 10**400, -5, 1e300],
                         ids=["inf", "-inf", "nan", "10**400", "-5", "1e300"])
def test_typhoon_unusable_probability_circle_is_unknown(bad):
    fc, sp = _typhoon_files()
    fc[4]["probabilityCircle"]["radius"] = bad
    storm = parse_storm({"id": "TC2632"}, fc, sp, HOME)
    assert [p["circle_km"] for p in storm["track"] if p["hours"] == 5] == [None]
    assert storm["closest"]["circle_km"] is not None or storm["closest"]["between"][0] != 5
    assert storm["distance_km"] > 1000


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan"), 10**400],
                         ids=["inf", "-inf", "nan", "10**400"])
def test_typhoon_non_finite_pressure_and_wind_are_unknown(bad):
    fc, sp = _typhoon_files()
    sp[1]["pressure"] = bad
    sp[1]["maximumWind"]["sustained"]["m/s"] = bad
    storm = parse_storm({"id": "TC2632"}, fc, sp, HOME)
    assert storm["now"]["pressure_hpa"] is None and storm["now"]["max_wind_ms"] is None


def test_typhoon_good_storm_is_unchanged():
    fc, sp = _typhoon_files()
    storm = parse_storm({"id": "TC2632"}, fc, sp, HOME)
    assert [step["hours"] for step in storm["storm_zone"]["steps"]][:3] == [0, 1, 2]
    assert [p["circle_km"] for p in storm["track"] if p["hours"] == 2] == [31.5]
    assert storm["now"]["max_wind_ms"] == 50


# ---------- the heat-alert coordinator: a future ReportDate (H8) ----------

async def test_h8_future_report_date_is_rejected_and_does_not_mute_later_alerts(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer, caplog) -> None:
    fired: list = []
    freezer.move_to("2026-07-20T08:10:00+00:00")  # 17:10 JST
    text = load_text("alert_20260720_17.csv").replace("ReportDate,2026/07/20", "ReportDate,2099/07/20")
    assert "2099/07/20" in text
    _quiet(aioclient_mock, 20260720, (5, 10, 14))
    _quiet(aioclient_mock, 20260719)
    aioclient_mock.get(_url(20260720, 17), text=text)
    co = _heat(tokyo, entry, lambda t, d: fired.append((t, d)))
    co.set_alerts_ready()
    caplog.clear()
    await co.async_refresh()
    assert fired == [] and co.data.bulletin is None  # not believed, not held
    warned = _coordinator_warnings(caplog)
    assert len(warned) == 1 and "2099" in warned[0].getMessage() and warned[0].exc_info is None
    freezer.tick(timedelta(minutes=20))
    await co.async_refresh()  # asked for again, still rejected, named once
    assert len(_coordinator_warnings(caplog)) == 1

    freezer.move_to("2026-07-20T20:10:00+00:00")  # 05:10 JST on the 21st
    aioclient_mock.get(_url(20260721, 5), text=load_text("alert_20260721_05.csv"))
    await co.async_refresh()
    assert [(t, d["day"], d["target_date"]) for t, d in fired] == [("heat_alert", "today", "2026-07-21")]
    assert co.data.bulletin.report_time.year == 2026


async def test_h8_a_future_file_is_skipped_for_the_older_one_in_the_same_poll(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    freezer.move_to("2026-07-20T08:10:00+00:00")
    aioclient_mock.get(_url(20260720, 17), text=load_text("alert_20260720_17.csv").replace(
        "ReportDate,2026/07/20", "ReportDate,2099/07/20"))
    aioclient_mock.get(_url(20260720, 14), text=load_text("alert_20260720_17.csv"))
    co = _heat(tokyo, entry)
    await co.async_refresh()
    assert co.data.bulletin is not None and co.data.bulletin.report_time.year == 2026


@pytest.mark.parametrize(("report_time", "believed"), [("17:15:00", True), ("17:40:00", False)])
async def test_h8_tolerance_for_a_slow_clock(tokyo: HomeAssistant, aioclient_mock, entry, freezer,
                                             report_time, believed) -> None:
    freezer.move_to("2026-07-20T08:10:00+00:00")  # 17:10 JST
    text = load_text("alert_20260720_17.csv")
    assert "ReportTime,17:00:00" in text
    aioclient_mock.get(_url(20260720, 17), text=text.replace("ReportTime,17:00:00", f"ReportTime,{report_time}"))
    aioclient_mock.get(_url(20260720, 14), status=404)
    co = _heat(tokyo, entry)
    await co.async_refresh()
    # five minutes ahead is a slow clock, inside the tolerance; thirty minutes cannot be a clock error
    assert (co.data.bulletin is not None) is believed
