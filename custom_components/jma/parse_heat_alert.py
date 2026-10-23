"""Parse the 環境省 熱中症警戒アラート / 熱中症特別警戒アラート CSV. No HA imports.

https://www.wbgt.env.go.jp/alert/dl/{year}/alert_{ymd}_{hh}.csv (fetched 2026-10-03): UTF-8, LF,
22 `key,value,,,...` meta lines (ReportDate, ReportTime, TargetDate1, TargetDate2, Status ...),
one header line (first cell 府県予報区), then one row per 府県予報区. Row columns: 0 name ·
3 府県予報区等コード · 6 TargetDate1 flag · 7 TargetDate2 flag · 8-10 daily-max WBGT lists
(`station:value/station:value`, empty when not computed). Flags: 0 none · 1 alert issued ·
2 special alert judged (provisional, NOT an issue) · 3 special alert issued · 9 outside issue hours.
A file exists for every issue in the season even when nothing is issued.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, TypedDict

from .const import (
    HEAT_ALERT_GRADE,
    HEAT_ALERT_HOURS,
    HEAT_ALERT_MEMORY_MAX,
    HEAT_ALERT_PUBLISH_LAG,
    HEAT_ALERT_STATUS_NORMAL,
)

JST = timezone(timedelta(hours=9))
_HEADER_FIRST_CELL = "府県予報区"
_COL_CODE, _COL_FLAG1, _COL_FLAG2, _COL_WBGT_FIRST, _COL_WBGT_LAST = 3, 6, 7, 8, 10
_FLAG_OUTSIDE_HOURS = "9"
HEAT_ALERT_NON_ALERT_STATUSES = ("訓練", "試験")  # drill, test: such a file never alerts


@dataclass(frozen=True)
class HeatAlertBulletin:
    report_time: datetime  # JST
    target1: date
    target2: date
    flag1: str
    flag2: str
    area: str  # column 0, e.g. the prefecture's 府県予報区 name
    wbgt: dict[str, int]  # station -> value; the figures are FOR `wbgt_date` (see parse_heat_alert)
    wbgt_max: int | None  # the AREA maximum over every station in `wbgt`: it includes the islands
    wbgt_date: date | None = None
    wbgt_home: int | None = None  # the value of the first requested station name present in `wbgt`
    wbgt_home_station: str | None = None


def _fourth_wednesday(year: int, month: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(2 - first.weekday()) % 7 + 21)  # Monday = 0, Wednesday = 2


def season(year: int) -> tuple[date, date]:
    """First and last day of a season: the 4th Wednesday of April, then 26 weeks later (the
    Wednesday that ends October's run: 2025 = 04-23 ... 10-22, 2026 = 04-22 ... 10-21 as the
    Ministry's pages say). Not "the 4th Wednesday of October": that is 10-28 in 2026."""
    start = _fourth_wednesday(year, 4)
    return start, start + timedelta(weeks=26)


def in_season(day: date) -> bool:
    """Both ends inclusive."""
    start, end = season(day.year)
    return start <= day <= end


def expected_files(now: datetime) -> list[tuple[date, int]]:
    """(issue date JST, hour) of the files that should exist at `now`, newest first: today's
    hours already published (hour + HEAT_ALERT_PUBLISH_LAG passed), then yesterday's."""
    now = now.astimezone(JST)
    today = now.date()
    out = [
        (today, h) for h in sorted(HEAT_ALERT_HOURS, reverse=True)
        if now >= datetime(today.year, today.month, today.day, h, tzinfo=JST) + HEAT_ALERT_PUBLISH_LAG
    ]
    yesterday = today - timedelta(days=1)
    return out + [(yesterday, h) for h in sorted(HEAT_ALERT_HOURS, reverse=True)]


def _date(text: str) -> date:
    return datetime.strptime(text.strip(), "%Y/%m/%d").date()


def _wbgt(*cells: str) -> dict[str, int]:
    """Stations of the last non-empty cell: `練馬:35/東京:33` -> {"練馬": 35, "東京": 33}."""
    cell = next((c for c in reversed(cells) if c.strip()), "")
    out: dict[str, int] = {}
    for item in cell.split("/"):
        name, sep, value = item.rpartition(":")
        try:
            if sep and name.strip():
                out[name.strip()] = int(value)
        except ValueError:
            continue
    return out


def file_status(text: str | None) -> str | None:
    """The file's own `Status` meta value (通常 / 訓練 / 試験), or None when there is none."""
    if not text:
        return None
    try:
        for cells in csv.reader(io.StringIO(text.lstrip("﻿"))):
            if cells and cells[0] == _HEADER_FIRST_CELL:
                return None
            if len(cells) > 1 and cells[0].strip() == "Status":
                return cells[1].strip()
    except csv.Error:  # not this file's layout (a field over the 128 KiB limit): there is no status
        return None
    return None


def parse_heat_alert(text: str | None, office: str, stations: tuple[str, ...] = ()) -> HeatAlertBulletin | None:
    """The office's row, or None: empty text, another layout (HTML, or text csv cannot read), no row for the office, a
    missing / garbled date, or a status other than 通常 (訓練 drill, 試験 test).

    Figures have a day, set by the issue HOUR: ReportTime 05:00 => TargetDate1, from column 10
    (else 9, else 8); any other hour => TargetDate2, from column 9 (else 8; never column 10) (columns 8 and 9 of a day's 17:00 file equal
    those of the next 05:00 file: they are the forecast for the next day). All empty: {} / None.
    `wbgt_max` is the maximum over the whole row, islands included. `stations` are station NAMES as
    the CSV writes them; `wbgt_home` is the value of the first one present."""
    if not text:
        return None
    meta: dict[str, str] = {}
    row: list[str] | None = None
    in_rows = False
    try:
        for cells in csv.reader(io.StringIO(text.lstrip("﻿"))):
            if not cells:
                continue
            if not in_rows:
                if cells[0] == _HEADER_FIRST_CELL:
                    in_rows = True
                elif len(cells) > 1:
                    meta.setdefault(cells[0].strip(), cells[1].strip())
            elif len(cells) > _COL_FLAG2 and cells[_COL_CODE].strip() == office:
                row = cells
                break
    except csv.Error:  # another layout (an HTML page with an unclosed quote: a field over the 128 KiB limit)
        return None
    if not in_rows or row is None or meta.get("Status") != HEAT_ALERT_STATUS_NORMAL:
        return None
    try:
        report_time = datetime.strptime(
            f"{meta['ReportDate']} {meta['ReportTime']}", "%Y/%m/%d %H:%M:%S").replace(tzinfo=JST)
        target1, target2 = _date(meta["TargetDate1"]), _date(meta["TargetDate2"])
    except (KeyError, ValueError):
        return None
    cols = [c.strip() for c in row[_COL_WBGT_FIRST:_COL_WBGT_LAST + 1]]
    cols += [""] * (3 - len(cols))
    wbgt_date: date | None
    if report_time.hour == 5:  # the 05:00 issue: figures for the day itself
        wbgt, wbgt_date = _wbgt(cols[2] or cols[1] or cols[0]), target1
    else:  # any other issue: the forecast for TargetDate2, never the 05:00 column
        wbgt, wbgt_date = _wbgt(cols[1] or cols[0]), target2
    if not wbgt:
        wbgt_date = None
    home = next(((n, wbgt[n]) for n in stations if n in wbgt), (None, None))
    return HeatAlertBulletin(
        report_time=report_time, target1=target1, target2=target2,
        flag1=row[_COL_FLAG1].strip(), flag2=row[_COL_FLAG2].strip(), area=row[0].strip(),
        wbgt=wbgt, wbgt_max=max(wbgt.values()) if wbgt else None, wbgt_date=wbgt_date,
        wbgt_home=home[1], wbgt_home_station=home[0])


def with_home(bulletin: HeatAlertBulletin, stations: tuple[str, ...]) -> HeatAlertBulletin:
    """The same bulletin with `wbgt_home` taken from `stations` (names learnt after it was parsed)."""
    name, value = next(((n, bulletin.wbgt[n]) for n in stations if n in bulletin.wbgt), (None, None))
    return replace(bulletin, wbgt_home=value, wbgt_home_station=name)


def _grade(bulletin: HeatAlertBulletin, day: date) -> str | None:
    """"alert" / "special" / "none" for a date; None (unknown) when the file does not cover it
    or its flag is 9 (outside issue hours)."""
    if day == bulletin.target1:
        flag = bulletin.flag1
    elif day == bulletin.target2:
        flag = bulletin.flag2
    else:
        return None
    if flag == _FLAG_OUTSIDE_HOURS:
        return None
    return HEAT_ALERT_GRADE.get(flag, "none")


class Grades(TypedDict):
    today: str | None
    tomorrow: str | None
    special_judged: bool


def grades(bulletin: HeatAlertBulletin, today: date) -> Grades:
    """{"today": g, "tomorrow": g, "special_judged": bool}; a date's grade comes from whichever
    target column equals it (flag 2 = judged, not issued, is "none" with special_judged set)."""
    return {
        "today": _grade(bulletin, today),
        "tomorrow": _grade(bulletin, today + timedelta(days=1)),
        "special_judged": "2" in (bulletin.flag1, bulletin.flag2),
    }


def new_alerts(bulletin: HeatAlertBulletin, today: date, memory: list[str]) -> list[dict[str, Any]]:
    """One `heat_alert` payload per (target date, grade, day) not announced yet. `memory` (the
    "heat" list) gets the new keys appended in place and is trimmed to HEAT_ALERT_MEMORY_MAX."""
    events: list[dict[str, Any]] = []
    for target, flag, day in (
        (bulletin.target1, bulletin.flag1, "today" if bulletin.target1 == today else None),
        (bulletin.target2, bulletin.flag2,
         "tomorrow" if bulletin.target2 == today + timedelta(days=1) else None),
    ):
        grade = HEAT_ALERT_GRADE.get(flag)
        if day is None or grade is None:
            continue
        key = f"{target.isoformat()}|{grade}|{day}"
        if key in memory:
            continue
        memory.append(key)
        events.append({
            "grade": grade,
            "day": day,
            "target_date": target.isoformat(),
            "report_time": bulletin.report_time.isoformat(),
            "area": bulletin.area,
            # the figures count only for the day they were computed for
            "wbgt_max": bulletin.wbgt_max if bulletin.wbgt_date == target else None,
            "wbgt_home": bulletin.wbgt_home if bulletin.wbgt_date == target else None,
            "special": grade == "special",
        })
    del memory[:-HEAT_ALERT_MEMORY_MAX]
    return events
