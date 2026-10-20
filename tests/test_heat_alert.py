"""Heat alert: parser, new-alert memory, coordinator requests, sensor.

Real captures of the 環境省 alert CSV (tests/fixtures/alert_*.csv). The two `試験` (test) files of
2024-04 are real too; the only synthetic input is the flag-3 special alert, built where it is used.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.jma.api import JmaApi
from custom_components.jma.const import HEAT_ALERT_MEMORY_MAX, URL_AMEDAS_TABLE, URL_HEAT_ALERT, WBGT_ATTRIBUTION
from custom_components.jma.coordinator import HeatAlertCoordinator, JmaConfig
from custom_components.jma.parse_heat_alert import (
    HeatAlertBulletin,
    expected_files,
    file_status,
    grades,
    in_season,
    new_alerts,
    parse_heat_alert,
)

from .conftest import ENTRY_DATA, mock_heat_alert, mock_jma, setup
from .helpers import load_text

JST = timezone(timedelta(hours=9))
TOKYO = "東京都"
D20, D21 = date(2026, 7, 20), date(2026, 7, 21)


def _bulletin(name: str, office: str = "130000") -> HeatAlertBulletin:
    b = parse_heat_alert(load_text(f"alert_{name}.csv"), office)
    assert b is not None
    return b


def _jst(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=JST)


def _special_text(flag1: str, flag2: str) -> str:
    """The real 2024-04-11 17:00 file (Status 試験, Tokyo flags 1,1, 埼玉県 flags 1,3) with ONLY the
    status line swapped to 通常 and the Tokyo row's two flags replaced: no real special-alert file of
    a 通常 issue exists to test flag 3 for Tokyo."""
    out = []
    for line in load_text("alert_20240411_17.csv").split("\n"):
        if line.startswith("Status,"):
            line = line.replace("試験", "通常")
        elif line.startswith(f"{TOKYO},"):
            cells = line.split(",")
            cells[6], cells[7] = flag1, flag2
            line = ",".join(cells)
        out.append(line)
    return "\n".join(out)


# ---------- parser: every fixture ----------

def test_fixture_flags_and_dates():
    expect = {  # file: (flag1, flag2, target1, target2, report time)
        "20260720_17": ("1", "1", "2026-07-20", "2026-07-21", "2026-07-20T17:00:00+09:00"),
        "20260721_05": ("1", "9", "2026-07-21", "2026-07-22", "2026-07-21T05:00:00+09:00"),
        "20261003_05": ("0", "9", "2026-10-03", "2026-10-04", "2026-10-03T05:00:00+09:00"),
        "20261003_10": ("0", "0", "2026-10-03", "2026-10-04", "2026-10-03T10:00:00+09:00"),
        "20261003_17": ("0", "0", "2026-10-03", "2026-10-04", "2026-10-03T17:00:00+09:00"),
    }
    for name, (f1, f2, t1, t2, reported) in expect.items():
        b = _bulletin(name)
        assert (b.flag1, b.flag2) == (f1, f2), name
        assert (b.target1.isoformat(), b.target2.isoformat()) == (t1, t2), name
        assert b.report_time.isoformat() == reported and b.area == TOKYO, name


def test_wbgt_figures_have_a_day():
    # 05:00 column (index 10) filled -> that column, FOR TargetDate1; else the 17:00 / 10:00 column
    # FOR TargetDate2 (columns 8-9 of a day's 17:00 file equal those of the next 05:00 file).
    b17 = _bulletin("20260720_17")  # columns 8 and 9 filled, 10 empty: column 9, the forecast for 07-21
    assert (b17.wbgt_date, b17.wbgt["練馬"], b17.wbgt_max, len(b17.wbgt)) == (D21, 33, 33, 11)
    b05 = _bulletin("20260721_05")  # all three filled: column 10, the 05:00 run for 07-21
    assert (b05.wbgt_date, b05.wbgt["練馬"], b05.wbgt_max) == (D21, 34, 34)
    assert b05.target1 == b05.wbgt_date
    b05o = _bulletin("20261003_05")
    assert (b05o.wbgt_date, b05o.wbgt["東京"], b05o.wbgt_max) == (date(2026, 10, 3), 21, 31)  # 父島 31
    b10 = _bulletin("20261003_10")  # only column 8 filled: for TargetDate2
    assert (b10.wbgt_date, b10.wbgt["東京"], b10.wbgt_max) == (date(2026, 10, 4), 21, 30)  # 父島 30
    b17o = _bulletin("20261003_17")
    assert (b17o.wbgt_date, b17o.wbgt_max) == (date(2026, 10, 4), 30)


def test_wbgt_all_columns_empty_and_home_station():
    text = load_text("alert_20261003_10.csv")
    emptied = "\n".join(
        ",".join(c if i != 8 else "" for i, c in enumerate(line.split(","))) if line.startswith(TOKYO) else line
        for line in text.split("\n"))  # real file, the Tokyo row's only filled column blanked
    b = parse_heat_alert(emptied, "130000")
    assert b is not None and b.wbgt == {} and b.wbgt_max is None and b.wbgt_date is None
    # the entry's own station names, primary then secondary
    b = parse_heat_alert(load_text("alert_20260721_05.csv"), "130000", ("東京", "東京"))
    assert (b.wbgt_home_station, b.wbgt_home) == ("東京", 33)
    b = parse_heat_alert(load_text("alert_20260721_05.csv"), "130000", ("存在しない", "東京"))
    assert (b.wbgt_home_station, b.wbgt_home) == ("東京", 33)
    b = parse_heat_alert(load_text("alert_20260721_05.csv"), "130000", ("存在しない",))
    assert (b.wbgt_home_station, b.wbgt_home) == (None, None) and b.wbgt_max == 34
    assert parse_heat_alert(load_text("alert_20260721_05.csv"), "130000").wbgt_home is None


def test_file_status():
    assert file_status(load_text("alert_20260720_17.csv")) == "通常"
    assert file_status(load_text("alert_20240411_17.csv")) == "試験"
    assert file_status("") is None and file_status(None) is None and file_status("<html></html>") is None


def test_test_status_files_parse_to_none():
    for name in ("20240411_17", "20240412_05"):
        assert parse_heat_alert(load_text(f"alert_{name}.csv"), "130000") is None
    # the same file with the status swapped is a normal bulletin: only the status stopped it
    b = parse_heat_alert(load_text("alert_20240411_17.csv").replace("試験", "通常"), "130000")
    assert b is not None and (b.flag1, b.flag2) == ("1", "1")
    assert parse_heat_alert(load_text("alert_20260720_17.csv").replace("Status,通常", "Status,訓練"), "130000") is None


def test_parse_rejects_bad_input():
    text = load_text("alert_20260720_17.csv")
    assert parse_heat_alert("", "130000") is None and parse_heat_alert(None, "130000") is None
    assert parse_heat_alert("<html><body>maintenance</body></html>", "130000") is None
    assert parse_heat_alert(text, "999999") is None  # no row for that office
    assert parse_heat_alert(text.replace("ReportDate,2026/07/20", "ReportDate,garbled"), "130000") is None
    assert parse_heat_alert(text.replace("TargetDate2,2026/07/21", "TargetDate2,"), "130000") is None
    assert parse_heat_alert("\n".join(ln for ln in text.split("\n") if not ln.startswith("ReportTime")), "130000") is None


def test_other_office_row_matches_on_column_3():
    b = parse_heat_alert(_special_text("1", "1"), "110000")
    assert b is not None and b.area == "埼玉県" and (b.flag1, b.flag2) == ("1", "3")


def test_special_alert_flag_3_and_judged_flag_2():
    b = parse_heat_alert(_special_text("3", "2"), "130000")
    assert b is not None and (b.flag1, b.flag2) == ("3", "2")
    assert grades(b, date(2024, 4, 11)) == {"today": "special", "tomorrow": "none", "special_judged": True}
    events = new_alerts(b, date(2024, 4, 11), [])
    assert [(e["grade"], e["day"], e["special"]) for e in events] == [("special", "today", True)]  # 2 is no issue
    b2 = parse_heat_alert(_special_text("1", "3"), "130000")
    events = new_alerts(b2, date(2024, 4, 11), [])
    assert [(e["grade"], e["day"], e["target_date"]) for e in events] == [
        ("alert", "today", "2024-04-11"), ("special", "tomorrow", "2024-04-12")]
    json.dumps(events)


# ---------- season + expected files ----------

def test_in_season_boundaries():
    for day, expected in [
        (date(2025, 4, 22), False), (date(2025, 4, 23), True), (date(2025, 10, 22), True),
        (date(2025, 10, 23), False),
        (date(2026, 4, 21), False), (date(2026, 4, 22), True), (date(2026, 10, 21), True),
        (date(2026, 10, 22), False),
        (date(2026, 7, 20), True), (date(2026, 10, 3), True), (date(2026, 1, 15), False),
        (date(2026, 12, 31), False),
    ]:
        assert in_season(day) is expected, day


def test_expected_files_at_each_time_of_day():
    d, y = date(2026, 7, 21), date(2026, 7, 20)
    yesterday = [(y, 17), (y, 14), (y, 10), (y, 5)]
    assert expected_files(_jst("2026-07-21T04:59:00")) == yesterday
    assert expected_files(_jst("2026-07-21T05:05:00")) == yesterday  # published at ~05:05, lag 6 min
    assert expected_files(_jst("2026-07-21T05:06:00")) == [(d, 5), *yesterday]
    assert expected_files(_jst("2026-07-21T10:10:00")) == [(d, 10), (d, 5), *yesterday]
    assert expected_files(_jst("2026-07-21T17:10:00")) == [(d, 17), (d, 14), (d, 10), (d, 5), *yesterday]
    assert expected_files(_jst("2026-07-21T00:30:00")) == yesterday
    # a UTC instant is read in JST: 2026-07-20 19:50Z is 04:50 JST on the 21st, 20:10Z is 05:10 JST
    assert expected_files(datetime.fromisoformat("2026-07-20T19:50:00+00:00")) == yesterday
    assert expected_files(datetime.fromisoformat("2026-07-20T20:10:00+00:00")) == [(d, 5), *yesterday]


# ---------- grades ----------

def test_grades_per_fixture():
    b = _bulletin("20260720_17")
    assert grades(b, D20) == {"today": "alert", "tomorrow": "alert", "special_judged": False}
    b = _bulletin("20260721_05")
    assert grades(b, D21) == {"today": "alert", "tomorrow": None, "special_judged": False}  # flag 9 = unknown
    assert grades(_bulletin("20261003_05"), date(2026, 10, 3)) == {
        "today": "none", "tomorrow": None, "special_judged": False}
    assert grades(_bulletin("20261003_10"), date(2026, 10, 3)) == {
        "today": "none", "tomorrow": "none", "special_judged": False}


def test_grades_after_midnight_with_yesterdays_17_file_and_no_event():
    b = _bulletin("20260720_17")
    # at 00:30 on the 21st: today (the 21st) is the file's TargetDate2; tomorrow (the 22nd) is in neither column
    assert grades(b, D21) == {"today": "alert", "tomorrow": None, "special_judged": False}
    assert new_alerts(b, D21, []) == []  # TargetDate1 is yesterday, TargetDate2 is today (not tomorrow)
    assert grades(b, date(2026, 7, 23)) == {"today": None, "tomorrow": None, "special_judged": False}


# ---------- new alerts ----------

def test_new_alerts_sequence_evening_morning_repeat_and_restart():
    memory: list[str] = []
    evening = new_alerts(_bulletin("20260720_17"), D20, memory)
    assert [(e["grade"], e["day"], e["target_date"]) for e in evening] == [
        ("alert", "today", "2026-07-20"), ("alert", "tomorrow", "2026-07-21")]
    assert memory == ["2026-07-20|alert|today", "2026-07-21|alert|tomorrow"]
    assert evening[0] == {"grade": "alert", "day": "today", "target_date": "2026-07-20",
                          "report_time": "2026-07-20T17:00:00+09:00", "area": TOKYO,
                          "wbgt_max": None, "wbgt_home": None,  # the figures are for 07-21, not 07-20
                          "special": False}
    assert evening[1]["wbgt_max"] == 33  # ...so the "tomorrow" event carries them
    json.dumps(evening)

    morning = new_alerts(_bulletin("20260721_05"), D21, memory)  # TargetDate2 flag 9: nothing for the 22nd
    assert [(e["grade"], e["day"], e["target_date"]) for e in morning] == [("alert", "today", "2026-07-21")]
    assert morning[0]["wbgt_max"] == 34  # the 05:00 run is for the day itself
    assert new_alerts(_bulletin("20260721_05"), D21, memory) == []  # the same file again
    # a restart: the saved memory (a plain JSON list) is all that is left
    restored = json.loads(json.dumps(memory))
    assert new_alerts(_bulletin("20260721_05"), D21, restored) == []
    assert new_alerts(_bulletin("20260720_17"), D20, restored) == []
    assert len(restored) == 3


def test_new_alerts_nothing_for_quiet_files_and_other_days():
    memory: list[str] = []
    assert new_alerts(_bulletin("20261003_10"), date(2026, 10, 3), memory) == [] and memory == []
    assert new_alerts(_bulletin("20260720_17"), date(2026, 7, 25), memory) == [] and memory == []


def test_memory_is_trimmed_to_the_cap_dropping_the_oldest():
    memory = [f"old-{i}" for i in range(HEAT_ALERT_MEMORY_MAX + 5)]
    events = new_alerts(_bulletin("20260720_17"), D20, memory)
    assert len(events) == 2 and len(memory) == HEAT_ALERT_MEMORY_MAX
    assert memory[-2:] == ["2026-07-20|alert|today", "2026-07-21|alert|tomorrow"]
    assert memory[0] == "old-7" and "old-0" not in memory


# ---------- coordinator ----------

def _coordinator(hass: HomeAssistant, entry, fire, memory, save, names=("東京", "東京")) -> HeatAlertCoordinator:
    entry.add_to_hass(hass)
    cfg = JmaConfig(office=ENTRY_DATA["office"], area=ENTRY_DATA["area"], area_name=ENTRY_DATA["area_name"],
                    class10=ENTRY_DATA["class10"], station_primary=ENTRY_DATA["station_primary"],
                    station_secondary=ENTRY_DATA["station_secondary"], latitude=35.6812, longitude=139.7671)
    co = HeatAlertCoordinator(hass, entry, JmaApi(async_get_clientsession(hass)), cfg, fire, memory, save)
    co.station_names = names  # preset: the poll does not ask for amedastable.json (the H5 tests pass ())
    return co


def _url(ymd: int, hh: int) -> str:
    return URL_HEAT_ALERT.format(year=str(ymd)[:4], ymd=ymd, hh=f"{hh:02d}")


def _requested(aioclient_mock) -> list[str]:
    return [str(call[1]).rsplit("/", 1)[-1] for call in aioclient_mock.mock_calls]


async def test_coordinator_evening_then_morning_then_held_file_is_not_requested_again(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    fired: list = []
    saves: list = []
    memory: list[str] = []
    freezer.move_to("2026-07-20T08:10:00+00:00")  # 17:10 JST
    mock_heat_alert(aioclient_mock, {(20260720, 17): "alert_20260720_17.csv"})
    co = _coordinator(tokyo, entry, lambda t, d: fired.append((t, d)), memory, lambda: saves.append(1))
    await co.async_refresh()
    assert _requested(aioclient_mock) == ["alert_20260720_17.csv"]
    assert co.data.bulletin.flag1 == "1" and fired == []  # held until the event entity exists
    co.set_alerts_ready()
    assert [(t, d["grade"], d["day"]) for t, d in fired] == [
        ("heat_alert", "alert", "today"), ("heat_alert", "alert", "tomorrow")]
    assert len(saves) == 1 and memory == ["2026-07-20|alert|today", "2026-07-21|alert|tomorrow"]

    freezer.tick(timedelta(minutes=20))  # 17:30: the file is held, no request at all
    await co.async_refresh()
    assert aioclient_mock.call_count == 1 and len(fired) == 2

    freezer.move_to("2026-07-20T20:10:00+00:00")  # 05:10 JST on the 21st: today's 05:00 file is not out here
    mock_heat_alert(aioclient_mock, {(20260721, 5): "alert_20260721_05.csv"})
    await co.async_refresh()
    assert _requested(aioclient_mock)[-1] == "alert_20260721_05.csv" and aioclient_mock.call_count == 2
    assert [(d["day"], d["target_date"]) for _, d in fired[2:]] == [("today", "2026-07-21")]
    assert co.data.bulletin.flag2 == "9" and len(saves) == 2


async def test_coordinator_out_of_season_makes_no_request(tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    freezer.move_to("2026-11-15T03:00:00+00:00")
    co = _coordinator(tokyo, entry, None, [], None, names=())  # no names yet: still no table request
    before = aioclient_mock.call_count
    await co.async_refresh()
    assert aioclient_mock.call_count == before == 0
    assert co.data.bulletin is None and co.last_error is None


async def test_coordinator_newest_404_falls_back_and_newest_is_retried(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    freezer.move_to("2026-07-21T01:10:00+00:00")  # 10:10 JST on the 21st
    aioclient_mock.get(_url(20260721, 10), status=404)
    mock_heat_alert(aioclient_mock, {(20260721, 5): "alert_20260721_05.csv"})
    fired: list = []
    co = _coordinator(tokyo, entry, lambda t, d: fired.append((t, d)), [], None)
    co.set_alerts_ready()
    await co.async_refresh()
    assert _requested(aioclient_mock) == ["alert_20260721_10.csv", "alert_20260721_05.csv"]
    assert co.data.bulletin.report_time == _jst("2026-07-21T05:00:00")
    assert [d["target_date"] for _, d in fired] == ["2026-07-21"]

    freezer.tick(timedelta(minutes=20))  # the newest name is asked again; the held 05:00 is not
    await co.async_refresh()
    assert _requested(aioclient_mock)[2:] == ["alert_20260721_10.csv"]
    assert co.data.bulletin.report_time == _jst("2026-07-21T05:00:00") and len(fired) == 1

    aioclient_mock.clear_requests()  # the 10:00 file appears: it replaces the held one
    mock_heat_alert(aioclient_mock, {(20260721, 10): "alert_20261003_10.csv"})
    freezer.tick(timedelta(minutes=20))
    await co.async_refresh()
    # mock_heat_alert serves the 3 October capture as the 21 July 10:00 file (ReportDate follows the URL)
    assert co.data.bulletin.report_time == _jst("2026-07-21T10:00:00") and len(fired) == 1  # quiet file


async def test_coordinator_at_most_two_requests_per_poll_and_recent_404s_are_skipped(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    freezer.move_to("2026-07-21T01:10:00+00:00")  # 10:10 JST on the 21st
    aioclient_mock.get(_url(20260721, 10), status=404)
    aioclient_mock.get(_url(20260721, 5), status=404)
    mock_heat_alert(aioclient_mock, {(20260720, 17): "alert_20260720_17.csv"})
    co = _coordinator(tokyo, entry, None, [], None)
    await co.async_refresh()
    assert _requested(aioclient_mock) == ["alert_20260721_10.csv", "alert_20260721_05.csv"]  # not the third
    assert co.data.bulletin is None

    freezer.tick(timedelta(minutes=20))  # 10:05 answered 404 within the hour: skipped; the third is asked
    await co.async_refresh()
    assert _requested(aioclient_mock)[2:] == ["alert_20260721_10.csv", "alert_20260720_17.csv"]
    assert co.data.bulletin.report_time == _jst("2026-07-20T17:00:00")

    freezer.tick(timedelta(minutes=20))  # held: only the newest name is asked
    await co.async_refresh()
    assert _requested(aioclient_mock)[4:] == ["alert_20260721_10.csv"] and aioclient_mock.call_count == 5


async def test_coordinator_restart_with_saved_memory_sends_nothing(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    freezer.move_to("2026-07-20T08:10:00+00:00")
    mock_heat_alert(aioclient_mock, {(20260720, 17): "alert_20260720_17.csv"})
    saved = ["2026-07-20|alert|today", "2026-07-21|alert|tomorrow"]
    fired: list = []
    co = _coordinator(tokyo, entry, lambda t, d: fired.append((t, d)), list(saved), None)
    co.set_alerts_ready()
    await co.async_refresh()
    assert aioclient_mock.call_count == 1 and co.data.bulletin is not None and fired == []


# ---------- sensor, through a config entry ----------

async def test_sensor_alert_state_attributes_and_event(tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    hass = tokyo
    freezer.move_to("2026-07-20T08:10:00+00:00")  # 17:10 JST
    mock_heat_alert(aioclient_mock, {(20260720, 17): "alert_20260720_17.csv"})
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    s = hass.states.get("sensor.jma_chiyoda_heat_alert")
    assert s.state == "alert"
    assert s.attributes["device_class"] == "enum" and s.attributes["options"] == ["none", "alert", "special"]
    assert s.attributes["attribution"] == WBGT_ATTRIBUTION
    assert s.attributes["tomorrow"] == "alert" and s.attributes["special_judged"] is False
    assert (s.attributes["target_today"], s.attributes["target_tomorrow"]) == ("2026-07-20", "2026-07-21")
    assert s.attributes["report_time"] == "2026-07-20T17:00:00+09:00" and s.attributes["area"] == TOKYO
    assert s.attributes["wbgt_max"] == 33 and s.attributes["wbgt"]["練馬"] == 33
    assert s.attributes["wbgt_date"] == "2026-07-21"  # the figures are the forecast for the 21st
    # setup hands the entry's AMeDAS station names (amedastable: 東京, 東京) to the coordinator
    # before its first poll, so the entry's own station is picked out of the prefecture row
    assert s.attributes["wbgt_home"] == 32 and s.attributes["wbgt_home_station"] == "東京"
    ev = hass.states.get("event.jma_chiyoda_alerts")  # the second of the two held events is the last shown
    assert ev.attributes["event_type"] == "heat_alert" and ev.attributes["day"] == "tomorrow"
    assert entry.runtime_data.memory.heat == ["2026-07-20|alert|today", "2026-07-21|alert|tomorrow"]


async def test_sensor_none_state(tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    hass = tokyo
    freezer.move_to("2026-10-03T04:12:00+00:00")  # 13:12 JST
    mock_heat_alert(aioclient_mock, {(20261003, 10): "alert_20261003_10.csv"})
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    s = hass.states.get("sensor.jma_chiyoda_heat_alert")
    assert s.state == "none" and s.attributes["tomorrow"] == "none"
    # a quiet file announces nothing (the event entity may show another alert of this setup)
    assert hass.states.get("event.jma_chiyoda_alerts").attributes.get("event_type") != "heat_alert"
    assert entry.runtime_data.memory.heat == []


async def test_sensor_state_follows_the_frozen_test_time_by_default(
        tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    """The conftest default (NOW = 2026-09-26 13:12 JST asks for the 10:00 file): the capture is the
    3 October one, which does not cover the 26th, so today's grade is unknown, not 'none'."""
    mock_jma(aioclient_mock)
    await setup(tokyo, entry)
    s = tokyo.states.get("sensor.jma_chiyoda_heat_alert")
    assert s.state == "unknown" and s.attributes["target_today"] is None


async def test_sensor_unavailable_out_of_season(tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    freezer.move_to("2026-11-15T03:00:00+00:00")
    mock_jma(aioclient_mock)
    before = [u for u in _requested(aioclient_mock) if u.startswith("alert_")]
    await setup(tokyo, entry)
    assert tokyo.states.get("sensor.jma_chiyoda_heat_alert").state == "unavailable"
    assert [u for u in _requested(aioclient_mock) if u.startswith("alert_")] == before == []


# ---------- unusable files ----------

def _shifted_text() -> str:
    """The real 2026-07-20 17:00 file with ONE edit: a new first column on every row after the header."""
    lines = load_text("alert_20260720_17.csv").split("\n")
    header = next(i for i, line in enumerate(lines) if line.startswith("府県予報区"))
    return "\n".join(lines[:header] + ["X," + line if line else line for line in lines[header:]])


def _heat_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name.startswith("custom_components.jma")
            and r.getMessage().startswith("heat alert:")]


async def test_changed_layout_is_warned_once_per_file_name(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer, caplog) -> None:
    shifted = _shifted_text()
    assert parse_heat_alert(load_text("alert_20260720_17.csv"), "130000") is not None
    assert parse_heat_alert(shifted, "130000") is None
    freezer.move_to("2026-07-20T08:10:00+00:00")  # 17:10 JST
    aioclient_mock.get(_url(20260720, 17), text=shifted)
    aioclient_mock.get(_url(20260720, 14), text=shifted)
    aioclient_mock.get(_url(20260720, 10), text=shifted)  # asked on the second poll (14:00 answered <1 h ago)
    fired: list = []
    co = _coordinator(tokyo, entry, lambda t, d: fired.append((t, d)), [], None)
    co.set_alerts_ready()
    with caplog.at_level(logging.DEBUG, logger="custom_components.jma"):
        await co.async_refresh()
        freezer.tick(timedelta(minutes=20))
        await co.async_refresh()  # the newest name is asked again: still one warning for it
    warnings = [r for r in _heat_records(caplog) if r.levelno == logging.WARNING]
    assert sorted(r.getMessage().split()[2] for r in warnings) == [
        "alert_20260720_10.csv", "alert_20260720_14.csv", "alert_20260720_17.csv"]
    assert co.last_update_success is True and co.data.bulletin is None and fired == []


async def test_drill_and_test_files_are_debug_only_and_send_nothing(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer, caplog) -> None:
    """The real 試験 file of 2024-04-11 (Tokyo flags 1,1) served as today's 17:00 file, and the real
    2026-07-20 file with ONE edit (status 訓練) as the 14:00 file."""
    freezer.move_to("2026-07-20T08:10:00+00:00")  # 17:10 JST
    aioclient_mock.get(_url(20260720, 17), text=load_text("alert_20240411_17.csv"))
    aioclient_mock.get(_url(20260720, 14), text=load_text("alert_20260720_17.csv").replace("Status,通常", "Status,訓練"))
    fired: list = []
    memory: list[str] = []
    co = _coordinator(tokyo, entry, lambda t, d: fired.append((t, d)), memory, None)
    co.set_alerts_ready()
    with caplog.at_level(logging.DEBUG, logger="custom_components.jma"):
        await co.async_refresh()
    records = _heat_records(caplog)
    assert [r.levelno for r in records] == [logging.DEBUG, logging.DEBUG]
    assert fired == [] and memory == [] and co.data.bulletin is None and co.last_update_success is True


async def test_coordinator_passes_station_names_to_the_parser(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    freezer.move_to("2026-07-20T20:10:00+00:00")  # 05:10 JST on the 21st
    mock_heat_alert(aioclient_mock, {(20260721, 5): "alert_20260721_05.csv"})
    fired: list = []
    co = _coordinator(tokyo, entry, lambda t, d: fired.append((t, d)), [], None)
    co.station_names = ("東京", "東京")  # the controller sets this from runtime.extra["station_names"]
    co.set_alerts_ready()
    await co.async_refresh()
    assert (co.data.bulletin.wbgt_home_station, co.data.bulletin.wbgt_home) == ("東京", 33)
    assert fired[0][1]["wbgt_home"] == 33 and fired[0][1]["wbgt_max"] == 34  # 東京 33; area max 34 (練馬)


# ---------- through the real wiring: storage -> memory -> setup -> event entity ----------

EVENT = "event.jma_chiyoda_alerts"
JULY_KEYS = ["2026-07-20|alert|today", "2026-07-21|alert|tomorrow"]


def _stored(entry, data: dict) -> dict:
    return {"version": 1, "minor_version": 1, "key": f"jma.{entry.entry_id}.alert_memory", "data": data}


async def test_restart_with_saved_heat_memory_sends_nothing_and_empty_memory_sends_the_events(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer, hass_storage) -> None:
    hass = tokyo
    freezer.move_to("2026-07-20T08:10:00+00:00")  # 17:10 JST, the real alert file of that day
    mock_heat_alert(aioclient_mock, {(20260720, 17): "alert_20260720_17.csv"})
    mock_jma(aioclient_mock)
    key = f"jma.{entry.entry_id}.alert_memory"
    hass_storage[key] = _stored(entry, {"heat": list(JULY_KEYS)})
    await setup(hass, entry)
    state = hass.states.get(EVENT)
    assert state.state == "unknown" or state.attributes.get("event_type") != "heat_alert"
    assert hass.states.get("sensor.jma_chiyoda_heat_alert").state == "alert"  # the alert itself is real

    assert await hass.config_entries.async_unload(entry.entry_id)
    hass_storage[key] = _stored(entry, {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    ev = hass.states.get(EVENT)
    assert ev.attributes["event_type"] == "heat_alert" and ev.attributes["day"] == "tomorrow"
    assert entry.runtime_data.memory.heat == JULY_KEYS


async def test_garbage_in_the_heat_memory_is_dropped_on_load(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer, hass_storage) -> None:
    freezer.move_to("2026-07-20T08:10:00+00:00")
    mock_heat_alert(aioclient_mock, {(20260720, 17): "alert_20260720_17.csv"})
    mock_jma(aioclient_mock)
    hass_storage[f"jma.{entry.entry_id}.alert_memory"] = _stored(entry, {"heat": [JULY_KEYS[0], 5, None, ["x"]]})
    await setup(tokyo, entry)
    assert entry.runtime_data.memory.heat == JULY_KEYS  # the one good key kept, the other announced
    assert entry.runtime_data.heat_alert.last_update_success is True


# ---------- the day of the figures comes from the issue hour ----------

def _blank_column(text: str, column: int) -> str:
    """The real file with ONE edit: the Tokyo row's cell in `column` emptied."""
    out = []
    for line in text.split("\n"):
        if line.startswith(f"{TOKYO},"):
            cells = line.split(",")
            cells[column] = ""
            line = ",".join(cells)
        out.append(line)
    return "\n".join(out)


def test_figures_day_follows_the_issue_hour_not_the_filled_column():
    base = load_text("alert_20260721_05.csv")
    b = parse_heat_alert(_blank_column(base, 10), "130000")  # the 05:00 file with its own column empty
    assert b.report_time.hour == 5 and b.wbgt_date == D21 == b.target1
    ref = parse_heat_alert(base, "130000")
    assert b.wbgt != ref.wbgt and b.wbgt_max == 33  # the value comes from the 17:00 column (index 9)
    b = parse_heat_alert(_blank_column(_blank_column(base, 10), 9), "130000")
    assert b.wbgt_date == D21 and b.wbgt["練馬"] == 35  # ...else the 10:00 column
    # any other hour: TargetDate2, never column 10 (a 17:00 file whose column 9 is empty and column 10
    # is filled: the real 07-21 05:00 row's column 10 pasted in)
    evening = load_text("alert_20260720_17.csv")
    row05 = next(ln for ln in base.split("\n") if ln.startswith(f"{TOKYO},")).split(",")
    cells = next(ln for ln in evening.split("\n") if ln.startswith(f"{TOKYO},")).split(",")
    cells[9], cells[10] = "", row05[10]
    odd = "\n".join(",".join(cells) if ln.startswith(f"{TOKYO},") else ln for ln in evening.split("\n"))
    b = parse_heat_alert(odd, "130000")
    assert b.report_time.hour == 17 and b.wbgt_date == date(2026, 7, 21) == b.target2
    assert b.wbgt["練馬"] == 35  # column 8 (the 10:00 run), not the 05:00 figure 34 of column 10


# ---------- station names heal themselves ----------

async def test_coordinator_asks_for_the_station_table_until_it_has_names(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    freezer.move_to("2026-07-20T20:10:00+00:00")  # 05:10 JST on the 21st
    mock_heat_alert(aioclient_mock, {(20260721, 5): "alert_20260721_05.csv"})
    aioclient_mock.get(URL_AMEDAS_TABLE, status=503)
    fired: list = []
    co = _coordinator(tokyo, entry, lambda t, d: fired.append((t, d)), [], None, names=())
    co.set_alerts_ready()
    await co.async_refresh()  # the table fails: the poll goes on without names
    assert co.last_update_success is True and co.station_names == ()
    assert co.data.bulletin.wbgt_home is None and fired[0][1]["wbgt_home"] is None
    assert _requested(aioclient_mock).count("amedastable.json") == 1  # at most one table request per poll

    aioclient_mock.clear_requests()
    aioclient_mock.get(URL_AMEDAS_TABLE, json={"44071": {"kjName": "練馬"}, "44132": {"kjName": "東京"}})
    await co.async_refresh()  # the held file is NOT requested again; its home figure is recomputed
    assert co.station_names == ("東京", "東京")
    assert _requested(aioclient_mock) == ["amedastable.json"]
    assert (co.data.bulletin.wbgt_home_station, co.data.bulletin.wbgt_home) == ("東京", 33)
    await co.async_refresh()
    assert _requested(aioclient_mock) == ["amedastable.json"]  # names known: no further table request

    # an odd shape is a debug line, not a failure
    co2 = _coordinator(tokyo, entry, None, [], None, names=())
    aioclient_mock.clear_requests()
    aioclient_mock.get(URL_AMEDAS_TABLE, json=["not", "a", "table"])
    mock_heat_alert(aioclient_mock, {(20260721, 5): "alert_20260721_05.csv"})
    await co2.async_refresh()
    assert co2.last_update_success is True and co2.station_names == ()


async def test_sensor_gets_the_home_figure_once_the_table_answers_on_a_later_poll(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    hass = tokyo
    freezer.move_to("2026-07-20T20:10:00+00:00")  # 05:10 JST on the 21st
    mock_heat_alert(aioclient_mock, {(20260721, 5): "alert_20260721_05.csv"})
    aioclient_mock.get(URL_AMEDAS_TABLE, status=503)  # registered first: wins over mock_jma's
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    s = hass.states.get("sensor.jma_chiyoda_heat_alert")
    assert s.state == "alert" and s.attributes["wbgt_date"] == "2026-07-21"
    assert s.attributes["wbgt_home"] is None and s.attributes["wbgt_home_station"] is None

    aioclient_mock.clear_requests()
    mock_jma(aioclient_mock)  # the table answers now
    await entry.runtime_data.heat_alert.async_refresh()
    await hass.async_block_till_done()
    s = hass.states.get("sensor.jma_chiyoda_heat_alert")
    assert (s.attributes["wbgt_home_station"], s.attributes["wbgt_home"]) == ("東京", 33)
    assert not [u for u in _requested(aioclient_mock) if u.startswith("alert_")]  # the CSV was not asked for again
