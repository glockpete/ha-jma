"""AMeDAS, nowcast-pixel and P2P parsers against samples of the published files."""

from __future__ import annotations

import copy
import io
from datetime import UTC, datetime, timedelta, timezone

import pytest
from PIL import Image

from custom_components.jma.errors import JmaParseError
from custom_components.jma.parse_amedas import (
    merge_observations,
    parse_latest_time,
    parse_point,
    point_file_parts,
)
from custom_components.jma.parse_nowcast import WetHold, band_at, frame_lists, rain_series, tile_for
from custom_components.jma.parse_quake import EewTracker, QuakeTracker, parse_551

from .conftest import LAT, LON, QUAKE_PLACE
from .helpers import clone, load_bytes, load_json

JST = timezone(timedelta(hours=9))


# ---------- AMeDAS ----------

def test_point_file_naming_blocks_of_three_hours():
    assert point_file_parts(parse_latest_time("2026-09-26T12:50:00+09:00")) == ("20260926", "12")
    assert point_file_parts(parse_latest_time("2026-09-27T00:10:00+09:00")) == ("20260927", "00")
    assert point_file_parts(parse_latest_time("2026-09-26T23:59:00+09:00")) == ("20260926", "21")
    # a UTC time is converted to JST first
    assert point_file_parts(datetime(2026, 9, 26, 15, 10, tzinfo=UTC)) == ("20260927", "00")


def test_parse_point_newest_values():
    obs = parse_point(load_json("point_62016_12.json"), "62016")
    assert obs.station == "62016"
    assert obs.observed_at == datetime(2026, 9, 26, 12, 50, tzinfo=JST)
    assert (obs.temperature, obs.humidity, obs.wind_speed) == (24.6, 66, 3.0)
    assert obs.pressure is None  # a station without a barometer
    assert obs.wind_bearing == 90.0  # index 4 = E
    assert obs.precip_24h == 4.0


def test_flagged_values_are_dropped_and_older_value_used():
    data = clone(load_json("point_62016_12.json"))
    newest = max(data)
    data[newest]["temp"] = [99.9, 5]  # suspect reading
    obs = parse_point(data, "62016")
    assert obs.temperature != 99.9
    assert obs.temperature is not None


def test_merge_takes_pressure_from_secondary():
    merged = merge_observations(
        parse_point(load_json("point_62016_12.json"), "62016"),
        parse_point(load_json("point_44132_12.json"), "44132"),
    )
    assert merged.temperature == 24.6 and merged.station == "62016"
    assert merged.pressure == 1013.9  # normalPressure of the secondary station
    assert merge_observations(None, None).temperature is None


def test_merge_with_only_the_secondary_station_returns_it_unchanged():
    secondary = parse_point(load_json("point_44132_12.json"), "44132")
    assert merge_observations(None, secondary) == secondary
    assert merge_observations(secondary, None) == secondary


def test_bad_point_file():
    with pytest.raises(JmaParseError):
        parse_point("<html>", "62016")
    with pytest.raises(JmaParseError):
        parse_latest_time("nonsense")


# ---------- nowcast ----------

def test_tile_for_chiyoda():
    x, y, px, py = tile_for(LAT, LON, 10)
    assert (x, y) == (909, 403)
    # tile numbers and pixel of Tokyo Station (35.6812, 139.7671) by the Web-Mercator formula
    assert (px, py) == (143, 58)


def test_rain_tile_fixture_has_bands():
    png = load_bytes("tile_hrpns_rain_z6_54_25.png")
    img = Image.open(io.BytesIO(png)).convert("RGBA")
    bands = {band_at(png, x, y, radius=0) for x in range(0, 256, 4) for y in range(0, 256, 4)}
    assert 0 in bands
    assert any(b >= 1 for b in bands)
    # the block max is never below the single pixel
    for x, y in [(10, 10), (128, 128), (250, 5)]:
        assert band_at(png, x, y) >= band_at(png, x, y, radius=0)
    assert img.size == (256, 256)


def test_blank_tile_is_zero():
    png = load_bytes("tile_hrpns_z10_909_403.png")
    assert band_at(png, 128, 128) == 0


def test_rgba_and_palette_paths():
    img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    img.putpixel((100, 100), (33, 140, 255, 255))  # band 3
    img.putpixel((101, 100), (254, 42, 3, 255))  # unlisted, nearest #FF2800 band 7
    buf = io.BytesIO()
    img.save(buf, "PNG")
    rgba = buf.getvalue()
    assert band_at(rgba, 100, 100, radius=0) == 3
    assert band_at(rgba, 101, 100, radius=0) == 7
    assert band_at(rgba, 99, 100, radius=2) == 7
    assert band_at(rgba, 50, 50) == 0
    with pytest.raises(JmaParseError):
        band_at(b"not a png", 1, 1)


def test_frame_lists_from_fixture():
    base, past, forecast = frame_lists(
        load_json("targetTimes_N1.json"), load_json("targetTimes_N2.json"), 12)
    assert len(past) == 12 and past[-1] == base
    assert len(forecast) == 12
    assert forecast == sorted(forecast) and forecast[0] > base


def test_rain_series():
    base = "20260926040000"
    frames = [(base, 0)] + [
        (f"2026092604{m:02d}00", b) for m, b in
        [(5, 0), (10, 1), (15, None), (20, 3), (25, 4), (30, 2), (35, 0),
         (40, 0), (45, 0), (50, 0), (55, 0)]
    ] + [("20260926050000", 0)]
    rs = rain_series(base, frames)
    assert len(rs.series) == 13
    assert rs.minutes_to_rain == 20  # band 1 (<1 mm/h) at +10 does not count
    assert not rs.raining_now
    assert rs.max_mmh == 20  # band 4 = 10-20 mm/h
    assert rs.series[3]["band"] is None  # an unreadable frame stays in the series
    dry = rain_series(base, [(base, 3)] + [(f"2026092604{m:02d}00", 0) for m in range(5, 60, 5)])
    assert dry.raining_now and dry.minutes_to_rain is None


def test_wet_hold_needs_two_dry_frames_and_ignores_repeats_and_unknowns():
    h = WetHold()
    seq = [("t1", True), ("t2", False), ("t3", False), ("t3", False), ("t4", None), ("t5", True)]
    assert [h.update(b, r) for b, r in seq] == [True, True, False, False, False, True]


def test_wet_hold_keeps_the_hold_through_an_unreadable_frame_right_after_wet():
    """coordinator.py passes None (not `raining_now`) when the observed band is unknown,
    so an unreadable frame right after a wet one must not count towards the two dry frames
    needed to clear it, nor reset the hold outright."""
    h = WetHold()
    seq = [("t1", True), ("t2", None), ("t3", False), ("t4", False)]
    assert [h.update(b, r) for b, r in seq] == [True, True, True, False]


# ---------- P2P ----------

def _p2p_551() -> list[dict]:
    return load_json("p2p_551_sample_a.json")


def _synthetic_551(scale_local: int = 30, qid: str = "q1", hypo: str | None = "東京湾") -> dict:
    return {
        "code": 551, "id": qid,
        "issue": {"type": "DetailScale", "time": "2026/09/26 12:20:39"},
        "earthquake": {"time": "2026/09/26 12:17:00", "maxScale": 40, "domesticTsunami": "None",
                       "hypocenter": {"name": hypo or "", "magnitude": 5.1 if hypo else -1,
                                      "depth": 40 if hypo else -1}},
        # Address forms as P2P publishes them (P2P history): a 東京 prefix on every station.
        "points": [
            {"pref": "東京都", "addr": "東京千代田区大手町", "isArea": False, "scale": scale_local},
            {"pref": "東京都", "addr": "東京港区芝", "isArea": False, "scale": 40},
            {"pref": "東京都", "addr": "東京八王子市大横町", "isArea": False, "scale": 45},
            {"pref": "埼玉県", "addr": "中央区", "isArea": False, "scale": 50},
        ],
    }


def test_551_fixture_parses():
    parsed = [parse_551(m, QUAKE_PLACE) for m in _p2p_551()]
    assert all(q is not None and q.id for q in parsed)


def test_551_prefecture_and_local():
    q = parse_551(_synthetic_551(), QUAKE_PLACE)
    assert q.pref_max_scale == 45  # 八王子 is in the prefecture; 埼玉 中央区 is not
    assert q.local_scale == 30
    assert q.hypocenter == "東京湾" and q.magnitude == 5.1
    assert parse_551({"code": 556}, QUAKE_PLACE) is None


def test_quake_tracker_repeats_only_when_it_matters():
    t = QuakeTracker(QUAKE_PLACE)
    assert t.feed(_synthetic_551(qid="a", hypo=None)) is not None  # 震度速報
    assert t.feed(_synthetic_551(qid="a", hypo=None)) is None  # same id
    assert t.feed(_synthetic_551(qid="b", hypo="東京湾")) is not None  # hypocentre now known
    assert t.feed(_synthetic_551(qid="c", hypo="東京湾")) is None  # nothing new


def _aug23() -> list[dict]:
    return load_json("p2p_551_sample_b.json")


def test_tracker_merges_a_quake_sequence_of_the_prefecture():
    t = QuakeTracker(QUAKE_PLACE)
    emitted = [q for m in sorted(_aug23(), key=lambda m: m["issue"]["time"]) if (q := t.feed(m))]
    assert all(q.pref_max_scale is not None for q in emitted)  # every event can pass the ≥30 filter
    assert any(q.hypocenter for q in emitted)                  # the epicentre update goes out
    assert emitted[-1].local_scale == 40                       # the detailed report is not swallowed


def test_destination_message_carries_the_scale_prompts_prefecture_scale():
    sp = min((m for m in _aug23() if m["issue"]["type"] == "ScalePrompt"), key=lambda m: m["issue"]["time"])
    dest = copy.deepcopy(next(m for m in _aug23() if m["issue"]["type"] == "DetailScale"))
    dest.update(id="dest", issue={"type": "Destination", "time": "2026/08/23 02:05:00"}, points=[])
    t = QuakeTracker(QUAKE_PLACE)
    assert t.feed(sp).pref_max_scale == 40
    q = t.feed(dest)
    assert q is not None and q.hypocenter and q.magnitude == 5.9 and q.pref_max_scale == 40


def test_tracker_keeps_two_quakes_in_the_same_minute_apart():
    t = QuakeTracker(QUAKE_PLACE)
    first = _synthetic_551(qid="x", hypo="東京湾")
    assert t.feed(first).pref_max_scale == 45
    second = _synthetic_551(qid="y", hypo="千葉県北西部")
    second["points"] = []
    q = t.feed(second)
    assert q is not None and q.hypocenter == "千葉県北西部" and q.pref_max_scale is None


def test_tracker_remembers_ids_it_has_seen():
    first = _aug23()[0]
    t = QuakeTracker(QUAKE_PLACE)
    assert t.feed(first) is not None
    assert t.feed(first) is None  # the same report again is skipped
    assert QuakeTracker(QUAKE_PLACE).feed(first) is not None  # a fresh tracker does not skip it


def test_tracker_remembers_at_most_500_ids():
    t = QuakeTracker(QUAKE_PLACE)
    for i in range(600):
        t.feed(_synthetic_551(qid=str(i), hypo="東京湾"))
    assert len(t._seen) == 500 and next(iter(t._seen)) == "100"


NOW = datetime(2026, 9, 26, 12, 17, 30, tzinfo=JST)


def _eew(serial: int, scale_from: int, scale_to: int, **kw) -> dict:
    msg = {
        "code": 556, "test": False, "cancelled": False,
        "issue": {"time": "2026/09/26 12:17:20", "eventId": "20260926121700", "serial": str(serial)},
        "earthquake": {"originTime": "2026/09/26 12:17:00",
                       "hypocenter": {"name": "東京湾", "magnitude": 6.1}},
        "areas": [
            {"pref": "千葉", "name": "千葉県北西部", "scaleFrom": 50, "scaleTo": 55},
            {"pref": "東京", "name": "東京都２３区", "scaleFrom": scale_from, "scaleTo": scale_to,
             "arrivalTime": "2026/09/26 12:17:40"},
        ],
    }
    msg.update(kw)
    return msg


def test_eew_burst_first_then_rising_only():
    t = EewTracker(QUAKE_PLACE.pref)
    first = t.feed(_eew(1, 45, 50), NOW)
    assert [e["type"] for e in first] == ["eew"] and first[0]["predicted_scale"] == 50
    assert t.feed(_eew(2, 45, 50), NOW) == []
    assert t.feed(_eew(3, 40, 45), NOW) == []  # lower: no new alarm
    rising = t.feed(_eew(4, 50, 55), NOW)
    assert rising[0]["predicted_scale"] == 55 and rising[0]["update"] is True


def test_eew_ignores_tests_other_areas_and_old_backfill():
    t = EewTracker(QUAKE_PLACE.pref)
    assert t.feed(_eew(1, 45, 50, test=True), NOW) == []
    other = _eew(1, 45, 50)
    other["areas"] = [a for a in other["areas"] if a["pref"] != "東京"]  # only 千葉県北西部 is left
    assert t.feed(other, NOW) == []
    assert t.feed(_eew(1, 45, 50), NOW + timedelta(minutes=5), backfill=True) == []
    assert t.feed(_eew(1, 45, 50), NOW, backfill=True)[0]["type"] == "eew"


def test_eew_area_counts_by_prefecture_not_by_the_spelling_of_its_name():
    """An early-warning area is matched by its prefecture field; a differently spelled area name counts."""
    msg = _eew(1, 45, 50)
    msg["areas"][1]["name"] = "東京都23区"
    ev = EewTracker(QUAKE_PLACE.pref).feed(msg, NOW)
    assert ev and ev[0]["areas"] == ["東京都23区"]


def test_eew_cancel_only_after_an_alert():
    t = EewTracker(QUAKE_PLACE.pref)
    assert t.feed(_eew(9, 45, 50, cancelled=True), NOW) == []
    assert t.feed(_eew(1, 45, 50), NOW) == []
    t = EewTracker(QUAKE_PLACE.pref)
    t.feed(_eew(1, 45, 50), NOW)
    assert [e["type"] for e in t.feed(_eew(2, 45, 50, cancelled=True), NOW)] == ["eew_cancelled"]
    assert t.feed(_eew(1, 45, 50), NOW) == []
    assert t.feed(_eew(3, 50, 55), NOW) == []


def test_eew_live_replay_has_same_freshness_limit_as_backfill():
    assert EewTracker(QUAKE_PLACE.pref).feed(_eew(1, 45, 50), NOW + timedelta(days=1)) == []
    msg = _eew(1, 45, 50)
    del msg["issue"]["time"]
    assert EewTracker(QUAKE_PLACE.pref).feed(msg, NOW, backfill=True) == []  # live: see tests/test_eew_clock.py
    # a slow host clock (message 5 min ahead) still alerts; the limit is EEW_FUTURE_TOLERANCE_S
    assert EewTracker(QUAKE_PLACE.pref).feed(_eew(1, 45, 50), NOW - timedelta(minutes=5)) != []
    assert EewTracker(QUAKE_PLACE.pref).feed(_eew(1, 45, 50), NOW - timedelta(minutes=15)) == []


def test_eew_99_means_or_more():
    ev = EewTracker(QUAKE_PLACE.pref).feed(_eew(1, 50, 99), NOW)
    assert ev[0]["predicted_scale"] == 50
