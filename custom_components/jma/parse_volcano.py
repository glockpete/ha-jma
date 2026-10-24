"""Parse JMA volcano data: the bulletins in force, the volcano list, a bulletin's headline, which volcanoes concern a
place, and the level-change tracker. No HA imports.

/bosai/volcano/data/warning.json is a list of bulletins, one per volcano that has a current 噴火警報・予報 (the file
captured on 2026-10-25 holds 14: volcanoes at levels 1 to 3 and volcanoes that have no levels):
{reportDatetime, eventId: "503", areas: ["4321400", ...], volcanoInfos: [{type, items: [...]}, ...]}.
`eventId` is the volcano's code (the `code` of volcano_list.json). `areas` are the municipalities the bulletin names,
as the class20 codes of JMA's area list. The section "噴火警報・予報（対象火山）" holds the volcano's own item,
{name: "レベル２（火口周辺規制）", code: "12", lastCode: "13", condition: "引下げ", areas: [{name, code}]};
const.VOLCANO_KINDS says what each `code` is. /data/warning/{code}.json is the same bulletin with the texts, of which
only `volcanoHeadline` is read. /const/volcano_list.json holds name_jp, name_en and latlon (strings) of the 120
volcanoes.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .const import (
    URL_VOLCANO_PAGE,
    VOLCANO_ABSENT_POLLS,
    VOLCANO_HEADLINE_MAX,
    VOLCANO_KINDS,
    VOLCANO_RADIUS_DEFAULT_KM,
    VOLCANO_RADIUS_MAX_KM,
)
from .errors import JmaParseError
from .parse_typhoon import _position, _when, haversine_km  # the typhoon parser's time and position rules

SECTION_VOLCANO = "噴火警報・予報（対象火山）"
# A volcano code is three digits (JMA's own page reads it with (\d{3})); it goes into a URL, so nothing else is let in.
_CODE = re.compile(r"[0-9]{3}")
_MUNICIPALITY_DIGITS = 5  # JIS X 0401: prefecture (2) + municipality (3); the last two digits of a class20 code split it
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class VolcanoWarning:
    """One bulletin of warning.json, reduced to what the sensor uses."""

    code: str
    name_ja: str | None
    kind_code: str
    kind_ja: str | None
    condition: str | None
    report_time: str
    areas: tuple[str, ...]


@dataclass(frozen=True)
class VolcanoInfo:
    """One volcano of volcano_list.json. The coordinates are None when the row has none that can be used."""

    code: str
    name_ja: str | None
    name_en: str | None
    lat: float | None
    lon: float | None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _record(rec: Any) -> VolcanoWarning | None:
    """One bulletin, or None when it lacks what the sensor needs (the other bulletins are still read)."""
    if not isinstance(rec, dict):
        return None
    code, report, infos = rec.get("eventId"), rec.get("reportDatetime"), rec.get("volcanoInfos")
    if (not isinstance(code, str) or not _CODE.fullmatch(code) or not isinstance(report, str)
            or _when(report) is None or not isinstance(infos, list)):
        return None
    section = next((s for s in infos if isinstance(s, dict) and s.get("type") == SECTION_VOLCANO), None)
    items = section.get("items") if section else None
    item = next((i for i in items if isinstance(i, dict) and _str(i.get("code"))), None) \
        if isinstance(items, list) else None
    if item is None:
        return None
    named = item.get("areas")
    first = named[0] if isinstance(named, list) and named else None
    listed = rec.get("areas")
    return VolcanoWarning(
        code=code,
        name_ja=_str(first.get("name")) if isinstance(first, dict) else None,
        kind_code=str(item["code"]),
        kind_ja=_str(item.get("name")),
        condition=_str(item.get("condition")),
        report_time=report,
        areas=tuple(a for a in listed if isinstance(a, str) and a) if isinstance(listed, list) else (),
    )


def parse_volcano_warnings(data: Any) -> list[VolcanoWarning]:
    """The bulletins of warning.json. A bulletin that cannot be read is left out; a non-empty list in which none can
    be read is a changed file (JmaParseError), so the coordinator keeps its last good data."""
    if not isinstance(data, list):
        raise JmaParseError(f"volcano warning.json: not a list ({type(data).__name__})")
    out: dict[str, VolcanoWarning] = {}
    for rec in data:
        warning = _record(rec)
        if warning is None:
            continue
        known = out.get(warning.code)
        # one bulletin per volcano: if a file lists a volcano twice, the newer report counts
        if known is None or (_when(warning.report_time) or _EPOCH) >= (_when(known.report_time) or _EPOCH):
            out[warning.code] = warning
    if data and not out:
        raise JmaParseError(f"volcano warning.json: none of the {len(data)} bulletins can be read")
    return list(out.values())


def parse_volcano_list(data: Any) -> dict[str, VolcanoInfo]:
    """volcano_list.json -> {code: VolcanoInfo}. A row without a usable code is left out, a row without usable
    coordinates is kept (its names still serve); a non-empty list with no usable row is a changed file."""
    if not isinstance(data, list):
        raise JmaParseError(f"volcano_list.json: not a list ({type(data).__name__})")
    out: dict[str, VolcanoInfo] = {}
    for row in data:
        if not isinstance(row, dict) or not isinstance(row.get("code"), str) or not _CODE.fullmatch(row["code"]):
            continue
        try:
            lat, lon = _position(*row["latlon"])
            coordinates: tuple[float | None, float | None] = (lat, lon)
        except (KeyError, TypeError, ValueError):
            coordinates = (None, None)
        out[row["code"]] = VolcanoInfo(row["code"], _str(row.get("name_jp")), _str(row.get("name_en")), *coordinates)
    if data and not out:
        raise JmaParseError(f"volcano_list.json: none of the {len(data)} rows can be read")
    return out


def _clip(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text: str = value.strip()
    return text if len(text) <= VOLCANO_HEADLINE_MAX else text[:VOLCANO_HEADLINE_MAX - 1] + "…"


def parse_headline(data: Any) -> str | None:
    """`volcanoHeadline` of /data/warning/{code}.json (JMA's own summary line(s), in Japanese), clipped to
    VOLCANO_HEADLINE_MAX characters. None when the bulletin has none."""
    if not isinstance(data, dict):
        raise JmaParseError(f"volcano warning/<code>.json: not an object ({type(data).__name__})")
    return _clip(data.get("volcanoHeadline"))


def radius_km(value: Any) -> float:
    """The distance option as a number of km: 0 (distance rule off) up to VOLCANO_RADIUS_MAX_KM. Anything else (an
    option edited by hand into text, a negative or absurd number) is the default."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return float(VOLCANO_RADIUS_DEFAULT_KM)
    return float(value) if math.isfinite(value) and 0 <= value <= VOLCANO_RADIUS_MAX_KM \
        else float(VOLCANO_RADIUS_DEFAULT_KM)


def municipality_listed(area: str, listed: tuple[str, ...]) -> bool:
    """True when the entry's class20 `area` is one of the municipalities a bulletin names. Compared by the JIS
    municipality code (the first five digits): a municipality that JMA splits into several class20 areas is one
    municipality in a bulletin."""
    if len(area) < _MUNICIPALITY_DIGITS:
        return False
    return any(code == area or code[:_MUNICIPALITY_DIGITS] == area[:_MUNICIPALITY_DIGITS] for code in listed
               if len(code) >= _MUNICIPALITY_DIGITS)


def relevance(warning: VolcanoWarning, info: VolcanoInfo | None, area: str, home: tuple[float, float],
              radius: float) -> tuple[str, float | None] | None:
    """(why it concerns the place, km from home to the volcano or None) or None when it does not.

    A bulletin that names municipalities concerns the place when its municipality is one of them (km is still given,
    for the attribute). A bulletin that names none concerns it when the volcano is within `radius` km of `home`
    (0 = never)."""
    distance = None if info is None or info.lat is None or info.lon is None \
        else round(haversine_km(info.lat, info.lon, *home), 1)
    if warning.areas:
        return ("municipality", distance) if municipality_listed(area, warning.areas) else None
    if distance is not None and radius > 0 and distance <= radius:
        return ("distance", distance)
    return None


def volcano_entry(warning: VolcanoWarning, info: VolcanoInfo | None, why: str, distance: float | None,
                  headline: str | None) -> dict[str, Any]:
    """One volcano as the sensor attribute and the events carry it. `level` is None for a kind code this release does
    not know (the volcano is listed, but the state does not count it)."""
    kind = VOLCANO_KINDS.get(warning.kind_code)
    return {
        "code": warning.code,
        "name_ja": warning.name_ja or (info.name_ja if info else None),
        "name_en": info.name_en if info else None,
        "level": kind[0] if kind else None,
        "level_system": kind[1] if kind else None,
        "warning_type": kind[2] if kind else None,
        "kind_code": warning.kind_code,
        "kind_ja": warning.kind_ja,
        "kind_en": kind[3] if kind else None,
        "condition": warning.condition,
        "headline": headline,
        "report_time": warning.report_time,
        "relevance": why,
        "distance_km": distance,
        "jma_url": URL_VOLCANO_PAGE.format(code=warning.code),
    }


def newest_report(volcanoes: list[dict[str, Any]]) -> str | None:
    """The latest `report_time` of the entries (as JMA wrote it), None when there is none."""
    times = [str(v["report_time"]) for v in volcanoes if _when(v.get("report_time")) is not None]
    return max(times, key=lambda s: _when(s) or _EPOCH, default=None)


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _effective(level: int) -> int:
    """Level 0 (not in the list) and level 1 (噴火予報, "an active volcano") are one state for the events: JMA lists a
    volcano at level 1 for a while after a lowering and then drops it, and that is not a change of activity."""
    return max(level, 1)


class VolcanoTracker:
    """volcano_raised / volcano_lowered decisions. Pure: `memory` is the "volcano" section of AlertMemory,
    {volcano code: {"level", "kind_code", "name_ja", "name_en", "report_time", "absent"}}; it is changed in place
    (every key is read defensively) and `dirty` tells the owner to save.

    An event is sent when a volcano's level changes between two steps of the scale 1 / 2 / 3 / 4 / 5, level 0 (no
    bulletin) counting as 1 (see _effective). Three guards keep a bad file from sending a pair of false events: a
    bulletin dated before the one remembered is ignored; a volcano with a kind code this release does not know is
    left as it was; a remembered volcano missing from the list is lowered to 0 only after VOLCANO_ABSENT_POLLS polls
    in a row without it."""

    def __init__(self, memory: dict[str, Any]) -> None:
        self._memory = memory
        self.dirty = False

    def _entry(self, code: str) -> dict[str, Any]:
        mem = self._memory.get(code)
        return mem if isinstance(mem, dict) else {}

    def _put(self, code: str, entry: dict[str, Any]) -> None:
        if self._memory.get(code) != entry:
            self._memory[code] = entry
            self.dirty = True

    def _forget(self, code: str) -> None:
        if self._memory.pop(code, None) is not None:
            self.dirty = True

    def update(self, volcanoes: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
        """`volcanoes`: the entries of volcano_entry() that concern the place, all of them."""
        events: list[tuple[str, dict[str, Any]]] = []
        present: set[str] = set()
        for volcano in volcanoes:
            code = str(volcano["code"])
            present.add(code)
            old = self._entry(code)
            level = volcano.get("level")
            if not isinstance(level, int):
                continue  # a kind code that is not known: left as it was
            remembered, reported = _when(old.get("report_time")), _when(volcano.get("report_time"))
            if remembered is not None and reported is not None and reported < remembered:
                self._put(code, {**old, "absent": 0})  # an older file than the one already seen
                continue
            before = _count(old.get("level"))
            if _effective(level) != _effective(before):
                kind = "volcano_raised" if _effective(level) > _effective(before) else "volcano_lowered"
                events.append((kind, self._payload(volcano, before, level)))
            self._put(code, {
                "level": level, "kind_code": volcano.get("kind_code"), "name_ja": volcano.get("name_ja"),
                "name_en": volcano.get("name_en"), "report_time": volcano.get("report_time"), "absent": 0,
            })
        for code in [k for k in self._memory if k not in present]:
            old = self._entry(code)
            absent = _count(old.get("absent")) + 1
            if absent < VOLCANO_ABSENT_POLLS:
                self._put(code, {**old, "absent": absent})
                continue
            if _effective(_count(old.get("level"))) > 1:
                events.append(("volcano_lowered", self._gone_payload(code, old)))
            self._forget(code)
        return events

    @staticmethod
    def _payload(volcano: dict[str, Any], old_level: int, new_level: int) -> dict[str, Any]:
        return {
            "volcano_code": volcano["code"],
            "name_ja": volcano.get("name_ja"),
            "name_en": volcano.get("name_en"),
            "old_level": old_level,
            "new_level": new_level,
            "kind_code": volcano.get("kind_code"),
            "kind_ja": volcano.get("kind_ja"),
            "kind_en": volcano.get("kind_en"),
            "warning_type": volcano.get("warning_type"),
            "level_system": volcano.get("level_system"),
            "condition": volcano.get("condition"),
            "headline": volcano.get("headline"),
            "report_time": volcano.get("report_time"),
            "distance_km": volcano.get("distance_km"),
            "jma_url": volcano.get("jma_url"),
        }

    @staticmethod
    def _gone_payload(code: str, old: dict[str, Any]) -> dict[str, Any]:
        """A volcano that left the list: names from memory, level 0, and nothing else JMA no longer says."""
        return {
            "volcano_code": code,
            "name_ja": _str(old.get("name_ja")),
            "name_en": _str(old.get("name_en")),
            "old_level": _count(old.get("level")),
            "new_level": 0,
            "kind_code": None,
            "kind_ja": None,
            "kind_en": None,
            "warning_type": None,
            "level_system": None,
            "condition": None,
            "headline": None,
            "report_time": None,
            "distance_km": None,
            "jma_url": URL_VOLCANO_PAGE.format(code=code),
        }
