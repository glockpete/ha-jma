"""r8 warnings parser against samples of the published files."""

from __future__ import annotations

import pytest

from custom_components.jma.errors import JmaParseError
from custom_components.jma.parse_warnings import (
    Warning,
    diff_warnings,
    parse_area_warnings,
    stale_bulletins,
    warning_names,
)

from .conftest import AREA
from .helpers import clone, load_json, load_text


def _sample_kind(code: str = "03", status: str = "発表") -> dict:
    """A kind object from the nationwide r8 map sample (a bulletin whose items carry properties)."""
    for record in load_json("warning_r8_map.json"):
        for item in record["warning"]["class20Items"]:
            for kind in item["kinds"]:
                if kind.get("code") == code and kind.get("status") == status:
                    return clone(kind)
    raise AssertionError("no such kind in the map fixture")


def _with_chiyoda(kinds_by_type: dict[str, list[dict]]) -> list:
    records = clone(load_json("warning_r8_130000.json"))
    for record in records:
        if record["dataTypeCode"] in kinds_by_type:
            for item in record["warning"]["class20Items"]:
                if item["areaCode"] == AREA:
                    item["kinds"] = kinds_by_type[record["dataTypeCode"]]
    return records


def test_quiet_chiyoda():
    aw = parse_area_warnings(load_json("warning_r8_130000.json"), AREA)
    assert aw.level == 0
    assert aw.warnings == []
    assert not aw.area_missing
    assert set(aw.control_times) == {"VPWW55", "VPWW56", "VPWW58", "VPWW59", "VPWW61"}
    assert aw.control_times["VPWW61"] == "2026-09-25T19:02:36Z"
    assert aw.report_time == "2026-09-26T04:02:00+09:00"


def test_level3_rain_kind_from_the_map_sample():
    aw = parse_area_warnings(_with_chiyoda({"VPWW55": [_sample_kind("03")]}), AREA)
    assert aw.level == 3
    (w,) = aw.warnings
    assert (w.phenomenon, w.level, w.status, w.data_type) == ("rain", 3, "発表", "VPWW55")
    assert w.name_ja == "レベル３大雨警報"
    assert w.name_en == "Level 3 heavy rain warning"
    assert "properties" in w.extra


def test_continuing_counts_cleared_does_not():
    kinds = [{"code": "03", "status": "継続"}, {"code": "14", "status": "解除"},
             {"status": "発表警報・注意報はなし"}]
    aw = parse_area_warnings(_with_chiyoda({"VPWW55": kinds}), AREA)
    assert [(w.code, w.status) for w in aw.warnings] == [("03", "継続")]


def test_two_levels_of_one_phenomenon_take_the_max():
    aw = parse_area_warnings(
        _with_chiyoda({"VPWW55": [{"code": "10", "status": "発表"}],
                      "VPWW56": [{"code": "43", "status": "発表"}]}), AREA)
    assert aw.level == 4
    assert [w.code for w in aw.warnings] == ["43", "10"]


def test_missing_area_is_level0_not_a_crash():
    aw = parse_area_warnings(load_json("warning_r8_130000.json"), "9999999")
    assert aw.area_missing and aw.level == 0 and aw.warnings == []


def test_a_stale_file_is_a_parse_error():
    with pytest.raises(JmaParseError):
        parse_area_warnings(load_json("warning_stale_130000.json"), AREA)
    with pytest.raises(JmaParseError):
        parse_area_warnings("<html></html>", AREA)


@pytest.mark.parametrize(
    ("phenomenon", "level", "ja", "en"),
    [
        ("landslide", 4, "レベル４土砂災害危険警報", "Level 4 landslide danger warning"),
        ("rain", 5, "レベル５大雨特別警報", "Level 5 heavy rain emergency warning"),
        ("wind", 2, "強風注意報", "Strong wind advisory"),
        ("wind", 3, "暴風警報", "Storm warning"),
        ("thunder", 2, "雷注意報", "Thunderstorm advisory"),
    ],
)
def test_names(phenomenon, level, ja, en):
    assert warning_names(phenomenon, level) == (ja, en)


def _w(phenomenon: str, level: int, code: str = "x") -> Warning:
    ja, en = warning_names(phenomenon, level)
    return Warning(code, phenomenon, level, "発表", ja, en, "VPWW55", None)


def test_diff_all_four_kinds():
    assert [c.event_type for c in diff_warnings([], [_w("rain", 3)])] == ["warning_issued"]
    raised = diff_warnings([_w("rain", 2)], [_w("rain", 3)])
    assert [(c.event_type, c.old_level, c.new_level) for c in raised] == [("warning_raised", 2, 3)]
    lowered = diff_warnings([_w("rain", 4)], [_w("rain", 3)])
    assert [(c.event_type, c.old_level, c.new_level) for c in lowered] == [("warning_lowered", 4, 3)]
    cleared = diff_warnings([_w("rain", 3)], [])
    assert [(c.event_type, c.old_level, c.new_level) for c in cleared] == [("warning_cleared", 3, 0)]
    assert diff_warnings([_w("wind", 3)], [_w("wind", 3)]) == []


def test_stale_check_today_is_clean():
    aw = parse_area_warnings(load_json("warning_r8_130000.json"), AREA)
    # extra.xml has H27 VPWW53/54 for 130000 at 01:57Z with no r8 change: must not count
    assert stale_bulletins(load_text("extra.xml"), "130000", aw.control_times) == []


def test_stale_check_catches_a_lagging_bulletin():
    aw = parse_area_warnings(load_json("warning_r8_130000.json"), AREA)
    lagging = dict(aw.control_times, VPWW61="2026-09-25T18:00:00Z")
    assert stale_bulletins(load_text("extra.xml"), "130000", lagging) == ["VPWW61"]


def test_stale_check_with_no_json_at_all():
    # A file that cannot be parsed gives no control times.
    assert stale_bulletins(load_text("extra.xml"), "130000", {}) == ["VPWW61"]


def test_stale_check_bad_xml():
    with pytest.raises(JmaParseError):
        stale_bulletins("<not xml", "130000", {})


def test_headline_comes_from_the_warnings_own_bulletin():
    records = clone(load_json("warning_r8_130000.json"))
    own, other = records[0], records[1]
    for item in own["warning"]["class20Items"]:
        if item["areaCode"] == AREA:
            item["kinds"] = [_sample_kind("03")]
    own["headlineText"], own["reportDatetime"] = "千代田区に大雨警報", "2026-09-26T10:00:00+09:00"
    other["headlineText"], other["reportDatetime"] = "伊豆諸島に雷注意報", "2026-09-26T11:00:00+09:00"
    area = parse_area_warnings(records, AREA)
    (w,) = [w for w in area.warnings if w.phenomenon == "rain"]
    assert area.headlines[w.data_type] == "千代田区に大雨警報"
