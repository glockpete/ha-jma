<p align="center">
  <img src="docs/images/banner.jpg" alt="JMA Weather (Unofficial) for Home Assistant: forecasts, warnings, rain radar, typhoons and earthquakes" width="100%">
</p>

<p align="center">
  <a href="https://github.com/glockpete/ha-jma/actions/workflows/validate.yml"><img alt="Validate" src="https://github.com/glockpete/ha-jma/actions/workflows/validate.yml/badge.svg"></a>
  <a href="https://github.com/glockpete/ha-jma/actions/workflows/tests.yml"><img alt="Tests" src="https://github.com/glockpete/ha-jma/actions/workflows/tests.yml/badge.svg"></a>
  <a href="https://github.com/hacs/integration"><img alt="HACS custom repository" src="https://img.shields.io/badge/HACS-Custom-orange.svg"></a>
  <img alt="Home Assistant 2026.9 or newer" src="https://img.shields.io/badge/Home%20Assistant-2026.9%2B-41BDF5.svg">
  <a href="LICENSE"><img alt="MIT licence" src="https://img.shields.io/badge/licence-MIT-blue.svg"></a>
</p>

# JMA Weather (Unofficial)

Forecasts, warnings, rain radar, typhoons, earthquakes, tsunami and heat-stroke alerts for **any place in Japan**,
read from the public data files of the Japan Meteorological Agency (気象庁) and a few partner services, with no
account and no API key. It is a community project; it is not made, endorsed or supported by the agency.

<p align="center">
  <a href="https://my.home-assistant.io/redirect/hacs_repository/?owner=glockpete&repository=ha-jma&category=integration"><img alt="Open your Home Assistant instance and open this repository in HACS" src="https://my.home-assistant.io/badges/hacs_repository.svg"></a>
  &nbsp;
  <a href="https://my.home-assistant.io/redirect/config_flow_start/?domain=jma"><img alt="Open your Home Assistant instance and start setting up JMA Weather" src="https://my.home-assistant.io/badges/config_flow_start.svg"></a>
</p>

日本語の説明は[下](#日本語の説明)にあります。

## Screenshots

<table>
  <tr>
    <td width="50%"><img src="docs/images/dashboard.png" alt="A dashboard with the weather forecast card and the JMA sensors for Chiyoda, Tokyo, in English"></td>
    <td width="50%"><img src="docs/images/dashboard-ja.png" alt="The same dashboard with the Home Assistant interface in Japanese"></td>
  </tr>
  <tr>
    <td align="center"><sub>Weather card and alert sensors, English interface</sub></td>
    <td align="center"><sub>The same dashboard, Japanese interface</sub></td>
  </tr>
</table>

<p align="center">
  <img src="docs/images/device.png" alt="The device page of the integration: the weather entity, thirteen sensors, the feed-problem sensor and the alerts event entity" width="85%">
  <br><sub>The device page: every entity of one entry. Taken from a Home Assistant 2026.9.4 test instance, with data from the agency's servers.</sub>
</p>

## What you get

- **A weather entity** with current conditions (AMeDAS observations and the rain nowcast), a daily forecast from
  the agency and an hourly forecast (Open-Meteo, JMA MSM model).
- **Warnings for your municipality**: a level sensor (0, or 2 to 5), a 3-hourly outlook, and an event entity that
  tells an automation when a warning is issued, raised, lowered or cleared.
- **Rain**: the highest rain intensity at your home over the next hour (5-minute nowcast), the hours after it,
  a landslide / flood / inundation risk level (キキクル) and frame lists for rain-radar map cards.
- **Typhoons**: every active storm with its track, wind, pressure and distance, and a *typhoon approach* alert when
  one is forecast to come near your home.
- **Earthquakes**: the strongest shindo in your prefecture and in your municipality, plus early warnings
  (緊急地震速報) as events. For the 23 special wards of Tokyo the events follow the 23 wards, not the whole
  prefecture.
- **Tsunami** information as a sensor and as events.
- **Heat-stroke alerts** (熱中症警戒アラート and 熱中症特別警戒アラート) from the Ministry of the Environment, and
  the forecast heat index (WBGT) where it is available.
- **A feed-problem sensor** and a diagnostics download that hides your location.
- English and Japanese interface text.

## Installation

### With HACS (custom repository)

1. Press the **Open in HACS** button above, or in HACS open the three-dot menu, choose **Custom repositories**,
   enter `https://github.com/glockpete/ha-jma` and choose the category **Integration**.
2. Download **JMA Weather (Unofficial)**, then restart Home Assistant.
3. Press the **Set up** button above, or go to Settings, Devices & services, **Add integration**.

### By hand

Copy the folder `custom_components/jma` of this repository into the `custom_components` folder of your Home
Assistant configuration and restart Home Assistant.

### Requirements

- Home Assistant 2026.9.0 or newer (the `homeassistant` value in `hacs.json`; a declared minimum, not one
  derived from a test run on older versions).
- Your home location set in Home Assistant (Settings, System, General). The rain tiles, the hourly forecast, the
  typhoon distance and the nearest observation stations are taken from it.
- Internet access to the services listed under [Data sources and attribution](#data-sources-and-attribution).
  The integration needs no extra Python packages.

## Set-up

Settings, Devices & services, **Add integration**, **JMA Weather (Unofficial)**. The flow asks:

1. **Forecast office (府県予報区).** The agency's forecast office for your prefecture or region, listed as
   `name (code)`, for example `東京都 (130000)`. The prefecture is read from the first two digits of the code.
2. **Ward / municipality for warnings (市区町村).** The municipality (class20 area) that warnings and local
   earthquake values are for, listed as `name (code)` for the chosen office, for example `千代田区 (1310100)`.
   Its Japanese name is stored in the entry as `area_name_ja`.
3. **Confirm.** Shows the municipality and the two observation stations chosen for you: the nearest AMeDAS
   station that measures temperature, and the nearest type-A station (used for pressure and for any value
   the first one lacks). Nothing is asked here; submit to finish.

<details>
<summary>The set-up dialogs</summary>

<img src="docs/images/setup-1-office.png" alt="Step 1: choose the forecast office" width="560">
<img src="docs/images/setup-2-municipality.png" alt="Step 2: choose the municipality" width="560">
<img src="docs/images/setup-3-confirm.png" alt="Step 3: confirm the municipality and the observation stations" width="560">
<img src="docs/images/options.png" alt="The options dialog: two station ids and the test-action switch" width="560">

</details>

Both lists are built from the agency's area list, <https://www.jma.go.jp/bosai/common/const/area.json>
(`offices` for the forecast office, `class20s` for the municipality). Station ids are in
<https://www.jma.go.jp/bosai/amedas/const/amedastable.json>.

The entry is named **JMA** followed by the English name of the municipality (for example `JMA Chiyoda`), and the
entity ids follow it: `weather.jma_chiyoda`, `sensor.jma_chiyoda_warning_level`, and so on. The title can be
changed in the UI.

**Options** (the entry's **Configure** button): the two station ids (five digits each, checked; any AMeDAS
station id is accepted) and a switch that enables the test action `jma.simulate`, which fires an alert event as
if the agency had produced it, so you can try an automation without waiting for a typhoon.

## Entities

All entities of an entry belong to one service device. `<place>` is the lower-case English name of the
municipality, for example `chiyoda`.

| Entity | What it is |
|---|---|
| `weather.jma_<place>` | Current conditions from the AMeDAS stations and the rain nowcast, daily forecast (agency forecast file, with the weekly file for later days) and hourly forecast (Open-Meteo, JMA MSM model). |
| `sensor.jma_<place>_warning_level` | 0 = nothing in force, 2 to 5 = highest warning level for the municipality; the warnings are in the attributes. |
| `sensor.jma_<place>_warning_outlook` | Highest warning level the agency's 3-hourly risk outlook expects for the municipality. |
| `sensor.jma_<place>_rain_next_hour` | Highest rain intensity at the Home Assistant home location over the next 60 minutes, mm/h (nowcast). |
| `sensor.jma_<place>_rain_outlook` | Rain intensity beyond the nowcast hour at the home location, mm/h. |
| `sensor.jma_<place>_kikikuru` | Landslide, flood and inundation risk level (キキクル) 0 to 5 at the home location. |
| `sensor.jma_<place>_radar_frames` | Frame lists for rain radar map cards (the agency's file has no CORS header, so Home Assistant relays it). |
| `sensor.jma_<place>_typhoon` | Number of active tropical cyclones (0 is a valid state). The `storms` attribute holds, for each storm, its position, pressure, wind, forecast and past track, the distance to the home location, the closest approach and the approach `stage` (`none`, `notice`, `urgent`). |
| `sensor.jma_<place>_last_earthquake` | Origin time of the newest earthquake reported, with the intensity in the prefecture and at the municipality ([below](#earthquake-values)). |
| `sensor.jma_<place>_tsunami` | Tsunami grade for your coast area: `none`, `watch`, `warning` or `major_warning`. The attributes carry the strongest grade anywhere in Japan, the areas under a warning, and for your coast area the expected height and arrival. Only places listed under [Known limits](#known-limits) have a coast area. |
| `sensor.jma_<place>_heat_alert` | Today's heat-stroke alert for the prefecture: `none`, `alert` (熱中症警戒アラート) or `special` (熱中症特別警戒アラート). The attributes say what is issued for tomorrow and carry the forecast WBGT figures of the prefecture's stations. Unavailable out of season. |
| `sensor.jma_<place>_wbgt` | Today's highest forecast heat index (WBGT), degrees C. Only where the file name is known ([Known limits](#known-limits)); unavailable out of season. |
| `sensor.jma_<place>_overview` | The agency's text overview for the office (Japanese). |
| `sensor.jma_<place>_japan` | Nationwide city forecast (22 offices), today and tomorrow. |
| `binary_sensor.jma_<place>_feed_problem` | On once any feed has been bad for 30 minutes; the attributes say which and why. |
| `event.jma_<place>_alerts` | The alerts an automation can react to ([below](#events)). |

### Earthquake values

A point (an observation station of the agency) is in **the prefecture** when its prefecture is the one of the
office. It is **local** when, in addition, its address holds the Japanese name of the municipality (for a ward of a
designated city the name is also tried without its first `市`, because that is how the agency writes it:
`神戸市中央区` is matched as `神戸市中央区` and as `神戸中央区`). Addresses are compared as plain text; see
[Known limits](#known-limits) for what that cannot do.

**The area.** An entry in one of the 23 special wards of Tokyo also has an area, `東京都２３区`, between the
prefecture and the municipality. A point is in the area when it is the agency's area-level row for the 23 wards
(address `東京都２３区`) or when its address starts with the name of one of the 23 wards, whichever ward it is
(`東京千代田区丸の内`; a leading `東京都` or `東京` is ignored). The rest of Tokyo prefecture (the Tama cities and the
islands) is not in the area, and neither is a point whose address names no ward, such as `東京国際空港`. An entry
anywhere else, including a Tokyo municipality outside the 23 wards such as `八王子市`, has no area: `area` is empty
and everything follows the prefecture.

An **`earthquake` event** goes out for a quake as soon as a report of it holds at least one point in the scope of
the entry: the area for an entry that has one, the prefecture for every other entry. It goes out again when the
scope's maximum rises, when the local scale rises or when the epicentre becomes known; `change` says which
(`new`, `scale` or `detail`). A report that raises only the prefecture's maximum fires nothing for an entry that has
an area (a quake in Tama or on the islands alone fires nothing for a ward). The newest quake is still shown by
`sensor.jma_<place>_last_earthquake` whatever its scope. The same scope applies to the early warning (`eew`): for a
ward entry only the warning's area `東京都２３区` counts.

| Name | Meaning |
|---|---|
| `pref`, `local_name` | the prefecture and the municipality name the values are for |
| `area` | `東京都２３区` for an entry in one of the 23 special wards of Tokyo, empty for every other entry |
| `pref_max_scale`, `pref_shindo` | highest scale reported by a point in the prefecture, as the P2P code (10 to 70) and as the shindo label (`1`, `2`, `3`, `4`, `5-`, `5- or more`, `5+`, `6-`, `6+`, `7`) |
| `area_max_scale`, `area_shindo` | the same for the points of the area; unknown when none reported, and always unknown for an entry without an area |
| `local_scale`, `local_shindo` | the same for the local points; unknown when none reported |
| `max_scale`, `max_shindo` | highest scale anywhere |
| `hypocenter`, `magnitude`, `depth`, `time`, `issue_type`, `domestic_tsunami` | as reported |

### Events

`event.jma_<place>_alerts` fires these event types. The data of the event is in the entity's attributes.

| Event type | When | Data |
|---|---|---|
| `warning_issued`, `warning_raised`, `warning_lowered`, `warning_cleared` | a warning for the municipality appears, rises, falls or ends | `phenomenon`, `old_level`, `new_level`, `name_ja`, `name_en`, `headline`, `area`, `report_time` |
| `warning_in_force` | on the first poll after a start, a level 4 or 5 warning that is already in force | as above |
| `rain_arriving` | the nowcast expects rain at the home location | `minutes`, `band`, `mmh_upper`, `max_mmh` |
| `earthquake` | a point of the entry's scope reported: the area `東京都２３区` for the 23 special wards of Tokyo, the prefecture everywhere else ([details](#earthquake-values); not for quakes older than 10 minutes when history is replayed after a reconnect) | the [earthquake values](#earthquake-values), and `change` (`new`, `scale`, `detail`) |
| `eew` | an early warning (緊急地震速報) names an area of the prefecture (for the 23 special wards of Tokyo: the area `東京都２３区`), then again only if the forecast scale rises | `areas` (the matched areas of the warning), `area` (the entry's area, empty outside the 23 wards), `predicted_scale`, `predicted_shindo`, `scale_from`, `scale_to`, `arrival_time`, `hypocenter`, `magnitude`, `update` |
| `eew_cancelled` | the early warning that was announced is cancelled | `event_id`, `serial`, `pref`, `area`, `origin_time`, `hypocenter`, `magnitude` |
| `tsunami` | the picture for your coast area changes, or a warning or major warning is in force anywhere in Japan | `id`, `issued`, `home_grade`, `home_grade_ja`, `home_area`, `home_height`, `home_height_m`, `home_arrival`, `home_condition`, `home_immediate`, `max_grade`, `areas_major`, `areas_warning`, `count_major`, `count_warning`, `count_watch`, `previous_home_grade`, `previous_max_grade`, `backfill` |
| `tsunami_cancelled` | the tsunami information that was announced is cancelled or has expired | `id`, `issued`, `home_area`, `previous_home_grade`, `previous_max_grade`, `cancelled`, `expired`, `backfill` |
| `typhoon_approach` | a storm's approach stage to your home rises. **notice**: the forecast track passes within 300 km of your home inside 72 hours, or your home enters the storm-warning zone (暴風警戒域) inside 72 hours. **urgent**: your home enters the storm-warning zone inside 24 hours, or is already inside | `storm_id`, `number`, `number_short`, `name_en`, `name_ja`, `category`, `stage`, `previous_stage`, `closest_km`, `closest_time`, `closest_hours`, `zone_enters_time`, `zone_enters_hours`, `distance_km`, `pressure_hpa`, `max_wind_ms`, `issue` |
| `typhoon_cleared` | a storm that was announced has passed (its closest approach is behind it and three newer forecasts in a row show no approach stage), or it is no longer listed; `reason` is `passed` or `gone` | `storm_id`, `number`, `number_short`, `name_en`, `name_ja`, `previous_stage`, `reason` |
| `heat_alert` | a new 熱中症警戒アラート or 熱中症特別警戒アラート is issued for today or tomorrow | `grade` (`alert` or `special`), `day`, `target_date`, `area`, `wbgt_max`, `wbgt_home`, `special`, `report_time` |

Typhoon stages, heat alerts and tsunami information are remembered across restarts, so a restart does not
announce them again. Earthquakes, early warnings and warnings are not remembered on purpose: they are announced
again after a restart.

### Example automations

```yaml
# A warning for your municipality is issued or goes up.
automation:
  - alias: "JMA warning"
    triggers:
      - trigger: state
        entity_id: event.jma_chiyoda_alerts
    conditions:
      - condition: template
        value_template: >
          {{ trigger.to_state.attributes.event_type in ['warning_issued', 'warning_raised'] }}
    actions:
      - action: notify.notify
        data:
          title: "{{ trigger.to_state.attributes.name_ja }}  (level {{ trigger.to_state.attributes.new_level }})"
          message: "{{ trigger.to_state.attributes.headline }}"

  # An early earthquake warning names your prefecture (for the 23 wards of Tokyo: the 23 wards).
  - alias: "JMA early earthquake warning"
    triggers:
      - trigger: state
        entity_id: event.jma_chiyoda_alerts
    conditions:
      - condition: template
        value_template: "{{ trigger.to_state.attributes.event_type == 'eew' }}"
    actions:
      - action: notify.notify
        data:
          title: "Earthquake early warning"
          message: >
            Expected shindo {{ trigger.to_state.attributes.predicted_shindo }}
            in {{ trigger.to_state.attributes.areas | join(', ') }}.

  # A typhoon is forecast to come near.
  - alias: "JMA typhoon approach"
    triggers:
      - trigger: state
        entity_id: event.jma_chiyoda_alerts
    conditions:
      - condition: template
        value_template: "{{ trigger.to_state.attributes.event_type == 'typhoon_approach' }}"
    actions:
      - action: notify.notify
        data:
          title: "Typhoon {{ trigger.to_state.attributes.number }} {{ trigger.to_state.attributes.name_en }}: {{ trigger.to_state.attributes.stage }}"
          message: >
            Closest approach {{ trigger.to_state.attributes.closest_km }} km
            at {{ trigger.to_state.attributes.closest_time }}.

  # The heat-stroke alert is issued.
  - alias: "JMA heat alert"
    triggers:
      - trigger: state
        entity_id: event.jma_chiyoda_alerts
    conditions:
      - condition: template
        value_template: "{{ trigger.to_state.attributes.event_type == 'heat_alert' }}"
    actions:
      - action: notify.notify
        data:
          message: >
            Heat-stroke alert for {{ trigger.to_state.attributes.day }}
            ({{ trigger.to_state.attributes.grade }}).
```

With the option **Enable the jma.simulate test action** switched on, the action `jma.simulate` fires any of these events with data of
your choice, to try an automation without waiting for the real thing.

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
| Heat-stroke alert (Ministry of the Environment; new files at 05, 10, 14 and 17 JST in season) | 20 minutes | `INTERVAL_HEAT_ALERT` |
| Map-layer frame lists, キキクル, rain outlook (JMA) | 5 minutes | `INTERVAL_LAYERS` |
| Nationwide city forecast (JMA) | 30 minutes | `INTERVAL_JAPAN` |
| Earthquakes, early warnings and tsunami information (P2P地震情報) | one WebSocket per configured place, kept open (pushed; at most two places per home, see [Known limits](#known-limits)); the recent history and the tsunami list are read once on every connect | `URL_P2P_WS`, `URL_P2P_HISTORY`, `URL_P2P_TSUNAMI` |

A failed poll keeps the last good data until it is older than three times its interval (`STALE_FACTOR`); after that
the entities become unavailable. A feed whose file changed shape fails the same way and logs no traceback.

## Data sources and attribution

Every entity that shows data of these services carries the credit as its `attribution`. If you show the data in
a public place, give the credit its service asks for. The services' terms apply to you as the user.

| Service | Used for | Credit wording asked for | Limits and conditions |
|---|---|---|---|
| Japan Meteorological Agency, <https://www.jma.go.jp/jma/kishou/info/coment.html> | forecasts, warnings, AMeDAS, nowcast and risk tiles, typhoons | 「コンテンツを利用する際は出典を記載してください。」 Examples: 「出典：気象庁ホームページ　（当該ページのURL）」 and 「「図・写真等の名称」（気象庁ホームページより）」. The entities carry 「出典：気象庁ホームページ（https://www.jma.go.jp/bosai/）」 and 「気象庁の情報を加工して作成」 (the data are edited: levels, frames and forecasts are derived) as their `attribution`; where you display the data, give the address of the page it came from. For edited content: 「コンテンツを編集・加工等して利用する場合は、上記出典とは別に、編集・加工等を行ったことを記載してください。」 | Public Data License 1.0 (「公共データ利用規約（第1.0版）」), unless a page says otherwise. No commercial ban is stated; some content is restricted by law (気象業務法). The page states no request-rate limit. |
| Open-Meteo, <https://open-meteo.com/en/terms> | hourly forecast (JMA MSM model) | "You must give appropriate credit, provide a link to the licence, and indicate if changes were made" and "You must include a link next to any location Open-Meteo data are displayed", for example "Weather data by Open-Meteo.com" with a link to <https://open-meteo.com/> (the weather entity's `attribution` writes the address out) | CC BY 4.0 (licence: <https://open-meteo.com/en/licence>). The integration changes the data by mapping the hourly values to Home Assistant's forecast fields (the wind speed is requested in metres per second, so no unit conversion is made), by leaving out variables that have no value for any hour, by deriving the weather condition from the service's weather code and its day flag, by leaving out rows without a temperature value, and by serving only the hours that have not yet passed. "You may only use the free API services for non-commercial purposes." Free tier: "Less than 10'000 API calls per day, 5'000 per hour and 600 per minute." The integration asks once an hour. |
| P2P地震情報, <https://www.p2pquake.net/develop/json_api_v2/> | earthquake information, early warnings and tsunami information | 「2021 年 4 月 4 日までの気象庁 地震情報・津波予報: CC BY 4.0 準拠であり、気象庁の情報を利用している旨の表示が必要です」 (the page asks for this statement for the agency's earthquake and tsunami information up to the date in the quote; it gives no sentence for later information); no credit wording of its own is given | Free for commercial and non-commercial use (「商用・非商用問わず無償（無料）でご利用になれます」). Rate limits (per IP address): /history 60 requests per minute, /jma 10 requests per minute, /ws 2 connections. This integration uses /ws and /history, and `/jma/tsunami` once on every connect (and once a minute only until that list has answered). No warranty (「無保証」). |
| Ministry of the Environment, heat-stress information, <https://www.wbgt.env.go.jp/en/tos.php> | heat index (WBGT) and the heat-stroke alerts | "Source: Ministry of the Environment Heat Illness Prevention Information website (URL of the relevant page)" and, for edited content, "Created by editing the XX Survey (Ministry of the Environment) (URL of the relevant page)" (the entity's value is a daily maximum computed by the integration; its `attribution` carries both sentences with the site address, "XX Survey" written as the site's name) | Terms: see the ministry's page <https://www.wbgt.env.go.jp/en/tos.php>. The files used are `https://www.wbgt.env.go.jp/prev15WG/dl/yohou_<pref>.csv` (heat index) and `https://www.wbgt.env.go.jp/alert/dl/<year>/alert_<yyyymmdd>_<hh>.csv` (alerts). |
| Geospatial Information Authority of Japan, hazard-map portal, <https://disaportal.gsi.go.jp/> | flood-risk map layer listed for map cards | 「出典：「ハザードマップポータルサイト」」 (shown in the layer's attribution); edited: 「「ハザードマップポータルサイト」を加工して作成」 | Public Data License 1.0 unless stated otherwise; third-party rights are the user's to check (「利用者の責任において確認」). |

Where a service states no rate limit, none is stated here; the intervals above are the ones this integration uses.

## Known limits

- The agency's `/bosai/` JSON files and tiles are not a documented API; their shape can change without notice. A
  changed file makes that feed fail (shown by the feed-problem sensor), not the integration.
- River-flood warnings are not read (the agency keeps them in a separate map).
- The hourly forecast depends on Open-Meteo, whose free API is for **non-commercial use only**.
- Earthquake and tsunami information comes through P2P地震情報, a volunteer service that gives no guarantee of
  accuracy (「情報の正確性も一切保証しません」). **Do not use this integration as the only way to learn of a
  danger.** It is a convenience for automations, not a warning system.
- The earthquake service allows two connections per IP address, and each place keeps one open. At most two places
  per home can receive earthquake information; a third place opens no connection, logs one line saying so, and its
  earthquake and tsunami sensors stay unavailable.
- The overview text and warning names are in Japanese. The interface text of the integration is in English and
  Japanese.
- **Your tsunami coast area (津波予報区) is known only for Tokyo prefecture, by municipality:** 東京湾内湾 (the 23
  wards and the mainland), 伊豆諸島 (the Izu islands) and 小笠原諸島 (the Ogasawara islands). For every other
  prefecture `sensor.jma_<place>_tsunami` stays `none`; its `max_grade` attribute and the `tsunami` event still
  report a warning or major warning anywhere in Japan, with the areas named.
- **The heat index (WBGT) is created only for offices whose file name is known to the integration (Tokyo,
  `130000`).** For every other office the WBGT sensor is not created. The heat-stroke alert sensor works for every
  office.
- **Only the 23 special wards of Tokyo have an area table.** Earthquake events and early warnings follow the whole
  prefecture everywhere else, including the Tama cities and the islands of Tokyo; the finer areas of the other
  prefectures are not mapped.
- Weather, forecasts, warnings, rain and typhoons are offered for every municipality (class20 area) of the
  agency's list. The earthquake values for the **prefecture** work everywhere; the `local_*` values depend on the
  name match described under [Earthquake values](#earthquake-values), which has the limits below.

<details>
<summary>How well the municipality match works (counted over <code>tests/fixtures/area.json</code>)</summary>

Counted over the agency's list in `tests/fixtures/area.json` (1,805 areas) against the 1,510 station addresses in
the two P2P files of `tests/fixtures`:

- An area that is a **whole designated city** (for example the area `千葉市`) never matches: the agency writes its
  stations with the ward (`千葉中央区中央港`). Its `local_*` values stay unknown.
- **Split areas** whose name does not end in `市`, `区`, `町` or `村` (84 of the 1,805, for example `仙台市東部`)
  never match.
- A name that sits **inside a longer name** in the same prefecture also matches the longer one's stations (23 such
  pairs in the list; for example the areas `相馬市` (0720900) and `南相馬市` (0721200), or `大阪市` (2710000) and
  `東大阪市` (2722700)), so `local_*` can be too high for such a municipality.
- In 1,306 of the 1,510 addresses at least one area of the list matches; the others (for example 67 of the 101 in
  Kanagawa) match none.
- For the early warning (`eew`) the prefecture of an area is compared as P2P writes it; whether P2P writes a
  prefecture or a forecast-region name for Hokkaido, Okinawa or the Amami islands is not verified (no real
  early-warning sample in the tests).

</details>

## Troubleshooting

- **Diagnostics.** Settings, Devices & services, the integration, three dots, **Download diagnostics**. The file
  holds the health of each feed and the last error of each. Your coordinates, the area code, the area names, both
  station ids and the entry title are redacted (also inside error messages, where the x and y numbers of a map tile
  are replaced too); the office code stays.
- **Feed-problem sensor.** `binary_sensor.jma_<place>_feed_problem` names the feed and the reason in its
  attributes.
- **Log.** Add `custom_components.jma: debug` under `logger:` in `configuration.yaml` for the feed-by-feed detail.
- **No heat-index sensor, tsunami sensor always `none`.** Expected outside Tokyo (the heat index needs
  the Tokyo office, the coast area needs a Tokyo municipality), see [Known limits](#known-limits).
- **No earthquake values for a third place.** See [Known limits](#known-limits).

## Privacy

The radar-frames sensor has an attribute `home` that holds the home coordinates of Home Assistant. State attributes
stay inside your Home Assistant installation. The diagnostics file hides the place (coordinates, area code and
names, station ids, entry title).
The hourly forecast is requested from Open-Meteo for the home location of Home Assistant, rounded to four decimals,
so Open-Meteo receives that location with each request.
The typhoon attributes hold the distance from the home location and the closest point of the forecast track to it,
with its time.
Requests to the agency carry the map tile numbers of the home location (the rain nowcast and the map layers) and
the ids of the two observation stations (the AMeDAS point files).

## Removing the integration

1. Settings, Devices & services, **JMA Weather (Unofficial)**, the three dots of the entry, **Delete**. Repeat for
   every entry.
2. Remove the integration in HACS (or delete the folder `custom_components/jma` if it was installed by hand).
3. Restart Home Assistant.

## Reporting problems and contributing

Open an issue at <https://github.com/glockpete/ha-jma/issues> with the diagnostics file (it hides your place) and
the integration version from `manifest.json`. Please do not post your coordinates. See
[CONTRIBUTING.md](CONTRIBUTING.md) to run the tests; a security problem goes through
[SECURITY.md](SECURITY.md).

## Licence

MIT, see [LICENSE](LICENSE). The data the integration reads belongs to the services listed above and stays under
their terms.

Acknowledgement: some of the rain-tile palette colours were taken from the public project
[yucjk0120/jma_nowcast_dashboard](https://github.com/yucjk0120/jma_nowcast_dashboard).

---

## 日本語の説明

気象庁が公開しているデータを使って、日本のどの市区町村でも天気予報・気象警報・雨の予測・台風・地震情報・津波情報・
熱中症警戒アラートを Home Assistant に表示するカスタム統合です（**非公式**。気象庁が作成・承認したものではありません）。
アカウントも API キーも不要です。

- **導入**: HACS の「カスタムリポジトリ」に `https://github.com/glockpete/ha-jma`（カテゴリ：Integration）を追加するか、
  `custom_components/jma` を手動でコピーします。必要な Home Assistant は 2026.9.0 以降です。
- **設定**: 府県予報区、市区町村の順に選びます（観測所は自動で選ばれます）。
- **できること**: 天気（実況・日別・1時間ごと）、警報レベルとイベント、雨の予測（5分ごとのナウキャスト）、キキクル、台風の
  接近通知、都道府県と市区町村の震度、緊急地震速報、津波情報、熱中症警戒アラート。
- **制限**: 津波予報区は東京都（東京湾内湾・伊豆諸島・小笠原諸島）、暑さ指数（WBGT）は東京でのみ使えます。ほかの地域では津波センサーは `none` の
  ままですが、全国の津波警報・大津波警報はイベントと `max_grade` 属性に出ます。
- **東京23区**: 東京23区の市区町村では、地震イベントと緊急地震速報は23区（東京都２３区）の観測点・予報区だけが対象です。
  ほかの地域（東京都の多摩・島しょを含む）は都道府県全体が対象です。
- **地震・津波情報**は P2P地震情報 から受け取ります。**これだけを頼りに避難や安全の判断をしないでください。**
- **出典**: 各サービスの利用規約に従って表示してください。Open-Meteo の無料 API は非営利目的に限られます。
