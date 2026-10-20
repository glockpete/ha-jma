"""The earthquake part works for any place in Japan.

The prefecture of an entry comes from its office code; its local name is the Japanese name of its
area. A point counts for the entry when it sits in the entry's prefecture (pref_*) and, for the local
values, when its address holds the local name (local_*).
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components import jma as jma_package
from custom_components.jma.const import CONF_AREA_NAME_JA, DOMAIN, PREFECTURES
from custom_components.jma.parse_quake import (
    EewTracker,
    QuakePlace,
    QuakeTracker,
    eew_area_matches,
    local_forms,
    parse_551,
    prefecture_for_office,
)
from custom_components.jma.quake_ws import QuakeClient, quake_event_data

from .conftest import AREA, ENTRY_DATA, TITLE, mock_jma, setup  # the tests' place (tests/conftest.py)
from .conftest import QUAKE_PLACE as CHIYODA
from .helpers import load_json

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 9, 26, 12, 17, 30, tzinfo=JST)


def _551(points: list[dict], qid: str = "q1", when: str = "2026/09/26 12:17:00", hypo: str = "茨城県南部") -> dict:
    return {
        "code": 551, "id": qid, "issue": {"type": "DetailScale", "time": "2026/09/26 12:20:39"},
        "earthquake": {"time": when, "maxScale": 50, "domesticTsunami": "None",
                       "hypocenter": {"name": hypo, "magnitude": 5.0, "depth": 40}},
        "points": points,
    }


def _pt(pref: str, addr: str, scale: int, is_area: bool = False) -> dict:
    return {"pref": pref, "addr": addr, "isArea": is_area, "scale": scale}


# ---------- the prefecture table ----------

def test_prefecture_table_has_the_47_prefectures() -> None:
    assert len(PREFECTURES) == 47
    assert list(PREFECTURES) == [f"{n:02d}" for n in range(1, 48)]
    assert len(set(PREFECTURES.values())) == 47
    assert all(name == "北海道" or name[-1] in "都府県" for name in PREFECTURES.values())
    assert PREFECTURES["01"] == "北海道" and PREFECTURES["13"] == "東京都"
    assert PREFECTURES["26"] == "京都府" and PREFECTURES["27"] == "大阪府" and PREFECTURES["47"] == "沖縄県"


@pytest.mark.parametrize("office, prefecture", [
    ("016000", "北海道"),    # Sapporo
    ("130000", "東京都"),
    ("270000", "大阪府"),
    ("400000", "福岡県"),
    ("460040", "鹿児島県"),  # the Amami office is part of Kagoshima
    ("471000", "沖縄県"),
])
def test_office_codes_map_to_their_prefecture(office: str, prefecture: str) -> None:
    assert prefecture_for_office(office) == prefecture


def test_every_office_and_area_of_the_fixture_maps_and_agrees() -> None:
    area_json = load_json("area.json")
    for office in area_json["offices"]:
        assert prefecture_for_office(office) in PREFECTURES.values(), office
    for code, c20 in area_json["class20s"].items():  # the first two digits of an area code = its office's
        c10 = area_json["class15s"][c20["parent"]]["parent"]
        office = area_json["class10s"][c10]["parent"]
        assert code[:2] == office[:2], (code, office)


def test_every_prefecture_the_fixtures_name_is_in_the_table() -> None:
    names = set()
    for name in ("p2p_551_sample_a.json", "p2p_551_sample_b.json"):
        for msg in load_json(name):
            names |= {p["pref"] for p in msg["points"]}
    assert names and names <= set(PREFECTURES.values())


@pytest.mark.parametrize("office", ["", "99", "9", "xx0000", None])
def test_an_unknown_office_has_no_prefecture(office) -> None:
    assert prefecture_for_office(office) is None


# ---------- the local name, as written by the agency ----------

def test_local_forms() -> None:
    assert local_forms("千代田区") == ("千代田区",)
    assert local_forms("水俣市") == ("水俣市",)          # a city: the city character stays
    assert local_forms("益城町") == ("益城町",)
    assert local_forms("神戸市東灘区") == ("神戸市東灘区", "神戸東灘区")  # a ward of a designated city
    assert local_forms("") == ()
    assert local_forms(None) == ()


def test_a_ward_of_tokyo() -> None:
    q = parse_551(_551([
        _pt("東京都", "東京千代田区大手町", 20),
        _pt("東京都", "東京港区芝", 40),
        _pt("東京都", "東京八王子市大横町", 45),
    ]), CHIYODA)
    assert q.local_scale == 20 and q.pref_max_scale == 45  # the prefecture includes the city of 八王子
    assert q.pref == "東京都" and q.local_name == "千代田区"


def test_a_ward_of_a_designated_city_with_and_without_the_city_character() -> None:
    place = QuakePlace("兵庫県", "神戸市東灘区")
    without = parse_551(_551([_pt("兵庫県", "神戸東灘区住吉", 30)]), place)
    with_ = parse_551(_551([_pt("兵庫県", "神戸市東灘区住吉", 40)]), place)
    other_ward = parse_551(_551([_pt("兵庫県", "神戸灘区灘南通", 50)]), place)
    assert without.local_scale == 30 and with_.local_scale == 40
    assert other_ward.local_scale is None and other_ward.pref_max_scale == 50


def test_a_city_from_the_agency_sample() -> None:
    place = QuakePlace("熊本県", "水俣市")
    msgs = load_json("p2p_551_sample_a.json")
    expected = None
    for msg in msgs:
        q = parse_551(msg, place)
        scales = [p["scale"] for p in msg["points"] if p["addr"].startswith("水俣市")]
        if scales:
            expected = max(scales)
            assert q.local_scale == max(scales), msg["id"]
    assert expected is not None  # the sample does hold points of that city
    assert all(parse_551(m, QuakePlace("熊本県", "存在しない市")).local_scale is None for m in msgs)


def test_a_town() -> None:
    place = QuakePlace("熊本県", "益城町")
    q = parse_551(_551([_pt("熊本県", "益城町宮園", 70), _pt("熊本県", "熊本南区城南町", 60)]), place)
    assert q.local_scale == 70 and q.pref_max_scale == 70


def test_two_wards_of_the_same_name_in_two_cities_of_one_prefecture() -> None:
    osaka = QuakePlace("大阪府", "大阪市北区")
    sakai = QuakePlace("大阪府", "堺市北区")
    points = [_pt("大阪府", "大阪北区梅田", 30), _pt("大阪府", "堺北区新金岡", 50)]
    assert parse_551(_551(points), osaka).local_scale == 30
    assert parse_551(_551(points), sakai).local_scale == 50
    # the city character removed does not turn one city's ward into the other's
    yokohama = QuakePlace("神奈川県", "横浜市緑区")
    sagamihara = [_pt("神奈川県", "相模原緑区橋本", 40)]
    assert parse_551(_551(sagamihara), yokohama).local_scale is None
    assert parse_551(_551([_pt("神奈川県", "横浜緑区十日市場町", 30)]), yokohama).local_scale == 30
    assert parse_551(_551([_pt("神奈川県", "横浜市緑区十日市場町", 20)]), yokohama).local_scale == 20


def test_a_point_of_another_prefecture_never_counts() -> None:
    kita = QuakePlace("東京都", "北区")
    elsewhere = [_pt("大阪府", "大阪北区梅田", 50), _pt("大阪府", "東京北区王子", 50), _pt("愛知県", "名古屋北区", 45)]
    q = parse_551(_551(elsewhere), kita)
    assert q.pref_max_scale is None and q.local_scale is None  # also when the address holds the local name
    q = parse_551(_551(elsewhere + [_pt("東京都", "東京北区王子", 30)]), kita)
    assert q.pref_max_scale == 30 and q.local_scale == 30


def test_the_address_is_matched_as_text_only() -> None:
    for name in (".*", ".*区", "(千代田", "[千代田区", "千代田区|港区", "\\"):
        q = parse_551(_551([_pt("東京都", "東京千代田区大手町", 30)]), QuakePlace("東京都", name))
        assert q.local_scale is None, name
        assert q.pref_max_scale == 30
    tree = ast.parse(Path(jma_package.__file__).with_name("parse_quake.py").read_text(encoding="utf-8"))
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert "re" not in imported and "fnmatch" not in imported


def test_unknown_values_stay_unknown() -> None:
    points = [_pt("東京都", "東京千代田区大手町", 30)]
    q = parse_551(_551(points), QuakePlace("東京都", None))   # local name unknown
    assert q.pref_max_scale == 30 and q.local_scale is None and q.local_name is None
    q = parse_551(_551(points), QuakePlace(None, "千代田区"))  # no prefecture: nothing counts
    assert q.pref_max_scale is None and q.local_scale is None


def test_isarea_rows_are_not_read_so_they_count_like_any_other_row() -> None:
    """The key isArea is not read; an area-level row counts for the prefecture
    by its pref field and for the local values only if its address holds the local name."""
    area_row = _pt("東京都", "東京都２３区", 30, is_area=True)
    q = parse_551(_551([area_row]), CHIYODA)
    assert q.pref_max_scale == 30 and q.local_scale is None
    same_but_flag_off = dict(area_row, isArea=False)
    assert parse_551(_551([same_but_flag_off]), CHIYODA) == q
    assert "isArea" not in Path(jma_package.__file__).with_name("parse_quake.py").read_text(encoding="utf-8")


def test_scale_forms_are_accepted() -> None:
    q = parse_551(_551([_pt("東京都", "東京千代田区大手町", 45.0), _pt("東京都", "東京千代田区丸の内", "x")]), CHIYODA)
    assert q.local_scale == 45
    assert parse_551({"code": 556}, CHIYODA) is None


# ---------- the tracker ----------

def test_tracker_merges_per_entry_values() -> None:
    t = QuakeTracker(CHIYODA)
    first = t.feed(_551([_pt("東京都", "東京都２３区", 30, True)], qid="a", hypo=""))
    assert first is not None and first.pref_max_scale == 30 and first.local_scale is None
    second = t.feed(_551([_pt("東京都", "東京千代田区大手町", 40)], qid="b"))
    assert second.pref_max_scale == 40 and second.local_scale == 40
    assert t.feed(_551([_pt("東京都", "東京千代田区大手町", 40)], qid="c")) is None  # nothing new


def test_tracker_fires_again_when_the_local_scale_rises_alone() -> None:
    t = QuakeTracker(CHIYODA)
    assert t.feed(_551([_pt("東京都", "東京八王子市大横町", 45), _pt("東京都", "東京千代田区大手町", 20)], qid="a"))
    # same quake, the prefecture maximum stays 45, only the local value rises
    again = t.feed(_551([_pt("東京都", "東京千代田区大手町", 30)], qid="b"))
    assert again is not None and again.local_scale == 30 and again.pref_max_scale == 45


# ---------- the event rule ----------

class _Rec:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.quakes = 0

    def fire(self, t: str, d: dict) -> None:
        self.events.append((t, d))

    def on_quake(self) -> None:
        self.quakes += 1


def _client(place: QuakePlace) -> tuple[QuakeClient, _Rec]:
    rec = _Rec()
    return QuakeClient(api=None, fire=rec.fire, on_quake=rec.on_quake, place=place), rec  # type: ignore[arg-type]


def test_the_event_fires_for_a_point_of_the_entrys_prefecture_and_not_for_one_elsewhere() -> None:
    now = datetime(2026, 9, 26, 12, 20, tzinfo=JST)
    client, rec = _client(CHIYODA)
    client.handle(_551([_pt("神奈川県", "横浜緑区十日市場町", 50)], qid="far"), now)
    assert rec.events == [] and client.last_quake is not None  # tracked for the sensor, no push
    client.handle(_551([_pt("東京都", "東京八王子市大横町", 30)], qid="near", when="2026/09/26 12:18:00"), now)
    assert [t for t, _ in rec.events] == ["earthquake"]
    data = rec.events[0][1]
    assert data["pref"] == "東京都" and data["local_name"] == "千代田区"
    assert data["pref_max_scale"] == 30 and data["pref_shindo"] == "3"
    assert data["local_scale"] is None and data["local_shindo"] is None


def test_the_same_message_fires_for_the_entry_of_that_prefecture_only() -> None:
    now = datetime(2026, 9, 26, 13, 0, tzinfo=JST)
    kumamoto, rec_k = _client(QuakePlace("熊本県", "水俣市"))
    tokyo, rec_t = _client(CHIYODA)
    for msg in load_json("p2p_551_sample_a.json"):
        kumamoto.handle(msg, now, backfill=False)
        tokyo.handle(msg, now, backfill=False)
    assert [t for t, _ in rec_k.events if t == "earthquake"] != []
    assert [t for t, _ in rec_t.events if t == "earthquake"] == []
    assert tokyo.last_quake is not None  # the sensor follows the newest quake of the country


def test_replayed_old_quakes_are_recorded_not_alerted() -> None:
    client, rec = _client(CHIYODA)
    now = datetime(2026, 9, 26, 13, 0, tzinfo=JST)
    client.handle(_551([_pt("東京都", "東京千代田区大手町", 40)], qid="old", when="2026/09/26 12:00:00"), now, backfill=True)
    assert rec.events == [] and client.last_quake.id == "old"


# ---------- the early warning (EEW) ----------

def _eew(areas: list[dict], serial: int = 1) -> dict:
    return {
        "code": 556, "test": False, "cancelled": False,
        "issue": {"time": "2026/09/26 12:17:20", "eventId": "e1", "serial": str(serial)},
        "earthquake": {"originTime": "2026/09/26 12:17:00", "hypocenter": {"name": "茨城県南部", "magnitude": 6.1}},
        "areas": areas,
    }


def _area(pref: str, name: str, lo: int, hi: int, **kw) -> dict:
    return {"pref": pref, "name": name, "scaleFrom": lo, "scaleTo": hi, "arrivalTime": "2026/09/26 12:17:40", **kw}


def test_eew_counts_the_areas_of_the_entrys_prefecture() -> None:
    msg = _eew([_area("千葉", "千葉県北西部", 50, 55), _area("東京", "東京都２３区", 45, 50), _area("東京", "東京都多摩東部", 40, 45)])
    events = EewTracker("東京都").feed(msg, NOW)
    assert [e["type"] for e in events] == ["eew"]
    assert events[0]["areas"] == ["東京都２３区", "東京都多摩東部"]  # the event names the matched areas
    assert events[0]["pref"] == "東京都" and events[0]["predicted_scale"] == 50
    chiba = EewTracker("千葉県").feed(msg, NOW)
    assert chiba[0]["areas"] == ["千葉県北西部"] and chiba[0]["predicted_scale"] == 55
    assert EewTracker("大阪府").feed(msg, NOW) == []


def test_eew_uses_the_strongest_matched_area_for_the_numbers() -> None:
    msg = _eew([_area("東京", "東京都多摩東部", 40, 45), _area("東京", "東京都２３区", 50, 55)])
    ev = EewTracker("東京都").feed(msg, NOW)[0]
    assert ev["predicted_scale"] == 55 and ev["scale_from"] == 50 and ev["scale_to"] == 55


def test_eew_prefecture_forms() -> None:
    assert eew_area_matches({"pref": "北海道", "name": "北海道道央"}, "北海道")
    assert eew_area_matches({"pref": "京都", "name": "京都府南部"}, "京都府")
    assert not eew_area_matches({"pref": "京都", "name": "京都府南部"}, "東京都")
    assert not eew_area_matches({"pref": "東京", "name": "東京都２３区"}, "京都府")
    assert eew_area_matches({"pref": "東京都", "name": "x"}, "東京都")            # the full form
    assert eew_area_matches({"name": "大阪府北部"}, "大阪府")                     # no pref field: the name starts with it
    assert not eew_area_matches({"name": "京都府南部"}, "東京都")
    assert not eew_area_matches({"pref": "東京"}, None)
    assert not eew_area_matches({"pref": 5, "name": None}, "東京都")


def test_eew_rising_cancel_and_old_backfill_rules_hold_for_any_prefecture() -> None:
    t = EewTracker("大阪府")
    assert t.feed(_eew([_area("大阪", "大阪府北部", 45, 50)]), NOW)[0]["type"] == "eew"
    assert t.feed(_eew([_area("大阪", "大阪府北部", 45, 50)], 2), NOW) == []
    assert t.feed(_eew([_area("大阪", "大阪府北部", 50, 55)], 3), NOW)[0]["update"] is True
    cancelled = dict(_eew([], 4), cancelled=True)
    assert [e["type"] for e in t.feed(cancelled, NOW)] == ["eew_cancelled"]
    assert EewTracker("大阪府").feed(_eew([_area("大阪", "大阪府北部", 45, 50)]), NOW + timedelta(minutes=5), backfill=True) == []


def test_the_client_routes_eew_by_prefecture() -> None:
    now = datetime(2026, 9, 26, 12, 17, 30, tzinfo=JST)
    client, rec = _client(QuakePlace("千葉県", None))
    client.handle(_eew([_area("千葉", "千葉県北西部", 50, 55), _area("東京", "東京都２３区", 45, 50)]), now)
    assert rec.events[-1][0] == "eew" and rec.events[-1][1]["predicted_shindo"] == "6-"
    assert rec.events[-1][1]["areas"] == ["千葉県北西部"]


# ---------- the value names: prefecture, local, overall ----------

SCALE_KEYS = {"pref_max_scale", "area_max_scale", "local_scale", "max_scale",
              "pref_shindo", "area_shindo", "local_shindo", "max_shindo"}


def _scale_keys(mapping) -> set:
    return {key for key in mapping if key.endswith(("_scale", "_shindo"))}


def test_event_data_carries_prefecture_and_local_values() -> None:
    q = parse_551(_551([_pt("東京都", "東京千代田区大手町", 30)]), CHIYODA)
    data = quake_event_data(q)
    assert {"pref", "area", "local_name", "pref_max_scale", "area_max_scale", "local_scale",
            "pref_shindo", "area_shindo", "local_shindo", "max_shindo"} <= set(data)
    assert data["pref_shindo"] == "3" and data["local_shindo"] == "3"
    assert data["area"] is None and data["area_max_scale"] is None and data["area_shindo"] is None  # CHIYODA: no area
    assert _scale_keys(data) == SCALE_KEYS


# ---------- the entry: sensor attributes, event entity, the stored name ----------

async def test_sensor_attributes_and_event_entity(tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    sensor = hass.states.get("sensor.jma_chiyoda_last_earthquake")
    assert sensor.attributes["pref"] == "東京都" and sensor.attributes["local_name"] == "千代田区"  # before any quake
    assert sensor.attributes["area"] == "東京都２３区"  # Chiyoda is one of the 23 wards: its entry has the area
    assert _scale_keys(sensor.attributes) <= SCALE_KEYS
    msg = _551([_pt("東京都", "東京千代田区大手町", 30), _pt("東京都", "東京八王子市大横町", 45)],
               when="2026/09/26 13:10:00")
    entry.runtime_data.quake.handle(msg, datetime(2026, 9, 26, 13, 12, tzinfo=JST))
    await hass.async_block_till_done()
    attrs = hass.states.get("sensor.jma_chiyoda_last_earthquake").attributes
    assert attrs["pref"] == "東京都" and attrs["local_name"] == "千代田区"
    assert attrs["pref_max_scale"] == 45 and attrs["pref_shindo"] == "5-"
    assert attrs["local_scale"] == 30 and attrs["local_shindo"] == "3"
    assert attrs["area"] == "東京都２３区" and attrs["area_max_scale"] == 30 and attrs["area_shindo"] == "3"  # not Hachioji's 45
    assert _scale_keys(attrs) <= SCALE_KEYS
    ev = hass.states.get("event.jma_chiyoda_alerts")
    assert ev.attributes["event_type"] == "earthquake"
    assert ev.attributes["pref_max_scale"] == 45 and ev.attributes["local_scale"] == 30
    assert ev.attributes["area_max_scale"] == 30 and ev.attributes["area_shindo"] == "3"


async def test_renaming_the_entry_reloads_it(tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    """The device carries the entry title, so a rename (the README says the title can be changed in the UI)
    must reach it: the entry is reloaded."""
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    runtime = entry.runtime_data
    hass.config_entries.async_update_entry(entry, title="JMA Renamed")
    await hass.async_block_till_done()
    assert entry.runtime_data is not runtime
    device = dr.async_get(hass).async_get_device_by_identifier(("jma", AREA), entry.entry_id)
    assert device is not None and device.name == "JMA Renamed"


async def test_a_changed_option_reloads_the_entry(tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    runtime = entry.runtime_data
    hass.config_entries.async_update_entry(entry, options={"station_primary": "62016"})
    await hass.async_block_till_done()
    assert entry.runtime_data is not runtime


async def test_an_entry_without_any_stored_name_has_unknown_local_values(tokyo: HomeAssistant, aioclient_mock) -> None:
    """Neither `area_name_ja` nor a Japanese `area_name`: the local values are unknown. The prefecture values work, and
    so does the area (a ward entry does not need its name for it); see tests/test_quake_area.py for the legacy key."""
    hass = tokyo
    data = {k: v for k, v in ENTRY_DATA.items() if k not in (CONF_AREA_NAME_JA, "area_name")}
    entry = MockConfigEntry(domain=DOMAIN, title=TITLE, unique_id=AREA, data=data)
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    quake = entry.runtime_data.quake
    assert quake.place == QuakePlace.for_entry("東京都", None, AREA)  # the prefecture and the area work, no local name
    assert quake.place.local_name is None and quake.place.area == "東京都２３区"
    quake.handle(_551([_pt("東京都", "東京千代田区大手町", 30)], when="2026/09/26 13:10:00"),
                 datetime(2026, 9, 26, 13, 12, tzinfo=JST))
    await hass.async_block_till_done()
    attrs = hass.states.get("sensor.jma_chiyoda_last_earthquake").attributes
    assert attrs["pref_max_scale"] == 30 and attrs["local_scale"] is None and attrs["local_name"] is None
    assert attrs["area_max_scale"] == 30


async def test_a_third_entry_opens_no_earthquake_connection(
    tokyo: HomeAssistant, aioclient_mock, entry, caplog
) -> None:
    """The earthquake service allows two WebSocket connections per IP address."""
    hass = tokyo
    mock_jma(aioclient_mock)
    entries = [entry]
    for area, title in (("2710000", "JMA Osaka"), ("2714000", "JMA Sakai")):
        entries.append(MockConfigEntry(domain=DOMAIN, title=title, unique_id=area, data={**ENTRY_DATA, "area": area}))
    for each in entries:
        await setup(hass, each)
    assert [each.runtime_data.quake.enabled for each in entries] == [True, True, False]
    assert sum("No earthquake connection" in record.getMessage() for record in caplog.records) == 1
    assert hass.states.get("sensor.jma_sakai_last_earthquake").state == "unavailable"
    assert hass.states.get("sensor.jma_chiyoda_last_earthquake").state != "unavailable"
    assert hass.states.get("binary_sensor.jma_sakai_feed_problem").state == "off"  # not reported as a feed problem


def test_every_ward_of_a_designated_city_in_the_area_list_matches_with_and_without_the_city_character() -> None:
    """The names above are written by hand; these are the areas of the agency's own list (tests/fixtures/area.json)
    whose name holds both a city and a ward: each matches its own address form, the form without the first 市,
    and nothing of another prefecture."""
    areas = load_json("area.json")["class20s"]
    wards = {code: row["name"] for code, row in areas.items() if "市" in row["name"] and "区" in row["name"]}
    assert len(wards) >= 10, wards
    for code, name in wards.items():
        pref = PREFECTURES[code[:2]]
        other = next(p for p in PREFECTURES.values() if p != pref)
        place = QuakePlace(pref, name)
        assert place.is_local(pref, name + "1丁目"), name
        assert place.is_local(pref, name.replace("市", "", 1) + "1丁目"), name
        assert not place.is_local(other, name), name


def test_documented_limits_of_the_name_match_whole_designated_city_and_split_areas() -> None:
    """README, Known limits: the match is by name. An area that is a whole designated city (the agency writes its points
    with the ward), and a split area whose name does not end in 市 / 区 / 町 / 村, match no address. A name that is
    inside a longer municipality name also matches that municipality's points."""
    assert not QuakePlace("千葉県", "千葉市").is_local("千葉県", "千葉中央区中央港")
    assert not QuakePlace("宮城県", "仙台市東部").is_local("宮城県", "仙台宮城野区五輪")
    assert QuakePlace("福島県", "相馬市").is_local("福島県", "南相馬市原町区三島町")


async def test_the_config_flow_stores_the_japanese_name(tokyo: HomeAssistant, aioclient_mock) -> None:
    from homeassistant import config_entries
    from homeassistant.data_entry_flow import FlowResultType

    from custom_components.jma.const import DOMAIN
    mock_jma(aioclient_mock)
    result = await tokyo.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await tokyo.config_entries.flow.async_configure(result["flow_id"], {"office": "130000"})
    result = await tokyo.config_entries.flow.async_configure(result["flow_id"], {"area": AREA})
    result = await tokyo.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_AREA_NAME_JA] == "千代田区"
