"""The volcano sensor: JMA's 噴火警報・予報 (warning.json), the volcano list and the bulletin headlines.

The fixtures are captures of the live files made on 2026-10-25 (JST): `volcano_warning.json` (14 bulletins),
`volcano_list.json` (120 volcanoes), `volcano_warning_<code>.json` (503 阿蘇山, 506 桜島, 113 北海道駒ヶ岳, 326 西之島,
331 福徳岡ノ場, 509 口永良部島). None of the captured volcanoes concerns the tests' place (千代田区), so a test that
needs one edits the captured list in a way it says: it names the tests' municipality in a bulletin's `areas`, or it
clones a captured bulletin under another code (a "synthetic" bulletin).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.jma.api import JmaApi
from custom_components.jma.binary_sensor import feed_conditions
from custom_components.jma.const import (
    DOMAIN,
    HEALTH_VOLCANO_MAX_AGE,
    INTERVAL_VOLCANO,
    INTERVAL_VOLCANO_LIST,
    JMA_ATTRIBUTION,
    URL_VOLCANO_LIST,
    URL_VOLCANO_PAGE,
    URL_VOLCANO_WARNING,
    URL_VOLCANO_WARNINGS,
    VOLCANO_HEADLINE_MAX,
    VOLCANO_KINDS,
    VOLCANO_MAX_LISTED,
    VOLCANO_RADIUS_DEFAULT_KM,
    VOLCANO_RADIUS_MAX_KM,
)
from custom_components.jma.coordinator import JmaConfig, VolcanoCoordinator
from custom_components.jma.diagnostics import async_get_config_entry_diagnostics
from custom_components.jma.errors import JmaParseError
from custom_components.jma.memory import _clean_volcano, empty_memory
from custom_components.jma.parse_typhoon import haversine_km
from custom_components.jma.parse_volcano import (
    VolcanoInfo,
    VolcanoTracker,
    VolcanoWarning,
    municipality_listed,
    newest_report,
    parse_headline,
    parse_volcano_list,
    parse_volcano_warnings,
    radius_km,
    relevance,
    volcano_entry,
)
from custom_components.jma.sensor import VolcanoSensor

from .conftest import AREA, ENTRY_DATA, LAT, LON, TITLE, mock_jma, mock_volcano, setup
from .helpers import clone, load_json
from .test_icons import ICONS, _icon_for
from .test_options_flow_http import FLOW, _client

SENSOR = "sensor.jma_chiyoda_volcano"
EVENT = "event.jma_chiyoda_alerts"
FEED = "binary_sensor.jma_chiyoda_feed_problem"
HOME = (LAT, LON)
AREA_NAME = ENTRY_DATA["area_name"]


# ---------- helpers ----------

def _list() -> list[dict[str, Any]]:
    return clone(load_json("volcano_warning.json"))


def _name(*codes: str) -> list[dict[str, Any]]:
    """The captured list with the tests' municipality added to the `areas` of the bulletins of `codes`."""
    out = _list()
    for rec in out:
        if rec["eventId"] in codes:
            rec["areas"] = [*rec["areas"], AREA]
    return out


def _synthetic(source: str, code: str, name: str, areas: list[str]) -> dict[str, Any]:
    """A captured bulletin cloned under another volcano code, name and municipality list."""
    rec = next(r for r in _list() if r["eventId"] == source)
    rec["eventId"], rec["areas"] = code, areas
    for section in rec["volcanoInfos"]:
        if section["type"].endswith("（対象火山）"):
            section["items"][0]["areas"] = [{"name": name, "code": code}]
    return rec


def _set_kind(rec: dict[str, Any], code: str, name: str, report: str | None = None) -> dict[str, Any]:
    item = next(s for s in rec["volcanoInfos"] if s["type"].endswith("（対象火山）"))["items"][0]
    item.update(code=code, name=name)
    if report:
        rec["reportDatetime"] = report
    return rec


def _record(code: str) -> dict[str, Any]:
    return next(r for r in _list() if r["eventId"] == code)


def _headline(code: str) -> str:
    return load_json(f"volcano_warning_{code}.json")["volcanoHeadline"]


def _requested(aioclient_mock, part: str) -> list[str]:
    return [str(call[1]) for call in aioclient_mock.mock_calls if part in str(call[1])]


def _cfg(**changes: Any) -> JmaConfig:
    base: dict[str, Any] = {
        "office": ENTRY_DATA["office"], "area": AREA, "area_name": AREA_NAME, "class10": ENTRY_DATA["class10"],
        "station_primary": ENTRY_DATA["station_primary"], "station_secondary": ENTRY_DATA["station_secondary"],
        "latitude": LAT, "longitude": LON,
    }
    return JmaConfig(**{**base, **changes})


def _coordinator(hass: HomeAssistant, entry, fire, memory, save, **cfg: Any) -> VolcanoCoordinator:
    entry.add_to_hass(hass)
    return VolcanoCoordinator(hass, entry, JmaApi(async_get_clientsession(hass)), _cfg(**cfg), fire, memory, save)


def _stored(entry, data: dict) -> dict:
    return {"version": 1, "minor_version": 1, "key": f"jma.{entry.entry_id}.alert_memory", "data": data}


def _volcano(code: str = "503", level: int | None = 2, report: str = "2026-10-01T10:00:00+09:00",
             **more: Any) -> dict[str, Any]:
    """A volcano entry as volcano_entry() makes it, for the tracker."""
    return {"code": code, "name_ja": "阿蘇山", "name_en": "Asosan", "level": level, "kind_code": "12",
            "kind_ja": "レベル２（火口周辺規制）", "kind_en": "Level 2", "warning_type": "crater", "level_system": True,
            "condition": "引上げ", "headline": "見出し", "report_time": report, "distance_km": 12.3,
            "jma_url": URL_VOLCANO_PAGE.format(code=code), **more}


# ---------- the kind table ----------

def test_every_kind_code_of_the_capture_is_in_the_table_with_its_level() -> None:
    seen: dict[str, str] = {}
    for rec in _list():
        for section in rec["volcanoInfos"]:
            if section["type"].endswith("（対象火山）"):
                seen.update({i["code"]: i["name"] for i in section["items"]})
    assert seen == {
        "11": "レベル１（活火山であることに留意）", "12": "レベル２（火口周辺規制）", "13": "レベル３（入山規制）",
        "22": "火口周辺危険", "35": "活火山であることに留意（海底火山）", "36": "周辺海域警戒",
    }
    assert {code: VOLCANO_KINDS[code][0] for code in seen} == {
        "11": 1, "12": 2, "13": 3, "22": 2, "35": 1, "36": 2}


def test_the_table_has_the_five_levels_and_the_grades_of_volcanoes_without_levels() -> None:
    assert {c: (k[0], k[1]) for c, k in VOLCANO_KINDS.items()} == {
        "11": (1, True), "12": (2, True), "13": (3, True), "14": (4, True), "15": (5, True),
        "21": (1, False), "22": (2, False), "23": (3, False), "25": (4, False), "35": (1, False), "36": (2, False)}
    # JMA's own step 23 -> 22 is a lowering ("引下げ", 西之島, 2026-09-08): 23 ranks above 22
    assert VOLCANO_KINDS["23"][0] > VOLCANO_KINDS["22"][0]
    assert {k[2] for k in VOLCANO_KINDS.values()} == {"forecast", "crater", "residential", "sea"}
    assert all(k[3] for k in VOLCANO_KINDS.values())


# ---------- parsers ----------

def test_the_captured_list_is_read() -> None:
    warnings = parse_volcano_warnings(load_json("volcano_warning.json"))
    assert len(warnings) == 14
    aso = next(w for w in warnings if w.code == "503")
    assert aso == VolcanoWarning(
        code="503", name_ja="阿蘇山", kind_code="12", kind_ja="レベル２（火口周辺規制）", condition="引下げ",
        report_time="2026-09-01T16:00:00+09:00", areas=("4321400", "4343300"))
    sea = next(w for w in warnings if w.code == "324")
    assert (sea.kind_code, sea.areas) == ("36", ("1340000",))  # a code that is no class20 area


def test_a_bulletin_that_cannot_be_read_is_left_out_and_the_others_stay() -> None:
    data = _list()
    data.append("not a bulletin")
    data.append({"eventId": "12", "reportDatetime": "2026-10-01T00:00:00+09:00", "volcanoInfos": []})  # code of 2 digits
    data.append({"eventId": "../x", "reportDatetime": "2026-10-01T00:00:00+09:00", "volcanoInfos": []})
    data.append({"eventId": "600", "reportDatetime": "yesterday", "volcanoInfos": []})  # not a time
    data.append({"eventId": "601", "reportDatetime": "2026-10-01T00:00:00+09:00", "volcanoInfos": "x"})
    data.append({"eventId": "602", "reportDatetime": "2026-10-01T00:00:00+09:00", "volcanoInfos": []})  # no section
    data.append({"eventId": "603", "reportDatetime": "2026-10-01T00:00:00+09:00",
                 "volcanoInfos": [{"type": "噴火警報・予報（対象火山）", "items": [{"name": "x"}]}]})  # no kind code
    data.append({"eventId": "604", "reportDatetime": "2026-10-01T00:00:00+09:00",
                 "volcanoInfos": [{"type": "噴火警報・予報（対象火山）", "items": "x"}]})
    assert len(parse_volcano_warnings(data)) == 14


def test_a_bulletin_without_municipalities_or_names_is_still_read() -> None:
    rec = _record("503")
    del rec["areas"]
    rec["volcanoInfos"][0]["items"][0]["areas"] = []
    del rec["volcanoInfos"][0]["items"][0]["condition"]
    (w,) = parse_volcano_warnings([rec])
    assert (w.areas, w.name_ja, w.condition) == ((), None, None)
    rec["areas"] = ["4321400", 5, "", None]
    assert parse_volcano_warnings([rec])[0].areas == ("4321400",)


def test_a_file_in_which_no_bulletin_can_be_read_is_a_changed_file() -> None:
    with pytest.raises(JmaParseError):
        parse_volcano_warnings({"not": "a list"})
    with pytest.raises(JmaParseError, match="none of the 2"):
        parse_volcano_warnings(["x", {"eventId": "503"}])
    assert parse_volcano_warnings([]) == []  # an empty list is a list


def test_a_volcano_listed_twice_counts_once_with_the_newer_report() -> None:
    old, new = _record("503"), _record("503")
    old["reportDatetime"], new["reportDatetime"] = "2026-09-01T16:00:00+09:00", "2026-09-02T16:00:00+09:00"
    for pair in ([old, new], [new, old]):
        (w,) = parse_volcano_warnings(pair)
        assert w.report_time == "2026-09-02T16:00:00+09:00"


def test_the_volcano_list_is_read_with_the_rows_that_have_no_coordinates() -> None:
    volcanoes = parse_volcano_list(load_json("volcano_list.json"))
    assert len(volcanoes) == 120
    assert volcanoes["503"] == VolcanoInfo("503", "阿蘇山", "Asosan", 32.884, 131.104)
    # three rows of the capture are no volcanoes (全国の活火山 ...): no latlon, and their names stay
    assert volcanoes["900"] == VolcanoInfo("900", "全国の活火山", volcanoes["900"].name_en, None, None)


def test_a_bad_volcano_list_row_is_left_out_or_loses_its_coordinates() -> None:
    rows = [
        {"code": "503", "name_jp": "阿蘇山", "name_en": "Asosan", "latlon": ["32.884", "131.104"]},
        {"code": "504", "name_jp": "A", "latlon": ["x", "1"]},  # not a number
        {"code": "505", "name_jp": "B", "latlon": ["32.0"]},  # one value
        {"code": "506", "name_jp": "C", "latlon": ["95.0", "130.0"]},  # not a place on Earth
        {"code": "507", "name_jp": "D", "latlon": 5},  # not a list
        {"code": "50", "name_jp": "E"}, {"code": 509}, "row", {"name_jp": "F"},  # no usable code
    ]
    volcanoes = parse_volcano_list(rows)
    assert sorted(volcanoes) == ["503", "504", "505", "506", "507"]
    assert volcanoes["503"].lat == 32.884
    assert all(volcanoes[c].lat is None and volcanoes[c].lon is None for c in ("504", "505", "506", "507"))
    assert volcanoes["504"].name_en is None
    with pytest.raises(JmaParseError):
        parse_volcano_list({"a": 1})
    with pytest.raises(JmaParseError, match="none of the 2"):
        parse_volcano_list(["x", {"code": "1"}])
    assert parse_volcano_list([]) == {}


def test_the_headline_is_read_and_clipped() -> None:
    assert parse_headline(load_json("volcano_warning_503.json")) == _headline("503")
    assert parse_headline({"volcanoHeadline": "  text \n"}) == "text"
    assert parse_headline({"volcanoHeadline": ""}) is None and parse_headline({}) is None
    assert parse_headline({"volcanoHeadline": 5}) is None
    long = parse_headline({"volcanoHeadline": "あ" * 1000})
    assert long == "あ" * (VOLCANO_HEADLINE_MAX - 1) + "…" and len(long) == VOLCANO_HEADLINE_MAX
    with pytest.raises(JmaParseError):
        parse_headline(["x"])


@pytest.mark.parametrize(("value", "expected"), [
    (0, 0.0), (80, 80.0), (49.5, 49.5), (VOLCANO_RADIUS_MAX_KM, float(VOLCANO_RADIUS_MAX_KM)),
    (-1, 50.0), (VOLCANO_RADIUS_MAX_KM + 1, 50.0), ("far", 50.0), (None, 50.0), (True, 50.0),
    (float("inf"), 50.0), (float("nan"), 50.0),
])
def test_the_radius_option_is_read_or_the_default(value: Any, expected: float) -> None:
    assert VOLCANO_RADIUS_DEFAULT_KM == 50
    assert radius_km(value) == expected


def test_a_municipality_is_matched_by_its_jis_code() -> None:
    listed = ("4321400", "4343300")
    assert municipality_listed("4321400", listed)
    assert municipality_listed("4321410", listed)  # a class20 area that JMA cut out of the same municipality
    assert not municipality_listed(AREA, listed)
    assert not municipality_listed("4321", listed) and not municipality_listed("", listed)
    assert not municipality_listed("1340000", ("1310100",))
    assert not municipality_listed(AREA, ("13",)) and not municipality_listed(AREA, ())


def test_relevance_by_municipality_and_by_distance() -> None:
    hakone = VolcanoInfo("315", "箱根山", "Hakoneyama", 35.233, 139.021)
    named = VolcanoWarning("315", "箱根山", "12", None, None, "2026-10-01T10:00:00+09:00", (AREA,))
    other = VolcanoWarning("315", "箱根山", "12", None, None, "2026-10-01T10:00:00+09:00", ("1420100",))
    none = VolcanoWarning("315", "箱根山", "12", None, None, "2026-10-01T10:00:00+09:00", ())
    km = round(haversine_km(35.233, 139.021, *HOME), 1)
    assert km == 84.0
    # a bulletin that names municipalities: only they decide (the distance is still reported)
    assert relevance(named, hakone, AREA, HOME, 0) == ("municipality", km)
    assert relevance(other, hakone, AREA, HOME, 500) is None
    assert relevance(named, None, AREA, HOME, 0) == ("municipality", None)
    # a bulletin that names none: the distance decides
    assert relevance(none, hakone, AREA, HOME, 100) == ("distance", km)
    assert relevance(none, hakone, AREA, HOME, 84) == ("distance", km)  # on the edge counts
    assert relevance(none, hakone, AREA, HOME, 83.9) is None
    assert relevance(none, hakone, AREA, HOME, 0) is None  # 0 turns the rule off
    assert relevance(none, None, AREA, HOME, 500) is None  # no coordinates
    assert relevance(none, VolcanoInfo("315", "箱根山", None, None, None), AREA, HOME, 500) is None


def test_the_entry_of_a_known_and_of_an_unknown_kind() -> None:
    w = VolcanoWarning("503", "阿蘇山", "12", "レベル２（火口周辺規制）", "引下げ", "2026-09-01T16:00:00+09:00", (AREA,))
    info = VolcanoInfo("503", "阿蘇", "Asosan", 32.884, 131.104)
    assert volcano_entry(w, info, "municipality", 920.5, "見出し") == {
        "code": "503", "name_ja": "阿蘇山", "name_en": "Asosan", "level": 2, "level_system": True,
        "warning_type": "crater", "kind_code": "12", "kind_ja": "レベル２（火口周辺規制）",
        "kind_en": "Level 2 (Restriction on proximity to the crater)", "condition": "引下げ", "headline": "見出し",
        "report_time": "2026-09-01T16:00:00+09:00", "relevance": "municipality", "distance_km": 920.5,
        "jma_url": "https://www.jma.go.jp/bosai/volcano/#type=warning&event_id=503",
    }
    unknown = volcano_entry(VolcanoWarning("503", None, "99", None, None, w.report_time, ()), info, "distance", None, None)
    assert (unknown["name_ja"], unknown["level"], unknown["level_system"], unknown["warning_type"],
            unknown["kind_en"], unknown["kind_code"]) == ("阿蘇", None, None, None, None, "99")
    assert volcano_entry(w, None, "distance", None, None)["name_en"] is None


def test_the_newest_report_is_the_latest_in_time_not_in_text() -> None:
    entries = [{"report_time": "2026-10-01T00:30:00+09:00"}, {"report_time": "2026-09-30T20:00:00+00:00"},
               {"report_time": "bad"}, {"report_time": None}, {}]
    assert newest_report(entries) == "2026-09-30T20:00:00+00:00"  # 05:00 JST on the 1st, after 00:30 JST
    assert newest_report([]) is None and newest_report([{"report_time": "x"}]) is None


# ---------- the tracker ----------

def _kinds(events: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, str, int, int]]:
    return [(t, d["volcano_code"], d["old_level"], d["new_level"]) for t, d in events]


def test_the_tracker_sends_an_event_when_the_level_changes_and_only_then() -> None:
    memory: dict[str, Any] = {}
    tracker = VolcanoTracker(memory)
    assert _kinds(tracker.update([_volcano(level=2)])) == [("volcano_raised", "503", 0, 2)]
    assert tracker.dirty and memory["503"] == {
        "level": 2, "kind_code": "12", "name_ja": "阿蘇山", "name_en": "Asosan",
        "report_time": "2026-10-01T10:00:00+09:00", "absent": 0}
    tracker.dirty = False
    assert tracker.update([_volcano(level=2)]) == [] and not tracker.dirty  # nothing changed, nothing to save
    # a new report at the same level: remembered, no event
    assert tracker.update([_volcano(level=2, report="2026-10-02T10:00:00+09:00")]) == [] and tracker.dirty
    assert _kinds(tracker.update([_volcano(level=3, report="2026-10-03T10:00:00+09:00")])) == [
        ("volcano_raised", "503", 2, 3)]
    assert _kinds(tracker.update([_volcano(level=2, report="2026-10-04T10:00:00+09:00")])) == [
        ("volcano_lowered", "503", 3, 2)]
    assert _kinds(tracker.update([_volcano(level=1, report="2026-10-05T10:00:00+09:00")])) == [
        ("volcano_lowered", "503", 2, 1)]


def test_the_event_carries_the_volcano_and_the_bulletin() -> None:
    ((kind, data),) = VolcanoTracker({}).update([_volcano(level=3)])
    assert kind == "volcano_raised"
    assert data == {
        "volcano_code": "503", "name_ja": "阿蘇山", "name_en": "Asosan", "old_level": 0, "new_level": 3,
        "kind_code": "12", "kind_ja": "レベル２（火口周辺規制）", "kind_en": "Level 2", "warning_type": "crater",
        "level_system": True, "condition": "引上げ", "headline": "見出し", "report_time": "2026-10-01T10:00:00+09:00",
        "distance_km": 12.3, "jma_url": URL_VOLCANO_PAGE.format(code="503"),
    }


def test_level_0_and_level_1_are_one_state_for_the_events() -> None:
    """JMA lists a volcano at level 1 for a while after a lowering and then drops it: not a change of activity."""
    memory: dict[str, Any] = {}
    tracker = VolcanoTracker(memory)
    assert tracker.update([_volcano(level=1)]) == []  # first seen at level 1: remembered, silent
    assert memory["503"]["level"] == 1
    assert tracker.update([]) == [] and memory["503"]["absent"] == 1
    assert tracker.update([]) == [] and memory == {}  # gone after two polls, silently
    assert _kinds(tracker.update([_volcano(level=1), _volcano("113", 2)])) == [("volcano_raised", "113", 0, 2)]


def test_a_volcano_that_leaves_the_list_is_lowered_to_0_after_two_polls_in_a_row() -> None:
    memory: dict[str, Any] = {}
    tracker = VolcanoTracker(memory)
    tracker.update([_volcano(level=3), _volcano("113", 2, name_ja="北海道駒ヶ岳", name_en="Hokkaido-Komagatake")])
    # one poll without it, then it is back: nothing was sent, the count starts again
    assert tracker.update([_volcano("113", 2)]) == [] and memory["503"]["absent"] == 1
    assert tracker.update([_volcano(level=3), _volcano("113", 2)]) == [] and memory["503"]["absent"] == 0
    assert tracker.update([_volcano("113", 2)]) == []
    ((kind, data),) = tracker.update([_volcano("113", 2)])
    assert kind == "volcano_lowered" and "503" not in memory
    assert data == {
        "volcano_code": "503", "name_ja": "阿蘇山", "name_en": "Asosan", "old_level": 3, "new_level": 0,
        "kind_code": None, "kind_ja": None, "kind_en": None, "warning_type": None, "level_system": None,
        "condition": None, "headline": None, "report_time": None, "distance_km": None,
        "jma_url": URL_VOLCANO_PAGE.format(code="503"),
    }


def test_a_bulletin_older_than_the_one_remembered_is_ignored() -> None:
    memory: dict[str, Any] = {}
    tracker = VolcanoTracker(memory)
    tracker.update([_volcano(level=3, report="2026-10-03T10:00:00+09:00")])
    # an older file from the CDN says level 2: no lowered, no raised after it
    assert tracker.update([_volcano(level=2, report="2026-10-02T10:00:00+09:00")]) == []
    assert memory["503"]["level"] == 3 and memory["503"]["report_time"] == "2026-10-03T10:00:00+09:00"
    assert tracker.update([_volcano(level=3, report="2026-10-03T10:00:00+09:00")]) == []


def test_a_kind_this_release_does_not_know_leaves_the_memory_as_it_was() -> None:
    memory: dict[str, Any] = {}
    tracker = VolcanoTracker(memory)
    tracker.update([_volcano(level=3)])
    before = dict(memory["503"])
    assert tracker.update([_volcano(level=None, kind_code="99")]) == []
    assert memory["503"] == before  # not counted as absent either, however often it is seen
    assert tracker.update([_volcano(level=None, kind_code="99")]) == [] and memory["503"] == before
    assert tracker.update([_volcano(level=None, kind_code="99")]) == [] and memory["503"] == before


def test_a_restart_with_the_memory_sends_only_what_changed_meanwhile() -> None:
    memory: dict[str, Any] = {}
    VolcanoTracker(memory).update([_volcano(level=3), _volcano("113", 2)])
    restarted = json_round_trip(memory)
    tracker = VolcanoTracker(restarted)  # what AlertMemory would have stored and loaded
    assert tracker.update([_volcano(level=3), _volcano("113", 2)]) == [] and not tracker.dirty
    assert _kinds(VolcanoTracker(json_round_trip(memory)).update(
        [_volcano(level=4, report="2026-10-09T10:00:00+09:00"), _volcano("113", 2)])) == [("volcano_raised", "503", 3, 4)]


def json_round_trip(value: dict[str, Any]) -> dict[str, Any]:
    import json
    return json.loads(json.dumps(value))


def test_memory_entries_of_another_shape_are_not_trusted() -> None:
    memory: dict[str, Any] = {"503": "x", "113": {"level": "high"}, "108": {"level": None}}
    events = VolcanoTracker(memory).update([_volcano(level=2), _volcano("113", 2), _volcano("108", 2)])
    assert _kinds(events) == [("volcano_raised", "503", 0, 2), ("volcano_raised", "113", 0, 2),
                              ("volcano_raised", "108", 0, 2)]
    odd = VolcanoTracker({"700": 5})  # a remembered volcano that is not an object: forgotten when absent
    odd.update([])
    odd.update([])
    assert odd._memory == {}


def test_the_stored_memory_keeps_only_well_formed_volcano_entries() -> None:
    kept = {"level": 3, "kind_code": "13", "name_ja": "桜島", "name_en": None, "report_time": "2026-10-01T10:00:00+09:00",
            "absent": 1}
    cleaned = _clean_volcano({
        "506": {**kept, "extra": ["x"], "also": {"y": 1}}, "503": {"level": 7}, "113": {"level": True},
        "108": {"level": 2, "absent": -3}, "109": {"level": 2, "absent": "x"}, "105": "x", 5: {"level": 2}})
    assert cleaned == {"506": kept, "108": {"level": 2, "absent": 0}, "109": {"level": 2, "absent": 0}}
    assert _clean_volcano(None) == {} and _clean_volcano([1]) == {}
    assert empty_memory()["volcano"] == {}


# ---------- the sensor, through the real wiring ----------

async def test_a_place_no_volcano_concerns_reads_0(tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    mock_jma(aioclient_mock)
    await setup(tokyo, entry)
    state = tokyo.states.get(SENSOR)
    assert state.state == "0"
    assert state.attributes == {
        "attribution": JMA_ATTRIBUTION, "state_class": "measurement", "friendly_name": "JMA Chiyoda Volcano",
        "radius_km": 50.0, "max_listed": VOLCANO_MAX_LISTED, "count": 0, "volcanoes": [], "report_time": None,
        "unknown_codes": [],
    }
    assert _requested(aioclient_mock, "volcano/data/warning.json") and _requested(aioclient_mock, "volcano_list.json")
    assert _requested(aioclient_mock, "volcano/data/warning/") == []  # no bulletin text for a volcano that does not count
    assert VolcanoSensor._unrecorded_attributes == frozenset({"volcanoes"})


async def test_a_volcano_whose_bulletin_names_the_municipality_is_the_state(
        tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    aioclient_mock.get(URL_VOLCANO_WARNINGS, json=_name("503"))
    mock_jma(aioclient_mock)
    await setup(tokyo, entry)
    state = tokyo.states.get(SENSOR)
    assert state.state == "2"
    assert state.attributes["count"] == 1 and state.attributes["report_time"] == "2026-09-01T16:00:00+09:00"
    assert state.attributes["volcanoes"] == [{
        "code": "503", "name_ja": "阿蘇山", "name_en": "Asosan", "level": 2, "level_system": True,
        "warning_type": "crater", "kind_code": "12", "kind_ja": "レベル２（火口周辺規制）",
        "kind_en": "Level 2 (Restriction on proximity to the crater)", "condition": "引下げ",
        "headline": _headline("503"), "report_time": "2026-09-01T16:00:00+09:00", "relevance": "municipality",
        "distance_km": round(haversine_km(32.884, 131.104, LAT, LON), 1),
        "jma_url": "https://www.jma.go.jp/bosai/volcano/#type=warning&event_id=503",
    }]
    assert len(_requested(aioclient_mock, "volcano/data/warning/")) == 1  # the bulletin of the one that counts


async def test_the_state_is_the_highest_level_and_the_list_puts_it_first(
        tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    aioclient_mock.get(URL_VOLCANO_WARNINGS, json=_name("503", "506", "113"))
    mock_jma(aioclient_mock)
    await setup(tokyo, entry)
    state = tokyo.states.get(SENSOR)
    assert state.state == "3"  # 桜島, level 3
    listed = state.attributes["volcanoes"]
    # level 3 first, then the level 2 volcanoes by distance from the tests' place (北海道駒ヶ岳 is the nearer)
    assert [(v["code"], v["level"]) for v in listed] == [("506", 3), ("113", 2), ("503", 2)]
    assert state.attributes["count"] == 3
    assert state.attributes["report_time"] == "2026-10-02T07:30:00+09:00"  # the newest of the three reports
    assert [v["headline"] for v in listed] == [_headline("506"), _headline("113"), _headline("503")]


async def test_a_volcano_without_levels_is_a_grade_on_the_same_scale(
        tokyo: HomeAssistant, aioclient_mock) -> None:
    """西之島's bulletin (kind 22, 火口周辺危険) cloned for 箱根山, which names no municipality: the distance decides."""
    hakone = _synthetic("326", "315", "箱根山", [])
    data = [*_list(), hakone]
    aioclient_mock.get(URL_VOLCANO_WARNING.format(code="315"), status=404)  # no bulletin text for a synthetic volcano
    results = {}
    for radius in (100, 0, None):
        entry = MockConfigEntry(domain=DOMAIN, title=TITLE, unique_id=AREA, data=ENTRY_DATA,
                                options={} if radius is None else {"volcano_radius_km": radius})
        aioclient_mock.get(URL_VOLCANO_WARNINGS, json=data)
        mock_jma(aioclient_mock)
        await setup(tokyo, entry)
        results[radius] = tokyo.states.get(SENSOR)
        await tokyo.config_entries.async_unload(entry.entry_id)
        await tokyo.config_entries.async_remove(entry.entry_id)
        aioclient_mock.clear_requests()
        aioclient_mock.get(URL_VOLCANO_WARNING.format(code="315"), status=404)
    assert results[100].state == "2"
    (v,) = results[100].attributes["volcanoes"]
    assert (v["code"], v["name_ja"], v["name_en"]) == ("315", "箱根山", "Hakoneyama")
    assert (v["level"], v["level_system"], v["warning_type"], v["kind_code"]) == (2, False, "crater", "22")
    assert (v["relevance"], v["distance_km"], v["headline"]) == ("distance", 84.0, None)
    assert results[100].attributes["radius_km"] == 100.0
    assert results[0].state == "0" and results[0].attributes["radius_km"] == 0.0  # the rule is off
    assert results[None].state == "0" and results[None].attributes["radius_km"] == 50.0  # 84 km is outside 50


async def test_an_option_that_is_not_a_number_is_the_default_radius(tokyo: HomeAssistant, aioclient_mock) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title=TITLE, unique_id=AREA, data=ENTRY_DATA,
                            options={"volcano_radius_km": "far"})
    mock_jma(aioclient_mock)
    await setup(tokyo, entry)
    assert entry.runtime_data.cfg.volcano_radius_km == 50.0


async def test_the_sensor_goes_unavailable_when_the_list_cannot_be_read_at_all(
        tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    aioclient_mock.get(URL_VOLCANO_WARNINGS, status=503)
    mock_jma(aioclient_mock)
    await setup(tokyo, entry)
    assert tokyo.states.get(SENSOR).state == "unavailable"
    assert tokyo.states.get("sensor.jma_chiyoda_warning_level").state == "0"  # the other feeds go on


# ---------- events, through the real wiring ----------

async def test_a_volcano_already_at_a_level_is_announced_once_with_the_bulletin(
        tokyo: HomeAssistant, aioclient_mock, entry, hass_storage) -> None:
    aioclient_mock.get(URL_VOLCANO_WARNINGS, json=_name("506"))
    mock_jma(aioclient_mock)
    await setup(tokyo, entry)
    event = tokyo.states.get(EVENT)
    assert event.attributes["event_type"] == "volcano_raised"
    a = event.attributes
    assert (a["volcano_code"], a["name_ja"], a["name_en"], a["old_level"], a["new_level"]) == (
        "506", "桜島", "Sakurajima", 0, 3)
    assert (a["kind_code"], a["warning_type"], a["level_system"], a["area"]) == ("13", "crater", True, AREA_NAME)
    assert a["headline"] == _headline("506") and a["report_time"] == "2022-07-27T20:00:00+09:00"
    assert a["jma_url"] == URL_VOLCANO_PAGE.format(code="506")
    assert entry.runtime_data.memory.volcano["506"]["level"] == 3  # remembered, so a restart does not say it again


async def test_a_restart_with_the_memory_is_silent_unless_the_level_changed_meanwhile(
        tokyo: HomeAssistant, aioclient_mock, entry, hass_storage) -> None:
    remembered = {"level": 3, "kind_code": "13", "name_ja": "桜島", "name_en": "Sakurajima",
                  "report_time": "2022-07-27T20:00:00+09:00", "absent": 0}
    hass_storage[f"jma.{entry.entry_id}.alert_memory"] = _stored(entry, {"volcano": {"506": remembered}})
    aioclient_mock.get(URL_VOLCANO_WARNINGS, json=_name("506"))
    mock_jma(aioclient_mock)
    await setup(tokyo, entry)
    assert tokyo.states.get(SENSOR).state == "3"
    assert tokyo.states.get(EVENT).attributes.get("event_type") not in ("volcano_raised", "volcano_lowered")

    # the same, but JMA had it at level 4 when Home Assistant went down
    assert await tokyo.config_entries.async_unload(entry.entry_id)
    hass_storage[f"jma.{entry.entry_id}.alert_memory"] = _stored(entry, {"volcano": {"506": {**remembered, "level": 4}}})
    assert await tokyo.config_entries.async_setup(entry.entry_id)
    await tokyo.async_block_till_done()
    a = tokyo.states.get(EVENT).attributes
    assert (a["event_type"], a["volcano_code"], a["old_level"], a["new_level"]) == ("volcano_lowered", "506", 4, 3)


async def test_the_simulate_action_accepts_the_volcano_events(tokyo: HomeAssistant, aioclient_mock) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title=TITLE, unique_id=AREA, data=ENTRY_DATA,
                            options={"debug_simulate": True})
    mock_jma(aioclient_mock)
    await setup(tokyo, entry)
    for kind in ("volcano_raised", "volcano_lowered"):
        await tokyo.services.async_call(DOMAIN, "simulate", {"event_type": kind, "data": {"new_level": 3}}, blocking=True)
        await tokyo.async_block_till_done()
        a = tokyo.states.get(EVENT).attributes
        assert (a["event_type"], a["new_level"], a["simulated"]) == (kind, 3, True)


# ---------- the coordinator, poll by poll ----------

async def test_events_follow_the_level_across_polls_and_are_held_until_the_event_entity_exists(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    fired: list = []
    saves: list = []
    memory: dict[str, Any] = {}
    mock_volcano(aioclient_mock, warnings=_name("503"))
    co = _coordinator(tokyo, entry, lambda t, d: fired.append((t, d)), memory, lambda: saves.append(1))
    await co.async_refresh()
    assert fired == [] and co.data.level == 2  # held
    co.set_alerts_ready()
    assert _kinds(fired) == [("volcano_raised", "503", 0, 2)] and fired[0][1]["area"] == AREA_NAME
    assert len(saves) == 1 and memory["503"]["level"] == 2

    freezer.tick(INTERVAL_VOLCANO)
    await co.async_refresh()  # the same file
    assert len(fired) == 1 and len(saves) == 1

    freezer.tick(INTERVAL_VOLCANO)  # JMA raises it to level 3
    raised = _name("503")
    _set_kind(next(r for r in raised if r["eventId"] == "503"), "13", "レベル３（入山規制）", "2026-10-20T09:00:00+09:00")
    aioclient_mock.clear_requests()
    mock_volcano(aioclient_mock, warnings=raised)
    await co.async_refresh()
    assert _kinds(fired[1:]) == [("volcano_raised", "503", 2, 3)] and co.data.level == 3
    assert fired[1][1]["kind_code"] == "13" and fired[1][1]["report_time"] == "2026-10-20T09:00:00+09:00"
    assert len(saves) == 2

    aioclient_mock.clear_requests()
    mock_volcano(aioclient_mock, warnings=_list())  # then it is not in the list any more
    freezer.tick(INTERVAL_VOLCANO)
    await co.async_refresh()
    assert len(fired) == 2 and co.data.level == 0  # one poll without it: the state is 0, nothing is announced yet
    freezer.tick(INTERVAL_VOLCANO)
    await co.async_refresh()
    assert _kinds(fired[2:]) == [("volcano_lowered", "503", 3, 0)] and memory == {}
    assert fired[2][1]["name_ja"] == "阿蘇山" and fired[2][1]["headline"] is None


async def test_a_failed_poll_keeps_the_data_and_announces_nothing(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    fired: list = []
    mock_volcano(aioclient_mock, warnings=_name("503"))
    co = _coordinator(tokyo, entry, lambda t, d: fired.append((t, d)), {}, None)
    co.set_alerts_ready()
    await co.async_refresh()
    good = co.data
    assert len(fired) == 1
    aioclient_mock.clear_requests()
    aioclient_mock.get(URL_VOLCANO_WARNINGS, status=503)
    freezer.tick(INTERVAL_VOLCANO)
    await co.async_refresh()
    assert co.data is good and co.last_error and len(fired) == 1
    for _ in range(3):  # three intervals without an answer: the data is too old to keep
        freezer.tick(INTERVAL_VOLCANO)
        await co.async_refresh()
    assert co.last_update_success is False and len(fired) == 1


async def test_names_and_coordinates_come_once_a_day_and_survive_a_failed_request(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    mock_volcano(aioclient_mock, warnings=_name("503"))
    co = _coordinator(tokyo, entry, None, {}, None)
    await co.async_refresh()
    assert len(_requested(aioclient_mock, "volcano_list.json")) == 1
    assert co.data.volcanoes[0]["name_en"] == "Asosan"
    freezer.tick(INTERVAL_VOLCANO)
    await co.async_refresh()
    assert len(_requested(aioclient_mock, "volcano_list.json")) == 1  # a day has not passed

    aioclient_mock.clear_requests()
    aioclient_mock.get(URL_VOLCANO_LIST, status=503)
    mock_volcano(aioclient_mock, warnings=_name("503"))
    freezer.tick(INTERVAL_VOLCANO_LIST)
    await co.async_refresh()
    assert len(_requested(aioclient_mock, "volcano_list.json")) == 1
    assert co.last_update_success is True and co.data.volcanoes[0]["name_en"] == "Asosan"  # the last list is kept
    freezer.tick(INTERVAL_VOLCANO)
    await co.async_refresh()
    assert len(_requested(aioclient_mock, "volcano_list.json")) == 1  # a failed day is not retried every poll


async def test_without_a_volcano_list_the_names_come_from_the_bulletin_and_the_municipality_still_decides(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer, caplog) -> None:
    aioclient_mock.get(URL_VOLCANO_LIST, status=503)
    mock_volcano(aioclient_mock, warnings=[*_name("503"), _synthetic("326", "315", "箱根山", [])])
    aioclient_mock.get(URL_VOLCANO_WARNING.format(code="315"), status=404)
    co = _coordinator(tokyo, entry, None, {}, None, volcano_radius_km=500.0)
    with caplog.at_level(logging.DEBUG, logger="custom_components.jma"):
        await co.async_refresh()
    (v,) = co.data.volcanoes  # 箱根山 has no coordinates without the list: it cannot be placed
    assert (v["code"], v["name_ja"], v["name_en"], v["distance_km"], v["relevance"]) == (
        "503", "阿蘇山", None, None, "municipality")
    assert co.last_update_success is True
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
    # the list is asked for again at the next poll while there is none
    aioclient_mock.clear_requests()
    mock_volcano(aioclient_mock, warnings=[*_name("503"), _synthetic("326", "315", "箱根山", [])])
    aioclient_mock.get(URL_VOLCANO_WARNING.format(code="315"), status=404)
    freezer.tick(INTERVAL_VOLCANO)
    await co.async_refresh()
    # level 2 both: the nearer first (箱根山, 84 km, then 阿蘇山)
    assert [(v["code"], v["name_en"]) for v in co.data.volcanoes] == [("315", "Hakoneyama"), ("503", "Asosan")]

    # a list that is not what JMA publishes is the same as no list
    aioclient_mock.clear_requests()
    aioclient_mock.get(URL_VOLCANO_LIST, json={"changed": True})
    mock_volcano(aioclient_mock, warnings=_name("503"))
    co2 = _coordinator(tokyo, entry, None, {}, None)
    await co2.async_refresh()
    assert co2.data.volcanoes[0]["name_en"] is None and co2.last_update_success is True


async def test_a_headline_is_asked_for_once_per_report_and_a_failed_request_is_tried_again(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    detail = URL_VOLCANO_WARNING.format(code="503")
    aioclient_mock.get(detail, status=503)  # first registration wins: the first poll gets no headline
    mock_volcano(aioclient_mock, warnings=_name("503"))
    co = _coordinator(tokyo, entry, None, {}, None)
    await co.async_refresh()
    assert co.data.volcanoes[0]["headline"] is None and co.last_update_success is True
    assert len(_requested(aioclient_mock, "warning/503.json")) == 1

    aioclient_mock.clear_requests()
    mock_volcano(aioclient_mock, warnings=_name("503"))
    freezer.tick(INTERVAL_VOLCANO)
    await co.async_refresh()
    assert co.data.volcanoes[0]["headline"] == _headline("503")
    freezer.tick(INTERVAL_VOLCANO)
    await co.async_refresh()
    assert len(_requested(aioclient_mock, "warning/503.json")) == 1  # the same report: not asked for again

    raised = _name("503")
    _set_kind(next(r for r in raised if r["eventId"] == "503"), "13", "レベル３（入山規制）", "2026-10-20T09:00:00+09:00")
    aioclient_mock.clear_requests()
    mock_volcano(aioclient_mock, warnings=raised)
    freezer.tick(INTERVAL_VOLCANO)
    await co.async_refresh()
    assert len(_requested(aioclient_mock, "warning/503.json")) == 1  # a new report: asked for again

    # a bulletin file that is not an object: no headline, no failed poll
    aioclient_mock.clear_requests()
    aioclient_mock.get(detail, json=["not", "an", "object"])
    mock_volcano(aioclient_mock, warnings=_name("506"))
    freezer.tick(INTERVAL_VOLCANO)
    await co.async_refresh()
    assert co.last_update_success is True and co.data.volcanoes[0]["code"] == "506"


async def test_a_kind_code_this_release_does_not_know_is_listed_but_not_counted(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer, caplog) -> None:
    fired: list = []
    memory: dict[str, Any] = {}
    data = _name("503", "506")
    _set_kind(next(r for r in data if r["eventId"] == "506"), "99", "新しい区分")
    mock_volcano(aioclient_mock, warnings=data)
    co = _coordinator(tokyo, entry, lambda t, d: fired.append((t, d)), memory, None)
    co.set_alerts_ready()
    with caplog.at_level(logging.WARNING, logger="custom_components.jma"):
        await co.async_refresh()
        freezer.tick(INTERVAL_VOLCANO)
        await co.async_refresh()
    assert co.data.level == 2 and co.data.unknown_codes == ["99"] and co.data.count == 2
    unknown = next(v for v in co.data.volcanoes if v["code"] == "506")
    assert (unknown["level"], unknown["kind_code"], unknown["kind_ja"], unknown["kind_en"]) == (None, "99", "新しい区分", None)
    assert _kinds(fired) == [("volcano_raised", "503", 0, 2)] and "506" not in memory
    warned = [r for r in caplog.records if "kind code 99" in r.getMessage()]
    assert len(warned) == 1 and warned[0].levelno == logging.WARNING  # once, not at every poll


async def test_the_state_counts_every_volcano_and_the_attribute_lists_the_first_ten(
        tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    fired: list = []
    memory: dict[str, Any] = {}
    many = [_synthetic("503", f"{700 + i}", f"火山{i}", [AREA]) for i in range(15)]
    aioclient_mock.get(__import__("re").compile(r".*/volcano/data/warning/7\d\d\.json"), status=404)
    mock_volcano(aioclient_mock, warnings=many)
    co = _coordinator(tokyo, entry, lambda t, d: fired.append((t, d)), memory, None)
    co.set_alerts_ready()
    await co.async_refresh()
    assert co.data.count == 15 and len(co.data.volcanoes) == VOLCANO_MAX_LISTED == 10
    assert [v["code"] for v in co.data.volcanoes] == [f"{700 + i}" for i in range(10)]  # level, distance (none), code
    assert len(fired) == 15 and len(memory) == 15  # the events and the memory see all of them
    assert len(_requested(aioclient_mock, "volcano/data/warning/7")) == 10  # a bulletin text for the ones listed


# ---------- feed health, diagnostics, options, icons ----------

async def test_the_volcano_feed_is_part_of_feed_health(tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    mock_jma(aioclient_mock)
    await setup(tokyo, entry)
    rt = entry.runtime_data
    assert tokyo.states.get(FEED).attributes["volcano"] == {"problem": False, "since": None, "reason": None}
    now = dt_util.utcnow()
    assert feed_conditions(rt, now)["volcano"] is None
    assert feed_conditions(rt, rt.volcano.last_ok + HEALTH_VOLCANO_MAX_AGE)["volcano"] is None
    rt.volcano.last_error = "https://www.jma.go.jp/bosai/volcano/data/warning.json: HTTP 503"
    reason = feed_conditions(rt, rt.volcano.last_ok + HEALTH_VOLCANO_MAX_AGE + timedelta(seconds=1))["volcano"]
    assert reason.startswith("volcano warnings not fetched since ") and "HTTP 503" in reason
    # a feed that never answered is measured from the first try
    rt.volcano.last_ok = None
    assert feed_conditions(rt, rt.volcano.first_try + timedelta(minutes=14))["volcano"] is None
    assert "not fetched since None" in feed_conditions(rt, rt.volcano.first_try + timedelta(minutes=16))["volcano"]


async def test_the_diagnostics_list_the_volcano_feed_without_the_place(
        tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    aioclient_mock.get(URL_VOLCANO_WARNINGS, json=_name("503"))
    mock_jma(aioclient_mock)
    await setup(tokyo, entry)
    entry.runtime_data.volcano.last_error = f"area {AREA} {AREA_NAME} at {LAT},{LON}: HTTP 500"
    out = await async_get_config_entry_diagnostics(tokyo, entry)
    assert out["coordinators"]["jma volcano"]["last_update_success"] is True
    text = str(out["coordinators"]["jma volcano"])
    assert AREA not in text and AREA_NAME not in text and str(LAT) not in text and "HTTP 500" in text


async def test_the_options_form_offers_the_radius_and_stores_it_as_a_whole_number(
        tokyo: HomeAssistant, aioclient_mock, entry, hass_client) -> None:
    client = await _client(tokyo, aioclient_mock, entry, hass_client)
    form = await (await client.post(FLOW, json={"handler": entry.entry_id})).json()
    field = next(f for f in form["data_schema"] if f["name"] == "volcano_radius_km")
    assert field["selector"]["number"] == {
        "min": 0.0, "max": float(VOLCANO_RADIUS_MAX_KM), "step": 1.0, "unit_of_measurement": "km", "mode": "box"}
    assert field["description"] == {"suggested_value": 50} and field["optional"] is True
    stations = {"station_primary": entry.data["station_primary"], "station_secondary": entry.data["station_secondary"],
                "debug_simulate": False}
    saved = await client.post(f"{FLOW}/{form['flow_id']}", json={**stations, "volcano_radius_km": 80})
    assert (await saved.json())["type"] == "create_entry"
    await tokyo.async_block_till_done()
    assert entry.options["volcano_radius_km"] == 80 and isinstance(entry.options["volcano_radius_km"], int)
    assert entry.runtime_data.cfg.volcano_radius_km == 80.0  # the entry reloaded with it
    form = await (await client.post(FLOW, json={"handler": entry.entry_id})).json()
    assert next(f for f in form["data_schema"] if f["name"] == "volcano_radius_km")["description"] == {
        "suggested_value": 80}
    # a form that does not send it (left empty, or an old client): the default applies again
    saved = await client.post(f"{FLOW}/{form['flow_id']}", json=stations)
    assert (await saved.json())["type"] == "create_entry"
    await tokyo.async_block_till_done()
    assert "volcano_radius_km" not in entry.options and entry.runtime_data.cfg.volcano_radius_km == 50.0


async def test_a_radius_beyond_the_largest_is_refused_by_the_form(
        tokyo: HomeAssistant, aioclient_mock, entry, hass_client) -> None:
    client = await _client(tokyo, aioclient_mock, entry, hass_client)
    form = await (await client.post(FLOW, json={"handler": entry.entry_id})).json()
    response = await client.post(f"{FLOW}/{form['flow_id']}", json={
        "station_primary": "44132", "station_secondary": "44132", "debug_simulate": False,
        "volcano_radius_km": VOLCANO_RADIUS_MAX_KM + 1})
    assert response.status == 400
    assert dict(entry.options) == {}


def test_the_volcano_icon_follows_the_level() -> None:
    section = ICONS["entity"]["sensor"]["volcano"]
    assert [_icon_for(section, str(level)) for level in (0, 1, 2, 3, 4, 5)] == [
        "mdi:volcano-outline", "mdi:volcano-outline", "mdi:volcano", "mdi:alert", "mdi:alert-octagon",
        "mdi:alert-octagram"]
    assert _icon_for(section, "unavailable") == section["default"] == "mdi:volcano"
