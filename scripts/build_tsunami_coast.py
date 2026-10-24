#!/usr/bin/env python3
"""Regenerate custom_components/jma/tsunami_coast_data.py, the table from a municipality to its 津波予報区.

Source: the table the Japan Meteorological Agency publishes for exactly this question,
"津波予報区に対応する沿岸の市町村対応表" (tsunami forecast regions and the coastal municipalities of each):
https://www.jma.go.jp/jma/kishou/know/jishin/joho/tsunami-yohoku/tsunami_shichouson.pdf
The municipality codes are the class20 codes of JMA's area list, which the integration's config flow stores:
https://www.jma.go.jp/bosai/common/const/area.json

Two steps, so that the second one needs no network and no PDF library:

1. `--fetch-rows tests/fixtures/tsunami_shichouson_rows.json` downloads the PDF (GET) and saves the text of its
   table (pdfplumber, `Page.extract_tables()`) with the URL, the date it was fetched, the HTTP status, the
   Last-Modified header and the SHA-256 of the file.
2. `--rows ... --area ... --write` turns those rows into the table. `--check` only compares with the module that
   is in the repository (the tests do the same).

    pip install pdfplumber            # step 1 only
    python scripts/build_tsunami_coast.py --fetch-rows tests/fixtures/tsunami_shichouson_rows.json
    python scripts/build_tsunami_coast.py --area tests/fixtures/area.json --write

The PDF names municipalities, not codes, and writes the name of a few with a word in front of it (the note on the
PDF: municipalities that are read the same way get a part of the prefecture or region before the name, so that
a voice reads them apart), or with a part of the municipality behind it. `resolve` turns each name into the
class20 codes of the same prefecture by three rules and refuses to guess when none or several fit:

    R1  the name is the name of a class20 area, or of a municipality that the area list splits into parts
        (日高町 for 日高町日高 and 日高町門別): that area, or all its parts;
    R2  the name is the start of the names of several class20 areas (a designated city, with a class20 area
        for each ward): all of them;
    R3  the name is such a name with something added before or after it (the longest such name).

A municipality that is on two coasts (横須賀市, 富津市 ...) is listed under both regions in the PDF and gets both.
The class20 parts of a split city all get the coast of their municipality: the PDF does not split them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PDF_URL = "https://www.jma.go.jp/jma/kishou/know/jishin/joho/tsunami-yohoku/tsunami_shichouson.pdf"
AREA_URL = "https://www.jma.go.jp/bosai/common/const/area.json"
MODULE = Path(__file__).resolve().parent.parent / "custom_components" / "jma" / "tsunami_coast_data.py"

# JIS X 0401 order: the first two digits of a class20 code are the position in this list.
PREFECTURES = (
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県", "茨城県", "栃木県", "群馬県", "埼玉県",
    "千葉県", "東京都", "神奈川県", "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県", "岐阜県", "静岡県",
    "愛知県", "三重県", "滋賀県", "京都府", "大阪府", "兵庫県", "奈良県", "和歌山県", "鳥取県", "島根県", "岡山県",
    "広島県", "山口県", "徳島県", "香川県", "愛媛県", "高知県", "福岡県", "佐賀県", "長崎県", "熊本県", "大分県",
    "宮崎県", "鹿児島県", "沖縄県",
)
# The six villages of the Northern Territories (北方領土) that the PDF lists under 北海道太平洋沿岸東部. The area
# list has no class20 area for them; 国後泊村 is written with the 国後 before it so that it is not read as 泊村.
NORTHERN_TERRITORIES = frozenset({"色丹村", "国後泊村", "留夜別村", "留別村", "紗那村", "蘂取村"})

Rows = list[list[str]]  # [region, prefecture cell, municipality cell]


class ResolveError(Exception):
    """A name of the PDF that the rules cannot turn into exactly the codes it means."""


def fetch(url: str) -> tuple[bytes, dict[str, Any]]:
    """GET, nothing else. The headers are kept for the record."""
    request = urllib.request.Request(url, headers={"User-Agent": "ha-jma-table-builder"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read(), {"status": response.status, "last_modified": response.headers.get("Last-Modified")}


def extract_rows(pdf: bytes) -> dict[str, Any]:
    """The table of the PDF as rows. A row with an empty first cell continues the region above it."""
    import io

    import pdfplumber  # step 1 only

    rows: Rows = []
    region = ""
    as_of = ""
    with pdfplumber.open(io.BytesIO(pdf)) as document:
        for page in document.pages:
            if not as_of and (found := re.search(r"\((令和[^)]*?時点)\)", unicodedata.normalize("NFKC", page.extract_text() or ""))):
                as_of = found.group(1)
            for table in page.extract_tables():
                for raw in table:
                    cells = [re.sub(r"\s+", "", cell or "") for cell in raw]
                    if cells[0] == "津波予報区":
                        continue
                    region = cells[0] or region
                    rows.append([region, cells[1], cells[2]])
    return {"as_of": as_of, "rows": rows}


def prefecture_of(cell: str) -> str:
    for name in PREFECTURES:
        if cell.startswith(name):
            return name
    raise ResolveError(f"prefecture not recognised: {cell!r}")


def split_cell(cell: str) -> list[str]:
    """The municipality names of a cell, without the part-of-municipality notes: 松前町（白神岬の東） -> 松前町."""
    names = (re.sub(r"（[^）]*）", "", part) for part in re.sub(r"\s+", "", cell).split("、"))
    return [name for name in names if name]


def municipality_index(names: dict[str, str]) -> dict[str, list[str]]:
    """Every name that can stand for class20 codes: each area's own name, and the name of a municipality that
    the area list splits into parts (the common start of the names of the parts that share the first five digits
    of the code: 日高町日高 and 日高町門別 -> 日高町)."""
    index: dict[str, list[str]] = {n: [code] for code, n in names.items()}
    groups: dict[str, list[str]] = {}
    for code in names:
        groups.setdefault(code[:5], []).append(code)
    for codes in groups.values():
        if len(codes) > 1:
            stem = os.path.commonprefix([names[c] for c in codes]).split("（")[0]
            if len(stem) >= 2:  # also where the stem is the name of one part: 羽幌町 for 羽幌町 and 羽幌町天売焼尻
                index[stem] = list(codes)
    return index


def resolve_name(name: str, names: dict[str, str]) -> list[str]:
    """The class20 codes (of one prefecture: `names` is code -> name) that `name` of the PDF stands for."""
    index = municipality_index(names)
    if name in index:  # R1
        return index[name]
    parts = [code for code, n in names.items() if n.startswith(name)]
    if parts:  # R2
        return parts
    near = [s for s in index if len(s) >= 2 and (name.startswith(s) or name.endswith(s))]
    if not near:
        raise ResolveError(f"no class20 area for {name!r}")
    longest = max(len(s) for s in near)
    best = [s for s in near if len(s) == longest]
    if len(best) != 1:
        raise ResolveError(f"{name!r} fits several class20 areas: {sorted(best)}")
    return index[best[0]]  # R3


def resolve(rows: Rows, class20s: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    """class20 code -> the 津波予報区 names of its municipality, in the order of the PDF."""
    by_prefecture: dict[str, dict[str, str]] = {}
    for code, row in class20s.items():
        by_prefecture.setdefault(code[:2], {})[code] = row["name"]
    table: dict[str, list[str]] = {}
    for region, prefecture_cell, municipality_cell in rows:
        names = by_prefecture.get(f"{PREFECTURES.index(prefecture_of(prefecture_cell)) + 1:02d}", {})
        for name in split_cell(municipality_cell):
            if name in NORTHERN_TERRITORIES:
                continue
            for code in resolve_name(name, names):
                if region not in table.setdefault(code, []):
                    table[code].append(region)
    return {code: tuple(table[code]) for code in sorted(table)}


def render(table: dict[str, tuple[str, ...]], meta: dict[str, Any]) -> str:
    regions = sorted({r for v in table.values() for r in v})
    lines = [
        '"""GENERATED by scripts/build_tsunami_coast.py. Do not edit by hand: run the script.',
        "",
        "The 津波予報区 (tsunami forecast region) of each coastal municipality, by the class20 code of JMA's area",
        "list (the `area` the config flow stores). The names are the ones the tsunami information writes.",
        "",
        f"Source: JMA, 津波予報区に対応する沿岸の市町村対応表 ({meta['as_of']}):",
        f"  {meta['source_url']}",
        f"  fetched {meta['fetched']}: HTTP {meta['http_status']}, Last-Modified {meta['last_modified']},",
        f"  sha256 {meta['sha256']}",
        "Codes: the class20s of JMA's area list:",
        f"  {meta['area_url']}",
        f"{len(table)} municipality areas on a coast, {len(regions)} regions.",
        "A municipality on two coasts has both regions. A municipality that is not here is not on the PDF.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from typing import Final",
        "",
        "TSUNAMI_COAST: Final[dict[str, tuple[str, ...]]] = {",
    ]
    for code, names in table.items():
        lines.append(f'    "{code}": ({", ".join(json.dumps(n, ensure_ascii=False) for n in names)}{"," if len(names) == 1 else ""}),')
    lines.append("}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--fetch-rows", type=Path, help="download the PDF and save its table here (needs pdfplumber)")
    parser.add_argument("--rows", type=Path, default=Path("tests/fixtures/tsunami_shichouson_rows.json"))
    parser.add_argument("--area", type=Path, help="area.json (default: download it)")
    parser.add_argument("--write", action="store_true", help=f"write {MODULE.name}")
    parser.add_argument("--check", action="store_true", help=f"exit 1 if {MODULE.name} is not what the rows give")
    args = parser.parse_args()

    if args.fetch_rows:
        pdf, headers = fetch(PDF_URL)
        saved = {
            "source_url": PDF_URL, "fetched": datetime.now(UTC).date().isoformat(), "http_status": headers["status"],
            "last_modified": headers["last_modified"], "sha256": hashlib.sha256(pdf).hexdigest(), **extract_rows(pdf),
        }
        args.fetch_rows.write_text(json.dumps(saved, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
        print(f"{len(saved['rows'])} rows, {saved['as_of']}, saved to {args.fetch_rows}")
        return 0

    saved = json.loads(args.rows.read_text(encoding="utf-8"))
    area = json.loads(args.area.read_text(encoding="utf-8")) if args.area else json.loads(fetch(AREA_URL)[0])
    table = resolve(saved["rows"], area["class20s"])
    text = render(table, {**saved, "area_url": AREA_URL})
    if args.write:
        MODULE.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {MODULE} ({len(table)} areas)")
    if args.check and MODULE.read_text(encoding="utf-8") != text:
        print(f"{MODULE} differs from what {args.rows} gives", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
