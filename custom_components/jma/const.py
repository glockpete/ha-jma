"""Constants for the JMA (気象庁) weather integration.

The URLs and code tables below follow the agency's published files.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "jma"
VERSION: Final = "0.6.2"
# Names the product and its version only; some services reject the default Python user agent.
USER_AGENT: Final = f"ha-jma/{VERSION}"
# The most bytes of one HTTP answer that are read (after decompression). The largest file the integration
# meets (the sample files in the tests) is below 1 MB; tiles are a few KB. A body past this is refused.
HTTP_MAX_BODY_BYTES: Final = 8 * 1024 * 1024
# Credit wording as each service's terms ask for it. JMA: 「出典：気象庁ホームページ　（当該ページのURL）」 and, for
# edited content, a separate statement that it was edited. Open-Meteo (CC BY 4.0): credit, a link to the licence,
# an indication of changes, and a link next to the data (written out: an attribution is text).
JMA_ATTRIBUTION: Final = "出典：気象庁ホームページ（https://www.jma.go.jp/bosai/） · 気象庁の情報を加工して作成"
OPEN_METEO_ATTRIBUTION: Final = (
    "Weather data by Open-Meteo.com (https://open-meteo.com/), CC BY 4.0 (https://open-meteo.com/en/licence); "
    "values converted to Home Assistant forecast fields"
)
ATTRIBUTION: Final = f"{JMA_ATTRIBUTION} · {OPEN_METEO_ATTRIBUTION}"
# P2P earthquake information: no credit wording of its own; its terms ask for a statement when the agency's
# earthquake information is used.
QUAKE_ATTRIBUTION: Final = "P2P地震情報（https://www.p2pquake.net/） · 気象庁の地震情報を利用"
ALERTS_ATTRIBUTION: Final = f"{JMA_ATTRIBUTION} · {QUAKE_ATTRIBUTION}"

CONF_OFFICE: Final = "office"
CONF_AREA: Final = "area"
CONF_STATION_PRIMARY: Final = "station_primary"
CONF_STATION_SECONDARY: Final = "station_secondary"
CONF_DEBUG_SIMULATE: Final = "debug_simulate"

BOSAI: Final = "https://www.jma.go.jp/bosai"
URL_AREA: Final = f"{BOSAI}/common/const/area.json"
URL_AMEDAS_TABLE: Final = f"{BOSAI}/amedas/const/amedastable.json"
URL_FORECAST: Final = BOSAI + "/forecast/data/forecast/{office}.json"
URL_OVERVIEW: Final = BOSAI + "/forecast/data/overview_forecast/{office}.json"
# Only the r8 path is used. The older /warning/data/warning/{office}.json still answers 200 but is no longer
# updated since JMA's new warning system started.
URL_WARNINGS: Final = BOSAI + "/warning/data/r8/{office}.json"
URL_XML_EXTRA: Final = "https://www.data.jma.go.jp/developer/xml/feed/extra.xml"
URL_AMEDAS_LATEST: Final = f"{BOSAI}/amedas/data/latest_time.txt"
URL_AMEDAS_POINT: Final = BOSAI + "/amedas/data/point/{station}/{ymd}_{hh}.json"
URL_NOWC_TIMES: Final = BOSAI + "/jmatile/data/nowc/targetTimes_{n}.json"
URL_NOWC_TILE: Final = (
    BOSAI + "/jmatile/data/nowc/{basetime}/none/{validtime}/surf/hrpns/{z}/{x}/{y}.png"
)
URL_WARNING_PAGE: Final = BOSAI + "/warning/#area_type=class20s&area_code={area}"
URL_OPEN_METEO: Final = "https://api.open-meteo.com/v1/jma"
URL_P2P_WS: Final = "wss://api.p2pquake.net/v2/ws"
URL_P2P_HISTORY: Final = "https://api.p2pquake.net/v2/history"
# Note: /v2/history?codes=552 answers [] (live check 2026-10-03): tsunami bulletins are replayed from
# their own endpoint (72 messages back to 2016 on that day).
URL_P2P_TSUNAMI: Final = "https://api.p2pquake.net/v2/jma/tsunami"

# ---- warning timeline, typhoon, heat index and map layers (optional feeds) ----
# 3-hourly warning-risk outlook per class20 area (the JMA "timeline" page).
URL_WARNING_TIMELINE: Final = BOSAI + "/warning_timeline/data/{office}.json"
URL_TYPHOON_TARGET: Final = f"{BOSAI}/typhoon/data/targetTc.json"
URL_TYPHOON_FORECAST: Final = BOSAI + "/typhoon/data/{tc}/forecast.json"
URL_TYPHOON_SPECS: Final = BOSAI + "/typhoon/data/{tc}/specifications.json"
# 環境省 WBGT forecast, one CSV per prefecture. Only the offices below are mapped (an office is added
# only with a file name checked against the Ministry's site); for any other office there is no WBGT sensor.
URL_WBGT: Final = "https://www.wbgt.env.go.jp/prev15WG/dl/yohou_{pref}.csv"
WBGT_PREF: Final[dict[str, str]] = {"130000": "tokyo"}
# The ministry's wording (terms page): the source sentence, and for edited content the editing sentence; the
# sensor's value is a daily maximum computed here, so both are given. "XX Survey" is filled with the site's name.
WBGT_ATTRIBUTION: Final = (
    "Source: Ministry of the Environment Heat Illness Prevention Information website (https://www.wbgt.env.go.jp/) "
    "· Created by editing the Heat Illness Prevention Information website (Ministry of the Environment) "
    "(https://www.wbgt.env.go.jp/)"
)
# 環境省 熱中症警戒アラート / 熱中症特別警戒アラート, one CSV per issue (05, 10, 14, 17 JST), named for the
# issue date (manual R08_wbgt_data_service_manual.pdf p.2; fetched live 2026-10-03). UTF-8, LF.
URL_HEAT_ALERT: Final = "https://www.wbgt.env.go.jp/alert/dl/{year}/alert_{ymd}_{hh}.csv"
# Frame lists for map layers (targetTimes send no CORS header, so a browser cannot read them; HA relays them).
URL_RISK_TIMES: Final = f"{BOSAI}/jmatile/data/risk/targetTimes.json"
URL_RASRF_TIMES: Final = f"{BOSAI}/jmatile/data/rasrf/targetTimes.json"
URL_HIMAWARI_TIMES: Final = f"{BOSAI}/himawari/data/satimg/targetTimes_jp.json"
# Tile templates keep every placeholder; frames carry basetime/member/validtime.
# The member is part of the path and varies per frame (rasrf: "immed" for the 10-min run's
# +1..+6 h, "none" for the hourly run; risk: immed0/immed1/immed2/none). JMA's own map JS builds
# `{root}/{basetime}/{member||"none"}/{validtime}/surf/{element}/{z}/{x}/{y}.png`; a "none" frame
# requested under /immed/ answers 404.
URL_THNS_TILE: Final = (
    BOSAI + "/jmatile/data/nowc/{basetime}/none/{validtime}/surf/thns/{z}/{x}/{y}.png"
)
URL_TRNS_TILE: Final = (
    BOSAI + "/jmatile/data/nowc/{basetime}/none/{validtime}/surf/trns/{z}/{x}/{y}.png"
)
# 雷 (LIDEN) strikes per 5-min frame, GeoJSON points (properties.type 4 = cloud-to-ground).
# Not tiled: a consumer fetches it from the browser; HA only lists the frames.
URL_LIDEN_GEOJSON: Final = (
    BOSAI + "/jmatile/data/nowc/{basetime}/none/{validtime}/surf/liden/data.geojson"
)
URL_RISK_TILE: Final = (
    BOSAI + "/jmatile/data/risk/{basetime}/{member}/{validtime}/surf/{elem}/{z}/{x}/{y}.png"
)
URL_RASRF_TILE: Final = (
    BOSAI + "/jmatile/data/rasrf/{basetime}/{member}/{validtime}/surf/rasrf/{z}/{x}/{y}.png"
)
URL_HIMAWARI_TILE: Final = (
    BOSAI + "/himawari/data/satimg/{basetime}/jp/{validtime}/B13/TBB/{z}/{x}/{y}.jpg"
)
URL_GSI_FLOOD_TILE: Final = (
    "https://disaportaldata.gsi.go.jp/raster/01_flood_l2_shinsuishin/{z}/{x}/{y}.png"
)
# Hazard map portal (国土地理院) credit: 「出典：「ハザードマップポータルサイト」」
GSI_FLOOD_ATTRIBUTION: Final = "出典：「ハザードマップポータルサイト」"
# Nationwide city forecast: 22 offices, each {officeCode, name, srf, week}.
URL_JAPAN_FORECAST: Final = f"{BOSAI}/forecast/data/forecast/010000.json"
# officeCode -> (English name, lat, lon). The point is the office's temperature station
# (srf.timeSeries[2] area code, an AMeDAS id), from amedastable.json [deg, min].
JAPAN_CITIES: Final[dict[str, tuple[str, float, float]]] = {
    "014100": ("Kushiro", 42.985, 144.377),  # 19432 釧路
    "012000": ("Asahikawa", 43.757, 142.372),  # 12442 旭川
    "016000": ("Sapporo", 43.06, 141.328),  # 14163 札幌
    "020000": ("Aomori", 40.822, 140.768),  # 31312 青森
    "050000": ("Akita", 39.717, 140.098),  # 32402 秋田
    "040000": ("Sendai", 38.262, 140.897),  # 34392 仙台
    "150000": ("Niigata", 37.893, 139.018),  # 54232 新潟
    "170000": ("Kanazawa", 36.588, 136.633),  # 56227 金沢
    "130000": ("Tokyo", 35.692, 139.75),  # 44132 東京
    "090000": ("Utsunomiya", 36.548, 139.868),  # 41277 宇都宮
    "200000": ("Nagano", 36.662, 138.192),  # 48156 長野
    "230000": ("Nagoya", 35.167, 136.965),  # 51106 名古屋
    "270000": ("Osaka", 34.682, 135.518),  # 62078 大阪
    "370000": ("Takamatsu", 34.318, 134.053),  # 72086 高松
    "320000": ("Matsue", 35.457, 133.065),  # 68132 松江
    "340000": ("Hiroshima", 34.398, 132.462),  # 67437 広島
    "390000": ("Kochi", 33.567, 133.548),  # 74182 高知
    "400000": ("Fukuoka", 33.582, 130.375),  # 82182 福岡
    "460100": ("Kagoshima", 31.555, 130.547),  # 88317 鹿児島
    "460040": ("Amami", 28.385, 129.493),  # 88837 名瀬
    "471000": ("Naha", 26.207, 127.687),  # 91197 那覇
    "474000": ("Ishigaki", 24.337, 124.163),  # 94081 石垣島
}

INTERVAL_FORECAST: Final = timedelta(minutes=10)
INTERVAL_HOURLY: Final = timedelta(minutes=60)
INTERVAL_AMEDAS: Final = timedelta(minutes=10)
INTERVAL_WARNINGS: Final = timedelta(seconds=60)
INTERVAL_XML: Final = timedelta(minutes=5)
INTERVAL_NOWCAST: Final = timedelta(minutes=5)
INTERVAL_TIMELINE: Final = timedelta(minutes=10)
INTERVAL_TYPHOON: Final = timedelta(minutes=30)
INTERVAL_WBGT: Final = timedelta(minutes=60)  # the CSV has 3-hourly steps, issued a few times a day
INTERVAL_LAYERS: Final = timedelta(minutes=5)
INTERVAL_JAPAN: Final = timedelta(minutes=30)
INTERVAL_HEAT_ALERT: Final = timedelta(minutes=20)  # a new file at most four times a day
# Data older than this multiple of its interval makes the entities unavailable.
STALE_FACTOR: Final = 3

NOWCAST_ZOOM: Final = 10
NOWCAST_PAST_FRAMES: Final = 12  # 60 minutes at 5 minutes
RAIN_BAND_RAIN: Final = 2  # band >= 2 means >= 1 mm/h
RAIN_REARM: Final = timedelta(minutes=60)
# What one poll fans out to when a feed lists far more than JMA ever does (a broken or hostile feed): only the
# first entries are read. The captured feeds list 1 storm, 12 nowcast forecast frames (5-min steps) and 13 rasrf
# frames. At most TILE_MAX_INFLIGHT of those requests are in flight at once.
TYPHOON_MAX_STORMS: Final = 8
NOWCAST_MAX_FORECAST: Final = 13
RASRF_MAX_FRAMES: Final = 16
TILE_MAX_INFLIGHT: Final = 6

# Feed-health thresholds.
HEALTH_GRACE: Final = timedelta(minutes=30)
HEALTH_AMEDAS_MAX_AGE: Final = timedelta(minutes=40)
HEALTH_HOURLY_MAX_AGE: Final = timedelta(hours=3)  # Open-Meteo, polled hourly
HEALTH_NOWCAST_MAX_AGE: Final = timedelta(minutes=20)
# JMA issues at 05/11/17 JST, so the feed has an overnight gap of 12 h and a limit shorter than that would flag a problem every night.
HEALTH_FORECAST_MAX_AGE: Final = timedelta(hours=13)
HEALTH_QUAKE_MAX_DOWN: Final = timedelta(minutes=10)

EVENT_TYPES: Final = (
    "warning_issued",
    "warning_raised",
    "warning_lowered",
    "warning_cleared",
    "warning_in_force",  # first poll after a (re)start: an L4/L5 already in force
    "rain_arriving",
    "earthquake",
    "eew",
    "eew_cancelled",
    # safety alerts
    "tsunami",
    "tsunami_cancelled",
    "typhoon_approach",
    "typhoon_cleared",
    "heat_alert",
)

# r8 weather-warning code -> (phenomenon, level). Transcribed from the JMA warning page JS.
# River-flood codes are in a SEPARATE map (flood_xml.json) and reuse 20/21/22, which
# here mean fog / dry / avalanche. River-flood warnings are not read.
WARNING_CODES: Final[dict[str, tuple[str, int]]] = {
    "33": ("rain", 5), "43": ("rain", 4), "03": ("rain", 3), "10": ("rain", 2),
    "39": ("landslide", 5), "49": ("landslide", 4), "09": ("landslide", 3), "29": ("landslide", 2),
    "38": ("tide", 5), "48": ("tide", 4), "08": ("tide", 3), "19": ("tide", 2),
    "35": ("wind", 5), "05": ("wind", 3), "15": ("wind", 2),
    "32": ("wind_snow", 5), "02": ("wind_snow", 3), "13": ("wind_snow", 2),
    "36": ("snow", 5), "06": ("snow", 3), "12": ("snow", 2),
    "37": ("wave", 5), "07": ("wave", 3), "16": ("wave", 2),
    "14": ("thunder", 2), "17": ("snow_melting", 2), "20": ("fog", 2), "21": ("dry", 2),
    "22": ("avalanche", 2), "23": ("cold", 2), "24": ("frost", 2), "25": ("ice_accretion", 2),
    "26": ("snow_accretion", 2),
}

# phenomenon -> (Japanese stem, English stem, carries a レベル number in its name)
PHENOMENA: Final[dict[str, tuple[str, str, bool]]] = {
    "rain": ("大雨", "heavy rain", True),
    "landslide": ("土砂災害", "landslide", True),
    "tide": ("高潮", "storm surge", True),
    "wind": ("暴風", "storm", False),
    "wind_snow": ("暴風雪", "snowstorm", False),
    "snow": ("大雪", "heavy snow", False),
    "wave": ("波浪", "high waves", False),
    "thunder": ("雷", "thunderstorm", False),
    "snow_melting": ("融雪", "snowmelt", False),
    "fog": ("濃霧", "dense fog", False),
    "dry": ("乾燥", "dry air", False),
    "avalanche": ("なだれ", "avalanche", False),
    "cold": ("低温", "low temperature", False),
    "frost": ("霜", "frost", False),
    "ice_accretion": ("着氷", "icing", False),
    "snow_accretion": ("着雪", "snow accretion", False),
}
# Names that do not follow stem + kind.
NAME_OVERRIDES_JA: Final[dict[tuple[str, int], str]] = {
    ("wind", 2): "強風注意報",
    ("wind_snow", 2): "風雪注意報",
}
NAME_OVERRIDES_EN: Final[dict[tuple[str, int], str]] = {
    ("wind", 2): "strong wind advisory",
    ("wind_snow", 2): "gale and snow advisory",
}
KIND_JA: Final[dict[int, str]] = {2: "注意報", 3: "警報", 4: "危険警報", 5: "特別警報"}
KIND_EN: Final[dict[int, str]] = {
    2: "advisory", 3: "warning", 4: "danger warning", 5: "emergency warning",
}
# A kind is in force unless its status says otherwise. Besides 発表/継続, JMA sends step-down
# statuses (特別警報から警報, 警報から注意報, 危険警報から注意報 ...) whose code is the NEW, lower
# warning, still in force. JMA's own warning page counts every status except 解除 (its JS:
# `s.status!=="解除"`); treating only 発表/継続 as active would report a step-down as
# "cleared" and the next bulletin as a fresh alarm.
INACTIVE_STATUSES: Final = frozenset({"解除", "発表警報・注意報はなし"})
KNOWN_STATUSES: Final = INACTIVE_STATUSES | {
    "発表", "継続",
    "特別警報から危険警報", "特別警報から警報", "特別警報から注意報",
    "危険警報から警報", "危険警報から注意報", "警報から注意報",
}
# First poll after a (re)start: an in-force warning at this level or above is announced.
IN_FORCE_AT_START_MIN_LEVEL: Final = 4
# A partial or older r8 file (area or a record missing, a controlDatetime going backwards) is held
# for this many polls before it is believed; a real change is then reported a few minutes late.
WARNINGS_SUSPECT_MAX_POLLS: Final = 2

# The prefecture of an entry = the name for the first two digits of its office code (JIS X 0401
# numbering, which the agency's area codes follow). P2P writes the prefecture of a 551 point in this
# full form ("熊本県") and of a 556 area in a short form ("東京").
PREFECTURES: Final[dict[str, str]] = {
    "01": "北海道", "02": "青森県", "03": "岩手県", "04": "宮城県", "05": "秋田県", "06": "山形県",
    "07": "福島県", "08": "茨城県", "09": "栃木県", "10": "群馬県", "11": "埼玉県", "12": "千葉県",
    "13": "東京都", "14": "神奈川県", "15": "新潟県", "16": "富山県", "17": "石川県", "18": "福井県",
    "19": "山梨県", "20": "長野県", "21": "岐阜県", "22": "静岡県", "23": "愛知県", "24": "三重県",
    "25": "滋賀県", "26": "京都府", "27": "大阪府", "28": "兵庫県", "29": "奈良県", "30": "和歌山県",
    "31": "鳥取県", "32": "島根県", "33": "岡山県", "34": "広島県", "35": "山口県", "36": "徳島県",
    "37": "香川県", "38": "愛媛県", "39": "高知県", "40": "福岡県", "41": "佐賀県", "42": "長崎県",
    "43": "熊本県", "44": "大分県", "45": "宮崎県", "46": "鹿児島県", "47": "沖縄県",
}
# The Japanese name of the area (the municipality chosen at set-up) is stored under this key; the quake
# part matches point addresses with it.
CONF_AREA_NAME_JA: Final = "area_name_ja"
# The one area tier there is: the 23 special wards of Tokyo. The agency's class15 areas 130011 (２３区西部) and
# 130012 (２３区東部) hold exactly these class20 areas (tests/fixtures/area.json; a test compares them name for
# name and code for code). An entry in one of them is scoped to this area: earthquake events and early warnings
# need a ward, not just any point of Tokyo prefecture (Tama and the islands are Tokyo prefecture too).
TOKYO_23_WARDS: Final[dict[str, str]] = {
    "1310100": "千代田区", "1310200": "中央区", "1310300": "港区", "1310400": "新宿区",
    "1310500": "文京区", "1310600": "台東区", "1310700": "墨田区", "1310800": "江東区",
    "1310900": "品川区", "1311000": "目黒区", "1311100": "大田区", "1311200": "世田谷区",
    "1311300": "渋谷区", "1311400": "中野区", "1311500": "杉並区", "1311600": "豊島区",
    "1311700": "北区", "1311800": "荒川区", "1311900": "板橋区", "1312000": "練馬区",
    "1312100": "足立区", "1312200": "葛飾区", "1312300": "江戸川区",
}
# How P2P names it: the area-level 551 address and the 556 area name (full-width ２３; compared after NFKC).
AREA_TOKYO_23: Final = "東京都２３区"
EEW_BACKFILL_MAX_AGE_S: Final = 120
# A warning issued further ahead of the host clock than this is dropped (with one WARNING per event id):
# the clock is wrong. Within it a slow clock still alerts. Mirrors FUTURE_TOLERANCE of the tsunami tracker.
EEW_FUTURE_TOLERANCE_S: Final = 600
QUAKE_BACKFILL_MAX_AGE_S: Final = 600
# P2P scale code -> JMA shindo label.
SHINDO: Final[dict[int, str]] = {
    10: "1", 20: "2", 30: "3", 40: "4", 45: "5-", 46: "5- or more", 50: "5+", 55: "6-", 60: "6+", 70: "7",
}

# hrpns tile palette (PLTE of the tiles) -> band. Band 0 = no rain (transparent).
RAIN_PALETTE: Final[dict[tuple[int, int, int], int]] = {
    (242, 242, 255): 1,
    (160, 210, 255): 2,
    (33, 140, 255): 3,
    (0, 65, 255): 4,
    (250, 245, 0): 5,
    (255, 153, 0): 6,
    (255, 40, 0): 7,
    (180, 0, 104): 8,
    # Reported in some tiles by yucjk0120/jma_nowcast_dashboard, not in the JMA legend.
    (0, 200, 50): 5,
    (0, 130, 30): 5,
}
# band -> (lower mm/h, upper mm/h); band 8 has no upper bound, its sensor value is 80.
BAND_MMH: Final[tuple[tuple[float, float], ...]] = (
    (0, 0), (0, 1), (1, 5), (5, 10), (10, 20), (20, 30), (30, 50), (50, 80), (80, 80),
)

# ---- warning timeline (危険度の時系列) ----
# significancy code -> level, from JMA's own timeline page script (code2CssClass).
# "00" = not applicable (omitted); every other code ("01", "10", "11" ...) is level 0.
TIMELINE_LEVELS: Final[dict[str, int]] = {
    "51": 5, "50": 5, "41": 4, "31": 3, "30": 3, "22": 2, "21": 2, "20": 2,
}
TIMELINE_CODE_NOT_APPLICABLE: Final = "00"
# significancy type -> the phenomenon keys used by the warnings sensor. An unknown type keeps its
# raw name (logged once).
TIMELINE_PHENOMENA: Final[dict[str, str]] = {
    "大雨浸水危険度": "rain",
    "土砂災害危険度": "landslide",
    "高潮危険度": "tide",
    "風危険度": "wind",
    "雪危険度": "snow",
    "波危険度": "wave",
    "雷危険度": "thunder",
    "融雪危険度": "snow_melting",
    "濃霧危険度": "fog",
    "着氷危険度": "ice_accretion",
    "着雪危険度": "snow_accretion",
    "乾燥危険度": "dry",
    "なだれ危険度": "avalanche",
    "低温危険度": "cold",
    "霜危険度": "frost",
}
# forecastParts type -> step field. The digits are FULL-WIDTH (１, ６) as JMA writes them.
TIMELINE_VALUES: Final[dict[str, str]] = {
    "１時間最大雨量": "rain_1h_mm",
    "最大風速": "wind_ms",
    "風向": "wind_dir",
    "６時間最大降雪量": "snow_6h_cm",
}
TIMELINE_TEXT_VALUES: Final = frozenset({"wind_dir"})
# A class20 area on the coast carries 陸上 and 海上 locals; the configured place is on land.
TIMELINE_SEA_LOCAL: Final = "海上"

# ---- typhoon ----
TYPHOON_PAST_TRACK_MAX: Final = 60
TYPHOON_ATTR_MAX_BYTES: Final = 12 * 1024  # the whole `storms` attribute stays below this
EARTH_RADIUS_KM: Final = 6371.0

# ---- map layers + readings at the configured place ----
LAYERS_ZOOM: Final = 10  # the tile of the configured place for キキクル and rasrf, as the nowcast
N3_PAST: Final = timedelta(minutes=60)
RISK_FRAMES: Final = 7  # newest 7 at 10 min = the last hour
RASRF_AFTER: Final = timedelta(minutes=60)  # the nowcast already covers the first hour
HIMAWARI_FRAMES: Final = 12
HIMAWARI_STEP_MIN: Final = 10
LIDEN_FRAMES: Final = 12  # 5-min steps: the last hour
# キキクル legend colours (legend SVGs) -> level. Transparent = 0.
KIKIKURU_PALETTE: Final[dict[tuple[int, int, int], int]] = {
    (255, 255, 255): 1,  # #ffffff 今後の情報等に留意
    (242, 231, 0): 2,  # #f2e700 注意
    (255, 40, 0): 3,  # #ff2800 警戒
    (170, 0, 170): 4,  # #aa00aa 危険
    (12, 0, 12): 5,  # #0c000c 災害切迫
}
# flood_mesh (洪水) differs at level 1 only: cyan, not white.
KIKIKURU_FLOOD_PALETTE: Final[dict[tuple[int, int, int], int]] = {
    **{c: lv for c, lv in KIKIKURU_PALETTE.items() if lv != 1},
    (60, 255, 255): 1,  # #3cffff
}
KIKIKURU_ELEMENTS: Final[dict[str, str]] = {"inund": "inund", "land": "land", "flood": "flood_mesh"}

# ---- safety alerts ----
# Tsunami (P2P 552). class10 code of the place (the area list's third level, which the config flow stores as
# "class10") -> the 津波予報区 whose grade is "home". The office is too coarse: 130000 also holds the Izu and
# Ogasawara islands. Names as P2P writes them (live history 2026-10-03: 東京湾内湾 in the 2025-07-30 bulletins,
# 伊豆諸島 and 小笠原諸島 in the 2025-07-30 / 2025-07-31 / 2026-06-08 ones). 130010 東京地方 (the 23 wards and the
# mainland), 130020 伊豆諸島北部 and 130030 伊豆諸島南部 (both 伊豆諸島), 130040 小笠原諸島. Other prefectures: no entry.
TSUNAMI_COAST_AREA: Final[dict[str, str]] = {
    "130010": "東京湾内湾",
    "130020": "伊豆諸島",
    "130030": "伊豆諸島",
    "130040": "小笠原諸島",
}
# P2P grade -> rank. "Unknown" and anything else rank 0.
TSUNAMI_GRADE_RANK: Final[dict[str, int]] = {"Watch": 1, "Warning": 2, "MajorWarning": 3}
TSUNAMI_GRADE_KEY: Final[tuple[str, ...]] = ("none", "watch", "warning", "major_warning")
TSUNAMI_GRADE_JA: Final[dict[str, str]] = {
    "watch": "津波注意報", "warning": "津波警報", "major_warning": "大津波警報",
}
# A bulletin that was never cancelled stops counting as in force after this long (the longest
# real episode in P2P's history, 2025-07-30 -> 07-31, ran about 32 h).
TSUNAMI_MAX_AGE: Final = timedelta(hours=48)
TSUNAMI_BACKFILL_LIMIT: Final = 10
TSUNAMI_AREAS_MAX: Final = 12  # names per grade carried in events / attributes
TSUNAMI_LIVE_MAX_AGE_S: Final = 600  # a replayed bulletin newer than this counts as live

# Typhoon approach. Stage "notice": the track passes within NOTICE_KM inside NOTICE_HOURS, or home
# is inside the 暴風警戒域 inside NOTICE_HOURS. Stage "urgent": home inside the 暴風域 / 暴風警戒域
# inside URGENT_HOURS. A storm is "cleared" after CLEAR_POLLS polls in a row at stage none.
TYPHOON_NOTICE_KM: Final = 300
TYPHOON_NOTICE_HOURS: Final = 72
TYPHOON_URGENT_HOURS: Final = 24
TYPHOON_CLEAR_POLLS: Final = 3
TYPHOON_STAGE_RANK: Final[dict[str, int]] = {"none": 0, "notice": 1, "urgent": 2}

# Heat alert CSV flags (FlagExplanation line of every file):
# 0 発表無し · 1 熱中症警戒情報発表 · 2 熱中症特別警戒情報判定 · 3 熱中症特別警戒情報発表 · 9 発表時間外
HEAT_ALERT_HOURS: Final[tuple[int, ...]] = (5, 10, 14, 17)
HEAT_ALERT_PUBLISH_LAG: Final = timedelta(minutes=6)  # Last-Modified is ~5 min after the hour
HEAT_ALERT_STATUS_NORMAL: Final = "通常"  # 訓練 (drill) and 試験 (test) files never alert
HEAT_ALERT_GRADE: Final[dict[str, str]] = {"1": "alert", "3": "special"}
HEAT_ALERT_MEMORY_MAX: Final = 40
