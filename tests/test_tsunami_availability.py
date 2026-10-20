"""The tsunami sensor reads the earthquake WebSocket's state, so it is unavailable on an entry that does not hold one.

Only two entries can hold the connection (MAX_QUAKE_SOCKETS). On a third the sensor must read unavailable, not
unknown forever, or a lost tsunami feed there cannot be told from one that was never connected.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.jma.const import DOMAIN

from .conftest import ENTRY_DATA, mock_jma, setup


async def test_a_third_entry_has_an_unavailable_tsunami_sensor(tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    entries = [entry]
    for area, title in (("2710000", "JMA Osaka"), ("2714000", "JMA Sakai")):
        entries.append(MockConfigEntry(domain=DOMAIN, title=title, unique_id=area, data={**ENTRY_DATA, "area": area}))
    for each in entries:
        await setup(hass, each)
    assert [each.runtime_data.quake.enabled for each in entries] == [True, True, False]
    assert hass.states.get("sensor.jma_sakai_tsunami").state == "unavailable"
    for place in ("chiyoda", "osaka"):  # the two entries holding the connection: no replay has run yet
        assert hass.states.get(f"sensor.jma_{place}_tsunami").state == "unknown"
    # the earthquake sensor of the third entry keeps its own behaviour
    assert hass.states.get("sensor.jma_sakai_last_earthquake").state == "unavailable"
    assert hass.states.get("sensor.jma_chiyoda_last_earthquake").state != "unavailable"
