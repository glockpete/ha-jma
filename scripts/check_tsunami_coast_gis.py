#!/usr/bin/env python3
"""Check custom_components/jma/tsunami_coast_data.py against JMA's GIS data, a second, independent source.

JMA publishes the coast line of every 津波予報区 ("津波予報区") and the outline of every municipality
("市町村等（地震津波関係）") as shapefiles, https://www.data.jma.go.jp/developer/gis.html. They are cut from the
same coast data: 99.7 % of the vertices of the coast lines are, to the last decimal, vertices of a municipality
outline. A municipality is on the coast of a region where its outline shares at least one edge with the region's
coast line. This script counts that and compares it with the table, municipality area by municipality area.

The table is made from JMA's municipality list (a PDF), which is the statement of the agency; this is a check, not a
second source of the table. Known differences (2026-10-24, GIS 20240520 / 20241128, PDF as of 令和8年3月26日):
16 of 1805 class20 areas, listed by the script. In the GIS only: three slivers of a coast line that ends a few
hundred metres inside the next municipality (赤穂市, 岩国市, 直島町), and the parts of a split municipality that the
GIS cannot tell apart (北見市北見, 八雲町八雲, 八雲町熊石). In the table only: the towns on a lake or a bay without
a coast line (佐呂間町, 刈谷市, 与謝野町, 安来市, 小城市) and the inland wards of Kobe and Hiroshima, which the PDF lists
with their city.

    pip install pyshp numpy
    python scripts/check_tsunami_coast_gis.py --area tests/fixtures/area.json
"""

from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import math
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import shapefile  # pyshp

TSUNAMI_ZIP = "https://www.data.jma.go.jp/developer/gis/20240520_AreaTsunami_GIS.zip"
QUAKE_ZIP = "https://www.data.jma.go.jp/developer/gis/20241128_AreaInformationCity_quake_GIS.zip"
MODULE = Path(__file__).resolve().parent.parent / "custom_components" / "jma" / "tsunami_coast_data.py"


def read_shapefile(url: str, directory: Path) -> shapefile.Reader:
    """Download (GET) and open the one shapefile of a JMA zip. Its file names are in the old Windows encoding, so
    only the suffix is used."""
    archive = directory / (url.rsplit("/", 1)[1])
    with urllib.request.urlopen(url, timeout=600) as response:
        archive.write_bytes(response.read())
    target = directory / archive.stem
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(target)
    return shapefile.Reader(str(next(target.rglob("*.shp"))), encoding="utf-8")


def keys(points: Any) -> Any:
    """One integer per vertex: the coordinates to 1e-7 degree."""
    a = np.asarray(points, dtype=np.float64)
    return np.rint(a[:, 0] * 1e7).astype(np.int64) * (1 << 31) + np.rint(a[:, 1] * 1e7).astype(np.int64)


def edge_km(p: Any, q: Any) -> float:
    lat = math.radians((p[1] + q[1]) / 2)
    return math.hypot((p[0] - q[0]) * 111.32 * math.cos(lat), (p[1] - q[1]) * 110.57)


def shared_coast(tsunami: shapefile.Reader, quake: shapefile.Reader) -> tuple[list[tuple[str, str]], dict[tuple[str, int], float]]:
    """([(code, name) of each municipality outline], {(region name, outline index): km of coast line shared})."""
    outlines: list[tuple[str, str]] = []
    all_keys, all_index = [], []
    for i, sr in enumerate(quake.iterShapeRecords()):
        outlines.append((sr.record["regioncode"], sr.record["regionname"]))
        k = keys(sr.shape.points)
        all_keys.append(k)
        all_index.append(np.full(len(k), i, dtype=np.int32))
    sorted_keys = np.concatenate(all_keys)
    order = np.argsort(sorted_keys, kind="stable")
    sorted_keys, index = sorted_keys[order], np.concatenate(all_index)[order]
    shared: dict[tuple[str, int], float] = collections.defaultdict(float)
    for sr in tsunami.iterShapeRecords():
        if sr.record["code"] == "0":  # 帰属未定: coast lines that belong to no region
            continue
        points = np.asarray(sr.shape.points)
        bounds = [*sr.shape.parts, len(points)]
        for a, b in zip(bounds[:-1], bounds[1:], strict=True):
            part = points[a:b]
            lo = np.searchsorted(sorted_keys, keys(part), "left")
            hi = np.searchsorted(sorted_keys, keys(part), "right")
            owners = [set(index[low:high].tolist()) for low, high in zip(lo, hi, strict=True)]
            for j in range(len(part) - 1):
                for i in owners[j] & owners[j + 1]:
                    shared[sr.record["name"], i] += edge_km(part[j], part[j + 1])
    return outlines, dict(shared)


def outlines_of(code: str, outlines: list[tuple[str, str]]) -> list[int]:
    """The outlines that make up a class20 area: the same code; else those of the same municipality (the first five
    digits); else the wards of a designated city (the area list has one area for the city or for its halves)."""
    same = [i for i, (c, _) in enumerate(outlines) if c == code]
    if same:
        return same
    city = [i for i, (c, n) in enumerate(outlines) if c[:5] == code[:5] and "のうち" not in n]
    if city:
        return city
    return [i for i, (c, n) in enumerate(outlines) if c and "のうち" in n and 0 < int(c[:5]) - int(code[:5]) <= 30]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--area", type=Path, required=True, help="area.json")
    parser.add_argument("--tsunami-zip", default=TSUNAMI_ZIP)
    parser.add_argument("--quake-zip", default=QUAKE_ZIP)
    args = parser.parse_args()

    spec = importlib.util.spec_from_file_location("tsunami_coast_data", MODULE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    class20s = json.loads(args.area.read_text(encoding="utf-8"))["class20s"]

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        with read_shapefile(args.tsunami_zip, Path(tmp)) as tsunami, read_shapefile(args.quake_zip, Path(tmp)) as quake:
            outlines, shared = shared_coast(tsunami, quake)
    by_outline: dict[int, dict[str, float]] = collections.defaultdict(dict)
    for (region, i), km in shared.items():
        by_outline[i][region] = km

    agree = 0
    for code, row in sorted(class20s.items()):
        gis: dict[str, float] = {}
        for i in outlines_of(code, outlines):
            gis.update(by_outline.get(i, {}))
        table = set(module.TSUNAMI_COAST.get(code, ()))
        if set(gis) == table:
            agree += 1
            continue
        for region in sorted(set(gis) - table):
            print(f"GIS only    {code} {row['name']}: {region} ({gis[region]:.3f} km of coast line)")
        for region in sorted(table - set(gis)):
            print(f"table only  {code} {row['name']}: {region}")
    print(f"{agree} of {len(class20s)} class20 areas agree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
