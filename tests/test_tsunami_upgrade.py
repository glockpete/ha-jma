"""A tsunami bulletin in force while the integration is upgraded from 0.7.0.

0.7.0 (tag v0.7.0, memory.py and parse_tsunami.py) stored five keys in the "tsunami" section of the alert memory:
active, home, max, id, issued. The loader and the tracker still read and write exactly those, so the bulletin is not
announced again. One thing did change in 0.8.0: 0.7.0 knew the coast area (津波予報区) of the Tokyo areas only and
stored `home` "none" everywhere else. A place whose coast is known since 0.8.0 is told once about the bulletin in
force, now with its own coast's grade, and not again after that.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.jma.const import TSUNAMI_COAST_AREA
from custom_components.jma.memory import AlertMemory
from custom_components.jma.parse_tsunami import TsunamiTracker, parse_552

from .helpers import clone, load_json

HOME = TSUNAMI_COAST_AREA["130010"]  # 東京湾内湾, the coast of Chiyoda (class10 130010) in 0.7.0 and now


def _warning_bulletin() -> dict[str, Any]:
    """SYNTHETIC: the real bulletin of 2025-07-30 20:45 with 東京湾内湾 raised to Warning."""
    msg = clone(load_json("p2p_552_tokyo_bay.json"))
    for area in msg["areas"]:
        if area["name"] == HOME:
            area["grade"] = "Warning"
    msg["id"], msg["issue"]["time"] = "warn-1", "2025/07/30 20:45:06"
    return msg


def _issued(msg: dict[str, Any]):
    bulletin = parse_552(msg)
    assert bulletin is not None
    return bulletin.issued


def _stored_by_0_7_0(msg: dict[str, Any], home: str) -> dict[str, Any]:
    """The "tsunami" section 0.7.0 saved for `msg`: five keys, `issued` an ISO string with the +09:00 offset."""
    return {"active": True, "home": home, "max": "warning", "id": msg["id"], "issued": _issued(msg).isoformat()}


async def test_the_tsunami_section_of_0_7_0_is_loaded_whole(hass: HomeAssistant, hass_storage) -> None:
    section = _stored_by_0_7_0(_warning_bulletin(), "warning")
    key = "jma.abc.alert_memory"
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key,
                         "data": {"typhoon": {}, "heat": [], "tsunami": section}}  # 0.7.0 had no "volcano" section
    memory = AlertMemory(hass, "abc")
    await memory.async_load()
    assert memory.tsunami == section and memory.volcano == {}


def test_a_bulletin_in_force_is_not_announced_again_after_the_upgrade() -> None:
    msg = _warning_bulletin()
    section = _stored_by_0_7_0(msg, "warning")
    # the section the tracker writes today for this bulletin is the one 0.7.0 wrote: the shape did not change
    fresh: dict[str, Any] = {}
    TsunamiTracker(HOME, fresh).feed(msg, _issued(msg) + timedelta(seconds=60))
    assert fresh == section
    memory = json.loads(json.dumps(section))
    tracker = TsunamiTracker([HOME], memory)
    later = _issued(msg) + timedelta(minutes=3)
    assert tracker.replay([msg], later) == []  # the list read at the first connection after the start
    assert tracker.feed(clone(msg), later) == []  # and the same bulletin arriving live
    assert memory == section and tracker.dirty is False


def test_a_place_whose_coast_is_known_since_0_8_0_is_told_once_with_its_own_grade() -> None:
    msg = _warning_bulletin()
    # 0.7.0 for a place outside the Tokyo areas: no coast area, so the bulletin was a warning "somewhere in Japan"
    old: dict[str, Any] = {}
    first = TsunamiTracker(None, old).feed(msg, _issued(msg) + timedelta(seconds=60))
    assert [e["home_grade"] for e in first] == ["none"] and old == _stored_by_0_7_0(msg, "none")

    memory = json.loads(json.dumps(old))
    tracker = TsunamiTracker([HOME], memory)  # the same place after the upgrade: its coast is known now
    later = _issued(msg) + timedelta(minutes=3)
    (event,) = tracker.replay([msg], later)
    assert (event["type"], event["home_grade"], event["home_area_in_force"]) == ("tsunami", "warning", HOME)
    assert memory == _stored_by_0_7_0(msg, "warning") and tracker.dirty is True
    # told once: neither the same bulletin live nor the next read of the list says it again
    assert tracker.feed(clone(msg), later) == []
    assert tracker.replay([msg], later + timedelta(minutes=5)) == []
