"""Edge cases: address forms, warning statuses, partial feed files, nowcast lag and observed extremes.

Each test names the behaviour it protects.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.jma.const import HEALTH_FORECAST_MAX_AGE, URL_NOWC_TIMES
from custom_components.jma.parse_amedas import extreme_time, merge_observations, parse_point
from custom_components.jma.parse_forecast import parse_forecast
from custom_components.jma.parse_hourly import parse_hourly
from custom_components.jma.parse_nowcast import frame_lists
from custom_components.jma.parse_quake import EewTracker, parse_551
from custom_components.jma.parse_warnings import (
    diff_warnings,
    is_active,
    parse_area_warnings,
    suspect_reason,
)

from .conftest import AREA, QUAKE_PLACE, mock_jma, setup
from .helpers import clone, load_json

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 9, 26, 12, 17, 30, tzinfo=JST)


# ---------- P2P 551 addresses carry a 東京 prefix ----------

def _551(points: list[dict]) -> dict:
    return {
        "code": 551, "id": "x", "issue": {"type": "DetailScale", "time": "2026/09/17 00:10:00"},
        "earthquake": {"time": "2026/09/17 00:07:00", "maxScale": 30, "domesticTsunami": "None",
                       "hypocenter": {"name": "茨城県南部", "magnitude": 4.6, "depth": 50}},
        "points": points,
    }


def test_551_published_address_forms_match_wards():
    # Address forms as P2P history publishes them: a 東京 prefix, no 都 (a plain startswith on 東京都 matches none).
    q = parse_551(_551([
        {"pref": "東京都", "addr": "東京千代田区大手町", "isArea": False, "scale": 20},
        {"pref": "東京都", "addr": "東京港区芝", "isArea": False, "scale": 30},
        {"pref": "東京都", "addr": "東京八王子市大横町", "isArea": False, "scale": 40},
    ]), QUAKE_PLACE)
    assert q.local_scale == 20 and q.pref_max_scale == 40


def test_551_area_level_report_counts_for_the_prefecture_only():
    q = parse_551(_551([{"pref": "東京都", "addr": "東京都２３区", "isArea": True, "scale": 30}]), QUAKE_PLACE)
    assert q.pref_max_scale == 30 and q.local_scale is None


def test_551_float_scale_is_accepted():
    q = parse_551(_551([{"pref": "東京都", "addr": "東京千代田区丸の内", "isArea": False, "scale": 45.0}]), QUAKE_PLACE)
    assert q.local_scale == 45


def test_eew_float_scales_predict_and_rise():
    def eew(serial: int, scale_from: float) -> dict:
        return {
            "code": 556, "test": False, "cancelled": False,
            "issue": {"time": "2026/09/26 12:17:20", "eventId": "e1", "serial": str(serial)},
            "earthquake": {"originTime": "2026/09/26 12:17:00", "hypocenter": {"name": "東京湾"}},
            "areas": [{"name": "東京都２３区", "scaleFrom": scale_from, "scaleTo": 99.0}],
        }
    t = EewTracker(QUAKE_PLACE.pref)
    first = t.feed(eew(1, 45.0), NOW)
    assert first[0]["predicted_scale"] == 45
    rising = t.feed(eew(2, 60.0), NOW)
    assert rising and rising[0]["predicted_scale"] == 60 and rising[0]["update"] is True


# ---------- step-down statuses are in force ----------

def _r8(kinds: list[dict]) -> list:
    records = clone(load_json("warning_r8_130000.json"))
    for record in records:
        if record["dataTypeCode"] == "VPWW55":
            for item in record["warning"]["class20Items"]:
                if item["areaCode"] == AREA:
                    item["kinds"] = kinds
    return records


def test_step_down_status_is_in_force_and_lowers():
    before = parse_area_warnings(_r8([{"code": "03", "status": "発表"}]), AREA)  # L3 rain
    after = parse_area_warnings(_r8([{"code": "10", "status": "警報から注意報"}]), AREA)  # -> L2
    assert after.level == 2
    changes = diff_warnings(before.warnings, after.warnings)
    assert [(c.event_type, c.old_level, c.new_level) for c in changes] == [("warning_lowered", 3, 2)]


def test_l5_to_l4_step_down_is_lowered_not_cleared():
    l5 = parse_area_warnings(_r8([{"code": "33", "status": "発表"}]), AREA)
    l4 = parse_area_warnings(_r8([{"code": "43", "status": "特別警報から危険警報"}]), AREA)
    changes = diff_warnings(l5.warnings, l4.warnings)
    assert [(c.event_type, c.old_level, c.new_level) for c in changes] == [("warning_lowered", 5, 4)]


def test_inactive_statuses():
    assert not is_active("解除") and not is_active("発表警報・注意報はなし")
    assert not is_active(None) and not is_active("")
    assert is_active("継続") and is_active("危険警報から注意報") and is_active("未知の状態")


# ---------- partial / older r8 files are held ----------

def test_suspect_reasons():
    full = parse_area_warnings(load_json("warning_r8_130000.json"), AREA)
    assert suspect_reason(None, full) is None
    assert suspect_reason(dict(full.control_times), full) is None
    missing = parse_area_warnings(load_json("warning_r8_130000.json"), "9999999")
    assert "missing" in suspect_reason(dict(full.control_times), missing)
    fewer = parse_area_warnings(
        [r for r in load_json("warning_r8_130000.json") if r["dataTypeCode"] != "VPWW61"], AREA)
    assert "VPWW61" in suspect_reason(dict(full.control_times), fewer)
    newer = {k: "2030-01-01T00:00:00+09:00" for k in full.control_times}
    assert "older copy" in suspect_reason(newer, full)


async def test_partial_file_is_held_then_believed(tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    hass = tokyo
    mock_jma(aioclient_mock, warnings=_r8([{"code": "43", "status": "発表"}]))
    await setup(hass, entry)
    assert hass.states.get("sensor.jma_chiyoda_warning_level").state == "4"
    ev_before = hass.states.get("event.jma_chiyoda_alerts").state

    # One record (the one carrying Chiyoda's L4) disappears from the file.
    partial = [r for r in _r8([{"code": "43", "status": "発表"}]) if r["dataTypeCode"] != "VPWW55"]
    aioclient_mock.clear_requests()
    mock_jma(aioclient_mock, warnings=partial)
    for _ in range(2):  # held: no "cleared", the L4 stays on screen
        freezer.tick(timedelta(seconds=61))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert hass.states.get("sensor.jma_chiyoda_warning_level").state == "4"
        assert hass.states.get("event.jma_chiyoda_alerts").state == ev_before
    # The full file comes back: nothing to announce, nothing was ever "cleared".
    aioclient_mock.clear_requests()
    mock_jma(aioclient_mock, warnings=_r8([{"code": "43", "status": "発表"}]))
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.jma_chiyoda_warning_level").state == "4"
    assert hass.states.get("event.jma_chiyoda_alerts").state == ev_before


# ---------- an L4/L5 already in force at start is announced once ----------

async def test_in_force_at_start_is_announced(tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    hass = tokyo
    mock_jma(aioclient_mock, warnings=_r8([{"code": "43", "status": "継続"}]))
    await setup(hass, entry)
    ev = hass.states.get("event.jma_chiyoda_alerts")
    assert ev.attributes["event_type"] == "warning_in_force"
    assert ev.attributes["new_level"] == 4 and ev.attributes["phenomenon"] == "rain"


async def test_l3_in_force_at_start_stays_silent(tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    hass = tokyo
    mock_jma(aioclient_mock, warnings=_r8([{"code": "03", "status": "継続"}]))
    await setup(hass, entry)
    assert hass.states.get("event.jma_chiyoda_alerts").state == "unknown"


# ---------- N2 lags N1 ----------

def test_frames_use_newest_basetime_that_has_a_forecast():
    n1 = load_json("targetTimes_N1.json")
    n2 = load_json("targetTimes_N2.json")
    base, past, forecast = frame_lists(n1, n2, 12)
    assert len(forecast) == 12
    # N1 moves on 5 minutes; N2 (cached) carries only the previous basetime.
    newer = (datetime.strptime(base, "%Y%m%d%H%M%S") + timedelta(minutes=5)).strftime("%Y%m%d%H%M%S")
    n1_ahead = [{"basetime": newer, "validtime": newer, "elements": ["hrpns", "hrpns_nd"]}, *n1]
    base2, past2, forecast2 = frame_lists(n1_ahead, n2, 12)
    assert base2 == base and len(forecast2) == 12  # the basetime N2 has forecasts for, not the newest of N1
    assert past2[-1] == base and newer not in past2


async def test_rain_series_survives_the_n2_lag(tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    hass = tokyo
    n1 = load_json("targetTimes_N1.json")
    base = frame_lists(n1, load_json("targetTimes_N2.json"), 12)[0]
    newer = (datetime.strptime(base, "%Y%m%d%H%M%S") + timedelta(minutes=5)).strftime("%Y%m%d%H%M%S")
    aioclient_mock.get(URL_NOWC_TIMES.format(n="N1"),
                       json=[{"basetime": newer, "validtime": newer, "elements": ["hrpns"]}, *n1])
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    assert len(hass.states.get("sensor.jma_chiyoda_rain_next_hour").attributes["series"]) == 13


# ---------- the overnight forecast gap is 12 h ----------

def test_forecast_health_covers_the_overnight_gap():
    assert HEALTH_FORECAST_MAX_AGE > timedelta(hours=12)


# ---------- observed extremes ----------

def test_gust_is_the_days_maximum_with_its_time():
    obs = parse_point(load_json("point_62016_12.json"), "62016")  # synthetic file
    assert not hasattr(obs, "wind_gust")
    assert obs.gust_max == 7.4
    assert obs.gust_max_time == datetime(2026, 9, 26, 12, 21, tzinfo=JST)  # gustTime 03:21 UTC
    assert obs.temp_max_observed == 25.9
    assert obs.temp_max_observed_time == datetime(2026, 9, 26, 11, 12, tzinfo=JST)  # 02:12 UTC
    assert obs.temp_min_observed == 20.3
    assert obs.temp_min_observed_time == datetime(2026, 9, 26, 5, 8, tzinfo=JST)  # 20:08 UTC of the day before


def test_extreme_time_on_the_midnight_row_stays_in_the_closing_day():
    row = datetime(2026, 9, 27, 0, 0, tzinfo=JST)  # closes the 26th
    assert extreme_time(row, {"hour": 5, "minute": 10}) == datetime(2026, 9, 26, 14, 10, tzinfo=JST)
    assert extreme_time(row, {"hour": "x"}) is None


def test_old_rows_do_not_pose_as_now():
    data = clone(load_json("point_62016_12.json"))
    for key in sorted(data)[-3:]:  # the three newest rows lose their temperature
        data[key]["temp"] = [None, 5]
    obs = parse_point(data, "62016")
    assert obs.temperature is None  # the 12:20 reading is 30 min older than the 12:50 row
    merged = merge_observations(obs, parse_point(load_json("point_44132_12.json"), "44132"))
    assert merged.temperature == 18.8  # the secondary fills in with its own current reading


def test_merge_keeps_an_extreme_with_its_own_time():
    a = parse_point(load_json("point_62016_12.json"), "62016")
    b = parse_point(load_json("point_44132_12.json"), "44132")
    from dataclasses import replace
    a = replace(a, gust_max=None, gust_max_time=None)
    merged = merge_observations(a, b)
    assert merged.gust_max == 9.9 and merged.gust_max_time == b.gust_max_time


async def test_entity_shows_only_todays_extremes(
    tokyo: HomeAssistant, aioclient_mock, entry_two_stations, freezer
) -> None:
    hass = tokyo
    entry = entry_two_stations
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    w = hass.states.get("weather.jma_chiyoda").attributes
    assert "wind_gust_speed" not in w
    assert w["gust_max_today"] == 9.9 and w["gust_max_time"].startswith("2026-09-26T04:47")
    assert w["temp_max_observed"] == 23.6 and w["temp_min_observed"] == 18.6
    assert w["station_primary_name"] == "東京" and w["station_secondary_name"] == "能勢"
    # Just after midnight, the next day: the only AMeDAS file holds the previous day's extremes -> not "today".
    freezer.move_to("2026-09-26T15:05:00+00:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    w = hass.states.get("weather.jma_chiyoda").attributes
    assert w.get("gust_max_today") is None and w.get("temp_max_observed") is None


# ---------- forecast: evening layout by date, bounds, normals, wind ----------

def _evening_issue(report: str) -> list:
    data = clone(load_json("forecast_130000.json"))
    short = data[0]
    short["reportDatetime"] = report
    temps = short["timeSeries"][2]
    temps["timeDefines"] = ["2026-09-27T00:00:00+09:00", "2026-09-27T09:00:00+09:00"]
    for area in temps["areas"]:
        area["temps"] = ["18", "26"]
    return data


def test_evening_layout_recognised_off_schedule():
    for report in ("2026-09-26T17:00:00+09:00", "2026-09-26T18:10:00+09:00"):
        fc = parse_forecast(_evening_issue(report), "130010", "44132")
        today = next(d for d in fc.days if d.date.isoformat() == "2026-09-26")
        tomorrow = next(d for d in fc.days if d.date.isoformat() == "2026-09-27")
        assert today.temp_max is None, report
        assert (tomorrow.temp_min, tomorrow.temp_max) == (18, 26), report


def test_weekly_bounds_normals_and_wind_text():
    fc = parse_forecast(load_json("forecast_130000.json"), "130010", "44132")
    assert fc.normal_temp_max is not None and fc.normal_temp_min is not None
    assert fc.days[0].wind_ja  # short-term days carry JMA's wind sentence
    weekly = [d for d in fc.days if d.temp_max_upper is not None]
    assert weekly and all(d.temp_max_lower <= d.temp_max <= d.temp_max_upper for d in weekly)


# ---------- hourly: ragged arrays never raise; cloud and dew point ----------

def test_hourly_ragged_arrays_and_cloud_and_dew_point():
    data = clone(load_json("openmeteo_msm.json"))
    data["hourly"]["cloud_cover"] = [50] * 3
    data["hourly"]["dew_point_2m"] = [17.5] * len(data["hourly"]["time"])
    rows = parse_hourly(data)
    assert rows[0]["cloud_coverage"] == 50 and rows[0]["native_dew_point"] == 17.5
    assert rows[5].get("cloud_coverage") is None
