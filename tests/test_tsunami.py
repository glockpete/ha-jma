"""Tsunami (P2P 552): parser, tracker, QuakeClient routing / replay, TsunamiSensor.

Real captures only (tests/fixtures/p2p_552_*.json and p2p_jma_tsunami_history.json, newest first).
The Tokyo Bay Warning / MajorWarning cases are synthetic by necessity: no real bulletin ever gave
東京湾内湾 those grades (see `_variant`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.jma.const import (
    TSUNAMI_AREAS_MAX,
    TSUNAMI_BACKFILL_LIMIT,
    TSUNAMI_COAST_AREA,
    TSUNAMI_MAX_AGE,
    URL_P2P_HISTORY,
    URL_P2P_TSUNAMI,
)
from custom_components.jma.coordinator import signal_alert
from custom_components.jma.errors import JmaFetchError
from custom_components.jma.parse_tsunami import (
    TsunamiTracker,
    parse_552,
    relevance,
    summarise,
)
from custom_components.jma.quake_ws import HISTORY_PARAMS, TSUNAMI_RETRY_S, QuakeClient

from .conftest import QUAKE_PLACE, mock_heat_alert, mock_jma, setup
from .helpers import clone, load_json

JST = timezone(timedelta(hours=9))
REAL_RUN = QuakeClient.run  # taken at import, before conftest's autouse fixture replaces it
HOME = TSUNAMI_COAST_AREA["130010"]
HISTORY = load_json("p2p_jma_tsunami_history.json")  # newest first
GRADE = {"Watch": "watch", "Warning": "warning", "MajorWarning": "major_warning"}


def _just_after(msg: dict, seconds: int = 60) -> datetime:
    return parse_552(msg).issued + timedelta(seconds=seconds)


def _variant(grade: str, bulletin_id: str, issued: str) -> dict:
    """SYNTHETIC: p2p_552_tokyo_bay.json (real, 2025-07-30 20:45, Tokyo Bay Watch) with ONLY the
    東京湾内湾 area's grade changed, plus a fresh id and issue time so it reads as a later bulletin."""
    msg = clone(load_json("p2p_552_tokyo_bay.json"))
    for area in msg["areas"]:
        if area["name"] == HOME:
            area["grade"] = grade
    msg["id"] = bulletin_id
    msg["issue"]["time"] = issued
    return msg


def _types(events: list[dict]) -> list[str]:
    return [e["type"] for e in events]


# ---------------------------------------------------------------- parser

@pytest.mark.parametrize(("name", "areas", "cancelled", "top_rank"), [
    ("majorwarning", 21, False, 3),
    ("warning", 2, False, 2),
    ("watch", 1, False, 1),
    ("cancelled", 0, True, 0),
    ("many_areas_40", 40, False, 2),
    ("tokyo_bay", 39, False, 1),
])
def test_parse_each_real_fixture(name, areas, cancelled, top_rank):
    raw = load_json(f"p2p_552_{name}.json")
    b = parse_552(raw)
    assert b is not None and b.id == raw["id"]
    assert len(b.areas) == areas and b.cancelled is cancelled
    assert max((a.rank for a in b.areas), default=0) == top_rank
    assert b.issued.utcoffset() == timedelta(hours=9)
    assert b.issued.strftime("%Y/%m/%d %H:%M:%S") == raw["issue"]["time"]


def test_parse_area_fields_from_real_messages():
    major = parse_552(load_json("p2p_552_majorwarning.json"))
    noto = major.areas[0]
    assert (noto.name, noto.rank, noto.immediate) == ("石川県能登", 3, True)
    assert (noto.height, noto.height_m, noto.arrival) == ("５ｍ", 5.0, None)
    assert noto.condition == "津波到達中と推測"
    yamagata = major.areas[1]
    assert yamagata.arrival == datetime(2024, 1, 1, 16, 40, tzinfo=JST) and yamagata.condition is None
    warn = parse_552(load_json("p2p_552_warning.json"))
    assert warn.areas[1].condition == "ただちに津波来襲と予測" and warn.areas[1].immediate is True


@pytest.mark.parametrize("bad", [
    None, 5, "x", [], {}, {"code": 551}, {"code": 552}, {"code": 552, "id": ""},
    {"code": 552, "id": "a"}, {"code": 552, "id": "a", "issue": "x"},
    {"code": 552, "id": "a", "issue": {"time": "garbage"}},
    {"code": 552, "id": 7, "issue": {"time": "2025/07/30 09:40:10"}},
    {"code": 552, "id": "a", "issue": {"time": ["2025/07/30 09:40:10"]}},
])
def test_parse_rejects_malformed(bad):
    assert parse_552(bad) is None


def test_parse_skips_bad_areas_and_odd_types_never_raises():
    msg = {"code": 552, "id": "a", "issue": {"time": "2025/07/30 09:40:10"}, "cancelled": "yes",
           "areas": ["x", None, {"grade": "Watch"}, {"name": ""}, {"name": ["l"]},
                     {"name": "A", "grade": ["Warning"], "maxHeight": "3m", "firstHeight": 7,
                      "immediate": "true"},
                     {"name": "B", "grade": {"x": 1}, "maxHeight": {"description": 3, "value": True},
                      "firstHeight": {"arrivalTime": 5, "condition": 9}},
                     {"name": "C", "grade": "Unknown"}, {"name": "D", "grade": "Watch",
                      "maxHeight": {"description": "１ｍ", "value": "1"}}]}
    b = parse_552(msg)
    assert [a.name for a in b.areas] == ["A", "B", "C", "D"]
    assert b.cancelled is False  # only a real True cancels
    assert [a.rank for a in b.areas] == [0, 0, 0, 1]  # unknown grades rank 0 and are kept
    a, bb = b.areas[0], b.areas[1]
    assert (a.height, a.height_m, a.arrival, a.condition, a.immediate) == (None, None, None, None, False)
    assert (bb.height, bb.height_m, bb.arrival, bb.condition) == (None, None, None, None)
    assert b.areas[3].height_m is None  # a string value is not a number


def test_test_true_is_dropped():
    msg = clone(load_json("p2p_552_majorwarning.json"))
    msg["test"] = True
    assert parse_552(msg) is None
    msg["test"] = False
    assert parse_552(msg) is not None


def test_summarise_real_bulletins():
    now = _just_after(load_json("p2p_552_tokyo_bay.json"))
    p = summarise(parse_552(load_json("p2p_552_tokyo_bay.json")), HOME, now)
    assert (p.home, p.max, p.expired) == ("watch", "watch", False)
    assert p.counts == {"major_warning": 0, "warning": 0, "watch": 39}
    assert p.areas_major == [] and p.areas_warning == [] and relevance(p) == "home"
    assert p.home_area_entry.name == HOME
    many = load_json("p2p_552_many_areas_40.json")
    p = summarise(parse_552(many), HOME, _just_after(many))
    assert (p.home, p.max) == ("watch", "warning")
    assert p.counts["warning"] == 7 and len(p.areas_warning) == 7 and relevance(p) == "home"
    # SYNTHETIC: the same real bulletin with every area set to Warning, to exercise the name cap
    all_warn = clone(many)
    for area in all_warn["areas"]:
        area["grade"] = "Warning"
    p = summarise(parse_552(all_warn), HOME, _just_after(many))
    assert p.counts["warning"] == 40 and len(p.areas_warning) == TSUNAMI_AREAS_MAX == 12
    assert p.areas_warning == [a["name"] for a in all_warn["areas"]][:12]  # order as received
    major = load_json("p2p_552_majorwarning.json")
    p = summarise(parse_552(major), HOME, _just_after(major))
    assert (p.home, p.max, relevance(p)) == ("none", "major_warning", "national")
    assert p.counts["major_warning"] >= 1 and p.areas_major[0] == "石川県能登"
    assert summarise(parse_552(major), None, _just_after(major)).home == "none"  # office not mapped
    watch = load_json("p2p_552_watch.json")
    p = summarise(parse_552(watch), HOME, _just_after(watch))
    assert (p.home, p.max, relevance(p)) == ("none", "watch", "none")  # watch elsewhere: not announced


def test_summarise_cancelled_and_expired():
    cancelled = load_json("p2p_552_cancelled.json")
    p = summarise(parse_552(cancelled), HOME, _just_after(cancelled))
    assert (p.home, p.max, p.expired, p.counts) == (
        "none", "none", False, {"major_warning": 0, "warning": 0, "watch": 0})
    major = load_json("p2p_552_majorwarning.json")
    issued = parse_552(major).issued
    inside = summarise(parse_552(major), HOME, issued + TSUNAMI_MAX_AGE)
    assert inside.max == "major_warning" and inside.expired is False
    p = summarise(parse_552(major), HOME, issued + TSUNAMI_MAX_AGE + timedelta(seconds=1))
    assert (p.home, p.max, p.expired, p.counts["major_warning"]) == ("none", "none", True, 0)


# ---------------------------------------------------------------- tracker

def test_worked_example_2025_07_30_episode():
    tracker = TsunamiTracker(HOME, {})
    seq = []
    for idx in (21, 20, 19, 18, 17, 16):
        msg = HISTORY[idx]
        events = tracker.feed(msg, _just_after(msg))
        seq.append((idx, _types(events)))
        json.dumps(events)  # every payload is JSON-serialisable
        if idx == 20:
            e = events[0]
            assert (e["home_grade"], e["home_grade_ja"], e["max_grade"]) == ("watch", "津波注意報", "warning")
            assert (e["previous_home_grade"], e["previous_max_grade"]) == ("none", "none")
            assert (e["home_area"], e["home_height"], e["home_height_m"]) == (HOME, "１ｍ", 1.0)
            assert e["home_arrival"] == "2025-07-30T11:30:00+09:00" and e["issued"].endswith("+09:00")
            assert e["home_condition"] is None and e["home_immediate"] is False
            assert e["count_watch"] + e["count_warning"] + e["count_major"] == 40
            assert e["count_major"] == 0 and 0 < len(e["areas_warning"]) <= 12
            assert e["backfill"] is False and e["id"] == msg["id"]
        if idx == 17:
            e = events[0]
            # bulletin 18 (max fell to watch) updated memory silently, so "previous" is watch
            assert (e["previous_home_grade"], e["previous_max_grade"]) == ("watch", "watch")
            assert (e["cancelled"], e["expired"], e["backfill"]) == (False, False, False)
            assert e["home_area"] == HOME and e["id"] == msg["id"]
    assert seq == [(21, []), (20, ["tsunami"]), (19, []), (18, []),
                   (17, ["tsunami_cancelled"]), (16, [])]
    assert tracker.memory["active"] is False and tracker.dirty


def test_tsunami_condition_text_in_event():
    """p2p_552_majorwarning.json's 能登 'arriving now'; a home condition goes through the mapping."""
    msg = _variant("Warning", "x1", "2025/07/30 21:00:00")
    for area in msg["areas"]:
        if area["name"] == HOME:
            area["firstHeight"] = {"condition": "ただちに津波来襲と予測"}
            area["immediate"] = True
    [e] = TsunamiTracker(HOME, {}).feed(msg, _just_after(msg))
    assert e["home_condition"] == "expected immediately" and e["home_immediate"] is True
    assert e["home_arrival"] is None


def test_majorwarning_alone_is_one_national_tsunami_event():
    msg = load_json("p2p_552_majorwarning.json")
    tracker = TsunamiTracker(HOME, {})
    events = tracker.feed(msg, _just_after(msg))
    assert _types(events) == ["tsunami"]
    e = events[0]
    assert (e["home_grade"], e["max_grade"], e["home_grade_ja"]) == ("none", "major_warning", None)
    assert e["home_height"] is None and e["home_immediate"] is None and e["home_arrival"] is None
    assert e["areas_major"][0] == "石川県能登" and e["count_major"] >= 1
    assert tracker.memory == {"active": True, "home": "none", "max": "major_warning",
                              "id": msg["id"], "issued": "2024-01-01T16:22:32+09:00"}
    assert tracker.dirty


@pytest.mark.parametrize(("grade", "home", "mx"), [
    ("Warning", "warning", "warning"), ("MajorWarning", "major_warning", "major_warning"),
])
def test_tokyo_bay_warning_and_major_warning(grade, home, mx):
    """SYNTHETIC (see _variant): only the 東京湾内湾 grade differs from the real capture."""
    msg = _variant(grade, "syn", "2025/07/30 20:45:06")
    [e] = TsunamiTracker(HOME, {}).feed(msg, _just_after(msg))
    assert (e["type"], e["home_grade"], e["max_grade"]) == ("tsunami", home, mx)
    assert e["home_grade_ja"] == {"warning": "津波警報", "major_warning": "大津波警報"}[home]


def test_home_grade_raise_and_lower_each_send_an_event():
    tracker = TsunamiTracker(HOME, {})
    watch = load_json("p2p_552_tokyo_bay.json")
    assert _types(tracker.feed(watch, _just_after(watch))) == ["tsunami"]
    up = _variant("Warning", "up", "2025/07/30 21:00:00")
    [e] = tracker.feed(up, _just_after(up))
    assert (e["home_grade"], e["previous_home_grade"], e["previous_max_grade"]) == ("warning", "watch", "watch")
    up2 = _variant("MajorWarning", "up2", "2025/07/30 21:30:00")
    [e] = tracker.feed(up2, _just_after(up2))
    assert (e["home_grade"], e["previous_home_grade"]) == ("major_warning", "warning")
    same = _variant("MajorWarning", "same", "2025/07/30 21:40:00")
    assert tracker.feed(same, _just_after(same)) == []  # nothing changed
    down = _variant("Watch", "down", "2025/07/30 22:00:00")
    [e] = tracker.feed(down, _just_after(down))
    assert (e["home_grade"], e["previous_home_grade"], e["max_grade"]) == ("watch", "major_warning", "watch")
    assert tracker.memory["home"] == "watch" and tracker.memory["id"] == "down"


def test_national_max_rising_while_home_unchanged_sends_event_but_falling_is_silent():
    tracker = TsunamiTracker(HOME, {})
    watch = load_json("p2p_552_tokyo_bay.json")
    tracker.feed(watch, _just_after(watch))
    many = load_json("p2p_552_many_areas_40.json")  # 18:30 Warning x?, Tokyo Bay Watch (earlier!)
    assert tracker.feed(many, _just_after(many)) == []  # older than current: ignored
    first = HISTORY[20]  # 09:40 Warning, home watch: the max rises from watch to warning
    fresh = TsunamiTracker(HOME, {"active": True, "home": "watch", "max": "watch",
                                  "id": "old", "issued": "2025-07-30T08:00:00+09:00"})
    [e] = fresh.feed(first, _just_after(first))
    assert (e["home_grade"], e["max_grade"], e["previous_max_grade"]) == ("watch", "warning", "watch")


def test_class_change_home_to_national_sends_event():
    """Home watch falls away but a warning is still in force elsewhere: relevance home -> national."""
    msg = _variant("Watch", "home", "2025/07/30 21:00:00")
    tracker = TsunamiTracker(HOME, {})
    assert _types(tracker.feed(msg, _just_after(msg))) == ["tsunami"]
    gone = clone(HISTORY[20])  # Warning x?, but without 東京湾内湾
    gone["areas"] = [a for a in gone["areas"] if a["name"] != HOME]
    gone["id"], gone["issue"]["time"] = "gone", "2025/07/30 22:00:00"
    [e] = tracker.feed(gone, _just_after(gone))
    assert (e["type"], e["home_grade"], e["max_grade"], e["previous_home_grade"]) == (
        "tsunami", "none", "warning", "watch")


def test_replay_after_restart_with_same_memory_sends_nothing():
    msgs = [HISTORY[i] for i in (21, 20, 19)]
    first = TsunamiTracker(HOME, {})
    for m in msgs:
        first.feed(m, _just_after(m))
    saved = json.loads(json.dumps(first.memory))  # what AlertMemory would have stored
    restarted = TsunamiTracker(HOME, saved)
    now = _just_after(msgs[-1], 3600)
    assert restarted.replay(list(reversed(msgs)), now) == []  # history arrives newest first
    assert restarted.dirty is False and restarted.replayed is True
    assert restarted.current.id == HISTORY[19]["id"]


def test_replay_empty_memory_mid_episode_sends_one_event_with_backfill_by_age():
    msgs = [HISTORY[i] for i in (18, 19, 20, 21)]  # newest first, as the endpoint serves them
    newest = parse_552(HISTORY[18]).issued
    live = TsunamiTracker(HOME, {})
    events = live.replay(msgs, newest + timedelta(seconds=30))
    assert _types(events) == ["tsunami"] and events[0]["backfill"] is False
    assert (events[0]["home_grade"], events[0]["max_grade"]) == ("watch", "watch")
    assert events[0]["id"] == HISTORY[18]["id"]
    stale = TsunamiTracker(HOME, {})
    events = stale.replay(msgs, newest + timedelta(hours=3))
    assert _types(events) == ["tsunami"] and events[0]["backfill"] is True
    # any order gives the same single event
    shuffled = TsunamiTracker(HOME, {}).replay([msgs[2], msgs[0], msgs[3], msgs[1]], newest + timedelta(hours=3))
    assert [e["id"] for e in shuffled] == [HISTORY[18]["id"]]


def test_replay_empty_list_marks_replayed_and_does_nothing():
    tracker = TsunamiTracker(HOME, {"active": True, "home": "watch", "max": "watch"})
    assert tracker.replay([], datetime(2026, 10, 3, tzinfo=JST)) == []
    assert tracker.replayed is True and tracker.picture(datetime(2026, 10, 3, tzinfo=JST)) is None
    assert tracker.replay("not a list", datetime(2026, 10, 3, tzinfo=JST)) == []


def test_replay_never_moves_current_backwards():
    tracker = TsunamiTracker(HOME, {})
    newest = HISTORY[18]
    tracker.feed(newest, _just_after(newest))
    tracker.replay([HISTORY[20], HISTORY[21]], _just_after(newest, 120))
    assert tracker.current.id == newest["id"]


def test_expiry_after_48_hours_cancels_with_expired_flag():
    msg = load_json("p2p_552_majorwarning.json")
    tracker = TsunamiTracker(HOME, {})
    assert _types(tracker.feed(msg, _just_after(msg))) == ["tsunami"]
    later = parse_552(msg).issued + TSUNAMI_MAX_AGE + timedelta(minutes=1)
    [e] = tracker.replay([msg], later)  # next reconnect, nothing newer, nothing cancelled
    assert (e["type"], e["expired"], e["cancelled"], e["previous_max_grade"]) == (
        "tsunami_cancelled", True, False, "major_warning")
    assert tracker.memory["active"] is False
    assert tracker.picture(later).expired is True


def test_a_bulletin_already_older_than_48h_announces_nothing():
    msg = load_json("p2p_552_majorwarning.json")
    tracker = TsunamiTracker(HOME, {})
    assert tracker.feed(msg, parse_552(msg).issued + timedelta(days=3)) == []
    assert tracker.memory == {} or tracker.memory.get("active") is False


def test_cancelled_bulletin_after_active_episode():
    major = load_json("p2p_552_majorwarning.json")
    tracker = TsunamiTracker(HOME, {})
    tracker.feed(major, _just_after(major))
    cancel = clone(load_json("p2p_552_cancelled.json"))
    cancel["issue"]["time"] = "2024/01/02 10:00:00"  # the real capture is from 2026; keep it in order
    [e] = tracker.feed(cancel, _just_after(cancel))
    assert (e["type"], e["cancelled"], e["expired"], e["id"]) == ("tsunami_cancelled", True, False, cancel["id"])
    assert (e["previous_home_grade"], e["previous_max_grade"]) == ("none", "major_warning")
    assert e["home_area"] == HOME
    json.dumps(e)
    assert tracker.memory["active"] is False
    assert tracker.picture(_just_after(cancel)).max == "none"


def test_cancelled_bulletin_with_nothing_active_is_silent():
    cancel = load_json("p2p_552_cancelled.json")
    tracker = TsunamiTracker(HOME, {})
    assert tracker.feed(cancel, _just_after(cancel)) == [] and tracker.dirty is False
    assert tracker.current.cancelled is True


def test_already_seen_id_and_older_bulletin_are_ignored():
    tracker = TsunamiTracker(HOME, {})
    a, older = HISTORY[19], HISTORY[20]
    assert _types(tracker.feed(a, _just_after(a))) == ["tsunami"]
    assert tracker.feed(a, _just_after(a)) == []  # same id again
    assert tracker.feed(older, _just_after(a)) == []  # issued earlier than current
    assert tracker.current.id == a["id"]
    assert tracker.feed(older, _just_after(a)) == []  # and it stays seen


def test_dirty_only_when_memory_changes():
    tracker = TsunamiTracker(HOME, {})
    a, b = HISTORY[20], HISTORY[19]
    tracker.feed(a, _just_after(a))
    assert tracker.dirty is True
    tracker.dirty = False
    tracker.feed(b, _just_after(b))  # silent update: id and issued changed
    assert tracker.dirty is True and tracker.memory["id"] == b["id"]
    tracker.dirty = False
    tracker.replay([a, b], _just_after(b))
    assert tracker.dirty is False


@pytest.mark.parametrize("bad", [
    None, 1, "x", [], {"code": 552}, {"code": 552, "id": "a", "issue": {"time": "2025/07/30 09:40:10"},
                                       "areas": "nope"},
    {"code": 552, "id": "b", "issue": {"time": "2025/07/30 09:41:10"},
     "areas": [{"name": "x", "grade": ["Warning"], "maxHeight": []}, 7]},
])
def test_malformed_messages_never_raise(bad):
    tracker = TsunamiTracker(HOME, {})
    now = datetime(2025, 7, 30, 10, 0, tzinfo=JST)
    assert tracker.feed(bad, now) == []
    assert tracker.replay([bad, bad], now) == []


def test_a_bulletin_with_no_readable_area_cannot_lift_a_warning():
    """Only a cancellation has no areas. A later message that is not a cancellation and whose
    areas are missing or unreadable is not a bulletin: the Tokyo Bay warning stays as it was."""
    memory: dict = {}
    tracker = TsunamiTracker(HOME, memory)
    warning = _variant("Warning", "w1", "2025/07/30 21:00:00")
    assert _types(tracker.feed(warning, _just_after(warning))) == ["tsunami"]
    for i, areas in enumerate(([], None, "nope", [7, {"grade": "Warning"}])):
        broken = {"code": 552, "id": f"broken{i}", "cancelled": False, "areas": areas,
                  "issue": {"time": f"2025/07/30 21:0{i + 1}:00"}}
        assert parse_552(broken) is None
        assert tracker.feed(broken, datetime(2025, 7, 30, 21, 10, tzinfo=JST)) == []
    assert memory["active"] is True and memory["home"] == "warning"
    assert tracker.picture(datetime(2025, 7, 30, 21, 10, tzinfo=JST)).home == "warning"


# ---------------------------------------------------------------- QuakeClient

class _Rec:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.quakes = 0
        self.saves = 0

    def fire(self, t: str, d: dict) -> None:
        self.events.append((t, d))

    def on_quake(self) -> None:
        self.quakes += 1

    def save(self) -> None:
        self.saves += 1


def _client(api=None, memory=None, area=HOME, hass=None) -> tuple[QuakeClient, _Rec]:
    rec = _Rec()
    qc = QuakeClient(hass=hass, api=api, fire=rec.fire, on_quake=rec.on_quake,  # type: ignore[arg-type]
                     place=QUAKE_PLACE, memory=memory if memory is not None else {}, save=rec.save, tsunami_area=area)
    return qc, rec


def test_client_routes_552_fires_events_and_saves_when_dirty():
    qc, rec = _client()
    assert not hasattr(qc, "last_tsunami")
    msg = load_json("p2p_552_majorwarning.json")
    qc.handle(msg, _just_after(msg))
    assert [t for t, _ in rec.events] == ["tsunami"]
    data = rec.events[0][1]
    assert "type" not in data and data["max_grade"] == "major_warning"
    assert rec.saves == 1 and rec.quakes == 1
    assert qc.tsunami.current.id == msg["id"]
    qc.handle(msg, _just_after(msg))  # repeat: nothing fired, nothing saved, sensors still refreshed
    assert len(rec.events) == 1 and rec.saves == 1 and rec.quakes == 2
    qc._safe_handle({"code": 552, "id": "z", "issue": {"time": "2023/01/01 00:00:00"},  # older than current: ignored
                     "areas": [{"name": ["bad"], "grade": []}, {"name": "n", "grade": {}}]},
                    _just_after(msg), backfill=False)  # a malformed shape does not escape
    assert qc.tsunami.memory["active"] is True


def test_client_memory_dict_is_the_trackers_memory():
    memory = {"active": True, "home": "none", "max": "major_warning", "id": "i", "issued": "x"}
    qc, _rec = _client(memory=memory)
    assert qc.tsunami.memory is memory and qc.tsunami.home_area == HOME


def test_client_drops_test_true_for_551_and_552():
    qc, rec = _client()
    major = clone(load_json("p2p_552_majorwarning.json"))
    now = _just_after(major)
    major["test"] = True
    qc.handle(major, now)
    quake = {"code": 551, "id": "q", "test": True, "issue": {"type": "DetailScale"},
             "earthquake": {"time": "2026/09/26 12:58:00", "maxScale": 40, "domesticTsunami": "None",
                            "hypocenter": {"name": "東京湾", "magnitude": 4.5, "depth": 30}},
             "points": [{"pref": "東京都", "addr": "千代田区千代田", "isArea": False, "scale": 40}]}
    qc.handle(quake, datetime(2026, 9, 26, 13, 0, tzinfo=JST))
    assert rec.events == [] and qc.last_quake is None and qc.tsunami.current is None


def _api(history=None, tsunami=None):
    """api.get_json mock: a value is returned, an exception instance is raised."""
    async def get_json(url, params=None):
        got = history if url == URL_P2P_HISTORY else tsunami if url == URL_P2P_TSUNAMI else None
        if isinstance(got, Exception):
            raise got
        return got
    return NS(get_json=AsyncMock(side_effect=get_json))


def _quake_msg(qid: str, when: str, tokyo: int) -> dict:
    return {"code": 551, "id": qid, "issue": {"type": "DetailScale"},
            "earthquake": {"time": when, "maxScale": tokyo, "domesticTsunami": "None",
                           "hypocenter": {"name": "東京湾", "magnitude": 4.5, "depth": 30}},
            "points": [{"pref": "東京都", "addr": "千代田区千代田", "isArea": False, "scale": tokyo}]}


async def test_backfill_survives_a_tsunami_endpoint_failure(hass, freezer):
    freezer.move_to("2026-09-26T04:00:00+00:00")  # 13:00 JST
    api = _api(history=[_quake_msg("q1", "2026/09/26 12:58:00", 30)], tsunami=JmaFetchError("down"))
    qc, rec = _client(api=api, hass=hass)
    await _one_connection(qc)
    assert [t for t, _ in rec.events] == ["earthquake"]  # the quake replay was not lost
    assert qc.tsunami.replayed is False  # unknown, not "none", while the endpoint is down
    urls = [c.args[0] for c in api.get_json.await_args_list]
    assert urls == [URL_P2P_HISTORY, URL_P2P_TSUNAMI]
    assert api.get_json.await_args_list[0].args[1] == HISTORY_PARAMS
    assert ("codes", "552") not in HISTORY_PARAMS
    assert api.get_json.await_args_list[1].args[1] == [("limit", str(TSUNAMI_BACKFILL_LIMIT))]


async def test_backfill_survives_a_history_failure(hass, freezer):
    msg = load_json("p2p_552_majorwarning.json")
    freezer.move_to(_just_after(msg).astimezone(UTC))
    api = _api(history=JmaFetchError("down"), tsunami=[msg])
    qc, rec = _client(api=api, hass=hass)
    await _one_connection(qc)
    assert [t for t, _ in rec.events] == ["tsunami"] and rec.events[0][1]["backfill"] is False
    assert qc.tsunami.replayed is True and rec.saves == 1 and rec.quakes >= 1


async def test_backfill_empty_tsunami_list_marks_replayed_and_refreshes_sensors(freezer):
    freezer.move_to("2026-10-03T00:00:00+00:00")
    qc, rec = _client(api=_api(history=[], tsunami=[]))
    await qc._backfill_quakes()  # the units run() uses; run() itself would add its own connected/disconnected refreshes
    assert await qc._replay_tsunami_list() is True
    assert rec.events == [] and qc.tsunami.replayed is True and rec.quakes == 1


async def test_backfill_non_list_tsunami_answer_is_not_a_replay(hass, freezer):
    freezer.move_to("2026-10-03T00:00:00+00:00")
    qc, rec = _client(api=_api(history=[], tsunami={"error": "x"}), hass=hass)
    await _one_connection(qc)
    assert rec.events == [] and qc.tsunami.replayed is False  # a dict is not an answer


async def test_backfill_both_endpoints_failing_raises_nothing(hass, freezer):
    freezer.move_to("2026-10-03T00:00:00+00:00")
    qc, rec = _client(api=_api(history=JmaFetchError("a"), tsunami=JmaFetchError("b")), hass=hass)
    await _one_connection(qc)
    assert rec.events == [] and qc.tsunami.replayed is False
    # run() would swallow an exception and reconnect, so also call the two real units: neither may raise.
    await qc._backfill_quakes()
    assert await qc._replay_tsunami_list() is False
    assert rec.events == [] and qc.tsunami.replayed is False


# ---------------------------------------------------------------- TsunamiSensor

SENSOR = "sensor.jma_chiyoda_tsunami"


async def test_sensor_unknown_none_watch_warning(tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    qc = entry.runtime_data.quake
    assert qc.tsunami.home_area == HOME
    state = hass.states.get(SENSOR)
    assert state.state == "unknown"  # no replay has run yet
    assert state.attributes["device_class"] == "enum"
    assert state.attributes["options"] == ["none", "watch", "warning", "major_warning"]

    api = entry.runtime_data.api
    api.get_json = AsyncMock(side_effect=_api(history=[], tsunami=[]).get_json.side_effect)
    await qc._backfill_quakes()
    assert await qc._replay_tsunami_list() is True  # an empty list is still a successful replay
    await hass.async_block_till_done()
    assert hass.states.get(SENSOR).state == "none"

    watch = load_json("p2p_552_tokyo_bay.json")  # real: Tokyo Bay Watch, 2025-07-30 20:45 JST
    freezer.move_to(_just_after(watch).astimezone(UTC))
    qc.handle(watch, dt_util_now())
    await hass.async_block_till_done()
    state = hass.states.get(SENSOR)
    assert state.state == "watch"
    a = state.attributes
    assert (a["home_area"], a["max_grade"], a["id"], a["cancelled"]) == (HOME, "watch", watch["id"], False)
    assert a["counts"] == {"major_warning": 0, "warning": 0, "watch": 39}
    assert a["issued"] == "2025-07-30T20:45:06+09:00" and a["home_height"] == "１ｍ"
    assert a["home_height_m"] == 1.0 and a["home_condition"] == "first wave observed"
    assert a["home_arrival"] is None and a["home_immediate"] is False
    assert a["areas_major"] == [] and a["areas_warning"] == []

    warn = _variant("Warning", "syn-warn", "2025/07/30 21:00:00")  # SYNTHETIC, see _variant
    freezer.move_to(_just_after(warn).astimezone(UTC))
    qc.handle(warn, dt_util_now())
    await hass.async_block_till_done()
    state = hass.states.get(SENSOR)
    assert state.state == "warning" and state.attributes["max_grade"] == "warning"
    assert state.attributes["areas_warning"] == [HOME] or HOME in state.attributes["areas_warning"]

    cancel = clone(load_json("p2p_552_cancelled.json"))
    cancel["issue"]["time"] = "2025/07/30 22:00:00"
    freezer.move_to(_just_after(cancel).astimezone(UTC))
    qc.handle(cancel, dt_util_now())
    await hass.async_block_till_done()
    state = hass.states.get(SENSOR)
    assert state.state == "none" and state.attributes["cancelled"] is True

    # 48 h after the last bulletin the sensor reads none even if nothing else arrived
    freezer.move_to(_just_after(warn, 3 * 86400).astimezone(UTC))
    qc._on_quake()
    await hass.async_block_till_done()
    assert hass.states.get(SENSOR).state == "none"


def dt_util_now() -> datetime:
    from homeassistant.util import dt as dt_util
    return dt_util.now()


async def test_sensor_shows_national_warning_as_none_for_home(tokyo, aioclient_mock, entry, freezer) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    qc = entry.runtime_data.quake
    msg = load_json("p2p_552_majorwarning.json")
    freezer.move_to(_just_after(msg).astimezone(UTC))
    qc.handle(msg, dt_util_now())
    await hass.async_block_till_done()
    state = hass.states.get(SENSOR)
    assert state.state == "none" and state.attributes["max_grade"] == "major_warning"
    assert state.attributes["areas_major"][0] == "石川県能登"


async def test_event_entity_receives_a_json_serialisable_tsunami_payload(tokyo, aioclient_mock, entry, freezer) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    msg = load_json("p2p_552_tokyo_bay.json")
    freezer.move_to(_just_after(msg).astimezone(UTC))
    entry.runtime_data.quake.handle(msg, dt_util_now())
    await hass.async_block_till_done()
    state = hass.states.get("event.jma_chiyoda_alerts")
    assert state.attributes["event_type"] == "tsunami"
    assert state.attributes["home_grade"] == "watch"
    json.dumps(dict(state.attributes))


async def test_diagnostics_carry_a_tsunami_block(tokyo, aioclient_mock, entry, freezer) -> None:
    from custom_components.jma.diagnostics import async_get_config_entry_diagnostics
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    msg = load_json("p2p_552_majorwarning.json")
    freezer.move_to(_just_after(msg).astimezone(UTC))
    entry.runtime_data.quake.handle(msg, dt_util_now())
    diag = await async_get_config_entry_diagnostics(hass, entry)
    t = diag["tsunami"]
    assert t["current_id"] == msg["id"] and t["issued"] == "2024-01-01T16:22:32+09:00"
    assert t["memory"]["max"] == "major_warning"
    # the coast area is set for this place, and the download hides it (it is the class10 code in words)
    assert entry.runtime_data.quake.tsunami.home_area == HOME and t["home_area"] == "**REDACTED**"
    assert HOME not in json.dumps(diag, ensure_ascii=False)
    json.dumps(diag["tsunami"])


# ---------------------------------------------------------------- bulletin and clock edge cases

def test_replay_with_a_non_list_answer_is_not_a_replay():
    """an error object / None is not an answer; only a list (even empty) is."""
    tracker = TsunamiTracker(HOME, {})
    now = datetime(2026, 10, 3, tzinfo=JST)
    for bad in ({"error": "x"}, None, "x", 5):
        assert tracker.replay(bad, now) == [] and tracker.replayed is False
    assert tracker.replay([], now) == [] and tracker.replayed is True


def _warning_bulletin() -> dict:
    return _variant("Warning", "warn-1", "2025/07/30 20:45:06")


def test_future_dated_bulletin_does_not_block_the_real_ones():
    """a wrong year in issue.time (2035) must not become `current`."""
    tracker = TsunamiTracker(HOME, {})
    bad = clone(load_json("p2p_552_watch.json"))
    bad["id"], bad["issue"]["time"] = "bad-year", "2035/07/30 20:00:00"
    now = datetime(2025, 7, 30, 20, 50, tzinfo=JST)
    assert tracker.feed(bad, now) == [] and tracker.current is None
    [e] = tracker.feed(_warning_bulletin(), now)
    assert (e["type"], e["home_grade"]) == ("tsunami", "warning")
    assert tracker.current.id == "warn-1"
    assert tracker.feed(bad, now) == []  # still future-dated (and not remembered as seen, R2-4)
    # within the tolerance a slightly early clock is still a bulletin
    near = _variant("Warning", "near", "2025/07/30 21:00:00")
    assert tracker.feed(near, now) == [] and tracker.current.id == "near"  # exactly 10 min ahead is allowed


def test_future_dated_bulletin_in_a_replay_is_dropped():
    tracker = TsunamiTracker(HOME, {})
    bad = clone(load_json("p2p_552_majorwarning.json"))
    bad["id"], bad["issue"]["time"] = "bad-year", "2035/07/30 20:00:00"
    real = _warning_bulletin()
    now = datetime(2025, 7, 30, 20, 50, tzinfo=JST)
    events = tracker.replay([bad, real], now)
    assert [e["id"] for e in events] == ["warn-1"] and tracker.current.id == "warn-1"
    assert tracker.feed(bad, now) == []


def test_old_episode_in_memory_does_not_hide_a_new_one_with_the_same_grades():
    """memory left by the 2024-04-03 episode (history index 33, a Warning elsewhere) whose
    end was never seen; the real 2025-12-08 bulletin (index 12, again a Warning elsewhere) is
    announced 20 months later instead of being swallowed."""
    old, new = HISTORY[33], HISTORY[12]
    first = TsunamiTracker(HOME, {})
    assert _types(first.feed(old, _just_after(old))) == ["tsunami"]
    saved = json.loads(json.dumps(first.memory))
    later = TsunamiTracker(HOME, saved)
    events = later.feed(new, _just_after(new))
    assert _types(events) == ["tsunami"] and events[0]["previous_max_grade"] == "none"
    assert later.memory["id"] == new["id"] and later.memory["active"] is True


def test_stale_memory_gives_no_lifted_message_years_late():
    """memory active from the real 2024-01-01 major warning, next start on 2026-10-03 with
    the unrelated 2026-07-28 cancellation newest in the list: nothing is sent, memory is cleared."""
    tracker = TsunamiTracker(HOME, {"active": True, "home": "none", "max": "major_warning",
                                    "id": "old", "issued": "2024-01-01T16:22:32+09:00"})
    assert tracker.replay(HISTORY[:10], datetime(2026, 10, 3, 22, 0, tzinfo=JST)) == []
    assert tracker.memory["active"] is False and tracker.dirty is True


@pytest.mark.parametrize("issued", [None, "garbage", 5, ""])
def test_unreadable_memory_issued_counts_as_inactive(issued):
    msg = _warning_bulletin()
    tracker = TsunamiTracker(HOME, {"active": True, "home": "warning", "max": "warning",
                                    "id": "x", "issued": issued})
    assert _types(tracker.feed(msg, _just_after(msg))) == ["tsunami"]
    quiet = TsunamiTracker(HOME, {"active": True, "home": "warning", "max": "warning",
                                  "id": "x", "issued": issued})
    cancelled = load_json("p2p_552_cancelled.json")
    assert quiet.feed(cancelled, _just_after(cancelled)) == [] and quiet.memory["active"] is False


def test_tick_expires_a_bulletin_once_with_the_socket_silent():
    """a Tokyo Bay Warning, JMA says nothing more; 48 h later the tick lifts it, once."""
    msg = _warning_bulletin()
    issued = parse_552(msg).issued
    tracker = TsunamiTracker(HOME, {})
    assert TsunamiTracker(HOME, {}).tick(issued) == []  # no current bulletin
    assert _types(tracker.feed(msg, issued + timedelta(minutes=1))) == ["tsunami"]
    assert tracker.tick(issued + timedelta(hours=47)) == []
    [e] = tracker.tick(issued + TSUNAMI_MAX_AGE + timedelta(minutes=1))
    assert (e["type"], e["expired"], e["cancelled"], e["backfill"]) == ("tsunami_cancelled", True, False, False)
    assert (e["previous_home_grade"], e["previous_max_grade"], e["id"]) == ("warning", "warning", "warn-1")
    assert tracker.dirty and tracker.memory["active"] is False
    assert tracker.tick(issued + timedelta(days=3)) == []
    assert tracker.tick(issued + timedelta(days=30)) == []


def test_a_live_bulletin_refused_as_future_is_taken_up_by_the_tick_when_the_clock_catches_up():
    """host clock 15 min slow. A Tokyo Bay Warning arrives on the socket, is refused, and P2P
    never sends it again; the list already answered on this connection. The 10-minute tick must
    take it up once the clock has reached it, exactly once."""
    msg = _warning_bulletin()
    issued = parse_552(msg).issued
    tracker = TsunamiTracker(HOME, {})
    assert tracker.replay([], issued - timedelta(minutes=30)) == []  # the list answered: no retry
    slow = issued - timedelta(minutes=15)
    assert tracker.feed(msg, slow) == [] and tracker.current is None
    assert tracker.tick(slow + timedelta(minutes=2)) == []  # still 13 min ahead
    [e] = tracker.tick(slow + timedelta(minutes=10))  # now 5 min ahead: inside the tolerance
    assert (e["type"], e["home_grade"], e["backfill"]) == ("tsunami", "warning", False)
    assert tracker.current is not None and tracker.memory["active"] is True
    assert tracker.tick(slow + timedelta(minutes=20)) == []  # once
    assert tracker.feed(msg, slow + timedelta(minutes=21)) == []  # and it is now seen
    # a replay that accepts the bulletin first leaves nothing for the tick to repeat
    other = TsunamiTracker(HOME, {})
    assert other.feed(msg, slow) == []
    assert _types(other.replay([msg], issued + timedelta(minutes=1))) == ["tsunami"]
    assert other.tick(issued + timedelta(minutes=11)) == []


def test_client_tsunami_tick_fires_saves_refreshes_and_never_raises():
    qc, rec = _client()
    msg = _warning_bulletin()
    issued = parse_552(msg).issued
    qc.handle(msg, issued + timedelta(minutes=1))
    saves, quakes = rec.saves, rec.quakes
    qc.tsunami_tick(issued + timedelta(hours=1))
    assert len(rec.events) == 1 and rec.quakes == quakes  # nothing changed: the sensors are not written
    qc.tsunami_tick(issued + TSUNAMI_MAX_AGE + timedelta(minutes=5))
    assert [t for t, _ in rec.events] == ["tsunami", "tsunami_cancelled"] and rec.quakes == quakes + 1
    assert rec.events[1][1]["expired"] is True and "type" not in rec.events[1][1]
    assert rec.saves == saves + 1
    qc.tsunami.tick = lambda now: 1 / 0  # type: ignore[method-assign]
    qc.tsunami_tick(issued)  # must not raise


# ---- run(): retry, and a whole connection with a fake websocket

class _FrameSocket:
    """A fake aiohttp websocket: yields the given text frames, then stays open until `closed` is
    set (or ends at once when `closed` is None)."""

    def __init__(self, frames: list[str], closed: asyncio.Event | None = None) -> None:
        self._frames = list(frames)
        self._closed = closed

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._frames:
            return NS(type=aiohttp.WSMsgType.TEXT, data=self._frames.pop(0))
        if self._closed is not None:
            await self._closed.wait()
        return NS(type=aiohttp.WSMsgType.CLOSED, data=None)


async def _yield_loop(times: int = 60) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


def _api_with_sockets(sockets: list, tsunami_answers: list, history=None):
    """session.ws_connect hands out `sockets` in turn (the last one repeats); the tsunami list
    answers `tsunami_answers` in turn (an Exception instance is raised, the last one repeats)."""
    calls: list[str] = []
    answers = list(tsunami_answers)

    async def get_json(url, params=None):
        calls.append(url)
        if url == URL_P2P_HISTORY:
            return history if history is not None else []
        got = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(got, Exception):
            raise got
        return got

    pool = list(sockets)
    api = NS(session=NS(ws_connect=lambda *a, **k: pool.pop(0) if len(pool) > 1 else pool[0]),
             get_json=get_json)
    return api, calls


async def _one_connection(qc) -> None:
    """One connection of the REAL run(): a quiet fake socket, the history awaited, the tsunami list asked
    beside it (its retry wait held), then the connection is cut. `qc._api` answers with its own get_json."""
    qc._api.session = NS(ws_connect=lambda *a, **k: _FrameSocket([], asyncio.Event()))
    gate = asyncio.Event()

    async def hold(seconds: float) -> None:
        await gate.wait()

    qc._retry_sleep = hold
    task = asyncio.ensure_future(REAL_RUN(qc))
    await _yield_loop()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_run_logs_a_frame_that_is_not_json_and_goes_on_with_the_next(hass, freezer, caplog):
    msg = _warning_bulletin()
    freezer.move_to((parse_552(msg).issued + timedelta(minutes=1)).astimezone(UTC))
    closed = asyncio.Event()
    api, _ = _api_with_sockets([_FrameSocket(['{"code": 552, "trunc', json.dumps(msg)], closed)], [[]])
    qc, rec = _client(api=api, hass=hass)
    with caplog.at_level("DEBUG", logger="custom_components.jma.quake_ws"):
        task = asyncio.ensure_future(REAL_RUN(qc))
        await _yield_loop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert any("not JSON" in r.getMessage() for r in caplog.records)
    assert [t for t, _ in rec.events] == ["tsunami"]


def test_the_test_only_backfill_is_gone():
    """_backfill() was called by tests only; run() uses _backfill_quakes() and _tsunami_retry()."""
    assert not hasattr(QuakeClient, "_backfill")


async def test_run_retries_the_tsunami_list_every_minute_until_it_answers(hass, freezer):
    """the first list request fails; the socket stays open; the retry (driven here
    through the injectable wait, the freezer never lets a real sleep return) gets the list."""
    msg = _warning_bulletin()
    freezer.move_to((parse_552(msg).issued + timedelta(minutes=3)).astimezone(UTC))
    closed = asyncio.Event()
    api, calls = _api_with_sockets([_FrameSocket([], closed)], [JmaFetchError("timeout"), [msg]])
    qc, rec = _client(api=api, hass=hass)
    waits: list[float] = []

    async def fake_wait(seconds: float) -> None:
        waits.append(seconds)
        await asyncio.sleep(0)

    qc._retry_sleep = fake_wait
    task = asyncio.ensure_future(REAL_RUN(qc))
    await _yield_loop()
    assert waits == [TSUNAMI_RETRY_S] == [60]
    assert qc.tsunami.replayed is True and [t for t, _ in rec.events] == ["tsunami"]
    assert rec.events[0][1]["home_grade"] == "warning"
    assert calls.count(URL_P2P_TSUNAMI) == 2  # and it stopped asking
    await _yield_loop()
    assert calls.count(URL_P2P_TSUNAMI) == 2 and waits == [60]
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_run_retry_keeps_asking_while_the_answer_is_not_a_list_and_dies_with_the_connection(hass, freezer):
    freezer.move_to("2026-10-03T00:00:00+00:00")
    closed = asyncio.Event()
    api, calls = _api_with_sockets([_FrameSocket([], closed)], [{"error": "busy"}])
    qc, rec = _client(api=api, hass=hass)
    state: dict = {"asked": 0, "cancelled": False}

    async def fake_wait(seconds: float) -> None:
        state["asked"] += 1
        state["gate"] = gate = asyncio.Event()  # a fresh minute each time
        try:
            await gate.wait()
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    qc._retry_sleep = fake_wait
    task = asyncio.ensure_future(REAL_RUN(qc))
    await _yield_loop()
    assert state["asked"] == 1 and qc.tsunami.replayed is False and qc.connected is True
    state["gate"].set()  # one minute passes: the dict answer again is not a replay
    await _yield_loop()
    assert state["asked"] == 2 and qc.tsunami.replayed is False and calls.count(URL_P2P_TSUNAMI) == 2
    closed.set()  # the connection ends: the retry task is cancelled with it
    await _yield_loop()
    assert state["cancelled"] is True
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert rec.events == []


async def test_run_retry_survives_an_unexpected_error_without_touching_the_socket(hass, freezer):
    freezer.move_to("2026-10-03T00:00:00+00:00")
    closed = asyncio.Event()
    api, calls = _api_with_sockets([_FrameSocket([], closed)], [JmaFetchError("a"), RuntimeError("boom")])
    qc, _rec = _client(api=api, hass=hass)

    async def fake_wait(seconds: float) -> None:
        await asyncio.sleep(0)

    qc._retry_sleep = fake_wait
    task = asyncio.ensure_future(REAL_RUN(qc))
    await _yield_loop()
    assert qc.connected is True and not task.done()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_run_end_to_end_live_frame_then_reconnect_gives_one_event(hass, freezer):
    """The real run() with a fake websocket and the real replays: a Tokyo Bay Warning arrives as
    a text frame, the socket drops, the reconnect's list already contains that bulletin: one event."""
    msg = _warning_bulletin()
    freezer.move_to((parse_552(msg).issued + timedelta(minutes=2)).astimezone(UTC))
    frame = json.dumps(msg, ensure_ascii=False)
    drop, hang = asyncio.Event(), asyncio.Event()
    api, calls = _api_with_sockets(
        [_FrameSocket([frame], drop), _FrameSocket([], hang)], [[], [msg]])
    qc, rec = _client(api=api, hass=hass)
    real_sleep = asyncio.sleep

    async def quick_sleep(seconds, *a, **k):  # the reconnect backoff; the freezer would never return
        await real_sleep(0)

    with patch("custom_components.jma.quake_ws.asyncio.sleep", new=quick_sleep):
        task = asyncio.ensure_future(REAL_RUN(qc))
        await _yield_loop(100)  # connection 1: the frame is handled, its list (empty) has answered
        assert [t for t, _ in rec.events] == ["tsunami"] and calls.count(URL_P2P_TSUNAMI) == 1
        drop.set()
        await _yield_loop(200)
    assert calls.count(URL_P2P_TSUNAMI) == 2 and calls.count(URL_P2P_HISTORY) == 2  # two connections
    assert [t for t, _ in rec.events] == ["tsunami"]
    assert rec.events[0][1]["home_grade"] == "warning" and rec.events[0][1]["backfill"] is False
    assert qc.tsunami.replayed is True and qc.connected is True
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


# ---- through the real wiring: storage file -> AlertMemory -> setup -> event entity

EVENT = "event.jma_chiyoda_alerts"


def _memory_file(entry, msg: dict) -> dict:
    """The tsunami section a previous run would have saved for `msg` (made by the tracker itself)."""
    memory: dict = {}
    TsunamiTracker(HOME, memory).feed(msg, _just_after(msg))
    return {"version": 1, "minor_version": 1, "key": f"jma.{entry.entry_id}.alert_memory",
            "data": {"typhoon": {}, "heat": [], "tsunami": memory}}


def _wire(hass, entry, aioclient_mock, msg):
    """Counts tsunami events on the alert signal; returns (counter list, the api answers)."""
    seen: list[tuple[str, dict]] = []
    async_dispatcher_connect(hass, signal_alert(entry.entry_id),
                             lambda t, d: seen.append((t, d)) if t.startswith("tsunami") else None)
    mock_heat_alert(aioclient_mock, {(20250730, 17): "alert_20261003_17.csv"})
    mock_jma(aioclient_mock)

    async def get_json(url, params=None):
        return [] if url == URL_P2P_HISTORY else [msg] if url == URL_P2P_TSUNAMI else None

    return seen, get_json


async def _replay(entry, get_json) -> None:
    qc = entry.runtime_data.quake
    qc._api = NS(get_json=AsyncMock(side_effect=get_json))
    await _one_connection(qc)  # the two requests of a connection, as run() makes them


async def test_restart_with_matching_memory_sends_nothing_and_empty_storage_sends_one(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer, hass_storage) -> None:
    hass = tokyo
    msg = _warning_bulletin()
    freezer.move_to((parse_552(msg).issued + timedelta(minutes=3)).astimezone(UTC))
    seen, get_json = _wire(hass, entry, aioclient_mock, msg)
    hass_storage[f"jma.{entry.entry_id}.alert_memory"] = _memory_file(entry, msg)
    await setup(hass, entry)
    assert entry.runtime_data.memory.tsunami["home"] == "warning"  # loaded through AlertMemory
    await _replay(entry, get_json)
    await hass.async_block_till_done()
    assert seen == [] and hass.states.get(EVENT).state == "unknown"
    assert hass.states.get("sensor.jma_chiyoda_tsunami").state == "warning"  # the sensor still knows


async def test_empty_storage_replay_delivers_exactly_one_tsunami_event_to_the_event_entity(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer, hass_storage) -> None:
    hass = tokyo
    msg = _warning_bulletin()
    freezer.move_to((parse_552(msg).issued + timedelta(minutes=3)).astimezone(UTC))
    seen, get_json = _wire(hass, entry, aioclient_mock, msg)
    await setup(hass, entry)
    await _replay(entry, get_json)
    await _replay(entry, get_json)  # a second connection with the same list
    await hass.async_block_till_done()
    assert [t for t, _ in seen] == ["tsunami"]
    state = hass.states.get(EVENT)
    assert state.attributes["event_type"] == "tsunami" and state.attributes["home_grade"] == "warning"
    json.dumps(dict(state.attributes))


@pytest.mark.parametrize("state", [
    CoreState.not_running,  # the state integrations are set up in during bootstrap
    CoreState.starting,  # hass.is_running is already True here: memory.py checks the state itself
])
async def test_stopped_before_it_had_started_the_warning_is_sent_on_the_next_start(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer, hass_storage, state) -> None:
    """decided while HA is still starting -> the file does NOT yet say 'announced'; once
    HA has started the event is delivered AND the file is written."""
    hass = tokyo
    msg = _warning_bulletin()
    freezer.move_to((parse_552(msg).issued + timedelta(minutes=3)).astimezone(UTC))
    seen, get_json = _wire(hass, entry, aioclient_mock, msg)
    key = f"jma.{entry.entry_id}.alert_memory"
    hass.set_state(state)
    await setup(hass, entry)
    await _replay(entry, get_json)
    await hass.async_block_till_done()
    assert entry.runtime_data.memory.tsunami["home"] == "warning"  # decided ...
    assert hass.states.get(EVENT).state == "unknown"  # ... but not delivered
    freezer.tick(timedelta(seconds=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert key not in hass_storage or hass_storage[key]["data"]["tsunami"] == {}  # not written yet

    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(EVENT).attributes["event_type"] == "tsunami"
    assert hass_storage[key]["data"]["tsunami"]["home"] == "warning"
    assert hass_storage[key]["data"]["tsunami"]["id"] == "warn-1"


async def test_reload_inside_the_save_delay_sends_the_warning_once(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer, hass_storage) -> None:
    hass = tokyo
    msg = _warning_bulletin()
    freezer.move_to((parse_552(msg).issued + timedelta(minutes=3)).astimezone(UTC))
    seen, get_json = _wire(hass, entry, aioclient_mock, msg)
    key = f"jma.{entry.entry_id}.alert_memory"
    await setup(hass, entry)
    await _replay(entry, get_json)
    await hass.async_block_till_done()
    assert [t for t, _ in seen] == ["tsunami"]
    assert key not in hass_storage  # the 2 s delay has not run
    assert await hass.config_entries.async_unload(entry.entry_id)  # unload flushes
    await hass.async_block_till_done()
    assert hass_storage[key]["data"]["tsunami"]["home"] == "warning"
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await _replay(entry, get_json)
    await hass.async_block_till_done()
    assert [t for t, _ in seen] == ["tsunami"]  # still one


async def test_expiry_through_the_ten_minute_timer_with_the_socket_silent(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer, hass_storage) -> None:
    """three days later, nothing from JMA, the integration's own timer lifts the warning:
    one tsunami_cancelled (expired), the sensor reads none and the memory is inactive."""
    hass = tokyo
    msg = _warning_bulletin()
    issued = parse_552(msg).issued
    freezer.move_to((issued + timedelta(minutes=3)).astimezone(UTC))
    seen, get_json = _wire(hass, entry, aioclient_mock, msg)
    await setup(hass, entry)
    qc = entry.runtime_data.quake
    qc.connected = True  # "silent" is a socket that is up and says nothing; one that has not been up for 60 s reads
    qc.down_since = None  # unavailable after 10 minutes (what `_mark_back` does once a connection has lasted)
    qc.handle(msg, dt_util.now())
    await hass.async_block_till_done()
    assert hass.states.get("sensor.jma_chiyoda_tsunami").state == "warning"
    freezer.move_to((issued + timedelta(days=3)).astimezone(UTC))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=11))
    await hass.async_block_till_done()
    assert [t for t, _ in seen] == ["tsunami", "tsunami_cancelled"] and seen[1][1]["expired"] is True
    assert hass.states.get("sensor.jma_chiyoda_tsunami").state == "none"
    assert entry.runtime_data.memory.tsunami["active"] is False
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=30))
    await hass.async_block_till_done()
    assert [t for t, _ in seen] == ["tsunami", "tsunami_cancelled"]


# ---------------------------------------------------------------- fix round 2

def test_future_dated_bulletin_is_not_marked_seen_and_is_warned_once(caplog):
    """R2-4: host clock 11 minutes slow -> the real bulletin looks future-dated and is rejected;
    once the clock is right the same bulletin, arriving by replay, is accepted. One WARNING only."""
    msg = _warning_bulletin()
    issued = parse_552(msg).issued
    slow = issued - timedelta(minutes=11)
    tracker = TsunamiTracker(HOME, {})
    with caplog.at_level("WARNING", logger="custom_components.jma.parse_tsunami"):
        assert tracker.feed(msg, slow) == [] and tracker.current is None
        assert tracker.feed(msg, slow) == []
        assert tracker.replay([msg], slow) == [] and tracker.replay([msg], slow) == []
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1 and "warn-1" in warnings[0].getMessage()
    assert issued.isoformat() in warnings[0].getMessage()
    [e] = tracker.replay([msg], issued + timedelta(minutes=1))  # clock corrected
    assert (e["type"], e["home_grade"], e["id"]) == ("tsunami", "warning", "warn-1")


def test_live_future_dated_bulletin_is_accepted_by_a_later_feed_when_the_clock_is_right():
    msg = _warning_bulletin()
    issued = parse_552(msg).issued
    tracker = TsunamiTracker(HOME, {})
    assert tracker.feed(msg, issued - timedelta(minutes=11)) == []
    assert _types(tracker.feed(msg, issued + timedelta(minutes=1))) == ["tsunami"]


def test_replay_of_only_future_dated_bulletins_still_counts_as_an_answer():
    msg = _warning_bulletin()
    tracker = TsunamiTracker(HOME, {})
    assert tracker.replay([msg], parse_552(msg).issued - timedelta(minutes=11)) == []
    assert tracker.replayed is True and tracker.current is None and tracker.picture(
        parse_552(msg).issued) is None


async def test_sensor_reads_none_when_only_future_dated_bulletins_were_replayed(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer, hass_storage) -> None:
    hass = tokyo
    msg = _warning_bulletin()
    freezer.move_to((parse_552(msg).issued - timedelta(minutes=11)).astimezone(UTC))
    seen, get_json = _wire(hass, entry, aioclient_mock, msg)
    await setup(hass, entry)
    assert hass.states.get("sensor.jma_chiyoda_tsunami").state == "unknown"
    await _replay(entry, get_json)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.jma_chiyoda_tsunami").state == "none" and seen == []


async def test_run_retry_exists_on_every_connection_not_only_the_first(hass, freezer):
    """R2-1: connection 1 gets its list (empty) -> no retry. The socket drops, a Tokyo Bay Warning is
    issued meanwhile; on connection 2 the list request fails once, the retry on THAT connection asks
    again and the warning is announced, exactly once."""
    msg = _warning_bulletin()
    freezer.move_to((parse_552(msg).issued + timedelta(minutes=3)).astimezone(UTC))
    drop, hold = asyncio.Event(), asyncio.Event()
    api, calls = _api_with_sockets(
        [_FrameSocket([], drop), _FrameSocket([], hold)], [[], JmaFetchError("timeout"), [msg]])
    qc, rec = _client(api=api, hass=hass)
    waits: list[float] = []
    real_sleep = asyncio.sleep

    async def instant(seconds: float) -> None:
        waits.append(seconds)
        await real_sleep(0)

    async def quick_backoff(seconds, *a, **k):  # the reconnect backoff; the freezer would never return
        await real_sleep(0)

    qc._retry_sleep = instant
    with patch("custom_components.jma.quake_ws.asyncio.sleep", new=quick_backoff):
        task = asyncio.ensure_future(REAL_RUN(qc))
        await _yield_loop(100)  # connection 1 has its (empty) list: nothing to retry
        assert calls.count(URL_P2P_TSUNAMI) == 1 and waits == [] and rec.events == []
        drop.set()
        await _yield_loop(300)
    assert calls.count(URL_P2P_HISTORY) == 2 and calls.count(URL_P2P_TSUNAMI) == 3
    assert waits == [TSUNAMI_RETRY_S]  # only connection 2 retried, once
    assert [t for t, _ in rec.events] == ["tsunami"] and rec.events[0][1]["home_grade"] == "warning"
    assert qc.connected is True
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


# ---- the list never stands in front of the socket

def _eew_frame(now: datetime) -> str:
    stamp = now.astimezone(JST).strftime("%Y/%m/%d %H:%M:%S")
    return json.dumps({"code": 556, "test": False, "cancelled": False,
                       "issue": {"eventId": "live-1", "serial": "1", "time": stamp},
                       "earthquake": {"originTime": stamp, "hypocenter": {"name": "東京湾", "magnitude": 7.0}},
                       "areas": [{"name": "東京都２３区", "scaleFrom": 50, "scaleTo": 55, "arrivalTime": stamp}]},
                      ensure_ascii=False)


async def test_a_live_eew_frame_is_not_held_up_by_a_slow_tsunami_list(hass, freezer):
    """The socket loop makes no request before it reads the socket (the history and the tsunami list run beside
    it). It must not make the EEW wait for one: the history has answered, the tsunami list has not (a slow
    P2P API, up to the 20 s timeout), and an EEW frame is already waiting in the socket."""
    freezer.move_to("2026-09-26T04:12:00+00:00")
    closed, list_answered = asyncio.Event(), asyncio.Event()
    asked: list[str] = []

    async def get_json(url, params=None):
        asked.append(url)
        if url == URL_P2P_TSUNAMI:
            await list_answered.wait()
        return []

    fired: list[str] = []
    socket = _FrameSocket([_eew_frame(dt_util.now())], closed)
    qc = QuakeClient(hass, NS(session=NS(ws_connect=lambda *a, **k: socket), get_json=get_json),
                     lambda t, d: fired.append(t), lambda: None, place=QUAKE_PLACE, tsunami_area=HOME)
    task = asyncio.ensure_future(REAL_RUN(qc))
    try:
        await _yield_loop(200)
        assert asked == [URL_P2P_HISTORY, URL_P2P_TSUNAMI] and qc.connected is True
        assert fired == ["eew"], f"the EEW waited for the tsunami list (asked: {asked})"
        assert qc.tsunami.replayed is False  # the list is still out
        list_answered.set()
        await _yield_loop(50)
        assert qc.tsunami.replayed is True and fired == ["eew"]
    finally:
        list_answered.set()
        closed.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_a_connection_that_drops_at_once_cancels_its_list_and_the_next_one_asks_again(hass, freezer):
    """The list request belongs to its connection: a socket that closes before the list answered
    cancels it, and the next connection asks again and announces what is in force, once."""
    msg = _warning_bulletin()
    freezer.move_to((parse_552(msg).issued + timedelta(minutes=3)).astimezone(UTC))
    hold = asyncio.Event()
    api, calls = _api_with_sockets([_FrameSocket([]), _FrameSocket([], hold)], [[msg]])
    qc, rec = _client(api=api, hass=hass)
    real_sleep = asyncio.sleep

    async def quick_backoff(seconds, *a, **k):
        await real_sleep(0)

    with patch("custom_components.jma.quake_ws.asyncio.sleep", new=quick_backoff):
        task = asyncio.ensure_future(REAL_RUN(qc))
        await _yield_loop(300)
    assert calls.count(URL_P2P_HISTORY) == 2
    assert [t for t, _ in rec.events] == ["tsunami"] and rec.events[0][1]["home_grade"] == "warning"
    assert qc.tsunami.replayed is True and qc.connected is True
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
