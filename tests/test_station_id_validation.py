"""Station ids typed into the options flow. A Japanese input method types full-width digits (４４１３２);
`\\d` matches those, so the id used to be saved as typed and 404 when the station file URL was built,
quietly moving every reading to the other station. The flow now folds full-width to ASCII (NFKC) and
then accepts only five ASCII digits."""
from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from .test_options_flow_http import FLOW, _client

FULL_WIDTH = "４４１３２"  # U+FF14 U+FF14 U+FF11 U+FF13 U+FF12
MIXED = "４４1３2"
ARABIC_INDIC = "٤٤١٣٢"  # U+0664 U+0664 U+0661 U+0663 U+0662, decimal digits that NFKC leaves alone

BAD = ["", "4413", "441320", "4413a", ARABIC_INDIC, "44 132", "４４１３"]


async def _submit(client, entry, primary: str, secondary: str) -> dict:
    flow = await (await client.post(FLOW, json={"handler": entry.entry_id})).json()
    response = await client.post(f"{FLOW}/{flow['flow_id']}", json={
        "station_primary": primary, "station_secondary": secondary, "debug_simulate": False})
    assert response.status == 200, await response.text()
    return await response.json()


async def test_full_width_ids_are_accepted_and_stored_as_ascii(tokyo: HomeAssistant, aioclient_mock, entry,
                                                              hass_client) -> None:
    client = await _client(tokyo, aioclient_mock, entry, hass_client)
    result = await _submit(client, entry, FULL_WIDTH, FULL_WIDTH)
    assert result["type"] == "create_entry", result
    await tokyo.async_block_till_done()
    assert entry.options["station_primary"] == "44132"
    assert entry.options["station_secondary"] == "44132"


async def test_a_mixed_full_width_and_ascii_id_is_accepted_and_stored_as_ascii(tokyo: HomeAssistant, aioclient_mock,
                                                                              entry, hass_client) -> None:
    client = await _client(tokyo, aioclient_mock, entry, hass_client)
    result = await _submit(client, entry, MIXED, MIXED)
    assert result["type"] == "create_entry", result
    await tokyo.async_block_till_done()
    assert entry.options["station_primary"] == "44132"
    assert entry.options["station_secondary"] == "44132"


async def test_full_width_and_ideographic_spaces_around_the_id_are_stripped(tokyo: HomeAssistant, aioclient_mock,
                                                                           entry, hass_client) -> None:
    client = await _client(tokyo, aioclient_mock, entry, hass_client)
    result = await _submit(client, entry, " 44132 ", "　４４１３２　")  # U+3000 is the IME space
    assert result["type"] == "create_entry", result
    await tokyo.async_block_till_done()
    assert entry.options["station_primary"] == "44132"
    assert entry.options["station_secondary"] == "44132"


@pytest.mark.parametrize("bad", BAD)
async def test_a_bad_primary_station_is_refused_for_that_field(tokyo: HomeAssistant, aioclient_mock, entry,
                                                              hass_client, bad: str) -> None:
    client = await _client(tokyo, aioclient_mock, entry, hass_client)
    result = await _submit(client, entry, bad, "44132")
    assert result["type"] == "form" and result["errors"] == {"station_primary": "invalid_station"}
    assert dict(entry.options) == {}  # nothing was saved


@pytest.mark.parametrize("bad", BAD)
async def test_a_bad_secondary_station_is_refused_for_that_field(tokyo: HomeAssistant, aioclient_mock, entry,
                                                                hass_client, bad: str) -> None:
    client = await _client(tokyo, aioclient_mock, entry, hass_client)
    result = await _submit(client, entry, "44132", bad)
    assert result["type"] == "form" and result["errors"] == {"station_secondary": "invalid_station"}
    assert dict(entry.options) == {}  # nothing was saved


async def test_both_bad_fields_are_flagged_together(tokyo: HomeAssistant, aioclient_mock, entry, hass_client) -> None:
    client = await _client(tokyo, aioclient_mock, entry, hass_client)
    result = await _submit(client, entry, ARABIC_INDIC, "")
    assert result["errors"] == {"station_primary": "invalid_station", "station_secondary": "invalid_station"}
