"""Image work (Pillow) never runs on the event loop: Home Assistant's rule for blocking calls."""

from __future__ import annotations

import threading
from unittest.mock import patch

from homeassistant.core import HomeAssistant

from custom_components.jma import coordinator as coordinator_module
from custom_components.jma.parse_layers import kikikuru_level_at
from custom_components.jma.parse_nowcast import band_at

from .conftest import mock_jma, setup


async def test_pillow_functions_run_in_the_executor(tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    hass = tokyo
    loop_thread = threading.get_ident()
    seen: dict[str, list[int]] = {"band_at": [], "kikikuru_level_at": []}

    def spy(name, real):
        def wrapper(*args, **kwargs):
            seen[name].append(threading.get_ident())
            return real(*args, **kwargs)
        return wrapper

    mock_jma(aioclient_mock)
    with (
        patch.object(coordinator_module, "band_at", spy("band_at", band_at)),
        patch.object(coordinator_module, "kikikuru_level_at", spy("kikikuru_level_at", kikikuru_level_at)),
    ):
        await setup(hass, entry)
    # nowcast tiles, rasrf tiles and kikikuru tiles were all decoded ...
    assert len(seen["band_at"]) >= 13 and len(seen["kikikuru_level_at"]) >= 3
    # ... and none of those calls ran on the event loop's thread
    assert all(ident != loop_thread for ident in seen["band_at"] + seen["kikikuru_level_at"])
