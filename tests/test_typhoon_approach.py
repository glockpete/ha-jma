"""Typhoon approach: storm zone, stage, tracker, coordinator events.

Real captures only: typhoon_TC2633_* (issue 2026-10-03 21:45 JST, Choi-wan, no. 2627) and
typhoon_TC2632_* (issue 2026-09-27 22:45 JST, Surigae). Edits to a real file are said so.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.jma.api import JmaApi
from custom_components.jma.const import (
    TYPHOON_ATTR_MAX_BYTES,
    URL_TYPHOON_FORECAST,
    URL_TYPHOON_SPECS,
    URL_TYPHOON_TARGET,
)
from custom_components.jma.coordinator import JmaConfig, TyphoonCoordinator
from custom_components.jma.parse_typhoon import (
    ApproachTracker,
    approach_stage,
    fit_storms,
    haversine_km,
    parse_storm,
    parse_target_tc,
    storm_zone_steps,
    zone_entry,
    zone_margin_km,
)

from .conftest import ENTRY_DATA, mock_heat_alert, mock_jma, setup
from .helpers import clone, load_json

TOKYO = (35.6812, 139.7671)  # Tokyo Station, the coordinates the test fixtures use
AT_SEA = (33.1, 146.2)  # the +72 h centre of TC2633
NOW_2200 = datetime.fromisoformat("2026-10-03T22:00:00+09:00")
ENTERS_AT_SEA = "2026-10-06T08:02:00+09:00"  # first moment AT_SEA is inside the zone (computed)


def _tc(which: str = "TC2633") -> dict:
    name = "typhoon_targetTc_20261003.json" if which == "TC2633" else "typhoon_targetTc.json"
    return parse_target_tc(load_json(name))[0]


def _storm(home=TOKYO, which: str = "TC2633", forecast=None) -> dict:
    return parse_storm(_tc(which), forecast if forecast is not None else
                       load_json(f"typhoon_{which}_forecast.json"),
                       load_json(f"typhoon_{which}_specs.json"), home)


def _fparts(forecast) -> dict[int, dict]:
    return {p["advancedHours"]: p for p in forecast if isinstance(p.get("advancedHours"), int)}


# ---------- storm zone steps ----------

def test_zone_steps_tc2633_are_each_steps_own_circle():
    steps = storm_zone_steps(_fparts(load_json("typhoon_TC2633_forecast.json")))
    assert [s["hours"] for s in steps] == [0, 12, 24, 48, 72, 96]
    assert [s["radius_km"] for s in steps] == [129.6, 222.2, 277.8, 370.4, 435.2, 555.6]
    assert steps[3] == {"hours": 48, "time": "2026-10-05T21:00:00+09:00", "lat": 26.6, "lon": 145.67,
                        "radius_km": 370.4}  # the 48 h ARC centre, not the track centre [26.6, 145.3]
    assert steps[4]["lat"] == 33.1 and steps[4]["lon"] == 147.09  # arc centre again differs from [33.1, 146.2]
    assert steps[0]["time"] == "2026-10-03T21:00:00+09:00"


def test_zone_steps_tc2632_skip_steps_without_a_new_circle():
    steps = storm_zone_steps(_fparts(load_json("typhoon_TC2632_forecast.json")))
    assert [s["hours"] for s in steps] == [0, 1, 2, 5, 8, 11, 14, 17, 20, 23, 47, 71]  # 95 and 119 repeat 71's
    assert [s["radius_km"] for s in steps] == [83.3, 83.3, 114.8, 118.5, 124.1, 138.9, 142.6, 148.2,
                                               151.9, 157.4, 250.0, 388.9]
    assert (steps[-1]["lat"], steps[-1]["lon"]) == (31.0, 138.6)
    # step 20 lists its new circle in two arcs ([0,159.9] and [331.48,360]) but is one step
    assert [s["hours"] for s in steps].count(20) == 1


def test_zone_steps_largest_new_circle_wins_and_bad_shapes_are_skipped():
    """Real TC2633 with two edits: step 12 gets an extra, larger new arc; step 24's arc is garbage."""
    forecast = clone(load_json("typhoon_TC2633_forecast.json"))
    parts = _fparts(forecast)
    parts[12]["stormWarningArea"]["arc"].append([[22.0, 147.0], 300000, [0.0, 10.0]])
    parts[24]["stormWarningArea"]["arc"] = "not a list of arcs"
    steps = storm_zone_steps(parts)
    assert [(s["hours"], s["radius_km"]) for s in steps][:2] == [(0, 129.6), (12, 300.0)]
    assert 24 not in [s["hours"] for s in steps]
    assert storm_zone_steps({}) == [] and storm_zone_steps({0: {}, 1: {"stormWarningArea": None}}) == []


def test_parse_storm_carries_storm_zone():
    s = _storm()
    zone = s["storm_zone"]
    assert [x["hours"] for x in zone["steps"]] == [0, 12, 24, 48, 72, 96]
    assert zone["enters"] is None and zone["margin_km"] == 295.2
    assert json.loads(json.dumps(s)) == s  # serialisable
    assert len(json.dumps([s], ensure_ascii=False).encode()) < TYPHOON_ATTR_MAX_BYTES


# ---------- zone_entry / margin ----------

def test_zone_entry_home_inside_hull_but_inside_neither_end_circle():
    """Two REAL circles of TC2633, the 0 h one (20.3, 146.2, 129.6 km) and the 48 h one
    (26.6, 145.67, 370.4 km), with a home point CHOSEN by us at (22.5, 146.0): it lies in the convex
    hull of the two discs (the interpolated disc at f = 0.35) but in neither end circle, so a
    nearest-circle test would miss it."""
    steps = [s for s in storm_zone_steps(_fparts(load_json("typhoon_TC2633_forecast.json")))
             if s["hours"] in (0, 48)]
    a, b = steps
    home = (22.5, 146.0)
    assert haversine_km(a["lat"], a["lon"], *home) > a["radius_km"]
    assert haversine_km(b["lat"], b["lon"], *home) > b["radius_km"]
    assert zone_entry([a], home) is None and zone_entry([b], home) is None  # a single circle alone
    hit = zone_entry(steps, home)
    assert hit is not None
    assert 0 < hit["hours"] < 48 and hit["distance_km"] <= hit["radius_km"]
    assert a["time"] < hit["time"] < b["time"]
    # first sample inside is f = 0.14 of the leg: 6.72 h after 21:00 = 03:43, r = 129.6 + 0.14 * 240.8
    assert hit == {"hours": 6.7, "time": "2026-10-04T03:43:00+09:00", "radius_km": 163.3,
                   "distance_km": 147.1}
    assert zone_margin_km(steps, home) < 0


def test_zone_entry_single_circle_and_already_inside():
    a = storm_zone_steps(_fparts(load_json("typhoon_TC2633_forecast.json")))[0]
    inside = (a["lat"], a["lon"])
    hit = zone_entry([a], inside)
    assert hit == {"hours": 0.0, "time": a["time"], "radius_km": 129.6, "distance_km": 0.0}
    assert zone_entry([a], TOKYO) is None and zone_margin_km([a], inside) == -129.6
    assert zone_entry([], TOKYO) is None and zone_margin_km([], TOKYO) is None


def test_zone_entry_tc2632_real_fixture():
    s = _storm(home=(28.9, 135.5), which="TC2632")  # the +47 h centre
    assert s["storm_zone"]["enters"] == {"hours": 33.6, "time": "2026-09-29T07:34:00+09:00",
                                         "radius_km": 198.1, "distance_km": 192.9}
    assert s["storm_zone"]["margin_km"] == -250.0
    far = _storm(which="TC2632")
    assert far["storm_zone"]["enters"] is None and far["storm_zone"]["margin_km"] == 142.8


# ---------- stage, and the dry run ----------

def test_dry_run_tc2633_tokyo_none_then_home_at_sea_notice_then_urgent():
    tokyo = _storm(TOKYO)
    assert tokyo["storm_zone"]["margin_km"] == 295.2  # about +295 km outside
    assert tokyo["closest"]["hours"] == 72.0 and tokyo["closest"]["distance_km"] == 656
    assert approach_stage(tokyo, NOW_2200) == "none"

    sea = _storm(AT_SEA)
    assert sea["storm_zone"]["enters"]["time"] == ENTERS_AT_SEA
    assert sea["closest"]["distance_km"] == 0 and sea["closest"]["time"] == "2026-10-06T21:00:00+09:00"
    assert approach_stage(sea, NOW_2200) == "notice"
    enters = datetime.fromisoformat(ENTERS_AT_SEA)
    assert approach_stage(sea, enters - timedelta(hours=24, minutes=1)) == "notice"
    assert approach_stage(sea, enters - timedelta(hours=24)) == "urgent"
    assert approach_stage(sea, enters + timedelta(hours=3)) == "urgent"  # already inside counts


def test_stage_notice_by_entry_only_and_by_closest_only():
    sea = _storm(AT_SEA)
    far_closest = {**sea, "closest": {**sea["closest"], "distance_km": 500}}
    # inside 72 h of the zone entry (60 h away) with the closest clause false
    assert approach_stage(far_closest, NOW_2200) == "notice"
    assert approach_stage(far_closest, enters_minus(73)) == "none"
    no_entry = {**sea, "storm_zone": {**sea["storm_zone"], "enters": None}}
    assert approach_stage(no_entry, NOW_2200) == "notice"  # closest 0 km, 71 h away
    assert approach_stage({**no_entry, "closest": None}, NOW_2200) == "none"
    assert approach_stage({**no_entry, "closest": {"distance_km": 10, "time": None}}, NOW_2200) == "none"


def enters_minus(hours: float) -> datetime:
    return datetime.fromisoformat(ENTERS_AT_SEA) - timedelta(hours=hours)


def test_storm_without_storm_warning_area_stage_from_closest_only():
    """Real TC2633 with every `stormWarningArea` removed (a storm that has none)."""
    forecast = clone(load_json("typhoon_TC2633_forecast.json"))
    for part in forecast:
        part.pop("stormWarningArea", None)
    s = _storm(AT_SEA, forecast=forecast)
    assert s["storm_zone"] is None
    assert approach_stage(s, NOW_2200) == "notice"  # 0 km, 71 h ahead
    assert approach_stage(s, datetime.fromisoformat("2026-10-06T00:00:00+09:00")) == "notice"  # never urgent
    assert approach_stage(_storm(TOKYO, forecast=forecast), NOW_2200) == "none"
    assert fit_storms([s]) == [s]


def test_incomplete_storm_has_no_stage():
    bare = {"id": "TC2633", "number": "2627", "category": "TY", "issue": None, "now": None,
            "track": [], "past_track": [], "distance_km": None, "closest": None, "incomplete": True}
    assert approach_stage(bare, NOW_2200) is None


def _iso(n: int) -> str:
    """The n-th JMA issue after 2026-10-06 00:45 JST (strings that parse as times)."""
    return (datetime.fromisoformat("2026-10-06T00:45:00+09:00") + timedelta(hours=n)).isoformat()


# ---------- no track ----------

def test_no_track_storm_within_300_km_is_notice():
    """Real TC2633 for Tokyo Station, with `closest` removed (a storm whose track failed to parse) and
    the storm's own distance set to a number (edit to the real dict)."""
    s = _storm(TOKYO)
    near = {**s, "closest": None, "storm_zone": None, "distance_km": 250}
    assert approach_stage(near, NOW_2200) == "notice"
    assert approach_stage({**near, "distance_km": 300}, NOW_2200) == "notice"
    assert approach_stage({**near, "distance_km": 301}, NOW_2200) == "none"
    assert approach_stage({**near, "distance_km": None}, NOW_2200) == "none"
    assert approach_stage({**near, "closest": {"distance_km": 10, "time": None}}, NOW_2200) == "notice"
    far_track = {**s, "storm_zone": None, "distance_km": 250}  # a usable closest decides: 656 km
    assert approach_stage(far_track, NOW_2200) == "none"


# ---------- tracker ----------

NOW_LATE = datetime.fromisoformat("2026-10-07T00:00:00+09:00")  # after the sea storm's closest (10-06 21:00)


def _staged(stage, home=AT_SEA, **extra) -> dict:
    return {**_storm(home), "stage": stage, **extra}


def _types(events) -> list[str]:
    return [t for t, _ in events]


def test_tracker_rise_sends_one_event_per_step_up():
    memory: dict = {}
    t = ApproachTracker(memory)
    assert t.update([_staged("none")], NOW_2200) == [] and memory == {} and not t.dirty
    ev = t.update([_staged("notice")], NOW_2200)
    assert _types(ev) == ["typhoon_approach"]
    assert memory == {"TC2633": {
        "stage": "notice", "quiet": 0, "last_issue": "2026-10-03T21:45:00+09:00", "away": 0,
        "number": "2627", "name_en": "Choi-wan",
        "name_ja": load_json("typhoon_TC2633_forecast.json")[0]["name"]["jp"]}}
    assert t.dirty
    t.dirty = False
    assert t.update([_staged("notice")], NOW_2200) == [] and not t.dirty  # same stage: nothing, nothing to save
    ev2 = t.update([_staged("urgent")], NOW_2200)
    assert _types(ev2) == ["typhoon_approach"] and memory["TC2633"]["stage"] == "urgent"
    p1, p2 = ev[0][1], ev2[0][1]
    assert (p1["stage"], p1["previous_stage"]) == ("notice", "none")
    assert (p2["stage"], p2["previous_stage"]) == ("urgent", "notice")
    json.dumps(p1)  # serialisable
    storm = _storm(AT_SEA)
    assert p1 == {
        "storm_id": "TC2633", "number": "2627", "number_short": 27, "name_en": "Choi-wan",
        "name_ja": load_json("typhoon_TC2633_forecast.json")[0]["name"]["jp"], "category": "TY",
        "stage": "notice", "previous_stage": "none", "closest_km": 0, "closest_time": "2026-10-06T21:00:00+09:00",
        "closest_hours": 72.0, "zone_enters_time": ENTERS_AT_SEA, "zone_enters_hours": 59.0,
        "distance_km": storm["distance_km"], "pressure_hpa": storm["now"]["pressure_hpa"],
        "max_wind_ms": storm["now"]["max_wind_ms"], "issue": "2026-10-03T21:45:00+09:00",
    }
    assert storm["now"]["pressure_hpa"] is not None and storm["now"]["max_wind_ms"] is not None


def test_tracker_urgent_easing_to_notice_sends_nothing_and_keeps_urgent():
    memory = {"TC2633": {"stage": "urgent", "quiet": 2}}
    t = ApproachTracker(memory)
    assert t.update([_staged("notice")], NOW_2200) == []
    assert memory["TC2633"]["stage"] == "urgent" and memory["TC2633"]["quiet"] == 0


def test_tracker_three_quiet_issues_clear_a_receding_storm():
    memory = {"TC2633": {"stage": "notice", "quiet": 0}}
    t = ApproachTracker(memory)
    assert t.update([_staged("none", issue=_iso(1))], NOW_LATE) == [] and memory["TC2633"]["quiet"] == 1 and t.dirty
    assert memory["TC2633"]["last_issue"] == _iso(1)
    assert t.update([_staged("none", issue=_iso(1))], NOW_LATE) == []  # the same issue again: not counted
    assert t.update([_staged("none", issue=_iso(1))], NOW_LATE) == [] and memory["TC2633"]["quiet"] == 1
    assert t.update([_staged("none", issue=_iso(2))], NOW_LATE) == [] and memory["TC2633"]["quiet"] == 2
    ev = t.update([_staged("none", issue=_iso(3))], NOW_LATE)
    assert _types(ev) == ["typhoon_cleared"] and memory == {}
    p = ev[0][1]
    assert p["reason"] == "passed" and p["previous_stage"] == "notice"
    assert (p["storm_id"], p["number"], p["number_short"], p["name_en"]) == ("TC2633", "2627", 27, "Choi-wan")
    assert set(p) == {"storm_id", "number", "number_short", "name_en", "name_ja", "previous_stage", "reason"}
    # cleared storms start over: the next approach is announced again
    assert _types(t.update([_staged("notice")], NOW_2200)) == ["typhoon_approach"]


def test_tracker_stage_none_while_still_approaching_changes_nothing():
    """Stage none with the closest approach still ahead (a forecast that moved outside the
    thresholds): the remembered stage stands, nothing is counted, nothing is sent."""
    start = {"stage": "notice", "quiet": 1, "last_issue": _iso(0), "away": 0, "number": "2627",
             "name_en": "Choi-wan", "name_ja": None}
    memory = {"TC2633": dict(start)}
    t = ApproachTracker(memory)
    for issue in (_iso(1), _iso(2), _iso(3), _iso(4)):
        assert t.update([_staged("none", issue=issue)], NOW_2200) == []  # closest 10-06 21:00 is ahead
    assert memory == {"TC2633": start} and not t.dirty


def test_tracker_receding_without_closest_uses_the_distance():
    base = _staged("none", closest=None)
    for distance, counts in ((350, True), (300, False), (None, False)):
        memory = {"TC2633": {"stage": "notice", "quiet": 0}}
        ApproachTracker(memory).update([{**base, "distance_km": distance, "issue": _iso(1)}], NOW_2200)
        assert (memory["TC2633"]["quiet"] == 1) is counts, distance


def test_tracker_missing_issue_never_counts_and_non_none_resets_the_count():
    memory = {"TC2633": {"stage": "notice", "quiet": 2, "last_issue": _iso(1)}}
    t = ApproachTracker(memory)
    assert t.update([_staged("none", issue=None)], NOW_LATE) == [] and memory["TC2633"]["quiet"] == 2
    assert t.update([_staged("notice", issue=_iso(9))], NOW_LATE) == []
    assert memory["TC2633"]["quiet"] == 0 and memory["TC2633"]["last_issue"] == _iso(9)
    assert t.update([_staged("none", issue=_iso(10))], NOW_LATE) == [] and memory["TC2633"]["quiet"] == 1


def test_tracker_storm_gone_needs_time_and_names_come_from_memory():
    memory = {"TC2633": {"stage": "urgent", "quiet": 1, "number": "2627", "name_en": "Choi-wan",
                         "name_ja": "name-ja"}}
    t = ApproachTracker(memory)
    t0 = NOW_2200
    assert t.update([], t0) == [] and memory["TC2633"]["absent_since"] == t0.isoformat() and t.dirty
    assert t.update([], t0 + timedelta(minutes=1)) == []  # a second start a minute later clears nothing
    assert t.update([], t0 + timedelta(minutes=28)) == []  # (one minute of slack on the 30)
    assert t.update([_staged("urgent")], t0 + timedelta(minutes=30)) == []  # back again
    assert "absent_since" not in memory["TC2633"]
    memory["TC2633"]["name_ja"] = "name-ja"  # (the storm's own names were written back; use memory's)
    assert t.update([], t0 + timedelta(minutes=40)) == []  # a new absence starts its own clock
    assert t.update([], t0 + timedelta(minutes=68)) == []
    ev = t.update([], t0 + timedelta(minutes=71))
    assert _types(ev) == ["typhoon_cleared"] and memory == {}
    assert ev[0][1] == {"storm_id": "TC2633", "number": "2627", "number_short": 27, "name_en": "Choi-wan",
                        "name_ja": "name-ja", "previous_stage": "urgent", "reason": "gone"}
    # an entry from before the names were kept: still cleared, names unknown
    old = ApproachTracker({"TC2599": {"stage": "notice", "quiet": 0}})
    old.update([], t0)
    assert old.update([], t0 + timedelta(minutes=31))[0][1] == {
        "storm_id": "TC2599", "number": None, "number_short": None, "name_en": None, "name_ja": None,
        "previous_stage": "notice", "reason": "gone"}
    # an old-format entry (absent: 1, no absent_since) is a first absence now, not a clear
    legacy = {"TC2599": {"stage": "notice", "quiet": 0, "absent": 1}}
    assert ApproachTracker(legacy).update([], t0) == []
    assert legacy["TC2599"]["absent_since"] == t0.isoformat() and "absent" not in legacy["TC2599"]


def test_gone_does_not_wait_for_a_third_poll_when_the_second_is_a_second_early():
    """Home Assistant can start the next poll just under 30 minutes after the last one."""
    memory = {"TC2633": {"stage": "notice", "quiet": 0}}
    t = ApproachTracker(memory)
    assert t.update([], NOW_2200) == []
    assert t.update([], NOW_2200 + timedelta(minutes=28)) == []  # a restart in between: too soon
    ev = t.update([], NOW_2200 + timedelta(minutes=30) - timedelta(seconds=1))
    assert _types(ev) == ["typhoon_cleared"] and ev[0][1]["reason"] == "gone" and memory == {}


def test_tracker_incomplete_storm_is_untouched():
    start = {"stage": "notice", "quiet": 2, "last_issue": _iso(1), "away": 0}
    memory = {"TC2633": dict(start)}
    t = ApproachTracker(memory)
    bare = {"id": "TC2633", "number": "2627", "incomplete": True, "closest": None, "stage": None}
    assert t.update([bare], NOW_LATE) == [] and memory == {"TC2633": start} and not t.dirty
    assert t.update([{**_storm(AT_SEA), "stage": None}], NOW_LATE) == []  # kept from last-good: same
    assert memory == {"TC2633": start}


def test_tracker_restart_with_memory_at_notice_sends_no_event():
    memory = {"TC2633": {"stage": "notice", "quiet": 0}}
    assert ApproachTracker(memory).update([_staged("notice")], NOW_2200) == []
    assert memory["TC2633"]["stage"] == "notice"
    # ...but a rise to urgent after the restart is announced
    assert _types(ApproachTracker(memory).update([_staged("urgent")], NOW_2200)) == ["typhoon_approach"]


def test_tracker_survives_garbage_in_its_memory():
    for bad in ({"stage": "notice", "quiet": "two"}, {"stage": ["notice"], "quiet": 0},
                {"stage": "notice", "quiet": None, "absent_since": 4, "away": "x", "last_issue": 5, "number": []},
                "junk", None):
        for stage in ("none", "notice", "urgent"):
            memory = {"TC2633": bad if not isinstance(bad, dict) else dict(bad)}
            events = ApproachTracker(memory).update([_staged(stage, issue=_iso(1))], NOW_LATE)
            assert all(t == "typhoon_approach" for t, _ in events), (bad, stage)
        gone = {"TC2633": bad if not isinstance(bad, dict) else dict(bad)}
        t = ApproachTracker(gone)
        t.update([], NOW_2200)
        t.update([], NOW_2200 + timedelta(minutes=31))
        assert gone == {}


def test_tracker_number_short():
    ev = ApproachTracker({}).update([_staged("notice", number=None)], NOW_2200)
    assert ev[0][1]["number_short"] is None
    ev = ApproachTracker({}).update([_staged("notice", number="2601")], NOW_2200)
    assert ev[0][1]["number_short"] == 1


# ---------- fit_storms ----------

def _long_storm() -> dict:
    forecast = clone(load_json("typhoon_TC2632_forecast.json"))
    _fparts(forecast)[0]["track"]["typhoon"] = [[10 + i * 0.03, 150 - i * 0.03] for i in range(500)]
    return _storm(TOKYO, "TC2632", forecast)


def _size(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False).encode())


def test_fit_storms_drops_zone_steps_before_cutting_past_tracks():
    one = _long_storm()
    assert len(one["past_track"]) == 60 and one["storm_zone"]["steps"]
    three = [{**one, "id": f"TC26{i:02d}"} for i in range(3)]
    assert _size(three) >= TYPHOON_ATTR_MAX_BYTES  # over the cap only because of the steps
    fitted = fit_storms(three)
    assert _size(fitted) < TYPHOON_ATTR_MAX_BYTES and len(fitted) == 3
    for s in fitted:  # zone steps went, `enters` / `margin_km` and the full past track stayed
        assert "steps" not in s["storm_zone"] and s["storm_zone"]["margin_km"] == one["storm_zone"]["margin_km"]
        assert "enters" in s["storm_zone"] and s["past_track"] == one["past_track"]
    assert three[0]["storm_zone"]["steps"]  # the input is not modified


def test_fit_storms_still_cuts_past_tracks_when_dropping_steps_is_not_enough():
    one = _long_storm()
    five = [{**one, "id": f"TC26{i:02d}"} for i in range(5)]
    fitted = fit_storms(five)
    assert _size(fitted) < TYPHOON_ATTR_MAX_BYTES and len(fitted) == 5
    assert all("steps" not in s["storm_zone"] for s in fitted)
    assert all(len(s["past_track"]) < 60 and s["track"] == one["track"] for s in fitted)
    small = [one]
    assert fit_storms(small) == small and "steps" in small[0]["storm_zone"]  # under the cap: untouched


# ---------- coordinator ----------

EVENT = "event.jma_chiyoda_alerts"


def _register(aioclient_mock, forecast_status: int | None = None, issue: str | None = None) -> None:
    aioclient_mock.get(URL_TYPHOON_TARGET, json=load_json("typhoon_targetTc_20261003.json"))
    if forecast_status is None:
        aioclient_mock.get(URL_TYPHOON_FORECAST.format(tc="TC2633"),
                           json=_with_issue(load_json("typhoon_TC2633_forecast.json"), issue))
    else:
        aioclient_mock.get(URL_TYPHOON_FORECAST.format(tc="TC2633"), status=forecast_status)
    aioclient_mock.get(URL_TYPHOON_SPECS.format(tc="TC2633"), json=load_json("typhoon_TC2633_specs.json"))


def _with_issue(forecast, issue: str | None):
    """The real forecast file; with an `issue` given, ONE edit: the title part's issue time."""
    if issue is None:
        return forecast
    out = clone(forecast)
    out[0]["issue"]["JST"] = issue
    return out


def _coordinator(hass: HomeAssistant, entry, home, fire, memory, save) -> TyphoonCoordinator:
    entry.add_to_hass(hass)
    cfg = JmaConfig(office=ENTRY_DATA["office"], area=ENTRY_DATA["area"], area_name=ENTRY_DATA["area_name"],
                    class10=ENTRY_DATA["class10"], station_primary=ENTRY_DATA["station_primary"],
                    station_secondary=ENTRY_DATA["station_secondary"], latitude=home[0], longitude=home[1])
    return TyphoonCoordinator(hass, entry, JmaApi(async_get_clientsession(hass)), cfg, fire, memory, save)


async def test_coordinator_events_held_until_ready_then_stage_rise_and_save(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    hass = tokyo
    fired: list = []
    saves: list = []
    memory: dict = {}
    freezer.move_to("2026-10-03T13:00:00+00:00")  # 22:00 JST
    _register(aioclient_mock)
    co = _coordinator(hass, entry, AT_SEA, lambda t, d: fired.append((t, d)), memory, lambda: saves.append(1))

    await co.async_refresh()
    storm = co.data.storms[0]
    assert storm["stage"] == "notice" and storm["storm_zone"]["enters"]["time"] == ENTERS_AT_SEA
    assert fired == [] and len(co._pending) == 1  # the event entity does not exist yet
    assert memory["TC2633"]["stage"] == "notice" and memory["TC2633"]["name_en"] == "Choi-wan"
    assert len(saves) == 1
    co.set_alerts_ready()
    assert [t for t, _ in fired] == ["typhoon_approach"] and fired[0][1]["stage"] == "notice"

    await co.async_refresh()  # an unchanged poll sends nothing and does not save
    assert len(fired) == 1 and len(saves) == 1

    # within 24 h of the entry: urgent, announced straight away now the entity exists
    freezer.move_to("2026-10-05T00:00:00+00:00")  # 09:00 JST on the 5th, 23 h before the entry
    await co.async_refresh()
    assert [t for t, _ in fired] == ["typhoon_approach"] * 2 and fired[1][1]["stage"] == "urgent"
    assert fired[1][1]["previous_stage"] == "notice" and len(saves) == 2


async def test_coordinator_clears_a_receding_storm_once_per_issue_and_a_failed_poll_is_not_quiet(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    """Real TC2633 for Tokyo Station (stage none, 656 km) after its closest approach (10-06 21:00),
    memory notice. Each poll serves the real file with only the title's issue time changed."""
    hass = tokyo
    fired: list = []
    memory = {"TC2633": {"stage": "notice", "quiet": 0}}
    freezer.move_to("2026-10-06T15:00:00+00:00")  # 10-07 00:00 JST
    co = _coordinator(hass, entry, TOKYO, lambda t, d: fired.append((t, d)), memory, lambda: None)
    co.set_alerts_ready()
    _register(aioclient_mock, issue="2026-10-06T21:45:00+09:00")
    await co.async_refresh()
    assert co.data.storms[0]["stage"] == "none" and memory["TC2633"]["quiet"] == 1

    await co.async_refresh()  # the same issue again (e.g. an HA restart): not counted
    assert memory["TC2633"]["quiet"] == 1

    aioclient_mock.clear_requests()
    _register(aioclient_mock, forecast_status=500)
    freezer.tick(timedelta(minutes=30))
    await co.async_refresh()  # the forecast fails: last-good is kept and is NOT a quiet poll
    assert memory["TC2633"]["quiet"] == 1 and fired == [] and co.last_update_success is True

    for n, issue in enumerate(("2026-10-06T22:45:00+09:00", "2026-10-06T23:45:00+09:00"), start=2):
        aioclient_mock.clear_requests()
        _register(aioclient_mock, issue=issue)
        freezer.tick(timedelta(minutes=30))
        await co.async_refresh()
        if n == 2:
            assert memory["TC2633"]["quiet"] == 2 and fired == []
    assert [(t, d["reason"], d["name_en"]) for t, d in fired] == [("typhoon_cleared", "passed", "Choi-wan")]
    assert memory == {}


async def test_real_tc2632_forecast_wobble_does_not_send_has_passed(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    """Tokyo Station and the REAL TC2632 capture: closest approach 280 km at
    2026-10-01 12:29 JST (stage notice). Later issues are the real file with TWO edits: the +95 h
    centre moved 0.3 deg south and 0.3 deg east (about 43 km) and a new title issue time. The closest
    approach then is just over 300 km but still in the future: no event, the remembered stage
    stands; three coordinator starts a minute apart on one unchanged issue do not clear it either."""
    home = TOKYO
    fired: list = []
    memory: dict = {}
    real = load_json("typhoon_TC2632_forecast.json")

    def wobble(issue: str):
        out = clone(real)
        out[0]["issue"]["JST"] = issue
        for part in out:
            if part.get("advancedHours") == 95:
                part["center"] = [round(part["center"][0] - 0.3, 2), round(part["center"][1] + 0.3, 2)]
        return out

    def serve(forecast) -> None:
        aioclient_mock.clear_requests()
        aioclient_mock.get(URL_TYPHOON_TARGET, json=load_json("typhoon_targetTc.json"))
        aioclient_mock.get(URL_TYPHOON_FORECAST.format(tc="TC2632"), json=forecast)
        aioclient_mock.get(URL_TYPHOON_SPECS.format(tc="TC2632"), json=load_json("typhoon_TC2632_specs.json"))

    freezer.move_to("2026-09-28T04:00:00+00:00")  # 13:00 JST, 71.4 h before the closest approach
    serve(real)
    co = _coordinator(tokyo, entry, home, lambda t, d: fired.append((t, d)), memory, lambda: None)
    co.set_alerts_ready()
    await co.async_refresh()
    a = co.data.storms[0]
    assert (a["stage"], a["closest"]["distance_km"], a["closest"]["time"]) == (
        "notice", 280, "2026-10-01T12:29:00+09:00")
    assert [t for t, _ in fired] == ["typhoon_approach"] and memory["TC2632"]["stage"] == "notice"

    for n in range(3):  # three newer issues, each still over 300 km and still ahead
        serve(wobble(f"2026-09-28T{14 + n}:45:00+09:00"))
        freezer.tick(timedelta(minutes=30))
        await co.async_refresh()
        b = co.data.storms[0]
        assert b["stage"] == "none" and 300 < b["closest"]["distance_km"] < 320
        assert datetime.fromisoformat(b["closest"]["time"]) > datetime.fromisoformat("2026-09-28T05:00:00+00:00")
    assert [t for t, _ in fired] == ["typhoon_approach"]
    assert memory["TC2632"]["stage"] == "notice" and memory["TC2632"]["quiet"] == 0  # not receding: untouched

    # the closest approach has passed and the issue stays the same: three starts a minute apart
    freezer.move_to("2026-10-02T03:00:00+00:00")  # 10-02 12:00 JST
    serve(wobble("2026-10-02T09:45:00+09:00"))
    for _ in range(3):
        starts = _coordinator(tokyo, entry, home, lambda t, d: fired.append((t, d)), memory, lambda: None)
        starts.set_alerts_ready()
        await starts.async_refresh()
        freezer.tick(timedelta(minutes=1))
    assert [t for t, _ in fired] == ["typhoon_approach"] and memory["TC2632"]["quiet"] == 1


async def test_coordinator_gone_needs_time_two_starts_a_minute_apart_clear_nothing(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    hass = tokyo
    fired: list = []
    memory = {"TC2633": {"stage": "notice", "quiet": 0, "number": "2627", "name_en": "Choi-wan",
                         "name_ja": "name-ja"}}  # as left by a previous run
    freezer.move_to("2026-10-03T13:00:00+00:00")
    aioclient_mock.get(URL_TYPHOON_TARGET, status=404)
    for _ in range(2):  # two Home Assistant starts a minute apart on one empty answer
        co = _coordinator(hass, entry, TOKYO, lambda t, d: fired.append((t, d)), memory, lambda: None)
        co.set_alerts_ready()
        await co.async_refresh()  # the 404 branch is a poll without the storm too
        freezer.tick(timedelta(minutes=1))
    assert co.data.count == 0 and fired == [] and memory["TC2633"]["absent_since"] == "2026-10-03T13:00:00+00:00"

    aioclient_mock.clear_requests()
    aioclient_mock.get(URL_TYPHOON_TARGET, status=500)
    freezer.tick(timedelta(minutes=40))
    await co.async_refresh()  # a poll that FAILS is not a poll without it (and clears nothing)
    assert fired == [] and "TC2633" in memory

    aioclient_mock.clear_requests()
    aioclient_mock.get(URL_TYPHOON_TARGET, json=[])  # an empty list is the same as a 404
    freezer.move_to("2026-10-03T13:31:00+00:00")  # 31 minutes after the first absence
    await co.async_refresh()
    assert [(t, d["reason"], d["number_short"], d["name_ja"], d["previous_stage"]) for t, d in fired] == [
        ("typhoon_cleared", "gone", 27, "name-ja", "notice")]
    assert memory == {}


async def test_present_again_removes_absent_since(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    fired: list = []
    memory = {"TC2633": {"stage": "notice", "quiet": 0, "absent_since": "2026-10-03T21:40:00+09:00"}}
    freezer.move_to("2026-10-03T13:00:00+00:00")
    _register(aioclient_mock)
    co = _coordinator(tokyo, entry, TOKYO, lambda t, d: fired.append((t, d)), memory, lambda: None)
    co.set_alerts_ready()
    await co.async_refresh()
    assert fired == [] and "absent_since" not in memory["TC2633"]


# ---------- a remembered urgent decays, and "later" means later ----------

def _inbound(stage: str, now: datetime, issue: datetime) -> dict:
    """The real TC2633 dict for a home at sea with its closest approach 60 h AFTER `now`."""
    storm = _storm(AT_SEA)
    return {**storm, "stage": stage, "issue": issue.isoformat(),
            "closest": {**storm["closest"], "time": (now + timedelta(hours=60)).isoformat()}}


def test_remembered_urgent_decays_silently_while_inbound_so_a_turn_back_is_announced():
    """A scenario on the real TC2633 dict: urgent announced, the forecast moves away for
    three days (stage none, closest approach still ahead), then the storm turns back into the zone."""
    memory: dict = {}
    t = ApproachTracker(memory)
    start = datetime.fromisoformat("2026-10-05T09:00:00+09:00")
    assert _types(t.update([_inbound("urgent", start, start)], start)) == ["typhoon_approach"]
    assert memory["TC2633"]["stage"] == "urgent"
    stages = []
    for n in range(1, 25):  # 72 h of 3-hourly issues, storm far away, closest approach still ahead
        now = start + timedelta(hours=3 * n)
        t.dirty = False
        assert t.update([_inbound("none", now, now)], now) == []  # never an event while decaying
        stages.append((memory["TC2633"]["stage"], memory["TC2633"]["away"], t.dirty))
    assert stages[0] == ("urgent", 1, True) and stages[1] == ("urgent", 2, True)
    assert stages[2] == ("notice", 0, True)  # the third later issue: silent decay, saved
    assert all(st[0] == "notice" for st in stages[2:])
    back = start + timedelta(hours=75)
    again = t.update([_inbound("urgent", back, back)], back)
    assert _types(again) == ["typhoon_approach"] and again[0][1]["previous_stage"] == "notice"
    assert again[0][1]["stage"] == "urgent" and memory["TC2633"]["stage"] == "urgent"


def test_urgent_that_eases_for_two_issues_and_returns_never_decayed():
    memory: dict = {}
    t = ApproachTracker(memory)
    start = datetime.fromisoformat("2026-10-05T09:00:00+09:00")
    t.update([_inbound("urgent", start, start)], start)
    for n in (1, 2):
        now = start + timedelta(hours=3 * n)
        assert t.update([_inbound("none", now, now)], now) == []
    assert memory["TC2633"]["stage"] == "urgent" and memory["TC2633"]["away"] == 2
    back = start + timedelta(hours=9)
    assert t.update([_inbound("urgent", back, back)], back) == []  # no second urgent: it never decayed
    assert memory["TC2633"]["stage"] == "urgent" and memory["TC2633"]["away"] == 0  # any non-none stage resets


def test_a_remembered_notice_is_left_alone_while_inbound():
    start = datetime.fromisoformat("2026-10-05T09:00:00+09:00")
    memory = {"TC2633": {"stage": "notice", "quiet": 0, "last_issue": start.isoformat(), "away": 0}}
    before = dict(memory["TC2633"])
    t = ApproachTracker(memory)
    for n in range(1, 8):
        now = start + timedelta(hours=3 * n)
        assert t.update([_inbound("none", now, now)], now) == []
    assert memory["TC2633"] == before and not t.dirty


def test_only_a_later_issue_counts_older_copies_and_alternation_do_not():
    """Issues A, B, A (A < B): two counts would be wrong; A again is an older copy and counts nothing."""
    base = datetime.fromisoformat("2026-10-07T00:00:00+09:00")  # after the sea storm's closest
    a, b = (base + timedelta(hours=1)).isoformat(), (base + timedelta(hours=2)).isoformat()
    memory = {"TC2633": {"stage": "notice", "quiet": 0}}
    t = ApproachTracker(memory)
    for issue, expected in ((a, 1), (b, 2), (a, 2), (b, 2)):
        t.update([_staged("none", issue=issue)], base + timedelta(hours=3))
        assert memory["TC2633"]["quiet"] == expected
    assert memory["TC2633"]["quiet"] == 2
    # an unparseable or missing issue never counts; with no last_issue the first parseable one counts
    memory2 = {"TC2633": {"stage": "notice", "quiet": 0, "last_issue": "garbage"}}
    t2 = ApproachTracker(memory2)
    t2.update([_staged("none", issue="not a time")], base + timedelta(hours=3))
    t2.update([_staged("none", issue=None)], base + timedelta(hours=3))
    assert memory2["TC2633"]["quiet"] == 0
    t2.update([_staged("none", issue=a)], base + timedelta(hours=3))
    assert memory2["TC2633"]["quiet"] == 1 and memory2["TC2633"]["last_issue"] == a


async def test_one_failing_storm_does_not_mute_another(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    """TC2632 (real) and TC2633 (real, home at sea) listed together. TC2632's forecast is served
    three ways in turn: a 500; the real file with ONE edit, the title's `name` a string; garbage.
    TC2633 reaches notice, then urgent, and both events arrive; the poll never fails."""
    hass = tokyo
    fired: list = []
    memory: dict = {}
    both = [*load_json("typhoon_targetTc_20261003.json"), *load_json("typhoon_targetTc.json")]
    string_name = clone(load_json("typhoon_TC2632_forecast.json"))
    string_name[0]["name"] = "SURIGAE"

    def serve(tc2632) -> None:
        aioclient_mock.clear_requests()
        aioclient_mock.get(URL_TYPHOON_TARGET, json=both)
        aioclient_mock.get(URL_TYPHOON_FORECAST.format(tc="TC2633"), json=load_json("typhoon_TC2633_forecast.json"))
        aioclient_mock.get(URL_TYPHOON_SPECS.format(tc="TC2633"), json=load_json("typhoon_TC2633_specs.json"))
        if isinstance(tc2632, int):
            aioclient_mock.get(URL_TYPHOON_FORECAST.format(tc="TC2632"), status=tc2632)
        else:
            aioclient_mock.get(URL_TYPHOON_FORECAST.format(tc="TC2632"), json=tc2632)
        aioclient_mock.get(URL_TYPHOON_SPECS.format(tc="TC2632"), json=load_json("typhoon_TC2632_specs.json"))

    freezer.move_to("2026-10-03T13:00:00+00:00")  # TC2633: notice
    serve(500)
    co = _coordinator(hass, entry, AT_SEA, lambda t, d: fired.append((t, d)), memory, lambda: None)
    co.set_alerts_ready()
    await co.async_refresh()
    stages = {s["id"]: s for s in co.data.storms}
    assert co.last_update_success is True and co.data.count == 2
    assert stages["TC2633"]["stage"] == "notice" and stages["TC2632"]["incomplete"] is True
    assert [(t, d["storm_id"], d["stage"]) for t, d in fired] == [("typhoon_approach", "TC2633", "notice")]

    serve(string_name)  # a surprising shape: an AttributeError inside parse_storm
    freezer.move_to("2026-10-05T00:00:00+00:00")  # 23 h before the entry: urgent
    await co.async_refresh()
    assert co.last_update_success is True
    assert [(d["storm_id"], d["stage"]) for _, d in fired] == [("TC2633", "notice"), ("TC2633", "urgent")]

    serve("not a list")
    freezer.tick(timedelta(hours=2))
    await co.async_refresh()
    assert co.last_update_success is True and co.data.count == 2 and len(fired) == 2
    assert "TC2632" not in memory  # never remembered: it was never evaluated


async def test_typhoon_approach_through_config_entry_event_entity_and_saved_memory(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer, hass_storage) -> None:
    hass = tokyo
    hass.config.latitude, hass.config.longitude = AT_SEA  # the entry reads the Home Assistant position at setup
    freezer.move_to("2026-10-03T13:00:00+00:00")  # 22:00 JST on the 3rd
    _register(aioclient_mock)
    mock_heat_alert(aioclient_mock, {(20261003, 17): "alert_20261003_17.csv"})
    mock_jma(aioclient_mock)
    await setup(hass, entry)

    ev = hass.states.get(EVENT)  # the first poll's event was held until the platforms existed
    assert ev.attributes["event_type"] == "typhoon_approach"
    assert (ev.attributes["storm_id"], ev.attributes["stage"], ev.attributes["number_short"]) == ("TC2633", "notice", 27)
    assert ev.attributes["zone_enters_time"] == ENTERS_AT_SEA

    sensor = hass.states.get("sensor.jma_chiyoda_typhoon")
    storm = sensor.attributes["storms"][0]
    assert sensor.state == "1" and storm["stage"] == "notice"
    assert storm["storm_zone"]["enters"]["time"] == ENTERS_AT_SEA and len(storm["storm_zone"]["steps"]) == 6
    assert _size(sensor.attributes["storms"]) < TYPHOON_ATTR_MAX_BYTES

    memory = entry.runtime_data.memory
    assert memory.typhoon["TC2633"]["stage"] == "notice" and memory.typhoon["TC2633"]["quiet"] == 0
    freezer.tick(timedelta(seconds=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass_storage[f"jma.{entry.entry_id}.alert_memory"]["data"]["typhoon"] == memory.typhoon

    # urgent once within 24 h of the entry
    freezer.move_to("2026-10-05T00:00:00+00:00")
    await entry.runtime_data.typhoon.async_refresh()
    await hass.async_block_till_done()
    ev = hass.states.get(EVENT)
    assert ev.attributes["event_type"] == "typhoon_approach" and ev.attributes["stage"] == "urgent"
    assert ev.attributes["previous_stage"] == "notice"
    assert memory.typhoon["TC2633"]["stage"] == "urgent"


def _stored(entry, data: dict) -> dict:
    key = f"jma.{entry.entry_id}.alert_memory"
    return {"version": 1, "minor_version": 1, "key": key, "data": data}


async def test_restart_with_saved_memory_sends_no_typhoon_event_and_empty_memory_sends_one(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer, hass_storage) -> None:
    hass = tokyo
    hass.config.latitude, hass.config.longitude = AT_SEA
    freezer.move_to("2026-10-03T13:00:00+00:00")  # stage notice
    _register(aioclient_mock)
    mock_heat_alert(aioclient_mock, {(20261003, 17): "alert_20261003_17.csv"})
    mock_jma(aioclient_mock)
    key = f"jma.{entry.entry_id}.alert_memory"
    hass_storage[key] = _stored(entry, {"typhoon": {"TC2633": {"stage": "notice", "quiet": 0}}})
    await setup(hass, entry)
    assert hass.states.get(EVENT).state == "unknown"  # already announced before the restart
    assert hass.states.get("sensor.jma_chiyoda_typhoon").attributes["storms"][0]["stage"] == "notice"

    assert await hass.config_entries.async_unload(entry.entry_id)
    hass_storage[key] = _stored(entry, {})  # a fresh install: nothing remembered
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    ev = hass.states.get(EVENT)
    assert ev.attributes["event_type"] == "typhoon_approach" and ev.attributes["storm_id"] == "TC2633"


async def test_typhoon_alert_decided_before_home_assistant_started_is_stored_only_once_delivered(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer, hass_storage) -> None:
    """HA is still starting (EVENT_HOMEASSISTANT_STARTED not fired): the alert is decided in the
    first poll but waits in the event queue, so it is not written to the memory file; if HA stopped
    here the next start would announce it again."""
    hass = tokyo
    hass.config.latitude, hass.config.longitude = AT_SEA
    freezer.move_to("2026-10-03T13:00:00+00:00")
    _register(aioclient_mock)
    mock_heat_alert(aioclient_mock, {(20261003, 17): "alert_20261003_17.csv"})
    mock_jma(aioclient_mock)
    key = f"jma.{entry.entry_id}.alert_memory"
    hass.set_state(CoreState.starting)
    await setup(hass, entry)
    freezer.tick(timedelta(seconds=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(EVENT).state == "unknown"  # nothing delivered yet
    assert key not in hass_storage or hass_storage[key]["data"]["typhoon"] == {}

    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    ev = hass.states.get(EVENT)
    assert ev.attributes["event_type"] == "typhoon_approach" and ev.attributes["stage"] == "notice"
    assert hass_storage[key]["data"]["typhoon"]["TC2633"]["stage"] == "notice"


async def test_garbage_in_the_memory_file_does_not_break_the_typhoon_poll(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer, hass_storage) -> None:
    hass = tokyo
    freezer.move_to("2026-10-03T13:00:00+00:00")
    _register(aioclient_mock)
    mock_heat_alert(aioclient_mock, {(20261003, 17): "alert_20261003_17.csv"})
    mock_jma(aioclient_mock)
    hass_storage[f"jma.{entry.entry_id}.alert_memory"] = _stored(entry, {"typhoon": {
        "TC2633": {"stage": "notice", "quiet": "two"}, "TC2599": {"stage": ["notice"], "quiet": 0}}})
    await setup(hass, entry)
    state = hass.states.get("sensor.jma_chiyoda_typhoon")
    assert state.state == "1" and state.attributes["storms"][0]["stage"] == "none"  # the poll worked
    assert entry.runtime_data.typhoon.last_update_success is True
    # and an entry injected at runtime, after the loader has cleaned the file, is survived too
    mem = entry.runtime_data.memory.typhoon
    mem["TC2633"] = {"stage": "notice", "quiet": "two", "absent": [], "last_issue": 3}
    await entry.runtime_data.typhoon.async_refresh()
    assert entry.runtime_data.typhoon.last_update_success is True


async def test_typhoon_far_home_sends_no_event(tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    hass = tokyo  # Tokyo Station (35.68, 139.77): stage none, margin +295.2 km
    freezer.move_to("2026-10-03T13:00:00+00:00")
    _register(aioclient_mock)
    mock_heat_alert(aioclient_mock, {(20261003, 17): "alert_20261003_17.csv"})
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    assert hass.states.get(EVENT).state == "unknown"
    storm = hass.states.get("sensor.jma_chiyoda_typhoon").attributes["storms"][0]
    assert storm["stage"] == "none" and storm["storm_zone"]["margin_km"] == 295.2
    assert entry.runtime_data.memory.typhoon == {}
