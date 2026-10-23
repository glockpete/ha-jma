"""Warning timeline, typhoon, WBGT, map layers, キキクル, rasrf.

Pure parser tests (no HA): fixtures are samples of the agency's published files.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, date, datetime, timedelta

import pytest
from PIL import Image

from custom_components.jma.const import (
    TIMELINE_PHENOMENA,
    TIMELINE_VALUES,
    URL_GSI_FLOOD_TILE,
    URL_RASRF_TILE,
)
from custom_components.jma.errors import JmaParseError
from custom_components.jma.parse_layers import (
    Kikikuru,
    LastGood,
    himawari_frames,
    kikikuru_level_at,
    layers_attribute,
    n3_frames,
    rain_outlook,
    rasrf_frames,
    risk_frames,
)
from custom_components.jma.parse_nowcast import band_at, tile_for
from custom_components.jma.parse_timeline import parse_timeline, significancy_level
from custom_components.jma.parse_typhoon import (
    fit_storms,
    haversine_km,
    parse_storm,
    parse_target_tc,
    thin,
)
from custom_components.jma.parse_wbgt import parse_wbgt

from .conftest import AREA, LAT, LON
from .helpers import clone, load_bytes, load_json, load_text

HOME = (LAT, LON)


def _png(pixels: dict[tuple[int, int], tuple[int, int, int, int]]) -> bytes:
    img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    for xy, rgba in pixels.items():
        img.putpixel(xy, rgba)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ---------- warning timeline ----------

def test_timeline_code_to_level_as_jma_page_script():
    for code, level in {"51": 5, "50": 5, "41": 4, "31": 3, "30": 3, "22": 2, "21": 2, "20": 2}.items():
        assert significancy_level(code) == level
    assert significancy_level("00") is None  # not applicable: omitted
    for code in ("01", "10", "11", "40", "99", ""):
        assert significancy_level(code) == 0
    assert significancy_level(None) == 0


def test_timeline_every_fixture_type_is_mapped():
    """Catches a half-width/full-width slip in the const keys (１時間最大雨量, ６時間最大降雪量)."""
    sig, fc = set(), set()
    for series in load_json("warning_timeline_130000.json")["timeSeries"]:
        for item in series["class20Items"]:
            for kind in item["kinds"]:
                sig |= {p["type"] for p in kind.get("significancyParts", [])}
                fc |= {p["type"] for p in kind.get("forecastParts", [])}
    assert sig <= set(TIMELINE_PHENOMENA)
    assert set(TIMELINE_VALUES) <= fc


def test_timeline_chiyoda_3_hourly_steps():
    out = parse_timeline(load_json("warning_timeline_130000.json"), AREA)
    assert out.report_time == "2026-09-27T23:00:00+09:00"
    assert len(out.steps) == 8
    assert out.steps[0]["start"] == "2026-09-28T00:00:00+09:00"
    assert out.steps[0]["end"] == "2026-09-28T03:00:00+09:00"
    assert out.steps[-1]["end"] == "2026-09-29T00:00:00+09:00"
    # 雷危険度 codes 01 01 20 20 20 20 01 01 -> L2 thunder at steps 2..5 only
    assert [s["levels"] for s in out.steps] == [{}, {}] + [{"thunder": 2}] * 4 + [{}, {}]
    # the values of the fixture's 1310100 item (１時間最大雨量)
    assert [s["rain_1h_mm"] for s in out.steps] == [1, 5, 5, 5, 3, 10, 1, 1]
    assert [s["wind_ms"] for s in out.steps] == [5, 5, 5, 8, 8, 8, 5, 8]
    assert [s["wind_dir"] for s in out.steps] == ["南"] * 7 + ["北"]
    assert [s["snow_6h_cm"] for s in out.steps] == [0] * 8
    assert out.level == 2
    assert out.peak == {"level": 2, "phenomena": ["thunder"], "start": "2026-09-28T06:00:00+09:00"}
    # daily: 乾燥/なだれ/低温 01, 霜 01 then 00 -> nothing >= 2; "00" omitted
    assert out.daily == [{"date": "2026-09-28", "levels": {}}, {"date": "2026-09-29", "levels": {}}]


def _chiyoda(data: dict, series: int) -> dict:
    return next(i for i in data["timeSeries"][series]["class20Items"] if i["areaCode"] == AREA)


def _part(item: dict, kind_type: str) -> dict:
    return next(p for k in item["kinds"] for p in k.get("significancyParts", []) + k.get("forecastParts", [])
                if p["type"] == kind_type)


def test_timeline_levels_peak_unknown_type_and_missing_values():
    data = clone(load_json("warning_timeline_130000.json"))
    item = _chiyoda(data, 0)
    _part(item, "大雨浸水危険度")["locals"][0]["codes"] = ["11", "21", "31", "41", "51", "50", "00", "01"]
    _part(item, "１時間最大雨量")["locals"][0]["values"][0] = {"unit": "mm", "condition": "値なし", "value": ""}
    item["kinds"].append({"status": "発表", "significancyParts": [
        {"type": "謎危険度", "locals": [{"codes": ["30"] + ["01"] * 7}]}]})
    _part(_chiyoda(data, 2), "霜危険度")["locals"][0]["codes"] = ["21", "00"]
    out = parse_timeline(data, AREA)
    assert [s["levels"].get("rain") for s in out.steps] == [None, 2, 3, 4, 5, 5, None, None]
    assert out.steps[0]["levels"] == {"謎危険度": 3}  # an unknown type keeps its raw name
    assert out.steps[0]["rain_1h_mm"] is None
    assert out.level == 5
    assert out.peak == {"level": 5, "phenomena": ["rain"], "start": "2026-09-28T12:00:00+09:00"}
    assert out.daily[0]["levels"] == {"frost": 2} and out.daily[1]["levels"] == {}
    # the state is the 3-hourly horizon only: a daily-only L2 does not raise a quiet outlook
    quiet = clone(load_json("warning_timeline_130000.json"))
    _part(_chiyoda(quiet, 2), "霜危険度")["locals"][0]["codes"] = ["21", "21"]
    for kind in _chiyoda(quiet, 0)["kinds"]:
        for p in kind.get("significancyParts", []):
            p["locals"][0]["codes"] = ["01"] * 8
    q = parse_timeline(quiet, AREA)
    assert q.level == 0 and q.peak is None and q.daily[0]["levels"] == {"frost": 2}


def test_timeline_sea_locals_do_not_count_on_land():
    data = clone(load_json("warning_timeline_130000.json"))
    port = next(i for i in data["timeSeries"][0]["class20Items"] if i["areaCode"] == "1310300")
    wind = _part(port, "風危険度")
    names = [loc.get("areaName") for loc in wind["locals"]]
    assert names == ["陸上", "海上"]
    wind["locals"][0]["codes"] = ["01"] * 8
    wind["locals"][1]["codes"] = ["31"] * 8
    assert "wind" not in parse_timeline(data, "1310300").steps[0]["levels"]
    wind["locals"][0]["codes"] = ["21"] * 8
    assert parse_timeline(data, "1310300").steps[0]["levels"]["wind"] == 2


def test_timeline_bad_files():
    with pytest.raises(JmaParseError):
        parse_timeline(load_json("warning_timeline_130000.json"), "9999999")
    with pytest.raises(JmaParseError):
        parse_timeline({"timeSeries": [{"timeDefines": [{"dateTime": "x", "duration": "PT3H"}],
                                        "class20Items": [{"areaCode": AREA, "kinds": []}]}]}, AREA)
    with pytest.raises(JmaParseError):
        parse_timeline([], AREA)


# ---------- typhoon ----------

def test_haversine_known_points():
    assert haversine_km(*HOME, *HOME) == 0
    assert haversine_km(35.0, 139.0, 36.0, 139.0) == pytest.approx(111.19, abs=0.01)


def test_target_tc():
    assert parse_target_tc(load_json("typhoon_targetTc.json")) == [
        {"id": "TC2632", "number": "2626", "category": "TY", "issue": "2026-09-27T22:45:00+09:00"}]
    assert parse_target_tc([]) == []
    with pytest.raises(JmaParseError):
        parse_target_tc({"x": 1})


def _storm(home=HOME) -> dict:
    tc = parse_target_tc(load_json("typhoon_targetTc.json"))[0]
    return parse_storm(tc, load_json("typhoon_TC2632_forecast.json"), load_json("typhoon_TC2632_specs.json"), home)


def test_storm_from_fixture():
    s = _storm()
    assert (s["id"], s["number"], s["name_ja"], s["name_en"], s["category"]) == (
        "TC2632", "2626", "スリゲ", "Surigae", "TY")
    assert s["issue"] == "2026-09-27T22:45:00+09:00"
    assert s["now"] == {"lat": 26.0, "lon": 129.3, "pressure_hpa": 945, "max_wind_ms": 50,
                        "time": "2026-09-27T22:00:00+09:00"}
    assert [t["hours"] for t in s["track"]] == [1, 2, 5, 8, 11, 14, 17, 20, 23, 47, 71, 95, 119]
    assert s["track"][0]["circle_km"] is None  # 推定 (estimate) has no 予報円
    assert s["track"][1]["circle_km"] == 31.5  # 31484 m
    assert s["track"][-1] == {"hours": 119, "time": "2026-10-02T21:00:00+09:00", "lat": 39.6,
                              "lon": 150.1, "circle_km": 463.0}
    past = s["past_track"]
    assert len(past) <= 60 and past[0] == [14.3, 140.7] and past[-1] == [26.0, 129.3]
    assert all(a != b for a, b in zip(past, past[1:], strict=False))
    # The tests' place is 1466.9 km from (26.0, 129.3) by a plain haversine.
    assert s["distance_km"] == round(haversine_km(26.0, 129.3, *HOME)) == 1467
    expect = min(s["track"], key=lambda t: haversine_km(t["lat"], t["lon"], *HOME))
    assert expect["hours"] == 95  # nearest CENTRE: 374 km at +95 h
    # The nearest point on the track line lies between the +71 h and +95 h centres. On the fixture's polyline
    # it is 280.3 km away, at 86.44 h, circle 344.0 km, 33.96 N 142.01 E; the plane projection of
    # closest_approach differs by ~0.1 h / 3 min.
    assert s["closest"] == {"distance_km": 280, "time": "2026-10-01T12:29:00+09:00", "hours": 86.5,
                            "circle_km": 344.1, "lat": 33.97, "lon": 142.02, "between": [71, 95],
                            "interpolated": True}
    assert len(json.dumps([s], ensure_ascii=False).encode()) < 12 * 1024


def test_storm_closest_at_a_known_point():
    s = _storm(home=(28.9, 135.5))  # exactly the +47 h centre
    assert s["closest"] == {"distance_km": 0, "time": "2026-09-29T21:00:00+09:00", "hours": 47.0,
                            "circle_km": 157.4, "lat": 28.9, "lon": 135.5, "between": [23, 47],
                            "interpolated": False}


def test_storm_long_past_track_is_thinned_and_small():
    forecast = clone(load_json("typhoon_TC2632_forecast.json"))
    now = next(p for p in forecast if p.get("advancedHours") == 0)
    now["track"]["typhoon"] = [[10 + i * 0.03, 150 - i * 0.03] for i in range(500)]
    tc = parse_target_tc(load_json("typhoon_targetTc.json"))[0]
    s = parse_storm(tc, forecast, load_json("typhoon_TC2632_specs.json"), HOME)
    assert len(s["past_track"]) == 60
    assert len(json.dumps([s, s], ensure_ascii=False).encode()) < 12 * 1024  # two storms


def test_many_storms_fit_under_12_kb():
    forecast = clone(load_json("typhoon_TC2632_forecast.json"))
    now = next(p for p in forecast if p.get("advancedHours") == 0)
    now["track"]["typhoon"] = [[10 + i * 0.03, 150 - i * 0.03] for i in range(500)]
    tc = parse_target_tc(load_json("typhoon_targetTc.json"))[0]
    one = parse_storm(tc, forecast, load_json("typhoon_TC2632_specs.json"), HOME)
    five = [{**one, "id": f"TC26{i:02d}"} for i in range(5)]
    assert len(json.dumps(five, ensure_ascii=False).encode()) > 12 * 1024
    fitted = fit_storms(five)
    assert len(json.dumps(fitted, ensure_ascii=False).encode()) < 12 * 1024
    assert len(fitted) == 5  # thinner past tracks are enough for five
    assert all(s["track"] == one["track"] and s["past_track"][-1] == one["past_track"][-1] for s in fitted)
    # eight at once: the farthest are left out, the nearest kept, original order kept
    eight = [{**one, "id": f"TC26{i:02d}", "distance_km": 1000 + (i * 37) % 8 * 100} for i in range(8)]
    fitted = fit_storms(eight)
    assert len(json.dumps(fitted, ensure_ascii=False).encode()) < 12 * 1024
    kept = {s["id"] for s in fitted}
    assert 0 < len(kept) < 8
    nearest = sorted(eight, key=lambda s: s["distance_km"])[: len(kept)]
    assert kept == {s["id"] for s in nearest}
    assert [s["id"] for s in fitted] == [s["id"] for s in eight if s["id"] in kept]
    assert fit_storms([one]) == [one]  # one storm is left alone


def test_thin_keeps_ends():
    pts = list(range(100))
    t = thin(pts, 60)
    assert len(t) == 60 and t[0] == 0 and t[-1] == 99 and t == sorted(t)
    assert thin(pts[:10], 60) == pts[:10]


def test_storm_bad_payloads():
    tc = {"id": "TC0000"}
    with pytest.raises(JmaParseError):
        parse_storm(tc, [], load_json("typhoon_TC2632_specs.json"), HOME)
    with pytest.raises(JmaParseError):
        parse_storm(tc, [{"part": "title"}], load_json("typhoon_TC2632_specs.json"), HOME)


# ---------- WBGT ----------

def test_wbgt_primary_row_x10_and_todays_max():
    csv = load_text("wbgt_yohou_tokyo.csv")
    w = parse_wbgt(csv, ("44132", "62016"), date(2026, 9, 28))
    assert w.station == "44132"
    assert w.issued == "2026-09-27T22:25:00+09:00"
    assert len(w.series) == 17
    # The last hour of a day, "24", is 00:00 of the next day, counted in the first day's column
    assert w.series[0] == {"time": "2026-09-28T00:00:00+09:00", "wbgt": 20.0}
    assert w.today_max == 22.0  # 44132 on the second day: 210 200 210 220 210 200 200 200
    assert parse_wbgt(csv, ("44132",), date(2026, 9, 27)).today_max == 20.0  # only the 24時 slot
    assert parse_wbgt(csv, ("44132",), date(2026, 9, 26)).today_max is None


def test_wbgt_fallback_missing_and_empty():
    csv = load_text("wbgt_yohou_tokyo.csv")
    w = parse_wbgt(csv, ("99999", "44132"), date(2026, 9, 29))
    assert w.station == "44132" and w.today_max == 21.0  # 210 210 210 180 170 160 160 170
    assert parse_wbgt(csv, ("99999", "88888"), date(2026, 9, 28)) is None
    assert parse_wbgt("", ("44132",), date(2026, 9, 28)) is None
    assert parse_wbgt(None, ("44132",), date(2026, 9, 28)) is None
    assert parse_wbgt(csv.splitlines()[0], ("44132",), date(2026, 9, 28)) is None
    assert parse_wbgt("<html>対象外</html>\n<body>", ("44132",), date(2026, 9, 28)) is None
    blank = csv.replace("44132,2026/09/27 22:25, 200,", "44132,2026/09/27 22:25,    ,")
    w = parse_wbgt(blank, ("44132",), date(2026, 9, 28))
    assert w.series[0]["wbgt"] is None and w.series[1]["wbgt"] == 21.0


def test_wbgt_station_row_with_no_figure_in_any_slot_is_no_data():
    header = load_text("wbgt_yohou_tokyo.csv").splitlines()[0]
    slots = len(header.split(",")) - 2  # the header starts with two empty cells
    blank = [""] * slots
    today = date(2026, 9, 28)
    assert parse_wbgt(f"{header}\n44132,2026/09/27 22:25,{','.join(blank)}\n", ("44132",), today) is None
    one_figure = [*blank[:3], "210", *blank[4:]]  # only the fourth slot, 09:00 on the 28th
    w = parse_wbgt(f"{header}\n44132,2026/09/27 22:25,{','.join(one_figure)}\n", ("44132",), today)
    assert w is not None and len(w.series) == slots == 17
    assert w.series[3] == {"time": "2026-09-28T09:00:00+09:00", "wbgt": 21.0}
    assert w.today_max == 21.0 and sum(1 for point in w.series if point["wbgt"] is not None) == 1


def test_wbgt_issue_time_in_another_format_is_left_out_but_the_figures_are_kept():
    csv = load_text("wbgt_yohou_tokyo.csv").replace("44132,2026/09/27 22:25,", "44132,2026-09-27T22:25,")
    assert "44132,2026-09-27T22:25," in csv  # the edit took
    w = parse_wbgt(csv, ("44132",), date(2026, 9, 28))
    assert w.station == "44132" and w.issued is None
    assert len(w.series) == 17 and w.today_max == 22.0  # the same figures as the unedited file


# ---------- map layers ----------

def test_n3_thunder_and_tornado_frames():
    n3 = load_json("targetTimes_N3_b.json")
    thns = n3_frames(n3, "thns")
    assert len(thns) == 13
    assert thns[0] == {"basetime": "20260927130000", "validtime": "20260927130000"}
    assert thns[6] == {"basetime": "20260927140000", "validtime": "20260927140000"}
    assert thns[-1] == {"basetime": "20260927140000", "validtime": "20260927150000"}
    assert all(f["validtime"][10:12] in {"00", "10", "20", "30", "40", "50"} for f in thns)  # no liden-only 5-min rows
    assert n3_frames(n3, "trns") == thns
    with pytest.raises(JmaParseError):
        n3_frames([{"basetime": "20260927140500", "validtime": "20260927140500", "elements": ["liden"]}], "thns")


def test_risk_newest_seven():
    frames = risk_frames(load_json("risk_targetTimes.json"))
    assert len(frames) == 7
    assert frames[-1] == {"basetime": "20260927140000", "member": "immed0", "validtime": "20260927140000"}
    assert [f["member"] for f in frames] == ["none"] * 4 + ["immed2", "immed1", "immed0"]
    assert [f["validtime"] for f in frames] == sorted(f["validtime"] for f in frames)


def test_rasrf_frames_follow_the_newest_run_per_validtime():
    base, frames = rasrf_frames(load_json("rasrf_targetTimes.json"))
    assert base == "20260927140000"
    assert len(frames) == 13
    assert frames[0] == {"basetime": "20260927140000", "member": "immed", "validtime": "20260927160000"}
    assert frames[4] == {"basetime": "20260927140000", "member": "immed", "validtime": "20260927200000"}
    assert frames[5] == {"basetime": "20260927130000", "member": "none", "validtime": "20260927210000"}
    assert frames[-1]["validtime"] == "20260928040000"
    assert "20260927150000" not in {f["validtime"] for f in frames}  # +1 h = the nowcast's hour
    base2, frames2 = rasrf_frames(load_json("rasrf_targetTimes_b.json"))
    assert base2 == "20260927150000" and len(frames2) == 14
    assert {f["member"] for f in frames2} == {"none"} and frames2[0]["validtime"] == "20260927170000"
    with pytest.raises(JmaParseError):
        rasrf_frames([{"basetime": "20260927140000", "validtime": "20260927140000",
                       "member": "none", "elements": ["sjfcstmap"]}])


def test_himawari_ten_minute_grid():
    frames = himawari_frames(load_json("himawari_targetTimes_jp.json"))
    assert len(frames) == 12
    assert frames[-1]["validtime"] == "20260927140000"
    assert frames[0]["validtime"] == "20260927121000"
    times = [datetime.strptime(f["validtime"], "%Y%m%d%H%M%S") for f in frames]
    assert all(t.minute % 10 == 0 and t.second == 0 for t in times)  # 2.5-min rapid scans dropped
    assert all(b - a == timedelta(minutes=10) for a, b in zip(times, times[1:], strict=False))


def _all_layers() -> dict:
    n3 = load_json("targetTimes_N3_b.json")
    return layers_attribute(
        n3={"thns": n3_frames(n3, "thns"), "trns": n3_frames(n3, "trns")},
        risk=risk_frames(load_json("risk_targetTimes.json")),
        rasrf=rasrf_frames(load_json("rasrf_targetTimes.json")),
        himawari=himawari_frames(load_json("himawari_targetTimes_jp.json")),
    )


def test_layers_attribute_shape_and_size():
    layers = _all_layers()
    assert list(layers) == ["thns", "trns", "risk_inund", "risk_land", "risk_flood", "rasrf",
                            "himawari_ir", "flood_hazard"]
    assert layers["risk_flood"]["template"].endswith(
        "/risk/{basetime}/{member}/{validtime}/surf/flood_mesh/{z}/{x}/{y}.png")
    assert layers["rasrf"]["template"] == URL_RASRF_TILE
    assert (layers["thns"]["zmax"], layers["risk_land"]["zmax"], layers["rasrf"]["zmax"]) == (8, 11, 10)
    assert (layers["himawari_ir"]["zmin"], layers["himawari_ir"]["zmax"]) == (3, 6)
    assert layers["flood_hazard"] == {"template": URL_GSI_FLOOD_TILE, "static": True, "zmax": 14,
                                      "attribution": "出典：「ハザードマップポータルサイト」"}
    for layer in layers.values():
        assert "{z}/{x}/{y}" in layer["template"]
    size = len(json.dumps(layers, ensure_ascii=False).encode())
    assert size < 16 * 1024, size


def test_one_failing_list_keeps_its_last_good_and_drops_nothing_else():
    t0 = datetime(2026, 9, 27, 14, 10, tzinfo=UTC)
    max_age = timedelta(minutes=15)  # 3 x 5 min
    sources = {k: LastGood(max_age) for k in ("n3", "risk", "rasrf", "himawari")}
    good = {
        "n3": {"thns": n3_frames(load_json("targetTimes_N3_b.json"), "thns"),
               "trns": n3_frames(load_json("targetTimes_N3_b.json"), "trns")},
        "risk": risk_frames(load_json("risk_targetTimes.json")),
        "rasrf": rasrf_frames(load_json("rasrf_targetTimes.json")),
        "himawari": himawari_frames(load_json("himawari_targetTimes_jp.json")),
    }
    for k, v in good.items():
        sources[k].ok(v, t0)
    for minutes in (5, 10):  # risk fails, the others answer
        now = t0 + timedelta(minutes=minutes)
        sources["risk"].fail(JmaParseError("risk targetTimes: not a list"))
        for k in ("n3", "rasrf", "himawari"):
            sources[k].ok(good[k], now)
        layers = layers_attribute(**{k: s.get(now) for k, s in sources.items()})
        assert "risk_inund" in layers and "thns" in layers  # risk keeps its last good list
    now = t0 + timedelta(minutes=15)
    for k in ("n3", "rasrf", "himawari"):
        sources[k].ok(good[k], now)
    layers = layers_attribute(**{k: s.get(now) for k, s in sources.items()})
    assert "risk_inund" not in layers  # too old now: dropped, the others stay
    assert {"thns", "trns", "rasrf", "himawari_ir", "flood_hazard"} <= set(layers)
    assert sources["risk"].last_error == "risk targetTimes: not a list"
    assert list(layers_attribute()) == ["flood_hazard"]


# ---------- キキクル at the configured place ----------

def test_kikikuru_palette_exact_nearest_transparent():
    px = {
        (10, 10): (255, 255, 255, 255), (20, 10): (242, 231, 0, 255), (30, 10): (255, 40, 0, 255),
        (40, 10): (170, 0, 170, 255), (50, 10): (12, 0, 12, 255), (60, 10): (60, 255, 255, 255),
        (70, 10): (250, 225, 10, 255),  # unlisted: nearest #f2e700 -> 2
        (80, 10): (170, 0, 170, 100),  # mostly transparent -> 0
    }
    png = _png(px)
    level = {x: kikikuru_level_at(png, x, 10, "land", radius=0) for x in range(10, 90, 10)}
    # #3cffff (60) is flood_mesh's level 1 and on no land colour's doorstep: unknown (0), not the nearest (white)
    assert level == {10: 1, 20: 2, 30: 3, 40: 4, 50: 5, 60: 0, 70: 2, 80: 0}
    assert kikikuru_level_at(png, 60, 10, "flood_mesh", radius=0) == 1  # #3cffff is flood L1
    assert kikikuru_level_at(png, 50, 10, "flood_mesh", radius=0) == 5
    assert kikikuru_level_at(png, 42, 12, "inund") == 4  # 5x5 block reaches (40, 10)
    assert kikikuru_level_at(png, 200, 200, "inund") == 0
    with pytest.raises(JmaParseError):
        kikikuru_level_at(b"not a png", 1, 1, "land")


def test_kikikuru_fixture_tile_and_reading():
    x, y, px, py = tile_for(*HOME, 10)
    assert (x, y) == (909, 403)
    assert kikikuru_level_at(load_bytes("tile_risk_transparent_z10.png"), px, py, "inund") == 0
    assert Kikikuru("20260927140000", 0, 2, None).level == 2
    assert Kikikuru("20260927140000", None, None, None).level is None


# ---------- rasrf at the configured place ----------

def test_rain_outlook_series_and_first_rain():
    frames = [("20260927160000", 1), ("20260927170000", None), ("20260927180000", 3),
              ("20260927190000", 2), ("20260927200000", 0)]
    ro = rain_outlook("20260927140000", frames)
    assert ro.basetime == "20260927140000"
    assert [s["band"] for s in ro.series] == [1, None, 3, 2, 0]
    assert ro.series[2] == {"time": "2026-09-27T18:00:00+00:00", "band": 3, "mmh_upper": 10}
    assert ro.first_rain == "2026-09-27T18:00:00+00:00"  # band 1 (<1 mm/h) does not count
    assert ro.max_mmh == 10
    dry = rain_outlook("20260927140000", [("20260927160000", 0), ("20260927170000", 1)])
    assert dry.first_rain is None and dry.max_mmh == 1
    assert rain_outlook("20260927140000", []).max_mmh is None


def test_rasrf_fixture_tile_uses_the_hrpns_palette():
    _x, _y, px, py = tile_for(*HOME, 10)
    png = load_bytes("tile_rasrf_z10_sample.png")
    assert band_at(png, px, py) == 1  # #F2F2FF, < 1 mm/h
    bands = {band_at(png, x, y, radius=0) for x in range(0, 256, 8) for y in range(0, 256, 8)}
    assert bands == {0, 1, 2}
