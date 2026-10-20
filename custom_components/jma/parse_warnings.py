"""Parse JMA's r8 warnings JSON and check it against the XML feed. No HA imports.

The r8 office file (/bosai/warning/data/r8/{office}.json) is a LIST of records, one per
new-format bulletin type (dataTypeCode VPWW55..VPWW61), each carrying class20Items with
`kinds: [{code, status, ...}]`. An area's warnings are the union over all records.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .const import (
    INACTIVE_STATUSES,
    KIND_EN,
    KIND_JA,
    KNOWN_STATUSES,
    NAME_OVERRIDES_EN,
    NAME_OVERRIDES_JA,
    PHENOMENA,
    WARNING_CODES,
)
from .errors import JmaParseError

_LOGGER = logging.getLogger(__name__)
ATOM = "{http://www.w3.org/2005/Atom}"
_logged_unknown: set[str] = set()


def _log_once(key: str, message: str, *args: Any) -> None:
    if key not in _logged_unknown:
        _logged_unknown.add(key)
        _LOGGER.warning(message, *args)


def is_active(status: Any) -> bool:
    """In force unless 解除 / なし. Step-down statuses carry the new, lower code, still in force."""
    return isinstance(status, str) and bool(status) and status not in INACTIVE_STATUSES


@dataclass(frozen=True)
class Warning:
    """One active warning / advisory for the area."""

    code: str
    phenomenon: str
    level: int
    status: str
    name_ja: str
    name_en: str
    data_type: str | None
    report_time: str | None
    extra: dict[str, Any] = field(default_factory=dict, compare=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code, "phenomenon": self.phenomenon, "level": self.level,
            "status": self.status, "name_ja": self.name_ja, "name_en": self.name_en,
            "data_type": self.data_type, "report_time": self.report_time, **self.extra,
        }


@dataclass(frozen=True)
class AreaWarnings:
    """Everything in force for one class20 area."""

    level: int
    warnings: list[Warning]
    headline: str | None
    report_time: str | None
    control_times: dict[str, str]
    area_missing: bool
    headlines: dict[str, str | None] = field(default_factory=dict)
    # In-force kinds whose code is not in WARNING_CODES: not warnings that can be ranked or alerted on
    # (they must not be listed as level-0 "active warnings" or announced as
    # warning_issued with new_level 0). Surfaced as the warning-level sensor's `unknown_codes`.
    unknown_codes: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class WarningChange:
    """A per-phenomenon change between two polls."""

    event_type: str  # warning_issued | warning_raised | warning_lowered | warning_cleared
    phenomenon: str
    old_level: int
    new_level: int
    warning: Warning


def warning_names(phenomenon: str, level: int) -> tuple[str, str]:
    """(Japanese, English) names, e.g. ('レベル３大雨警報', 'Level 3 heavy rain warning')."""
    stem_ja, stem_en, levelled = PHENOMENA.get(phenomenon, (phenomenon, phenomenon, False))
    ja = NAME_OVERRIDES_JA.get((phenomenon, level))
    en = NAME_OVERRIDES_EN.get((phenomenon, level))
    kind_ja = KIND_JA.get(level, "")
    kind_en = KIND_EN.get(level, "")
    if ja is None:
        prefix = f"レベル{chr(0xFF10 + level)}" if levelled and 0 <= level <= 9 else ""
        ja = f"{prefix}{stem_ja}{kind_ja}"
    if en is None:
        en = f"Level {level} {stem_en} {kind_en}" if levelled else f"{stem_en} {kind_en}"
    return ja, en[:1].upper() + en[1:]


def parse_area_warnings(records: Any, area: str) -> AreaWarnings:
    """The active warnings for one class20 area (e.g. 1310100 千代田区)."""
    if not isinstance(records, list) or not records:
        raise JmaParseError("r8 warnings: expected a non-empty list of records")
    control_times: dict[str, str] = {}
    found = False
    by_code: dict[str, Warning] = {}
    latest_report: str | None = None
    # Headlines only from bulletins with something in force HERE: the newest record of the
    # office can be about another area of the office (a municipality with nothing in force while the headline
    # is about an island group of the same office).
    own: tuple[str, str | None] | None = None
    headlines: dict[str, str | None] = {}
    unknown: dict[tuple[str, str, str], dict[str, Any]] = {}
    try:
        for record in records:
            dtc = record["dataTypeCode"]
            control_times[dtc] = record["controlDatetime"]
            report = record.get("reportDatetime")
            if report and (latest_report is None or report > latest_report):
                latest_report = report
            in_force_here = False
            for item in record["warning"]["class20Items"]:
                if item["areaCode"] != area:
                    continue
                found = True
                for kind in item["kinds"]:
                    status = kind.get("status")
                    code = kind.get("code")
                    if isinstance(status, str) and status not in KNOWN_STATUSES:
                        _log_once(f"status:{status}", "r8 warnings: unknown status %r (code %r), "
                                  "treated as in force", status, code)
                    if not is_active(status) or code is None:
                        continue
                    in_force_here = True
                    if code not in WARNING_CODES:
                        # Not a level-0 warning and not an alert: left out of the list and the diff,
                        # kept visible in `unknown_codes` so a code JMA adds later is not lost.
                        _log_once(f"code:{code}", "r8 warnings: unknown code %r (status %r) in %s: left out of the "
                                  "warning list and never alerted (listed in the sensor's unknown_codes)",
                                  code, status, dtc)
                        unknown.setdefault((str(code), str(status), dtc),
                                           {"code": str(code), "status": str(status), "data_type": dtc})
                        continue
                    phenomenon, level = WARNING_CODES[code]
                    name_ja, name_en = warning_names(phenomenon, level)
                    extra = {k: v for k, v in kind.items() if k not in ("code", "status")}
                    warning = Warning(code, phenomenon, level, status, name_ja, name_en,
                                      dtc, report, extra)
                    have = by_code.get(code)
                    if have is None or (report or "") > (have.report_time or ""):
                        by_code[code] = warning
            if in_force_here:
                headlines[dtc] = record.get("headlineText")
                if own is None or (report or "") > own[0]:
                    own = (report or "", record.get("headlineText"))
    except (KeyError, TypeError, AttributeError) as err:
        raise JmaParseError(f"r8 warnings: {err!r}") from err

    warnings = sorted(by_code.values(), key=lambda w: (-w.level, w.phenomenon, w.code))
    return AreaWarnings(
        level=max((w.level for w in warnings), default=0),
        warnings=warnings,
        headline=own[1] if own else None,
        report_time=latest_report,
        control_times=control_times,
        area_missing=not found,
        headlines=headlines,
        unknown_codes=[unknown[k] for k in sorted(unknown)],
    )


def _by_phenomenon(warnings: list[Warning]) -> dict[str, Warning]:
    out: dict[str, Warning] = {}
    for warning in warnings:
        have = out.get(warning.phenomenon)
        if have is None or warning.level > have.level:
            out[warning.phenomenon] = warning
    return out


def diff_warnings(prev: list[Warning], cur: list[Warning]) -> list[WarningChange]:
    """Per-phenomenon changes, judged on the highest level of each phenomenon."""
    old, new = _by_phenomenon(prev), _by_phenomenon(cur)
    changes: list[WarningChange] = []
    for phenomenon in sorted(old.keys() | new.keys()):
        before, after = old.get(phenomenon), new.get(phenomenon)
        old_level = before.level if before else 0
        new_level = after.level if after else 0
        if before is None and after is not None:
            changes.append(WarningChange("warning_issued", phenomenon, 0, new_level, after))
        elif after is None and before is not None:
            changes.append(WarningChange("warning_cleared", phenomenon, old_level, 0, before))
        elif after is not None and new_level > old_level:
            changes.append(WarningChange("warning_raised", phenomenon, old_level, new_level, after))
        elif after is not None and new_level < old_level:
            changes.append(WarningChange("warning_lowered", phenomenon, old_level, new_level, after))
    return changes


def _utc(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as err:
        raise JmaParseError(f"bad time {value!r}") from err


def _utc_or_none(value: Any) -> datetime | None:
    try:
        return _utc(value)
    except JmaParseError:
        return None


def suspect_reason(prev_control: dict[str, str] | None, area: AreaWarnings) -> str | None:
    """Why this r8 file looks partial or older than the last one believed, else None.

    Diffing such a file would announce a 'cleared' and then, one poll later, a fresh 'issued'
    (an L4 alarm at any hour), so the coordinator holds it for a few polls first.
    """
    if area.area_missing:
        return "the configured area is missing from the file"
    if not prev_control:
        return None
    missing = sorted(set(prev_control) - set(area.control_times))
    if missing:
        return f"records missing: {', '.join(missing)}"
    older: list[str] = []
    unreadable: list[str] = []
    for dtc, when in sorted(area.control_times.items()):
        before = _utc_or_none(prev_control.get(dtc))
        if before is None:
            continue  # new to the file, or accepted earlier with an unreadable time: nothing to compare
        now = _utc_or_none(when)
        if now is None:
            unreadable.append(dtc)
        elif now < before:
            older.append(dtc)
    if older:
        return f"older copy of: {', '.join(older)}"
    if unreadable:
        return f"unreadable control time of: {', '.join(unreadable)}"
    return None


def stale_bulletins(
    xml_text: str, office: str, control_times: dict[str, str], grace_s: int = 600
) -> list[str]:
    """New-format bulletin types the XML feed has for the configured office that the r8 JSON lacks.

    Only VPWW55..VPWW61 (the R06 formats) count. The old-format VPWW53/54 ("（Ｈ２７）")
    keep arriving with no r8 change and must never trip this.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as err:
        raise JmaParseError(f"extra.xml: {err}") from err
    pattern = re.compile(rf"_(VPWW(?:5[5-9]|6[01]))_{re.escape(office)}\.xml$")
    grace = timedelta(seconds=grace_s)
    stale: set[str] = set()
    for entry in root.iter(f"{ATOM}entry"):
        link = entry.find(f"{ATOM}link")
        updated = entry.findtext(f"{ATOM}updated")
        if link is None or not updated:
            continue
        match = pattern.search(link.get("href", ""))
        if not match:
            continue
        dtc = match.group(1)
        control = control_times.get(dtc)
        if control is None or _utc(updated) > _utc(control) + grace:
            stale.add(dtc)
    return sorted(stale)
