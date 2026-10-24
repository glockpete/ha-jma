"""The 環境省 WBGT points: the shipped table against its source, the office-to-file map, the nearest point."""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest

from custom_components.jma.const import PREFECTURES, WBGT_NEAREST_POINTS, WBGT_PREF_FILE
from custom_components.jma.parse_typhoon import haversine_km
from custom_components.jma.parse_wbgt import nearest_points, parse_wbgt, pref_file_for_office
from custom_components.jma.wbgt_points import WBGT_POINTS

from .conftest import LAT, LON
from .helpers import load_json, load_text

ROOT = Path(__file__).resolve().parents[1]
CAPTURED_FILES = ("hokkaido", "kagoshima", "okinawa", "osaka")  # whole prefecture files, captured 2026-10-25


def _generator():
    """scripts/gen_wbgt_points.py, loaded by path (scripts/ is not a package)."""
    spec = importlib.util.spec_from_file_location("gen_wbgt_points", ROOT / "scripts" / "gen_wbgt_points.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_shipped_table_is_the_ministrys_point_master_rendered_again() -> None:
    """tests/fixtures/wbgt_point_master.csv is the Ministry's file as fetched on 2026-10-25; the module is what
    scripts/gen_wbgt_points.py makes of it. A hand edit of the module, or a new master, fails here."""
    generated = _generator().render(load_text("wbgt_point_master.csv"), PREFECTURES, WBGT_PREF_FILE)
    shipped = (ROOT / "custom_components" / "jma" / "wbgt_points.py").read_text(encoding="utf-8")
    assert generated == shipped


def test_the_generator_reads_the_prefecture_tables_of_const_and_writes_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    gen = _generator()
    assert gen.load_const() == (PREFECTURES, WBGT_PREF_FILE)
    assert gen.main(["gen"]) == 2  # no master named: the usage text, nothing written
    assert gen.main(["gen", str(ROOT / "tests" / "fixtures" / "wbgt_point_master.csv")]) == 0
    out = capsys.readouterr().out
    assert out.count('\n        ("') == 841 and out.endswith("}\n")


def test_the_table_has_841_points_in_47_prefecture_files() -> None:
    numbers = [number for points in WBGT_POINTS.values() for number, *_ in points]
    assert len(numbers) == len(set(numbers)) == 841
    assert list(WBGT_POINTS) == list(WBGT_PREF_FILE.values())  # every file name has points, in prefecture order
    assert set(PREFECTURES) == set(WBGT_PREF_FILE)
    for points in WBGT_POINTS.values():
        assert [p[0] for p in points] == sorted(p[0] for p in points)
        for number, name, lat, lon in points:
            assert number.isdigit() and len(number) == 5 and name == name.strip() and name
            assert 24 < lat < 46 and 122 < lon < 146  # Yonaguni to Minami-Torishima, Hokkaido's north cape


@pytest.mark.parametrize("name", CAPTURED_FILES)
def test_a_captured_prefecture_file_lists_exactly_the_points_of_its_table(name: str) -> None:
    """The row keys of a file captured from the Ministry on 2026-10-25 are the table's points for that file."""
    rows = load_text(f"wbgt_yohou_{name}.csv").splitlines()[1:]
    assert sorted(row.split(",")[0] for row in rows if row) == [p[0] for p in WBGT_POINTS[name]]


def test_the_trimmed_tokyo_sample_only_holds_points_of_the_tokyo_file() -> None:
    rows = load_text("wbgt_yohou_tokyo.csv").splitlines()[1:]
    tokyo = {p[0] for p in WBGT_POINTS["tokyo"]}
    assert {row.split(",")[0] for row in rows if row} <= tokyo


def test_every_forecast_office_of_the_area_list_reads_a_prefecture_file_with_points() -> None:
    offices = load_json("area.json")["offices"]
    assert len(offices) == 58
    for code in offices:
        pref = pref_file_for_office(code)
        assert pref in WBGT_POINTS and WBGT_POINTS[pref], code
        assert PREFECTURES[code[:2]]  # the same two digits name the prefecture
    # the offices that are not a whole prefecture
    assert {pref_file_for_office(c) for c in ("011000", "012000", "013000", "014030", "014100", "015000", "016000",
                                              "017000")} == {"hokkaido"}
    assert pref_file_for_office("460040") == pref_file_for_office("460100") == "kagoshima"
    assert {pref_file_for_office(c) for c in ("471000", "472000", "473000", "474000")} == {"okinawa"}
    assert pref_file_for_office("130000") == "tokyo" and pref_file_for_office("270000") == "osaka"


@pytest.mark.parametrize("office", [None, 130000, "", "9", "990000", "ab0000", b"130000"])
def test_a_value_that_is_not_an_office_code_has_no_file(office: object) -> None:
    assert pref_file_for_office(office) is None


def at(number: str) -> tuple[float, float]:
    """A place 1.4 km from the Ministry's own position of a point (0.01 degree each way): the tests' homes are
    derived from the agency's table, not from addresses."""
    (_, _, lat, lon) = next(p for points in WBGT_POINTS.values() for p in points if p[0] == number)
    return lat + 0.01, lon + 0.01


@pytest.mark.parametrize(
    ("office", "number", "name"),
    [
        ("130000", "44132", "東京"),  # Tokyo (the 23 wards)
        ("130000", "44263", "八丈島"),  # an island of the same office
        ("270000", "62078", "大阪"),
        ("016000", "14163", "札幌"),  # Hokkaido: seven offices, one file of 163 points
        ("014030", "20432", "帯広"),
        ("471000", "91197", "那覇"),
        ("474000", "94081", "石垣島"),
        ("460040", "88837", "名瀬"),  # the Amami office reads Kagoshima's file
        ("460100", "88317", "鹿児島"),
    ],
)
def test_the_nearest_point_of_a_place_is_the_point_at_that_place(office: str, number: str, name: str) -> None:
    lat, lon = at(number)
    points = nearest_points(pref_file_for_office(office), lat, lon)
    assert (points[0].number, points[0].name) == (number, name)
    assert 1.2 < points[0].km < 1.6
    assert len(points) == WBGT_NEAREST_POINTS == 3
    assert [p.km for p in points] == sorted(p.km for p in points)
    assert len({p.number for p in points}) == 3


def test_tokyo_station_reads_the_tokyo_point_as_before() -> None:
    """The place of the tests, Tokyo Station, is served by 44132, the point the one-office version read."""
    assert nearest_points(pref_file_for_office("130000"), LAT, LON)[0].number == "44132"


def test_the_distance_is_the_great_circle_distance_to_the_point_rounded_to_a_tenth_of_a_km() -> None:
    first = nearest_points("tokyo", LAT, LON)[0]
    (_, _, plat, plon) = next(p for p in WBGT_POINTS["tokyo"] if p[0] == first.number)
    assert first.km == round(haversine_km(LAT, LON, plat, plon), 1) == 1.9


def test_a_place_outside_the_prefecture_still_gets_the_nearest_point_of_the_file() -> None:
    """The home of Home Assistant can lie anywhere; the points are those of the office's file and the distance says
    how far the nearest is (the sensor shows it as `station_km`)."""
    point = nearest_points("osaka", LAT, LON)[0]
    assert point.number in {p[0] for p in WBGT_POINTS["osaka"]} and 350 < point.km < 450


def test_count_limits_the_list_and_an_unknown_file_has_no_points() -> None:
    assert [p.number for p in nearest_points("tokyo", LAT, LON, 1)] == ["44132"]
    assert len(nearest_points("tokyo", LAT, LON, 99)) == len(WBGT_POINTS["tokyo"]) == 11
    assert nearest_points("tokyo", LAT, LON, 0) == ()
    assert nearest_points("atlantis", LAT, LON) == ()
    assert nearest_points(None, LAT, LON) == ()


@pytest.mark.parametrize(("name", "number", "first_day_max", "rows"), [
    ("osaka", "62078", 20.0, 6), ("okinawa", "94081", 26.0, 16), ("kagoshima", "88837", 25.0, 27),
    ("hokkaido", "20432", 10.0, 163),
])
def test_the_captured_files_parse_for_their_nearest_point(name: str, number: str, first_day_max: float,
                                                          rows: int) -> None:
    """The end-of-season captures (issued 2026-10-22 00:25; 24 slots from 22 October 03:00 to 25 October 00:00)."""
    text = load_text(f"wbgt_yohou_{name}.csv")
    assert len(text.splitlines()) == rows + 1
    forecast = parse_wbgt(text, (number,), date(2026, 10, 22))
    assert forecast is not None and forecast.station == number
    assert forecast.issued == "2026-10-22T00:25:00+09:00"
    assert len(forecast.series) == 24 and forecast.series[0]["time"] == "2026-10-22T03:00:00+09:00"
    assert forecast.series[-1]["time"] == "2026-10-25T00:00:00+09:00"
    assert forecast.today_max == first_day_max
    # The last slot, 00:00 on the 25th, is the 24時 slot of the 24th; no slot belongs to a later day.
    assert parse_wbgt(text, (number,), date(2026, 10, 24)).today_max is not None
    assert parse_wbgt(text, (number,), date(2026, 10, 25)).today_max is None
