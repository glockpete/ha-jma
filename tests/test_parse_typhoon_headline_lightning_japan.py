"""Typhoon closest approach on the track line, per-area warning headline, LIDEN lightning frames and the
nationwide city forecast. Pure parser tests (no HA)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from custom_components.jma.const import EVENT_TYPES, JAPAN_CITIES, URL_LIDEN_GEOJSON
from custom_components.jma.errors import JmaParseError
from custom_components.jma.parse_forecast import HA_CONDITIONS
from custom_components.jma.parse_japan import parse_japan
from custom_components.jma.parse_layers import layers_attribute, liden_frames, n3_frames
from custom_components.jma.parse_typhoon import closest_approach, haversine_km, parse_storm
from custom_components.jma.parse_warnings import parse_area_warnings
from custom_components.jma.telops import TELOPS

from .conftest import AREA, LAT, LON
from .helpers import clone, load_json

HOME = (LAT, LON)
COMPONENT = Path(__file__).parent.parent / "custom_components" / "jma"


def test_every_event_type_is_offered_and_translated():
    services = yaml.safe_load((COMPONENT / "services.yaml").read_text(encoding="utf-8"))
    assert services["simulate"]["fields"]["event_type"]["selector"]["select"]["options"] == list(EVENT_TYPES)
    for name in ("translations/en.json", "translations/ja.json"):
        strings = json.loads((COMPONENT / name).read_text(encoding="utf-8"))
        states = strings["entity"]["event"]["alerts"]["state_attributes"]["event_type"]["state"]
        assert list(states) == list(EVENT_TYPES), name
    en = json.loads((COMPONENT / "translations/en.json").read_text(encoding="utf-8"))
    assert en["entity"]["event"]["alerts"]["state_attributes"]["event_type"]["state"][
        "warning_in_force"] == "Warning already in force"

# ---------- typhoon: closest approach on the track polyline ----------

# An invented track: the centres, the times and the circle radii.
NOW = {"lat": 29.1, "lon": 136.1, "time": "2026-09-29T21:00:00+09:00"}
TRACK = [
    {"hours": 12, "time": "2026-09-30T09:00:00+09:00", "lat": 30.2, "lon": 137.4, "circle_km": 50.0},
    {"hours": 24, "time": "2026-09-30T21:00:00+09:00", "lat": 31.7, "lon": 139.1, "circle_km": 90.0},
    {"hours": 48, "time": "2026-10-01T21:00:00+09:00", "lat": 36.7, "lon": 144.7, "circle_km": 170.0},
    {"hours": 72, "time": "2026-10-02T21:00:00+09:00", "lat": 45.6, "lon": 163.7, "circle_km": 260.0},
]


def _jma_parts(now: dict, track: list[dict]) -> tuple[list, list]:
    """forecast.json / specifications.json in JMA's shape for these centres."""
    forecast: list[dict] = [{"part": "title", "typhoonNumber": "2626", "name": {"jp": "スリゲ", "en": "Surigae"}}]
    forecast.append({"advancedHours": 0, "center": [now["lat"], now["lon"]],
                     "validtime": {"JST": now["time"]}, "track": {}})
    for t in track:
        forecast.append({"advancedHours": t["hours"], "center": [t["lat"], t["lon"]],
                         "validtime": {"JST": t["time"]},
                         "probabilityCircle": {"radius": t["circle_km"] * 1000}})
    specs = [{"part": "title"}, {"advancedHours": 0, "pressure": 950}]
    return forecast, specs


def test_closest_is_on_the_line_between_two_centres():
    forecast, specs = _jma_parts(NOW, TRACK)
    s = parse_storm({"id": "TC2632"}, forecast, specs, HOME)
    c = s["closest"]
    assert min(round(haversine_km(t["lat"], t["lon"], *HOME)) for t in TRACK) == 447  # nearest centre
    assert c["distance_km"] == 255
    assert c["between"] == [24, 48] and c["interpolated"] is True
    assert c["time"] == "2026-10-01T08:45:00+09:00"  # to the minute
    assert c["hours"] == 35.8
    t = datetime.fromisoformat(c["time"]) - datetime.fromisoformat(NOW["time"])
    assert abs(t / timedelta(hours=1) - c["hours"]) < 0.1  # hours after now.time
    assert c["circle_km"] == 129.2  # interpolated between the two circles (90 and 170)
    assert (c["lat"], c["lon"]) == (34.15, 141.84)
    assert set(c) == {"distance_km", "time", "hours", "circle_km", "lat", "lon", "between", "interpolated"}


def test_closest_exactly_a_centre():
    c = closest_approach(NOW, TRACK, (31.7, 139.1))  # the +24 h centre
    assert c["distance_km"] == 0 and c["interpolated"] is False
    assert c["hours"] == 24.0 and c["time"] == "2026-09-30T21:00:00+09:00"
    assert c["between"] == [12, 24]  # a tie keeps the earlier segment
    assert c["circle_km"] == 90.0 and (c["lat"], c["lon"]) == (31.7, 139.1)


def test_closest_with_a_single_track_point():
    one = TRACK[:1]
    ahead = closest_approach(NOW, one, HOME)  # home lies beyond the +12 h end
    assert ahead["between"] == [0, 12] and ahead["interpolated"] is False
    assert ahead["hours"] == 12.0 and ahead["time"] == "2026-09-30T09:00:00+09:00"
    assert ahead["distance_km"] == round(haversine_km(30.2, 137.4, *HOME))
    behind = closest_approach(NOW, one, (20.0, 125.0))  # home behind the storm: nearest is now
    assert behind["hours"] == 0.0 and behind["time"] == NOW["time"] and behind["interpolated"] is False
    assert behind["circle_km"] == 0.0  # the now end counts as 0
    mid = closest_approach(NOW, one, (30.5, 136.0))  # off to the side of the segment
    assert mid["interpolated"] is True and 0 < mid["hours"] < 12


def test_closest_missing_times_and_circles():
    no_time = closest_approach({**NOW, "time": None}, TRACK[:1], (30.5, 136.0))
    assert no_time["time"] is None and no_time["distance_km"] > 0 and no_time["hours"] > 0
    gap = [{**TRACK[0], "time": None}, *TRACK[1:]]
    assert closest_approach(NOW, gap, (30.9, 138.0))["time"] is None  # nearest on +12..+24 h
    estimate = [{"hours": 1, "time": "2026-09-29T22:00:00+09:00", "lat": 29.2, "lon": 136.2,
                 "circle_km": None}, *TRACK]
    c = closest_approach(NOW, estimate, (29.0, 136.3))  # nearest on now..+1 h (no 予報円)
    assert c["between"] == [0, 1] and c["circle_km"] is None
    assert closest_approach(NOW, [], HOME) is None


# ---------- warnings: headline only from bulletins in force for the area ----------


def test_quiet_area_has_no_headline():
    records = load_json("warning_r8_130000.json")
    assert any(r.get("headlineText") for r in records)  # the office does have headlines
    aw = parse_area_warnings(records, AREA)
    assert aw.warnings == [] and aw.headline is None and aw.headlines == {}
    assert aw.report_time == "2026-09-26T04:02:00+09:00"  # the newest of the office


def test_headline_is_the_newest_record_in_force_here():
    records = clone(load_json("warning_r8_130000.json"))
    # 1338100 is in force in VPWW56 (older bulletin) and VPWW61 (newer bulletin): the newer wins
    aw = parse_area_warnings(records, "1338100")
    assert aw.headline.startswith("伊豆諸島北部、伊豆諸島南部では")
    assert set(aw.headlines) == {"VPWW56", "VPWW61"}
    # VPWW61 no longer in force there: the older VPWW56 headline, not the office's newest
    vpww61 = next(r for r in records if r["dataTypeCode"] == "VPWW61")
    for item in vpww61["warning"]["class20Items"]:
        if item["areaCode"] == "1338100":
            item["kinds"] = [{**k, "status": "解除"} for k in item["kinds"]]
    aw = parse_area_warnings(records, "1338100")
    assert aw.headline == "伊豆諸島南部では、土砂災害に注意してください。"
    assert set(aw.headlines) == {"VPWW56"}
    assert aw.report_time == "2026-09-26T04:02:00+09:00"


def _item(area: str, kinds: list[dict]) -> dict:
    return {"areaCode": area, "kinds": kinds}


def test_headline_hand_made_records():
    b = {"dataTypeCode": "VPWW55", "controlDatetime": "2026-09-29T01:00:00Z",
         "reportDatetime": "2026-09-29T10:00:00+09:00", "headlineText": "東京地方では、大雨に警戒してください。",
         "warning": {"class20Items": [_item(AREA, [{"code": "03", "status": "継続"}])]}}
    a = {"dataTypeCode": "VPWW61", "controlDatetime": "2026-09-29T03:00:00Z",
         "reportDatetime": "2026-09-29T12:00:00+09:00", "headlineText": "伊豆諸島北部では、高波に注意してください。",
         "warning": {"class20Items": [_item(AREA, [{"code": "16", "status": "解除"}]),
                                      _item("1336100", [{"code": "16", "status": "発表"}])]}}
    aw = parse_area_warnings([b, a], AREA)
    assert aw.headline == "東京地方では、大雨に警戒してください。"
    assert aw.headlines == {"VPWW55": "東京地方では、大雨に警戒してください。"}
    assert aw.report_time == "2026-09-29T12:00:00+09:00"
    # nothing in force: a status with no code does not count either
    quiet = clone([b, a])
    quiet[0]["warning"]["class20Items"][0]["kinds"] = [{"status": "発表警報・注意報はなし"}, {"status": "発表"}]
    assert parse_area_warnings(quiet, AREA).headline is None


# ---------- LIDEN lightning frames ----------

@pytest.mark.parametrize("fixture", ["targetTimes_N3.json", "targetTimes_N3_b.json"])
def test_liden_frames_newest_twelve_ascending(fixture):
    n3 = load_json(fixture)
    listed = sorted({r["validtime"] for r in n3 if "liden" in r.get("elements", [])
                     and r["basetime"] == r["validtime"]})
    assert len(listed) > 12  # both sample files carry more than an hour of 5-min rows
    frames = liden_frames(n3)
    assert len(frames) == 12
    assert [f["validtime"] for f in frames] == listed[-12:]
    assert all(f["basetime"] == f["validtime"] for f in frames)
    times = [datetime.strptime(f["validtime"], "%Y%m%d%H%M%S") for f in frames]
    assert all(b - a == timedelta(minutes=5) for a, b in zip(times, times[1:], strict=False))
    assert [f["validtime"] for f in liden_frames(n3, count=3)] == listed[-3:]


def test_liden_empty_is_no_data_and_left_out():
    rows = [{"basetime": "20260927140000", "validtime": "20260927140000", "elements": ["thns", "trns"]}]
    assert liden_frames(rows) == []
    assert liden_frames([]) == []
    with pytest.raises(JmaParseError):
        liden_frames({"not": "a list"})
    n3 = {"thns": n3_frames(rows, "thns"), "trns": n3_frames(rows, "trns"), "liden": []}
    assert "liden" not in layers_attribute(n3=n3)


def test_liden_layer_shape():
    n3_data = load_json("targetTimes_N3_b.json")
    n3 = {"thns": n3_frames(n3_data, "thns"), "trns": n3_frames(n3_data, "trns"),
          "liden": liden_frames(n3_data)}
    layer = layers_attribute(n3=n3)["liden"]
    assert layer == {"template": URL_LIDEN_GEOJSON, "frames": n3["liden"], "format": "geojson"}
    assert "{z}" not in layer["template"] and "{x}" not in layer["template"] and "{y}" not in layer["template"]
    assert layer["template"].endswith("/nowc/{basetime}/none/{validtime}/surf/liden/data.geojson")
    assert layer["frames"][-1] == {"basetime": "20260927140500", "validtime": "20260927140500"}


# ---------- nationwide city forecast ----------

TODAY = date(2026, 9, 30)  # a day after the date of the newest issue in the sample


def test_japan_fixture_cities():
    data = load_json("forecast_010000.json")
    jf = parse_japan(data, TODAY)
    assert len(jf.cities) == 22
    assert [c["office"] for c in jf.cities] == [o["officeCode"] for o in data]  # the file's order
    assert jf.report_time == "2026-09-29T21:00:00+09:00"  # 仙台 re-issued at 21:00
    tokyo = next(c for c in jf.cities if c["office"] == "130000")
    assert (tokyo["name"], tokyo["name_en"], tokyo["lat"], tokyo["lon"]) == ("東京", "Tokyo", 35.692, 139.75)
    assert tokyo["days"] == [
        # evening layout: the 29th 17:00 issue's "tomorrow" is today; min/max from temps[0..1]
        {"date": "2026-09-30", "code": "203", "text": "くもり 時々 雨", "condition": "rainy",
         "icon": "202.svg", "pop": 70, "temp_max": 20, "temp_min": 17},
        # no 6-h pops for the day after; temps from the weekly block, as parse_forecast fills them
        {"date": "2026-10-01", "code": "200", "text": "くもり", "condition": "cloudy",
         "icon": "200.svg", "pop": None, "temp_max": 27, "temp_min": 17},
    ]
    for city in jf.cities:
        assert len(city["days"]) == 2, city["office"]
        for day in city["days"]:
            assert day["code"] in TELOPS, (city["office"], day)
            assert day["condition"] in HA_CONDITIONS and day["condition"] != "exceptional"
            assert day["icon"] and day["icon"].endswith(".svg")
    size = len(json.dumps(jf.cities, ensure_ascii=False).encode("utf-8"))
    assert size < 12_000, size


def test_japan_cities_table_matches_the_amedas_table():
    """JAPAN_CITIES = each office's temperature station, [deg, min] -> decimal, 3 dp."""
    table = load_json("amedastable.json")
    for office in load_json("forecast_010000.json"):
        station = office["srf"]["timeSeries"][2]["areas"]["area"]["code"]
        lat, lon = table[station]["lat"], table[station]["lon"]
        expect = (round(lat[0] + lat[1] / 60, 3), round(lon[0] + lon[1] / 60, 3))
        assert JAPAN_CITIES[office["officeCode"]][1:] == expect, office["officeCode"]


def test_japan_today_and_tomorrow_only():
    data = load_json("forecast_010000.json")
    days = parse_japan(data, date(2026, 9, 29)).cities[0]["days"]
    assert [d["date"] for d in days] == ["2026-09-29", "2026-09-30"]
    assert days[0]["temp_max"] is None  # the 17:00 issue carries no temperature for its own day
    tokyo = next(c for c in parse_japan(data, date(2026, 10, 1)).cities if c["office"] == "130000")
    # the 2nd is weekly-only: code and temps from the weekly block, no text, no 6-h pops
    assert tokyo["days"][1] == {"date": "2026-10-02", "code": "201", "text": None, "condition": "partlycloudy",
                                "icon": "201.svg", "pop": None, "temp_max": 25, "temp_min": 18}
    assert all(c["days"] == [] for c in parse_japan(data, date(2026, 10, 20)).cities)  # not carried


def test_japan_one_bad_office_is_skipped_all_bad_raises():
    data = clone(load_json("forecast_010000.json"))
    data[3]["srf"] = "maintenance"
    data[5]["srf"]["timeSeries"] = []
    jf = parse_japan(data, TODAY)
    assert len(jf.cities) == 20 and all(c["office"] not in ("020000", "040000") for c in jf.cities)
    assert jf.report_time == "2026-09-29T17:00:00+09:00"  # 仙台 (21:00) was one of the bad ones
    unmapped = clone(load_json("forecast_010000.json"))
    unmapped[0]["officeCode"] = "999999"
    assert len(parse_japan(unmapped, TODAY).cities) == 21
    for office in data:
        office["srf"] = None
    with pytest.raises(JmaParseError):
        parse_japan(data, TODAY)
    with pytest.raises(JmaParseError):
        parse_japan({"officeCode": "130000"}, TODAY)
    with pytest.raises(JmaParseError):
        parse_japan([], TODAY)
