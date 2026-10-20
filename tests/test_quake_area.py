"""The area tier of the earthquake part: prefecture -> area -> local.

An entry in one of the 23 special wards of Tokyo is scoped to the area 東京都２３区: an `earthquake` event and an
early warning (`eew`) need a ward (or an area-level report of the 23 wards), not just any point of the prefecture.
Everywhere else (and for a Tokyo address outside the 23 wards, e.g. Hachioji) there is no area and the scope stays
the prefecture.

The expected names and codes are read from the agency's own list (tests/fixtures/area.json); the address forms
are the ones in tests/fixtures/p2p_551_sample_b.json (a real P2P history sample).
"""

from __future__ import annotations

import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components import jma as jma_package
from custom_components.jma import const as jma_const
from custom_components.jma.const import CONF_AREA_NAME_JA, DOMAIN
from custom_components.jma.parse_quake import (
    EewTracker,
    QuakePlace,
    QuakeTracker,
    eew_area_matches,
    parse_551,
)
from custom_components.jma.quake_ws import QuakeClient, quake_event_data

from .conftest import ENTRY_DATA, mock_jma, setup
from .helpers import load_json

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 9, 26, 12, 17, 30, tzinfo=JST)
AREA_23 = "東京都２３区"  # literal on purpose: the test pins the string the agency writes
CHIYODA, SETAGAYA, HACHIOJI, OSAKA_CITY = "1310100", "1311200", "1320100", "2710000"


def _chiyoda() -> QuakePlace:
    return QuakePlace.for_entry("東京都", "千代田区", CHIYODA)


def _setagaya() -> QuakePlace:
    return QuakePlace.for_entry("東京都", "世田谷区", SETAGAYA)


def _hachioji() -> QuakePlace:
    return QuakePlace.for_entry("東京都", "八王子市", HACHIOJI)


def _osaka() -> QuakePlace:
    return QuakePlace.for_entry("大阪府", "大阪市", OSAKA_CITY)


def _551(points: list[dict], qid: str = "q1", when: str = "2026/09/26 12:17:00", hypo: str = "茨城県南部") -> dict:
    return {
        "code": 551, "id": qid, "issue": {"type": "DetailScale", "time": "2026/09/26 12:20:39"},
        "earthquake": {"time": when, "maxScale": 50, "domesticTsunami": "None",
                       "hypocenter": {"name": hypo, "magnitude": 5.0, "depth": 40}},
        "points": points,
    }


def _pt(pref: str, addr: str, scale: int) -> dict:
    return {"pref": pref, "addr": addr, "isArea": False, "scale": scale}


def _chiyoda_pt(scale: int) -> dict:
    return _pt("東京都", "東京千代田区丸の内", scale)


def _setagaya_pt(scale: int) -> dict:
    return _pt("東京都", "東京世田谷区成城", scale)


def _area_pt(scale: int) -> dict:
    return _pt("東京都", "東京都２３区", scale)  # the area-level 震度速報 row: the whole 23 wards, no ward named


def _tama_pt(scale: int) -> dict:
    return _pt("東京都", "東京八王子市元本郷町", scale)


def _island_pt(scale: int) -> dict:
    return _pt("東京都", "東京大島町元町", scale)


# ---------- the table of the 23 wards ----------

def test_the_ward_table_is_the_23_wards_of_the_agencys_list_name_for_name_and_code_for_code() -> None:
    area_json = load_json("area.json")
    class15s, class20s = area_json["class15s"], area_json["class20s"]
    assert class15s["130011"]["name"] == "２３区西部" and class15s["130012"]["name"] == "２３区東部"
    from_children = {code: class20s[code]["name"]
                     for key in ("130011", "130012") for code in class15s[key]["children"]}
    from_parents = {code: row["name"] for code, row in class20s.items() if row["parent"] in ("130011", "130012")}
    assert from_children == from_parents and len(from_children) == 23
    assert jma_const.TOKYO_23_WARDS == from_children
    assert jma_const.TOKYO_23_WARDS[CHIYODA] == "千代田区" and jma_const.TOKYO_23_WARDS[SETAGAYA] == "世田谷区"


def test_the_area_name_and_the_ward_names_are_stable_under_nfkc() -> None:
    assert jma_const.AREA_TOKYO_23 == AREA_23
    assert unicodedata.normalize("NFKC", AREA_23) == "東京都23区"
    for name in jma_const.TOKYO_23_WARDS.values():  # is_area compares the NFKC form of an address with them
        assert unicodedata.normalize("NFKC", name) == name


# ---------- QuakePlace ----------

def test_for_entry_sets_the_area_for_a_ward_and_only_for_a_ward() -> None:
    chiyoda = _chiyoda()
    assert chiyoda.pref == "東京都" and chiyoda.local_name == "千代田区"
    assert chiyoda.area == AREA_23 and chiyoda.scoped_to_area is True
    assert chiyoda.area_members == tuple(jma_const.TOKYO_23_WARDS.values()) and len(chiyoda.area_members) == 23
    for place in (_hachioji(), _osaka(), QuakePlace.for_entry("東京都", None, None)):
        assert place.area is None and place.area_members == () and place.scoped_to_area is False
    every_ward = {code: QuakePlace.for_entry("東京都", name, code) for code, name in jma_const.TOKYO_23_WARDS.items()}
    assert all(p.area == AREA_23 for p in every_ward.values())
    assert QuakePlace.for_entry("東京都", "千代田区", ["1310100"]).area is None  # not a code: no area, no crash
    assert QuakePlace("東京都", "千代田区") == QuakePlace.for_entry("東京都", "千代田区", "9999999")


@pytest.mark.parametrize("point_pref, addr, expected", [
    ("東京都", "東京都２３区", True),                    # the area-level row
    ("東京都", "東京都23区", True),                      # half-width digits
    ("東京都", "東京千代田区丸の内", True),                # the P2P station form: 東京 + ward
    ("東京都", "東京都千代田区丸の内", True),              # the full prefecture in front
    ("東京都", "千代田区丸の内", True),                    # no prefecture word at all
    ("東京都", "東京江戸川区船堀", True),
    ("東京都", "東京北区西ヶ原", True),
    ("東京都", "東京八王子市元本郷町", False),          # Tama
    ("東京都", "八王子市堀之内", False),                 # Tama, as P2P writes it (no 東京)
    ("東京都", "東京大島町元町", False),                 # island
    ("東京都", "東京利島村東山", False),
    ("東京都", "青ヶ島村", False),
    ("東京都", "東京都多摩東部", False),                 # the area-level rows of the other parts of Tokyo
    ("東京都", "東京都多摩西部", False),
    ("東京都", "東京国際空港", False),                   # names no ward
    ("東京都", "東京府中市寿町", False),                 # 東京 + 府中市: not a ward after the prefix is removed
    ("東京都", "東京八王子市港区町", False),             # a ward name INSIDE an address does not count
    ("東京都", "東京都港区", True),
    ("東京都", "", False),
    ("東京都", None, False),
    ("東京都", 5, False),
    ("大阪府", "東京千代田区丸の内", False),               # another prefecture
    ("大阪府", "東京都２３区", False),
    (None, "東京千代田区丸の内", False),
])
def test_is_area(point_pref: Any, addr: Any, expected: bool) -> None:
    assert _chiyoda().is_area(point_pref, addr) is expected


def test_is_area_is_startswith_not_in_and_differs_from_is_local() -> None:
    chiyoda = _chiyoda()
    minato = QuakePlace.for_entry("東京都", "港区", "1310300")
    inside = "東京八王子市港区町"
    assert minato.is_local("東京都", inside) is True       # the local match is `in` (documented)
    assert chiyoda.is_area("東京都", inside) is False       # the area match is a prefix match
    assert chiyoda.is_area("東京都", "東京港区芝") is True    # any ward counts for the area, not only the entry's own


def test_a_place_without_an_area_or_without_a_prefecture_has_no_area_points() -> None:
    assert _hachioji().is_area("東京都", "東京千代田区丸の内") is False
    assert _osaka().is_area("大阪府", "大阪北区梅田") is False
    assert QuakePlace.for_entry(None, "千代田区", CHIYODA).is_area("東京都", "東京千代田区丸の内") is False
    assert QuakePlace("東京都", "千代田区").is_area("東京都", "東京都２３区") is False


def test_the_ward_rule_over_the_real_p2p_sample_equals_the_rule_of_0_5_4() -> None:
    """Every Tokyo point of the real sample (ward addresses, the area-level row of the 23 wards, and the Tama and
    island addresses with their area-level rows): the area set is rebuilt here from the 0.5.4 rule."""
    wards = tuple(jma_const.TOKYO_23_WARDS.values())
    chiyoda, seen_ward, seen_other = _chiyoda(), set(), set()
    for msg in load_json("p2p_551_sample_b.json"):
        for point in msg["points"]:
            if point["pref"] != "東京都":
                continue
            addr = point["addr"]
            old_rule = addr == "東京都２３区" or addr.removeprefix("東京").startswith(wards)
            assert chiyoda.is_area(point["pref"], addr) is old_rule, addr
            (seen_ward if old_rule else seen_other).add(addr)
    assert len(seen_ward) >= 60 and len(seen_other) >= 60
    assert "東京都２３区" in seen_ward and "東京都多摩東部" in seen_other and "東京利島村東山" in seen_other


# ---------- parse_551: the area maximum ----------

def test_area_max_scale_is_the_wards_and_the_area_row_only() -> None:
    points = [_chiyoda_pt(30), _setagaya_pt(40), _area_pt(20), _tama_pt(45), _island_pt(50),
              _pt("神奈川県", "横浜緑区十日市場町", 60)]
    q = parse_551(_551(points), _chiyoda())
    assert q.area == AREA_23 and q.area_max_scale == 40
    assert q.pref_max_scale == 50          # the prefecture still includes Tama and the island
    assert q.local_scale == 30             # Chiyoda only
    assert q.scope_max_scale == 40
    assert q.max_scale == 50               # the message's own maximum, unchanged


def test_the_area_row_counts_by_itself() -> None:
    q = parse_551(_551([_area_pt(20), _tama_pt(45)]), _chiyoda())
    assert q.area_max_scale == 20 and q.pref_max_scale == 45 and q.local_scale is None and q.scope_max_scale == 20


def test_tama_and_island_points_alone_leave_the_area_unknown() -> None:
    q = parse_551(_551([_tama_pt(45), _island_pt(50)]), _chiyoda())
    assert q.area == AREA_23 and q.area_max_scale is None and q.pref_max_scale == 50
    assert q.scope_max_scale is None       # the area is the scope: no fall-back to the prefecture


def test_the_entrys_own_ward_gives_the_local_scale_and_any_ward_the_area() -> None:
    q = parse_551(_551([_chiyoda_pt(30), _setagaya_pt(40)]), _setagaya())
    assert q.local_scale == 40 and q.area_max_scale == 40 and q.local_name == "世田谷区"


def test_an_entry_outside_the_23_wards_has_no_area_and_scopes_on_the_prefecture() -> None:
    for place, points, pref_max in (
        (_hachioji(), [_chiyoda_pt(30), _tama_pt(45), _island_pt(50)], 50),
        (_osaka(), [_pt("大阪府", "大阪北区梅田", 30), _pt("大阪府", "堺北区新金岡", 45)], 45),
    ):
        q = parse_551(_551(points), place)
        assert q.area is None and q.area_max_scale is None
        assert q.pref_max_scale == pref_max and q.scope_max_scale == pref_max


def test_a_quake_without_any_point_of_the_prefecture_has_no_scope_scale() -> None:
    for place in (_chiyoda(), _hachioji(), _osaka()):
        q = parse_551(_551([_pt("愛知県", "名古屋北区", 45)]), place)
        assert q.scope_max_scale is None and q.area_max_scale is None and q.pref_max_scale is None


def test_the_quake_dict_carries_area_and_area_max_scale() -> None:
    q = parse_551(_551([_setagaya_pt(40), _tama_pt(45)]), _chiyoda())
    d = q.as_dict()
    assert d["area"] == AREA_23 and d["area_max_scale"] == 40 and d["pref_max_scale"] == 45
    other = parse_551(_551([_tama_pt(45)]), _hachioji()).as_dict()
    assert other["area"] is None and other["area_max_scale"] is None


# ---------- the tracker ----------

def test_the_tracker_merges_the_area_maximum_across_reports() -> None:
    t = QuakeTracker(_chiyoda())
    assert t.feed(_551([_area_pt(20)], qid="a", hypo="")).area_max_scale == 20
    second = t.feed(_551([_setagaya_pt(40)], qid="b", hypo=""))
    assert second.area_max_scale == 40
    third = t.feed(_551([_chiyoda_pt(30)], qid="c", hypo=""))   # a lower figure does not lower the picture
    assert third is not None and third.area_max_scale == 40 and third.local_scale == 30


def test_a_rise_of_the_prefecture_alone_is_no_change_for_an_area_entry() -> None:
    t = QuakeTracker(_chiyoda())
    assert t.feed(_551([_area_pt(20), _tama_pt(30)], qid="a", hypo="")) is not None
    assert t.last_change == "new"
    assert t.feed(_551([_tama_pt(45)], qid="b", hypo="")) is None      # only pref_max_scale rose
    assert t.last_change == "new"                                       # an ignored report leaves it alone
    rose = t.feed(_551([_setagaya_pt(40)], qid="c", hypo=""))           # the area rose
    assert rose is not None and t.last_change == "scale"
    assert rose.area_max_scale == 40 and rose.pref_max_scale == 45


def test_the_same_rise_is_a_scale_change_for_an_entry_without_an_area() -> None:
    t = QuakeTracker(_hachioji())
    assert t.feed(_551([_tama_pt(30)], qid="a", hypo="")) is not None
    assert t.feed(_551([_tama_pt(45)], qid="b", hypo="")) is not None
    assert t.last_change == "scale"


def test_local_figure_and_epicentre_are_detail_changes_when_the_area_maximum_stands() -> None:
    t = QuakeTracker(_chiyoda())
    assert t.feed(_551([_setagaya_pt(40), _chiyoda_pt(20)], qid="a", hypo="")) is not None
    local = t.feed(_551([_chiyoda_pt(30)], qid="b", hypo=""))           # the area stays 40, Chiyoda rises
    assert local is not None and t.last_change == "detail" and local.local_scale == 30
    where = t.feed(_551([_chiyoda_pt(30)], qid="c", hypo="茨城県南部"))  # the epicentre becomes known
    assert where is not None and t.last_change == "detail" and where.hypocenter == "茨城県南部"
    assert t.feed(_551([_chiyoda_pt(30)], qid="d", hypo="茨城県南部")) is None


# ---------- the client: which quakes raise an `earthquake` event ----------

class _Rec:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.quakes = 0

    def fire(self, t: str, d: dict) -> None:
        self.events.append((t, d))

    def on_quake(self) -> None:
        self.quakes += 1

    def kinds(self) -> list[str]:
        return [t for t, _ in self.events]


def _client(place: QuakePlace) -> tuple[QuakeClient, _Rec]:
    rec = _Rec()
    return QuakeClient(api=None, fire=rec.fire, on_quake=rec.on_quake, place=place), rec  # type: ignore[arg-type]


QUAKE_NOW = datetime(2026, 9, 26, 12, 20, tzinfo=JST)


def test_a_tama_only_quake_raises_no_event_for_a_ward_entry_but_is_still_the_last_quake() -> None:
    client, rec = _client(_chiyoda())
    client.handle(_551([_tama_pt(45), _island_pt(50)], qid="t", when="2026/09/26 12:18:00"), QUAKE_NOW)
    assert rec.events == []
    assert client.last_quake is not None and client.last_quake.id == "t"   # the sensor keeps following it
    assert client.last_quake.pref_max_scale == 50 and client.last_quake.area_max_scale is None
    assert rec.quakes == 1


def test_the_same_tama_quake_raises_an_event_for_a_tama_entry() -> None:
    client, rec = _client(_hachioji())
    client.handle(_551([_tama_pt(45), _island_pt(50)], qid="t", when="2026/09/26 12:18:00"), QUAKE_NOW)
    assert rec.kinds() == ["earthquake"]
    data = rec.events[0][1]
    assert data["pref_max_scale"] == 50 and data["area"] is None and data["area_max_scale"] is None
    assert data["area_shindo"] is None and data["change"] == "new"


def test_an_entry_in_another_prefecture_still_fires_on_the_prefecture() -> None:
    client, rec = _client(_osaka())
    client.handle(_551([_pt("大阪府", "大阪北区梅田", 30)], qid="o", when="2026/09/26 12:18:00"), QUAKE_NOW)
    assert rec.kinds() == ["earthquake"]
    assert rec.events[0][1]["pref_max_scale"] == 30 and rec.events[0][1]["area"] is None


def test_a_ward_point_raises_the_event_for_a_ward_entry() -> None:
    client, rec = _client(_chiyoda())
    client.handle(_551([_setagaya_pt(40), _tama_pt(45)], qid="w", when="2026/09/26 12:18:00"), QUAKE_NOW)
    assert rec.kinds() == ["earthquake"]
    data = rec.events[0][1]
    assert data["area"] == AREA_23 and data["area_max_scale"] == 40 and data["area_shindo"] == "4"
    assert data["pref_max_scale"] == 45 and data["pref_shindo"] == "5-"
    assert data["local_scale"] is None and data["change"] == "new"


def test_the_area_level_row_alone_raises_the_event_for_a_ward_entry() -> None:
    client, rec = _client(_chiyoda())
    client.handle(_551([_area_pt(20)], qid="s", when="2026/09/26 12:18:00", hypo=""), QUAKE_NOW)
    assert rec.kinds() == ["earthquake"]
    assert rec.events[0][1]["area_max_scale"] == 20 and rec.events[0][1]["area_shindo"] == "2"


def test_tama_first_then_a_ward_the_event_comes_with_the_ward() -> None:
    client, rec = _client(_chiyoda())
    client.handle(_551([_tama_pt(45)], qid="a", when="2026/09/26 12:18:00", hypo=""), QUAKE_NOW)
    assert rec.events == []
    client.handle(_551([_setagaya_pt(30)], qid="b", when="2026/09/26 12:18:00", hypo=""), QUAKE_NOW)
    assert rec.kinds() == ["earthquake"]
    data = rec.events[0][1]
    assert data["change"] == "scale" and data["area_max_scale"] == 30 and data["pref_max_scale"] == 45


def test_a_later_rise_of_the_prefecture_alone_raises_no_second_event_for_a_ward_entry() -> None:
    client, rec = _client(_chiyoda())
    client.handle(_551([_chiyoda_pt(30), _tama_pt(30)], qid="a", when="2026/09/26 12:18:00", hypo=""), QUAKE_NOW)
    client.handle(_551([_tama_pt(50)], qid="b", when="2026/09/26 12:18:00", hypo=""), QUAKE_NOW)
    assert rec.kinds() == ["earthquake"]
    client.handle(_551([_chiyoda_pt(30)], qid="c", when="2026/09/26 12:18:00", hypo="茨城県南部"), QUAKE_NOW)
    assert [d["change"] for _, d in rec.events] == ["new", "detail"]   # the epicentre still goes out
    assert rec.events[1][1]["pref_max_scale"] == 50 and rec.events[1][1]["area_max_scale"] == 30


def test_event_data_has_area_shindo() -> None:
    q = parse_551(_551([_setagaya_pt(40), _tama_pt(50)]), _chiyoda())
    data = quake_event_data(q)
    assert data["area_shindo"] == "4" and data["area_max_scale"] == 40 and data["area"] == AREA_23
    assert data["pref_shindo"] == "5+" and data["local_shindo"] is None
    none = quake_event_data(parse_551(_551([_tama_pt(50)]), _hachioji()))
    assert none["area_shindo"] is None and none["area"] is None


def test_the_payloads_carry_none_of_the_private_lineage_keys() -> None:
    q = parse_551(_551([_chiyoda_pt(30), _area_pt(20)]), _chiyoda())
    client, rec = _client(_chiyoda())
    client.handle(_551([_chiyoda_pt(30)]), QUAKE_NOW)
    client.handle(_eew([_area("東京", AREA_23, 45, 50)]), NOW)
    keys = set(q.as_dict()) | set(quake_event_data(q))
    for _, data in rec.events:
        keys |= set(data)
    assert {"area", "area_max_scale", "area_shindo"} <= keys
    stale = {k for k in keys if k.startswith(("tokyo23", "chiyoda", "tokyo_23", "ward_"))}
    assert stale == set()
    source = Path(jma_package.__file__).with_name("parse_quake.py").read_text(encoding="utf-8")
    assert "tokyo23" not in source and "chiyoda" not in source.lower()


# ---------- the early warning ----------

def _eew(areas: list[dict], serial: int = 1, event_id: str = "e1") -> dict:
    return {
        "code": 556, "test": False, "cancelled": False,
        "issue": {"time": "2026/09/26 12:17:20", "eventId": event_id, "serial": str(serial)},
        "earthquake": {"originTime": "2026/09/26 12:17:00", "hypocenter": {"name": "茨城県南部", "magnitude": 6.1}},
        "areas": areas,
    }


def _area(pref: str, name: str, lo: int, hi: int) -> dict:
    return {"pref": pref, "name": name, "scaleFrom": lo, "scaleTo": hi, "arrivalTime": "2026/09/26 12:17:40"}


def test_eew_names_the_23_wards_and_carries_the_area() -> None:
    events = EewTracker("東京都", AREA_23).feed(_eew([_area("東京", AREA_23, 45, 50)]), NOW)
    assert [e["type"] for e in events] == ["eew"]
    assert events[0]["area"] == AREA_23 and events[0]["pref"] == "東京都"
    assert events[0]["areas"] == [AREA_23] and events[0]["predicted_scale"] == 50


def test_eew_for_tama_and_the_islands_only_does_not_fire_for_a_ward_entry_but_does_for_a_tama_entry() -> None:
    msg = _eew([_area("東京", "東京都多摩東部", 45, 50), _area("東京", "東京都島しょ", 40, 45)])
    assert EewTracker("東京都", AREA_23).feed(msg, NOW) == []
    tama = EewTracker("東京都", None).feed(msg, NOW)
    assert [e["type"] for e in tama] == ["eew"] and tama[0]["area"] is None
    assert tama[0]["areas"] == ["東京都多摩東部", "東京都島しょ"] and tama[0]["predicted_scale"] == 50
    assert [e["type"] for e in EewTracker("東京都").feed(msg, NOW)] == ["eew"]   # the default: no area


def test_eew_numbers_come_from_the_23_wards_not_from_a_stronger_tama_area() -> None:
    msg = _eew([_area("東京", "東京都多摩東部", 55, 60), _area("東京", AREA_23, 40, 45)])
    events = EewTracker("東京都", AREA_23).feed(msg, NOW)
    assert events[0]["areas"] == [AREA_23] and events[0]["predicted_scale"] == 45
    assert events[0]["scale_from"] == 40 and events[0]["scale_to"] == 45


def test_eew_half_width_23_matches() -> None:
    events = EewTracker("東京都", AREA_23).feed(_eew([_area("東京", "東京都23区", 45, 50)]), NOW)
    assert [e["type"] for e in events] == ["eew"] and events[0]["areas"] == ["東京都23区"]


def test_eew_a_name_that_only_starts_like_the_area_does_not_match() -> None:
    assert not eew_area_matches({"pref": "東京", "name": "東京都２３区西部"}, "東京都", AREA_23)
    assert not eew_area_matches({"pref": "東京", "name": "x東京都２３区"}, "東京都", AREA_23)
    assert not eew_area_matches({"pref": "東京", "name": None}, "東京都", AREA_23)
    assert not eew_area_matches({"pref": "東京"}, "東京都", AREA_23)
    assert not eew_area_matches({"pref": "東京", "name": AREA_23}, None, AREA_23)
    assert not eew_area_matches("東京都２３区", "東京都", AREA_23)
    assert eew_area_matches({"pref": "東京", "name": AREA_23}, "東京都", AREA_23)
    assert eew_area_matches({"name": "東京都23区"}, "東京都", AREA_23)    # no pref field: the name decides


def test_eew_area_name_none_behaves_as_before() -> None:
    assert eew_area_matches({"pref": "東京", "name": "東京都多摩東部"}, "東京都", None)
    assert eew_area_matches({"pref": "東京", "name": "東京都多摩東部"}, "東京都")
    assert eew_area_matches({"name": "大阪府北部"}, "大阪府")
    assert not eew_area_matches({"pref": "京都", "name": "京都府南部"}, "東京都", None)
    assert eew_area_matches({"pref": "東京", "name": "東京都多摩東部"}, "東京都", "")   # an empty name is no area name
    assert [e["type"] for e in EewTracker("東京都", "").feed(_eew([_area("東京", "東京都多摩東部", 45, 50)]), NOW)] == ["eew"]


def test_eew_tama_first_then_the_wards_is_an_announcement_not_an_update() -> None:
    t = EewTracker("東京都", AREA_23)
    assert t.feed(_eew([_area("東京", "東京都多摩東部", 45, 50)], 1), NOW) == []
    events = t.feed(_eew([_area("東京", "東京都多摩東部", 45, 50), _area("東京", AREA_23, 40, 45)], 2), NOW)
    assert [e["type"] for e in events] == ["eew"] and events[0]["update"] is False
    assert events[0]["areas"] == [AREA_23] and events[0]["predicted_scale"] == 45
    assert t.feed(_eew([_area("東京", AREA_23, 40, 45)], 3), NOW) == []              # not rising
    assert t.feed(_eew([_area("東京", AREA_23, 50, 55)], 4), NOW)[0]["update"] is True


def test_eew_cancel_of_an_announced_id_emits_eew_cancelled_with_the_area() -> None:
    t = EewTracker("東京都", AREA_23)
    assert t.feed(_eew([_area("東京", AREA_23, 45, 50)]), NOW)[0]["type"] == "eew"
    cancelled = t.feed(dict(_eew([], 2), cancelled=True), NOW)
    assert [e["type"] for e in cancelled] == ["eew_cancelled"]
    assert cancelled[0]["area"] == AREA_23 and cancelled[0]["event_id"] == "e1" and cancelled[0]["pref"] == "東京都"


def test_eew_cancel_of_an_id_never_announced_to_a_ward_entry_emits_nothing() -> None:
    t = EewTracker("東京都", AREA_23)
    assert t.feed(_eew([_area("東京", "東京都多摩東部", 45, 50)]), NOW) == []
    assert t.feed(dict(_eew([], 2), cancelled=True), NOW) == []


def test_the_client_builds_the_eew_tracker_with_the_entrys_area() -> None:
    client, rec = _client(_chiyoda())
    client.handle(_eew([_area("東京", "東京都多摩東部", 45, 50)], 1), NOW)
    assert rec.events == []
    client.handle(_eew([_area("東京", AREA_23, 45, 50)], 2), NOW)
    assert rec.kinds() == ["eew"]
    assert rec.events[0][1]["area"] == AREA_23 and rec.events[0][1]["predicted_shindo"] == "5+"
    client.handle(dict(_eew([], 3), cancelled=True), NOW)
    assert rec.kinds() == ["eew", "eew_cancelled"] and rec.events[1][1]["area"] == AREA_23
    other, rec_o = _client(_hachioji())
    other.handle(_eew([_area("東京", "東京都多摩東部", 45, 50)], 1), NOW)
    assert rec_o.kinds() == ["eew"] and rec_o.events[0][1]["area"] is None


# ---------- the stored name of the entry: area_name_ja, then the legacy area_name ----------

_GONE = object()


def _data(**over: Any) -> dict:
    data = {**ENTRY_DATA, "area": CHIYODA, "area_name": "千代田区", CONF_AREA_NAME_JA: "千代田区"}
    for key, value in over.items():
        if value is _GONE:
            data.pop(key, None)
        else:
            data[key] = value
    return data


@pytest.mark.parametrize("over, expected", [
    ({}, "千代田区"),                                                    # both keys: the legacy one is read on its own
    ({CONF_AREA_NAME_JA: _GONE}, "千代田区"),                            # entries made before 0.6
    ({CONF_AREA_NAME_JA: _GONE, "area_name": "世田谷区"}, "世田谷区"),
    ({CONF_AREA_NAME_JA: _GONE, "area_name": ""}, None),
    ({CONF_AREA_NAME_JA: _GONE, "area_name": CHIYODA}, None),            # the numeric code is never a name
    ({CONF_AREA_NAME_JA: _GONE, "area_name": 1310100}, None),
    ({CONF_AREA_NAME_JA: _GONE, "area_name": None}, None),
    ({CONF_AREA_NAME_JA: _GONE, "area_name": _GONE}, None),
])
def test_legacy_name(over: dict, expected: str | None) -> None:
    assert jma_package._legacy_name(_data(**over)) == expected


def _entry(**over: Any) -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, title="JMA Chiyoda", unique_id=CHIYODA, data=_data(**over))


async def test_an_entry_made_before_0_6_finds_its_name_under_area_name(tokyo: HomeAssistant, aioclient_mock) -> None:
    hass = tokyo
    entry = _entry(**{CONF_AREA_NAME_JA: _GONE})
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    quake = entry.runtime_data.quake
    assert quake.place.local_name == "千代田区" and quake.place.area == AREA_23 and quake.place.pref == "東京都"
    quake.handle(_551([_chiyoda_pt(30), _setagaya_pt(40), _tama_pt(45)], when="2026/09/26 13:10:00"),
                 datetime(2026, 9, 26, 13, 12, tzinfo=JST))
    await hass.async_block_till_done()
    attrs = hass.states.get("sensor.jma_chiyoda_last_earthquake").attributes
    assert attrs["local_name"] == "千代田区" and attrs["local_scale"] == 30 and attrs["local_shindo"] == "3"
    assert attrs["area"] == AREA_23 and attrs["area_max_scale"] == 40 and attrs["area_shindo"] == "4"
    assert attrs["pref_max_scale"] == 45
    assert hass.states.get("event.jma_chiyoda_alerts").attributes["event_type"] == "earthquake"


@pytest.mark.parametrize("over", [
    {CONF_AREA_NAME_JA: _GONE, "area_name": _GONE},     # neither key
    {CONF_AREA_NAME_JA: _GONE, "area_name": CHIYODA},    # the area code in the name's place
    {CONF_AREA_NAME_JA: "", "area_name": 1310100},      # an empty name, and a number
])
async def test_an_entry_with_no_usable_name_has_unknown_local_values(
    tokyo: HomeAssistant, aioclient_mock, over: dict
) -> None:
    hass = tokyo
    entry = _entry(**over)
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    quake = entry.runtime_data.quake
    assert quake.place.local_name is None and quake.place.area == AREA_23   # the area does not need the name
    quake.handle(_551([_chiyoda_pt(30), _setagaya_pt(40)], when="2026/09/26 13:10:00"),
                 datetime(2026, 9, 26, 13, 12, tzinfo=JST))
    await hass.async_block_till_done()
    attrs = hass.states.get("sensor.jma_chiyoda_last_earthquake").attributes
    assert attrs["local_scale"] is None and attrs["local_name"] is None
    assert attrs["area_max_scale"] == 40 and attrs["pref_max_scale"] == 40


async def test_area_name_ja_wins_over_the_legacy_name(tokyo: HomeAssistant, aioclient_mock) -> None:
    hass = tokyo
    entry = _entry(area_name="legacy")
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    assert entry.runtime_data.quake.place.local_name == "千代田区"


async def test_the_sensor_shows_the_area_before_any_quake(tokyo: HomeAssistant, aioclient_mock) -> None:
    hass = tokyo
    chiyoda = _entry()
    mock_jma(aioclient_mock)
    await setup(hass, chiyoda)
    attrs = hass.states.get("sensor.jma_chiyoda_last_earthquake").attributes
    assert attrs["pref"] == "東京都" and attrs["local_name"] == "千代田区" and attrs["area"] == AREA_23


async def test_the_sensor_shows_no_area_for_a_tokyo_address_outside_the_wards(
    tokyo: HomeAssistant, aioclient_mock
) -> None:
    hass = tokyo
    entry = MockConfigEntry(domain=DOMAIN, title="JMA Hachioji", unique_id=HACHIOJI, data=_data(
        area=HACHIOJI, area_name="八王子市", **{CONF_AREA_NAME_JA: "八王子市"}))
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    quake = entry.runtime_data.quake
    assert quake.place.area is None and quake.place.local_name == "八王子市"
    attrs = hass.states.get("sensor.jma_hachioji_last_earthquake").attributes
    assert "area" in attrs and attrs["area"] is None
    quake.handle(_551([_tama_pt(45), _island_pt(50)], when="2026/09/26 13:10:00"),
                 datetime(2026, 9, 26, 13, 12, tzinfo=JST))
    await hass.async_block_till_done()
    assert hass.states.get("event.jma_hachioji_alerts").attributes["event_type"] == "earthquake"


# ---------- the README names the new values ----------

def test_the_readme_names_the_area_values() -> None:
    readme = (Path(jma_package.__file__).parents[2] / "README.md").read_text(encoding="utf-8")
    for key in ("`area`", "`area_max_scale`", "`area_shindo`", AREA_23):
        assert key in readme, key
