# JMA weather (気象庁) for Home Assistant

## What it does

A Home Assistant custom integration (domain `jma`) for any place in Japan. It reads the Japan Meteorological
Agency's public data files and turns them into one weather entity, warning and rain sensors, an event entity
for automations, and earthquake information (shindo, early warnings). You choose a municipality; the
earthquake values and the heat index have limits described under [Known limits](#known-limits). It is a
community project and is not made or endorsed by the Japan Meteorological Agency.

日本語の概要: 気象庁が公開しているデータを使って、日本のどの市区町村でも天気予報・気象警報・雨の予測・台風・地震情報を
Home Assistant に表示するカスタム統合です。HACS のカスタムリポジトリとして追加するか、`custom_components/jma`
を手動でコピーして導入します。設定では、府県予報区、市区町村の順に選びます（観測所は自動で選ばれます）。
地震情報は P2P地震情報から受け取り、都道府県の最大震度（`pref_max_scale` / `pref_shindo`）と、選んだ市区町村の
震度（`local_scale` / `local_shindo`）を出します。暑さ指数（WBGT）は、ファイル名が確認できている予報区
（表にある府県予報区：東京）でのみ作成されます。出典は各サービスの利用規約に従って表示してください。Open-Meteo の無料 API は
非営利目的に限られます。この統合は気象庁が作成・承認したものではありません。

## Requirements

- Home Assistant 2026.9.0 or newer. This is the `homeassistant` value in `hacs.json`; it is a declared
  minimum, not one derived from a test.
- Home Assistant's own home location (Settings, System, General). The rain tiles, the hourly forecast, the
  typhoon distance and the nearest observation stations are taken from it.
- Internet access to the services listed under [Data sources and attribution](#data-sources-and-attribution).
  The integration lists no extra Python requirements.

## Installation with HACS (custom repository)

1. In HACS, open the three-dot menu and choose **Custom repositories**.
2. Repository: `https://github.com/glockpete/ha-jma`, category **Integration**.
3. Install **JMA weather (気象庁)** and restart Home Assistant.

## Installation by hand

Copy the folder `custom_components/jma` of this repository into the `custom_components` folder of your Home
Assistant configuration and restart Home Assistant.

## Set-up

Settings, Devices & services, **Add integration**, **JMA weather (気象庁)**. The flow asks:

1. **Forecast office (府県予報区).** The agency's forecast office for your prefecture or region, listed as
   `name (code)`, for example `東京都 (130000)`. The prefecture is read from the first two digits of the code.
2. **Ward / municipality for warnings (市区町村).** The municipality (class20 area) that warnings and local
   earthquake values are for, listed as `name (code)` for the chosen office, for example `千代田区 (1310100)`.
   Its Japanese name is stored in the entry as `area_name_ja`.
3. **Confirm.** Shows the municipality and the two observation stations chosen for you: the nearest AMeDAS
   station that measures temperature, and the nearest type-A station (used for pressure and for any value
   the first one lacks). Nothing is asked here; submit to finish. This is what the set-up picks; the options
   accept any station id (a five-digit format check) for either slot.

Finding a code: both lists are built from the agency's area list,
<https://www.jma.go.jp/bosai/common/const/area.json> (`offices` for the forecast office, `class20s` for the
municipality). Station ids are in <https://www.jma.go.jp/bosai/amedas/const/amedastable.json>.

The entry is named **JMA** followed by the English name of the municipality (for example `JMA Chiyoda`), and
the entity ids follow it: `weather.jma_chiyoda`, `sensor.jma_chiyoda_warning_level`, and so on. The title can
be changed in the UI.

Options (the entry's **Configure** button): the two station ids (five digits each, checked) and a switch that
enables the test action `jma.simulate`, which fires an alert event as if the agency had produced it.

## Entities

All entities belong to one service device per entry. `<place>` is the lower-case English name of the
municipality, for example `chiyoda`.

| Entity | What it is |
|---|---|
| `weather.jma_<place>` | Current conditions from the AMeDAS stations and the rain nowcast, daily forecast (agency forecast file, with the weekly file for later days) and hourly forecast (Open-Meteo, JMA MSM model). |
| `sensor.jma_<place>_warning_level` | 0 = nothing in force, 2 to 5 = highest warning level for the municipality; the warnings are in the attributes. |
| `sensor.jma_<place>_rain_next_hour` | Highest rain intensity at the Home Assistant home location over the next 60 minutes, mm/h (nowcast). |
| `sensor.jma_<place>_radar_frames` | Frame lists for rain radar map cards (the agency's file has no CORS header, so Home Assistant relays it). |
| `sensor.jma_<place>_overview` | The agency's text overview for the office (Japanese). |
| `sensor.jma_<place>_warning_outlook` | Highest warning level the agency's 3-hourly risk outlook expects for the municipality. |
| `sensor.jma_<place>_typhoon` | Number of active tropical cyclones (0 is a valid state); tracks, wind and distance to the home location in the attributes. |
| `sensor.jma_<place>_wbgt` | Today's highest forecast heat index (WBGT), degrees C. Only where the file name is known; unavailable out of season. |
| `sensor.jma_<place>_kikikuru` | Landslide, flood and inundation risk level (キキクル) 0 to 5 at the home location. |
| `sensor.jma_<place>_rain_outlook` | Rain intensity beyond the nowcast hour at the home location, mm/h. |
| `sensor.jma_<place>_japan` | Nationwide city forecast (22 offices), today and tomorrow. |
| `sensor.jma_<place>_last_earthquake` | Origin time of the newest earthquake reported, with the intensity in the prefecture and at the municipality (below). |
| `binary_sensor.jma_<place>_feed_problem` | On once any feed has been bad for 30 minutes; the attributes say which and why. |
| `event.jma_<place>_alerts` | The alerts an automation can react to (below). |

### Earthquake values

A point (an observation station of the agency) is in **the prefecture** when its prefecture is the one of the
office. It is **local** when, in addition, its address holds the Japanese name of the municipality (for a
ward of a designated city the name is also tried without its first `市`, because that is how the agency writes
it: `神戸市中央区` is matched as `神戸市中央区` and as `神戸中央区`). Addresses are compared as plain text; see
[Known limits](#known-limits) for what that cannot do.

| Name | Meaning |
|---|---|
| `pref`, `local_name` | the prefecture and the municipality name the values are for |
| `pref_max_scale`, `pref_shindo` | highest scale reported by a point in the prefecture, as the P2P code (10 to 70) and as the shindo label (`1`, `2`, `3`, `4`, `5-`, `5- or more`, `5+`, `6-`, `6+`, `7`) |
| `local_scale`, `local_shindo` | the same for the local points; unknown when none reported |
| `max_scale`, `max_shindo` | highest scale anywhere |
| `hypocenter`, `magnitude`, `depth`, `time`, `issue_type`, `domestic_tsunami` | as reported |

### Events (`event.jma_<place>_alerts`)

| Event type | When |
|---|---|
| `warning_issued`, `warning_raised`, `warning_lowered`, `warning_cleared` | a warning for the municipality appears, rises, falls or ends; data: `phenomenon`, `old_level`, `new_level`, `name_ja`, `name_en`, `headline`, `area`, `report_time` |
| `warning_in_force` | on the first poll after a start, a level 4 or 5 warning that is already in force |
| `rain_arriving` | the nowcast expects rain at the home location; data: `minutes`, `band`, `mmh_upper`, `max_mmh` |
| `earthquake` | a point of the prefecture reported (not for quakes older than 10 minutes when history is replayed after a reconnect); data: the earthquake values above |
| `eew` | an early warning (緊急地震速報) names an area of the prefecture, then again only if the forecast scale rises; data: `areas` (the matched area names), `predicted_scale`, `predicted_shindo`, `scale_from`, `scale_to`, `arrival_time`, `hypocenter`, `magnitude`, `update` |
| `eew_cancelled` | the early warning that was announced is cancelled |

## Update intervals

| Feed | Interval | Constant in `const.py` |
|---|---|---|
| Forecast and overview (JMA) | 10 minutes | `INTERVAL_FORECAST` |
| Hourly forecast (Open-Meteo) | 60 minutes | `INTERVAL_HOURLY` |
| AMeDAS observations (JMA) | 10 minutes | `INTERVAL_AMEDAS` |
| Warnings (JMA) | 60 seconds | `INTERVAL_WARNINGS` |
| Cross-check of the warnings against the agency's XML feed | 5 minutes | `INTERVAL_XML` |
| Rain nowcast (JMA tiles) | 5 minutes | `INTERVAL_NOWCAST` |
| Warning outlook (JMA) | 10 minutes | `INTERVAL_TIMELINE` |
| Typhoons (JMA) | 30 minutes | `INTERVAL_TYPHOON` |
| Heat index (Ministry of the Environment) | 60 minutes | `INTERVAL_WBGT` |
| Map-layer frame lists, キキクル, rain outlook (JMA) | 5 minutes | `INTERVAL_LAYERS` |
| Nationwide city forecast (JMA) | 30 minutes | `INTERVAL_JAPAN` |
| Earthquakes and early warnings (P2P地震情報) | one WebSocket per configured place, kept open (pushed; at most two places per home, see [Known limits](#known-limits)), plus the recent history once on every connect | `URL_P2P_WS`, `URL_P2P_HISTORY` |

A failed poll keeps the last good data until it is older than three times its interval (`STALE_FACTOR`);
after that the entities become unavailable. A feed whose file changed shape fails the same way and logs no
traceback.

## Data sources and attribution

Every entity that shows data of these services carries the credit as its `attribution`. If you show
the data in a public place, give the credit its service asks for. The services' terms apply to you as the
user.

| Service | Used for | Credit wording asked for | Limits and conditions |
|---|---|---|---|
| Japan Meteorological Agency, <https://www.jma.go.jp/jma/kishou/info/coment.html> | forecasts, warnings, AMeDAS, nowcast and risk tiles, typhoons | 「コンテンツを利用する際は出典を記載してください。」 Examples: 「出典：気象庁ホームページ　（当該ページのURL）」 and 「「図・写真等の名称」（気象庁ホームページより）」. The entities carry 「出典：気象庁ホームページ（https://www.jma.go.jp/bosai/）」 and 「気象庁の情報を加工して作成」 (the data are edited: levels, frames and forecasts are derived) as their `attribution`; where you display the data, give the address of the page it came from. For edited content: 「コンテンツを編集・加工等して利用する場合は、上記出典とは別に、編集・加工等を行ったことを記載してください。」 | Public Data License 1.0 (「公共データ利用規約（第1.0版）」), unless a page says otherwise. No commercial ban is stated; some content is restricted by law (気象業務法). The page states no request-rate limit. |
| Open-Meteo, <https://open-meteo.com/en/terms> | hourly forecast (JMA MSM model) | "You must give appropriate credit, provide a link to the licence, and indicate if changes were made" and "You must include a link next to any location Open-Meteo data are displayed", for example "Weather data by Open-Meteo.com" with a link to <https://open-meteo.com/> (the weather entity's `attribution` writes the address out) | CC BY 4.0 (licence: <https://open-meteo.com/en/licence>). The integration changes the data by mapping the hourly values to Home Assistant's forecast fields (the wind speed is requested in metres per second, so no unit conversion is made), by leaving out variables that have no value for any hour, by deriving the weather condition from the service's weather code and its day flag, by leaving out rows without a temperature value, and by serving only the hours that have not yet passed. "You may only use the free API services for non-commercial purposes." Free tier: "Less than 10'000 API calls per day, 5'000 per hour and 600 per minute." The integration asks once an hour. |
| P2P地震情報, <https://www.p2pquake.net/develop/json_api_v2/> | earthquake information and early warnings | 「2021 年 4 月 4 日までの気象庁 地震情報・津波予報: CC BY 4.0 準拠であり、気象庁の情報を利用している旨の表示が必要です」 (the page asks for this statement for the agency's earthquake and tsunami information up to the date in the quote; it gives no sentence for later information); no credit wording of its own is given | Free for commercial and non-commercial use (「商用・非商用問わず無償（無料）でご利用になれます」). Rate limits (per IP address): /history 60 requests per minute, /jma 10 requests per minute, /ws 2 connections (this integration uses /history and /ws, not /jma). No warranty (「無保証」). |
| Ministry of the Environment, heat-stress information, <https://www.wbgt.env.go.jp/en/tos.php> | heat index (WBGT) | "Source: Ministry of the Environment Heat Illness Prevention Information website (URL of the relevant page)" and, for edited content, "Created by editing the XX Survey (Ministry of the Environment) (URL of the relevant page)" (the entity's value is a daily maximum computed by the integration; its `attribution` carries both sentences with the site address, "XX Survey" written as the site's name) | Terms: see the ministry's page <https://www.wbgt.env.go.jp/en/tos.php>. The file used is `https://www.wbgt.env.go.jp/prev15WG/dl/yohou_<pref>.csv`. |
| Geospatial Information Authority of Japan, hazard-map portal, <https://disaportal.gsi.go.jp/> | flood-risk map layer listed for map cards | 「出典：「ハザードマップポータルサイト」」 (shown in the layer's attribution); edited: 「「ハザードマップポータルサイト」を加工して作成」 | Public Data License 1.0 unless stated otherwise; third-party rights are the user's to check (「利用者の責任において確認」). |

Where a service states no rate limit, none is stated here; the intervals above are the ones this
integration uses.

## Known limits

- The agency's `/bosai/` JSON files and tiles are not a documented API; their shape can change without
  notice. A changed file makes that feed fail (shown by the feed-problem sensor), not the integration.
- River-flood warnings are not read (the agency keeps them in a separate map).
- The hourly forecast depends on Open-Meteo, whose free API is for **non-commercial use only**.
- Earthquake information comes through P2P地震情報, a volunteer service that gives no guarantee of accuracy
  (「情報の正確性も一切保証しません」). Do not use it as the only way to learn of a danger.
- The earthquake service allows two connections per IP address, and each place keeps one open. At most two places
  per home can receive earthquake information; a third place opens no connection, logs one line saying so, and
  its earthquake sensor stays unavailable.
- The overview text and warning names are in Japanese. The integration has English and Japanese
  translations of its own interface.
- **The heat index (WBGT) is created only for offices whose file name is known to the integration (the offices in the table:
  Tokyo, `130000`); for every other office the WBGT sensor is not created.**
- Weather, forecasts, warnings, rain and typhoons are offered for every municipality (class20 area) of the
  agency's list. The earthquake values for the **prefecture** work everywhere; the `local_*` values depend on
  the name match described under [Earthquake values](#earthquake-values), which has these limits (counted
  over the agency's list in `tests/fixtures/area.json`, 1,805 areas, against the 1,510 station addresses in
  the two P2P files of `tests/fixtures`):
  - An area that is a **whole designated city** (for example the area `千葉市`) never matches: the agency writes
    its stations with the ward (`千葉中央区中央港`). Its `local_*` values stay unknown.
  - **Split areas** whose name does not end in `市`, `区`, `町` or `村` (84 of the 1,805, for example
    `仙台市東部`) never match.
  - A name that sits **inside a longer name** in the same prefecture also matches the longer one's stations
    (23 such pairs in the list; for example the areas `相馬市` (0720900) and `南相馬市` (0721200), or `大阪市` (2710000) and `東大阪市` (2722700)), so
    `local_*` can be too high for such a municipality.
  - In 1,306 of the 1,510 addresses at least one area of the list matches; the others (for example 67 of the
    101 in Kanagawa) match none.
  - For the early warning (`eew`) the prefecture of an area is compared as P2P writes it; whether P2P writes a
    prefecture or a forecast-region name for Hokkaido, Okinawa or the Amami islands is not verified (no real
    early-warning sample in the tests).

## Troubleshooting

- **Diagnostics.** Settings, Devices & services, the integration, three dots, **Download diagnostics**. The file
  holds the health of each feed and the last error of each. Your coordinates, the area code, the area names,
  both station ids and the entry title are redacted (also inside error messages, where the x and y numbers of a
  map tile are replaced too); the office code stays.
- **Feed-problem sensor.** `binary_sensor.jma_<place>_feed_problem` names the feed (`warnings`, `amedas`,
  `amedas_station`, `hourly`, `nowcast`, `forecast`, `quake`) and the reason in its attributes.
- **Log.** Add `custom_components.jma: debug` under `logger:` in `configuration.yaml` for the feed-by-feed
  detail.
- **No heat-index sensor.** Expected outside the Tokyo office, see [Known limits](#known-limits).
- **No earthquake values for a third place.** See [Known limits](#known-limits).

## Privacy

The radar-frames sensor has an attribute `home` that holds the home coordinates of Home Assistant. State
attributes stay inside your Home Assistant installation. The diagnostics file hides the place (coordinates, area
code and names, station ids, entry title).
The hourly forecast is requested from Open-Meteo for the home location of Home Assistant, rounded to four decimals,
so Open-Meteo receives that location with each request.
The typhoon attributes hold the distance from the home location and the closest point of the forecast track to it,
with its time.
Requests to the agency carry the map tile numbers of the home location (the rain nowcast and the map layers) and
the ids of the two observation stations (the AMeDAS point files).

## Removing the integration

1. Settings, Devices & services, **JMA weather (気象庁)**, the three dots of the entry, **Delete**. Repeat for
   every entry.
2. Remove the integration in HACS (or delete the folder `custom_components/jma` if it was installed by hand).
3. Restart Home Assistant.

## Reporting problems

Open an issue at <https://github.com/glockpete/ha-jma/issues>, with the diagnostics file (it hides your place)
and the integration version from `manifest.json`. Please do not post your coordinates.

## Licence

MIT, see `LICENSE`. The data the integration reads belongs to the services listed above and stays under their
terms.

Acknowledgement: some of the rain-tile palette colours were taken from the public project
yucjk0120/jma_nowcast_dashboard.
