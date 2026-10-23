"""HA quality scale `repair-issues`: two problems a user can fix are shown on the repairs page. An entry without an
earthquake connection (the service allows two per IP address) and an AMeDAS station that stays silent while the other
one answers."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.jma.const import DOMAIN, URL_AMEDAS_POINT

from .conftest import AREA, ENTRY_DATA, mock_jma, setup

TRANSLATIONS = Path(__file__).parent.parent / "custom_components" / "jma" / "translations"


def _issue(hass: HomeAssistant, issue_id: str) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id)


def _three_entries() -> list[MockConfigEntry]:
    """The service allows two connections per IP address (MAX_QUAKE_SOCKETS): the third entry has none."""
    out = [MockConfigEntry(domain=DOMAIN, title="JMA Test", unique_id=AREA, data=ENTRY_DATA)]
    for area, title in (("2710000", "JMA Second"), ("2714000", "JMA Third")):
        out.append(MockConfigEntry(domain=DOMAIN, title=title, unique_id=area, data={**ENTRY_DATA, "area": area}))
    return out


# ---------- C1: no earthquake connection ----------

async def test_the_entry_without_a_quake_connection_has_a_repair_issue_until_one_is_free(
        tokyo: HomeAssistant, aioclient_mock, caplog) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    e1, e2, e3 = _three_entries()
    with caplog.at_level(logging.WARNING):
        for e in (e1, e2, e3):
            await setup(hass, e)
    assert "No earthquake connection for JMA Third" in caplog.text  # the log line stays
    assert _issue(hass, f"no_quake_socket_{e1.entry_id}") is None
    assert _issue(hass, f"no_quake_socket_{e2.entry_id}") is None
    issue = _issue(hass, f"no_quake_socket_{e3.entry_id}")
    assert issue is not None and issue.translation_key == "no_quake_socket"
    assert issue.translation_placeholders == {"title": "JMA Third", "limit": "2"}
    assert issue.is_fixable is False and issue.severity is ir.IssueSeverity.WARNING

    assert await hass.config_entries.async_unload(e1.entry_id)  # a connection comes free
    assert await hass.config_entries.async_reload(e3.entry_id)
    await hass.async_block_till_done()
    assert e3.runtime_data.quake.enabled
    assert _issue(hass, f"no_quake_socket_{e3.entry_id}") is None


async def test_the_quake_issue_goes_with_the_entry_that_has_it(tokyo: HomeAssistant, aioclient_mock) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    e1, e2, e3 = _three_entries()
    for e in (e1, e2, e3):
        await setup(hass, e)
    issue_id = f"no_quake_socket_{e3.entry_id}"
    assert _issue(hass, issue_id) is not None

    assert await hass.config_entries.async_reload(e3.entry_id)  # still no free connection: asked again
    await hass.async_block_till_done()
    assert _issue(hass, issue_id) is not None and not e3.runtime_data.quake.enabled

    assert await hass.config_entries.async_unload(e3.entry_id)
    await hass.async_block_till_done()
    assert _issue(hass, issue_id) is None  # unloaded

    assert await hass.config_entries.async_setup(e3.entry_id)
    await hass.async_block_till_done()
    assert _issue(hass, issue_id) is not None
    await hass.config_entries.async_remove(e3.entry_id)
    await hass.async_block_till_done()
    assert _issue(hass, issue_id) is None  # removed


async def test_a_freed_connection_reloads_the_waiting_entry_and_clears_its_issue(
        tokyo: HomeAssistant, aioclient_mock) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    e1, e2, e3 = _three_entries()
    for e in (e1, e2, e3):
        await setup(hass, e)
    assert _issue(hass, f"no_quake_socket_{e3.entry_id}") is not None
    await hass.config_entries.async_remove(e1.entry_id)  # the entry that held a connection is removed
    await hass.async_block_till_done()
    assert e3.runtime_data.quake.enabled
    assert _issue(hass, f"no_quake_socket_{e3.entry_id}") is None


# ---------- C2: one AMeDAS station silent ----------

def _station_issue_id(entry: MockConfigEntry) -> str:
    return f"station_silent_{entry.entry_id}"


async def _tick(hass: HomeAssistant, freezer, delta: timedelta, times: int = 1) -> None:
    for _ in range(times):
        freezer.tick(delta)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


async def _tick_until(hass: HomeAssistant, freezer, condition, limit: int = 8) -> None:
    """Ten-minute steps until `condition()` holds. The AMeDAS poll is not on every step, so a fixed count is brittle."""
    for _ in range(limit):
        if condition():
            return
        await _tick(hass, freezer, timedelta(minutes=10, seconds=5))
    assert condition()


async def _silent_station_past_the_grace(hass: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    """62016 answers HTTP 500 from the first poll on; 44132 answers. The condition holds once the station has been
    silent for 40 minutes, and the feed-problem sensor reports it 30 minutes (HEALTH_GRACE) after that."""
    aioclient_mock.get(URL_AMEDAS_POINT.format(station="62016", ymd="20260926", hh="12"), status=500)
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    await _tick(hass, freezer, timedelta(minutes=10, seconds=5), times=5)  # 50 min: the reason is shown
    problem = hass.states.get("binary_sensor.jma_chiyoda_feed_problem").attributes["amedas_station"]
    assert problem["reason"] and problem["problem"] is False
    assert _issue(hass, _station_issue_id(entry)) is None  # held, but not yet past the grace
    await _tick_until(hass, freezer, lambda: _issue(hass, _station_issue_id(entry)) is not None)


async def test_a_silent_station_past_the_grace_is_a_repair_issue_until_it_answers(
        tokyo: HomeAssistant, aioclient_mock, entry_two_stations, freezer) -> None:
    hass = tokyo
    entry = entry_two_stations
    await _silent_station_past_the_grace(hass, aioclient_mock, entry, freezer)
    assert hass.states.get("binary_sensor.jma_chiyoda_feed_problem").attributes["amedas_station"]["problem"] is True
    issue = _issue(hass, _station_issue_id(entry))
    assert issue is not None and issue.translation_key == "station_silent"
    assert issue.translation_placeholders == {"station": "62016", "title": "JMA Chiyoda"}
    assert issue.is_fixable is False and issue.severity is ir.IssueSeverity.WARNING

    aioclient_mock.clear_requests()
    mock_jma(aioclient_mock)  # 62016 answers again
    await _tick_until(hass, freezer, lambda: _issue(hass, _station_issue_id(entry)) is None)
    assert hass.states.get("binary_sensor.jma_chiyoda_feed_problem").attributes["amedas_station"]["reason"] is None


async def test_the_station_issue_goes_when_the_entry_unloads(
        tokyo: HomeAssistant, aioclient_mock, entry_two_stations, freezer) -> None:
    hass = tokyo
    entry = entry_two_stations
    await _silent_station_past_the_grace(hass, aioclient_mock, entry, freezer)
    assert _issue(hass, _station_issue_id(entry)) is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert _issue(hass, _station_issue_id(entry)) is None


async def test_one_station_for_both_roles_never_raises_the_station_issue(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    await _tick(hass, freezer, timedelta(minutes=10, seconds=5), times=8)
    assert _issue(hass, _station_issue_id(entry)) is None


# ---------- the texts ----------

def test_the_issue_texts_exist_in_both_languages_with_their_placeholders() -> None:
    wanted = {"no_quake_socket": ("{title}", "{limit}"), "station_silent": ("{station}", "{title}")}
    for language in ("en", "ja"):
        issues = json.loads((TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8"))["issues"]
        assert set(issues) == set(wanted)
        for key, placeholders in wanted.items():
            assert issues[key]["title"] and issues[key]["description"]
            text = issues[key]["title"] + issues[key]["description"]
            for placeholder in placeholders:
                assert placeholder in text, (language, key, placeholder)
