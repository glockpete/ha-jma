"""The tsunami home coast follows the municipality (area list: class20 -> class15 -> class10), not the office.

Office 130000 (Tokyo) also holds the Izu and Ogasawara islands. P2P writes the forecast regions 東京湾内湾,
伊豆諸島 and 小笠原諸島 (real captures in tests/fixtures/p2p_552_*.json and p2p_jma_tsunami_history.json); a
place on an island used to get the Tokyo Bay grade as its own and never saw its island coast.
Real captures only, except the one Tokyo Bay-only bulletin (see `_tokyo_bay_only`).
"""

from __future__ import annotations

from datetime import UTC, timedelta
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.jma.config_flow import area_chain
from custom_components.jma.const import DOMAIN, TSUNAMI_COAST_AREA, URL_AREA
from custom_components.jma.coordinator import signal_alert
from custom_components.jma.parse_tsunami import TsunamiTracker, parse_552, summarise

from .conftest import ENTRY_DATA, mock_heat_alert, mock_jma, setup
from .helpers import clone, load_json

AREA_JSON = load_json("area.json")
HISTORY = load_json("p2p_jma_tsunami_history.json")  # newest first
# (municipality code, what the entry's tsunami tracker must call its home coast)
CHIYODA, OSHIMA, HACHIJO, AOGASHIMA, OGASAWARA = "1310100", "1336100", "1340100", "1340200", "1342100"
BAY, IZU, OGASAWARA_ISLANDS = "東京湾内湾", "伊豆諸島", "小笠原諸島"
SAGAMI, UCHIBO = "相模湾・三浦半島", "千葉県内房"
OSAKA = "2710000"  # 大阪市, office 270000
YOKOSUKA, FUTTSU, UEDA = "1420100", "1222600", "2020300"  # 横須賀市 (office 140000), 富津市 (120000), 上田市 (200000)


def _entry(area: str, class10: str | None = None) -> MockConfigEntry:
    """What the config flow stores for that municipality (class10 and office from the area list)."""
    chain_class10, office = area_chain(AREA_JSON, area)
    class10 = class10 or chain_class10
    name = AREA_JSON["class20s"][area]["name"]
    return MockConfigEntry(domain=DOMAIN, title="JMA Test", unique_id=area, data={
        **ENTRY_DATA, "office": office, "area": area, "area_name": name, "area_name_ja": name, "class10": class10})


def _tokyo_bay_only() -> dict[str, Any]:
    """SYNTHETIC: p2p_552_tokyo_bay.json (real, 2025-07-30 20:45, Watch for 東京湾内湾, 伊豆諸島 and 小笠原諸島)
    with every area but 東京湾内湾 taken out."""
    msg = clone(load_json("p2p_552_tokyo_bay.json"))
    msg["areas"] = [a for a in msg["areas"] if a["name"] == BAY]
    assert len(msg["areas"]) == 1
    return msg


async def _run(hass: HomeAssistant, aioclient_mock, freezer, area: str, msg: dict[str, Any]
               ) -> tuple[MockConfigEntry, list[dict[str, Any]]]:
    """Set the entry up, move the clock to three minutes after the bulletin and feed it as a live frame."""
    entry = _entry(area)
    seen: list[dict[str, Any]] = []
    async_dispatcher_connect(hass, signal_alert(entry.entry_id),
                             lambda t, d: seen.append({"type": t, **d}) if t.startswith("tsunami") else None)
    mock_heat_alert(aioclient_mock, {(20250730, 17): "alert_20261003_17.csv"})
    mock_jma(aioclient_mock)
    freezer.move_to((parse_552(msg).issued + timedelta(minutes=3)).astimezone(UTC))
    await setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    entry.runtime_data.quake.handle(msg, dt_util.now())
    await hass.async_block_till_done()
    return entry, seen


# ---------------------------------------------------------------- the table

@pytest.mark.parametrize(("area", "coast"), [
    (CHIYODA, BAY),                  # 千代田区 -> 130011 -> 130010 東京地方
    (HACHIJO, IZU),                  # 八丈町 -> 130031 -> 130030 伊豆諸島南部
    (AOGASHIMA, IZU),                # 青ヶ島村 -> 130031 -> 130030
    (OSHIMA, IZU),                   # 大島町 -> 130021 -> 130020 伊豆諸島北部
    (OGASAWARA, OGASAWARA_ISLANDS),  # 小笠原村 -> 130040 -> 130040 小笠原諸島
    (OSAKA, "大阪府"),                # JMA's list (tsunami_coast_data.py): 大阪市 is on the coast of 大阪府
    (YOKOSUKA, f"{BAY} / {SAGAMI}"), # JMA's list names 横須賀市 under both areas
    (UEDA, None),                    # inland: not on JMA's list
])
async def test_home_coast_of_an_entry_comes_from_its_municipality(
        tokyo: HomeAssistant, aioclient_mock, area, coast) -> None:
    mock_jma(aioclient_mock)
    entry = _entry(area)
    await setup(tokyo, entry)
    assert entry.state is ConfigEntryState.LOADED
    tracker = entry.runtime_data.quake.tsunami
    assert tracker.home_area == coast, (area, AREA_JSON["class20s"][area]["name"], tracker.home_area)
    attributes = tokyo.states.get("sensor.jma_test_tsunami").attributes
    assert attributes["home_area"] == coast
    assert attributes["home_areas"] == ([] if coast is None else coast.split(" / "))


def test_the_table_is_keyed_by_class10_codes_of_the_tokyo_office_and_names_p2p_writes() -> None:
    assert TSUNAMI_COAST_AREA == {"130010": BAY, "130020": IZU, "130030": IZU, "130040": OGASAWARA_ISLANDS}
    for class10 in TSUNAMI_COAST_AREA:
        assert AREA_JSON["class10s"][class10]["parent"] == "130000"
    written = {a["name"] for msg in HISTORY for a in msg["areas"]}
    assert set(TSUNAMI_COAST_AREA.values()) <= written


def test_every_municipality_of_the_tokyo_office_has_a_coast_and_no_other_office_has() -> None:
    for code in AREA_JSON["class20s"]:
        try:
            class10, office = area_chain(AREA_JSON, code)
        except KeyError:
            continue
        assert (class10 in TSUNAMI_COAST_AREA) == (office == "130000"), (code, class10, office)


async def test_setup_does_not_need_the_area_list_for_the_coast(tokyo: HomeAssistant, aioclient_mock) -> None:
    """The class10 of the place is stored by the config flow; an area list that answers 503 changes nothing."""
    mock_jma(aioclient_mock, area_ok=False)
    entry = _entry(HACHIJO)
    await setup(tokyo, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.quake.tsunami.home_area == IZU
    assert not [call for call in aioclient_mock.mock_calls if str(call[1]) == URL_AREA]


async def test_an_unknown_class10_means_no_coast(
        tokyo: HomeAssistant, aioclient_mock) -> None:
    mock_jma(aioclient_mock)
    entry = _entry(CHIYODA, class10="999999")
    await setup(tokyo, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.quake.tsunami.home_area is None
    assert tokyo.states.get("sensor.jma_test_tsunami").attributes["home_area"] is None


# ---------------------------------------------------------------- end to end, real setup + real P2P 552

@pytest.mark.parametrize(("area", "message", "state"), [
    # 2025-07-30 09:40, 伊豆諸島 Warning, 小笠原諸島 Warning, 東京湾内湾 Watch
    (HACHIJO, HISTORY[20], "warning"),
    (OSHIMA, HISTORY[20], "warning"),
    (OGASAWARA, HISTORY[20], "warning"),
    (CHIYODA, HISTORY[20], "watch"),
    # 2025-07-31 10:45, 伊豆諸島 Watch only
    (HACHIJO, HISTORY[17], "watch"),
    (CHIYODA, HISTORY[17], "none"),
    (OGASAWARA, HISTORY[17], "none"),
    # 2025-07-30 08:37, 小笠原諸島 Watch only
    (OGASAWARA, HISTORY[21], "watch"),
    (HACHIJO, HISTORY[21], "none"),
    (CHIYODA, HISTORY[21], "none"),
    # 2025-07-30 20:45, Watch for all three
    (HACHIJO, HISTORY[18], "watch"),
    (OGASAWARA, HISTORY[18], "watch"),
    (CHIYODA, HISTORY[18], "watch"),
    (OSAKA, HISTORY[18], "watch"),    # 大阪府 is in that bulletin (it was "none" while only Tokyo had a coast area)
    (OSAKA, HISTORY[17], "none"),     # 2025-07-31 10:45 has no 大阪府
    (UEDA, HISTORY[18], "none"),      # inland
    # 2025-07-30 09:40: 相模湾・三浦半島 Warning, 千葉県内房 Warning, 東京湾内湾 Watch
    (YOKOSUKA, HISTORY[20], "warning"),   # on both coasts: the stronger grade, not the first
    (FUTTSU, HISTORY[20], "warning"),
    (YOKOSUKA, HISTORY[21], "none"),      # 2025-07-30 08:37 names neither of its areas
])
async def test_the_home_grade_is_the_grade_of_the_entrys_own_coast(
        tokyo: HomeAssistant, aioclient_mock, freezer, area, message, state) -> None:
    entry, _ = await _run(tokyo, aioclient_mock, freezer, area, message)
    assert tokyo.states.get("sensor.jma_test_tsunami").state == state


@pytest.mark.parametrize(("area", "state"), [
    (CHIYODA, "watch"),   # still gets the Tokyo Bay grade
    (HACHIJO, "none"),    # the Tokyo Bay grade is not its coast
    (OSHIMA, "none"),
    (OGASAWARA, "none"),
])
async def test_a_tokyo_bay_only_bulletin_is_the_home_grade_of_the_bay_only(
        tokyo: HomeAssistant, aioclient_mock, freezer, area, state) -> None:
    entry, seen = await _run(tokyo, aioclient_mock, freezer, area, _tokyo_bay_only())
    assert tokyo.states.get("sensor.jma_test_tsunami").state == state
    attrs = tokyo.states.get("sensor.jma_test_tsunami").attributes
    assert attrs["home_height"] == (None if state == "none" else "１ｍ")
    assert [e["home_grade"] for e in seen] == ([state] if state != "none" else [])  # a watch elsewhere is not announced


async def test_the_event_of_an_island_entry_names_its_island_coast(
        tokyo: HomeAssistant, aioclient_mock, freezer) -> None:
    entry, seen = await _run(tokyo, aioclient_mock, freezer, HACHIJO, HISTORY[20])
    [event] = seen
    assert (event["type"], event["home_area"], event["home_grade"], event["max_grade"]) == (
        "tsunami", IZU, "warning", "warning")
    assert (event["home_areas"], event["home_area_in_force"]) == ([IZU], IZU)


# ---------------------------------------------------------------- a place on two coasts (JMA lists 横須賀市 twice)

async def test_a_place_on_two_coasts_follows_the_stronger_grade_and_says_which_area_it_is(
        tokyo: HomeAssistant, aioclient_mock, freezer) -> None:
    """2025-07-30 09:40, real: 相模湾・三浦半島 Warning, 東京湾内湾 Watch. 横須賀市 is on both."""
    entry, seen = await _run(tokyo, aioclient_mock, freezer, YOKOSUKA, HISTORY[20])
    state = tokyo.states.get("sensor.jma_test_tsunami")
    sagami = next(a for a in parse_552(HISTORY[20]).areas if a.name == SAGAMI)
    assert state.state == "warning"
    assert (state.attributes["home_area"], state.attributes["home_areas"]) == (f"{BAY} / {SAGAMI}", [BAY, SAGAMI])
    assert state.attributes["home_area_in_force"] == SAGAMI
    assert (state.attributes["home_height"], state.attributes["home_immediate"]) == (sagami.height, sagami.immediate)
    [event] = seen
    assert (event["home_grade"], event["home_areas"], event["home_area_in_force"]) == ("warning", [BAY, SAGAMI], SAGAMI)
    assert event["home_height"] == sagami.height


async def test_a_place_on_two_coasts_named_in_one_of_them_only(
        tokyo: HomeAssistant, aioclient_mock, freezer) -> None:
    entry, seen = await _run(tokyo, aioclient_mock, freezer, YOKOSUKA, _tokyo_bay_only())
    state = tokyo.states.get("sensor.jma_test_tsunami")
    assert state.state == "watch" and state.attributes["home_area_in_force"] == BAY
    assert [e["home_area_in_force"] for e in seen] == [BAY]


async def test_the_attributes_before_any_bulletin_list_the_areas(
        tokyo: HomeAssistant, aioclient_mock) -> None:
    mock_jma(aioclient_mock)
    entry = _entry(YOKOSUKA)
    await setup(tokyo, entry)
    assert tokyo.states.get("sensor.jma_test_tsunami").attributes["home_areas"] == [BAY, SAGAMI]


def test_the_strongest_grade_of_several_home_areas_wins_whatever_their_order() -> None:
    bulletin = parse_552(HISTORY[20])
    now = bulletin.issued + timedelta(minutes=3)
    for names in ((BAY, SAGAMI), (SAGAMI, BAY), [BAY, SAGAMI], (BAY, SAGAMI, UCHIBO)):
        picture = summarise(bulletin, names, now)
        assert picture.home == "warning" and picture.home_area_entry.name == (UCHIBO if UCHIBO in names else SAGAMI)
    assert summarise(bulletin, (BAY,), now).home == "watch"


def test_equal_grades_take_the_area_that_comes_first_in_the_bulletin() -> None:
    bulletin = parse_552(HISTORY[18])  # 2025-07-30 20:45, Watch for all
    now = bulletin.issued + timedelta(minutes=3)
    first = next(a.name for a in bulletin.areas if a.name in (BAY, SAGAMI))
    for names in ((BAY, SAGAMI), (SAGAMI, BAY)):
        picture = summarise(bulletin, names, now)
        assert (picture.home, picture.home_area_entry.name) == ("watch", first)


def test_a_name_is_one_area_not_a_list_of_letters() -> None:
    bulletin = parse_552(HISTORY[20])
    now = bulletin.issued + timedelta(minutes=3)
    assert summarise(bulletin, BAY, now).home == "watch"
    assert summarise(bulletin, None, now).home == "none"
    assert summarise(bulletin, "東京", now).home == "none"
    assert summarise(bulletin, (), now).home_area_entry is None


def test_a_tracker_keeps_the_areas_of_its_place() -> None:
    assert (TsunamiTracker(None, {}).home_area, TsunamiTracker(None, {}).home_areas) == (None, ())
    assert (TsunamiTracker(BAY, {}).home_area, TsunamiTracker(BAY, {}).home_areas) == (BAY, (BAY,))
    two = TsunamiTracker([BAY, SAGAMI], {})
    assert (two.home_area, two.home_areas) == (f"{BAY} / {SAGAMI}", (BAY, SAGAMI))


def test_the_events_of_a_place_on_two_coasts_and_of_a_place_with_none_in_the_bulletin() -> None:
    now = parse_552(HISTORY[20]).issued + timedelta(minutes=3)
    tracker = TsunamiTracker((BAY, SAGAMI), {})
    [raised] = tracker.feed(HISTORY[20], now)
    assert (raised["type"], raised["home_grade"], raised["home_areas"], raised["home_area_in_force"]) == (
        "tsunami", "warning", [BAY, SAGAMI], SAGAMI)
    later = parse_552(HISTORY[16]).issued + timedelta(minutes=3)  # 2025-07-31 16:30, cancelled
    [cancelled] = tracker.feed(HISTORY[16], later)
    assert (cancelled["type"], cancelled["home_areas"], cancelled["home_area"]) == (
        "tsunami_cancelled", [BAY, SAGAMI], f"{BAY} / {SAGAMI}")
    far = TsunamiTracker(("新潟県上中下越",), {})  # a warning elsewhere is announced with no area of its own in force
    [elsewhere] = far.feed(HISTORY[20], now)
    assert (elsewhere["home_grade"], elsewhere["home_area_in_force"], elsewhere["home_areas"]) == (
        "none", None, ["新潟県上中下越"])
