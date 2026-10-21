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

Weather forecasts, warnings, rain radar, typhoons, earthquakes, tsunami information and heat-stroke alerts for
**any place in Japan**, in Home Assistant.

The data comes from the public files of 気象庁 (the Japan Meteorological Agency, JMA) and a few partner services.
You need no account and no API key.

> **Important.** This is a community project. JMA did not make it, does not endorse it and does not support it.
> **Do not use it for evacuation or safety decisions, and do not rely on it as your only way to learn of a
> danger.** It is a convenience for automations, not a warning system.

<p align="center">
  <a href="https://my.home-assistant.io/redirect/hacs_repository/?owner=glockpete&repository=ha-jma&category=integration"><img alt="Open your Home Assistant instance and open this repository in HACS" src="https://my.home-assistant.io/badges/hacs_repository.svg"></a>
  &nbsp;
  <a href="https://my.home-assistant.io/redirect/config_flow_start/?domain=jma"><img alt="Open your Home Assistant instance and start setting up JMA Weather" src="https://my.home-assistant.io/badges/config_flow_start.svg"></a>
</p>

日本語 / Japanese: [README.ja.md](README.ja.md)

## Contents

- [Screenshots](#screenshots)
- [Features](#features)
- [Install](#install)
- [Set up](#set-up)
- [Use it in automations](#use-it-in-automations)
- [Entities](#entities)
- [Events](#events)
- [Earthquake values](#earthquake-values)
- [Update intervals](#update-intervals)
- [Known limits](#known-limits)
- [Data sources and attribution](#data-sources-and-attribution)
- [Privacy](#privacy)
- [Troubleshooting](#troubleshooting)
- [Removing the integration](#removing-the-integration)
- [Reporting problems and contributing](#reporting-problems-and-contributing)
- [Licence](#licence)

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
  <br><sub>The device page shows every entity of one place. Taken from a Home Assistant 2026.9.4 test instance,
  with data from JMA's servers.</sub>
</p>

## Features

- **Weather.** Current conditions from JMA's AMeDAS weather stations and the rain nowcast, a daily forecast from
  JMA, and an hourly forecast from Open-Meteo (JMA's MSM model).
- **Warnings for your municipality.** A level sensor (0, or 2 to 5), a 3-hourly outlook, and events when a warning
  is issued, raised, lowered or cleared.
- **Rain.** The heaviest rain expected at your home in the next hour (5-minute nowcast), the rain outlook for the
  hours after that, and the キキクル risk level (JMA's landslide, flood and inundation risk map).
- **Map layers.** Frame lists for rain radar and other JMA map layers, for map cards on a dashboard.
- **Typhoons.** Every active storm with its track, wind, pressure and distance, and an alert when one is forecast
  to come near your home.
- **Earthquakes.** The strongest shindo (震度, the Japanese intensity scale) in your prefecture and in your
  municipality, and early warnings (緊急地震速報) as events. In the 23 special wards of Tokyo, these follow the
  23 wards instead of the whole prefecture.
- **Tsunami.** The tsunami grade for your coast as a sensor, and events.
- **Heat stroke.** The Ministry of the Environment's heat-stroke alerts (熱中症警戒アラート, heat-stroke alert, and
  熱中症特別警戒アラート, special heat-stroke alert), and the forecast heat index (WBGT,
  wet-bulb globe temperature) where it is available.
- **Health.** A feed-problem sensor, and a diagnostics download that hides your location.
- Interface text in English and Japanese.

## Install

### Requirements

- Home Assistant 2026.9.0 or newer. This is the minimum declared in `hacs.json`; older versions were not tested.
- Your home location set in Home Assistant (**Settings > System > General**). The integration uses it for the rain
  values, the hourly forecast, the typhoon distance and the choice of the nearest weather stations.
- Internet access to the services in [Data sources and attribution](#data-sources-and-attribution).

The integration needs no extra Python packages.

### Install with HACS

1. Select the **Open in HACS** button above. Or, in HACS, open the three-dot menu, select **Custom repositories**,
   enter `https://github.com/glockpete/ha-jma` and select the type **Integration**.
2. Download **JMA Weather (Unofficial)**.
3. Restart Home Assistant.
4. Continue with [Set up](#set-up).

### Install by hand

1. Copy the folder `custom_components/jma` of this repository into the `custom_components` folder of your Home
   Assistant configuration.
2. Restart Home Assistant.
3. Continue with [Set up](#set-up).

## Set up

1. Select the **Set up** button above, or go to **Settings > Devices & services > Add integration** and select
   **JMA Weather (Unofficial)**.
2. **Choose the forecast office (府県予報区).** This is JMA's forecast office for your prefecture or region. The
   list shows `name (code)`, for example `東京都 (130000)` (Tokyo). The first two digits of the code give the
   prefecture.
3. **Choose the municipality (市区町村).** Warnings and local earthquake values are for this municipality (a JMA
   "class20" area). The list shows the municipalities of the office you chose, for example `千代田区 (1310100)`
   (Chiyoda ward).
4. **Confirm.** The dialog shows the municipality and the two weather stations chosen for you:
   - the nearest AMeDAS station that measures temperature, and
   - the nearest type-A station, used for air pressure and for any value the first station lacks.

   There is nothing to fill in. Submit to finish.

You can add more places the same way. Each municipality can be set up once.

<details>
<summary>The set-up dialogs</summary>

<img src="docs/images/setup-1-office.png" alt="Step 1: choose the forecast office" width="560">
<img src="docs/images/setup-2-municipality.png" alt="Step 2: choose the municipality" width="560">
<img src="docs/images/setup-3-confirm.png" alt="Step 3: confirm the municipality and the observation stations" width="560">
<img src="docs/images/options.png" alt="The options dialog: two station ids and the test-action switch" width="560">

</details>

### Names and entity ids

The entry is named **JMA** and the English name of the municipality, for example `JMA Chiyoda`. The entity ids
follow that name, for example `weather.jma_chiyoda` and `sensor.jma_chiyoda_warning_level`. You can rename the
entry in the UI.

The entry also stores the Japanese name of the municipality as `area_name_ja`. The earthquake part uses it to
match station addresses (see [Earthquake values](#earthquake-values)).

### Options

Open the entry and select **Configure** to change:

- **The two station ids.** Each is five digits. The integration checks the format and accepts any AMeDAS station
  id.
- **Enable the jma.simulate test action.** See [Test your automations](#test-your-automations).

### Where the lists come from

Both lists come from JMA's area list, <https://www.jma.go.jp/bosai/common/const/area.json> (`offices` for the
forecast office, `class20s` for the municipality). The station ids are in
<https://www.jma.go.jp/bosai/amedas/const/amedastable.json>.

## Use it in automations

Every alert arrives on one event entity, `event.jma_<place>_alerts`. An automation triggers on a state change of
that entity and checks `event_type`. The event data is in the entity's attributes. All event types are listed in
[Events](#events).

### Example automations

The examples use the place Chiyoda (`event.jma_chiyoda_alerts`). Replace `chiyoda` with your own place.

```yaml
automation:
  # A warning for your municipality is issued or goes up.
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
          title: "{{ trigger.to_state.attributes.name_ja }} (level {{ trigger.to_state.attributes.new_level }})"
          message: "{{ trigger.to_state.attributes.headline }}"

  # An earthquake early warning names your prefecture (in the 23 wards of Tokyo: the 23 wards).
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

  # A heat-stroke alert is issued.
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

### Test your automations

The action `jma.simulate` fires any of the event types with data you choose, as if JMA had sent it. Use it to try
an automation without waiting for a real typhoon or earthquake.

1. Open the entry, select **Configure** and switch on **Enable the jma.simulate test action**.
2. In **Developer tools > Actions**, run `jma.simulate` with an `event_type` and, optionally, `data`.

The action is always listed. By default it reaches every entry that has the option switched on. To reach one entry
only, name it in the optional field `config_entry_id`. If no entry has the option on, the action fails with a
message that says so. A simulated event carries `simulated: true` in its data.

## Entities

All entities of one place belong to one service device. In the entity ids below, `<place>` is the lower-case
English name of the municipality, for example `chiyoda`.

### Weather and warnings

| Entity | What it shows |
|---|---|
| `weather.jma_<place>` | Current conditions from the two AMeDAS stations and the rain nowcast. A daily forecast from JMA's forecast file, with JMA's weekly file for the later days. An hourly forecast from Open-Meteo (JMA's MSM model). |
| `sensor.jma_<place>_warning_level` | The highest warning level in force for the municipality: 0 when nothing is in force, otherwise 2 to 5. The warnings themselves are in the attributes. |
| `sensor.jma_<place>_warning_outlook` | The highest warning level that JMA's 3-hourly risk outlook expects for the municipality. |
| `sensor.jma_<place>_overview` | JMA's text overview of the weather for the office, in Japanese. |
| `sensor.jma_<place>_japan` | The nationwide city forecast (22 offices) for today and tomorrow. |

### Rain and maps

| Entity | What it shows |
|---|---|
| `sensor.jma_<place>_rain_next_hour` | The heaviest rain expected at your home location in the next 60 minutes, in mm/h, from the nowcast. The value is the upper bound of the nowcast's rain band. |
| `sensor.jma_<place>_rain_outlook` | The heaviest rain expected at your home location after the nowcast hour, in mm/h. |
| `sensor.jma_<place>_kikikuru` | The キキクル risk level at your home location, 0 to 5: the highest of the landslide, flood and inundation levels. |
| `sensor.jma_<place>_radar_frames` | Frame lists and tile addresses for rain radar map cards, and for other layers: thunder and tornado nowcasts, lightning strikes, キキクル, the rain outlook, the Himawari satellite image and the hazard-map flood layer. JMA's frame lists send no CORS header, so a browser cannot read them directly; Home Assistant relays them. |

### Storms and heat

| Entity | What it shows |
|---|---|
| `sensor.jma_<place>_typhoon` | The number of active tropical cyclones (0 is a normal value). The `storms` attribute lists each storm: position, pressure, wind, forecast track, past track, distance to your home, closest approach and approach `stage` (`none`, `notice` or `urgent`). |
| `sensor.jma_<place>_heat_alert` | Today's heat-stroke alert for the prefecture: `none`, `alert` (熱中症警戒アラート) or `special` (熱中症特別警戒アラート). The attributes show what is issued for tomorrow and the forecast WBGT figures of the prefecture's stations. Unavailable out of season. |
| `sensor.jma_<place>_wbgt` | Today's highest forecast heat index (WBGT), in °C. Only created for the Tokyo office (see [Known limits](#known-limits)). Unavailable out of season. |

### Earthquakes and tsunami

| Entity | What it shows |
|---|---|
| `sensor.jma_<place>_last_earthquake` | The origin time of the newest earthquake reported, with its intensity in your prefecture and at your municipality. The attributes are described in [Earthquake values](#earthquake-values). |
| `sensor.jma_<place>_tsunami` | The tsunami grade for your coast area: `none`, `watch`, `warning` or `major_warning`. The attributes give the strongest grade anywhere in Japan, the areas under a warning, and, for your coast area, the expected height and arrival time. Only some places have a coast area (see [Known limits](#known-limits)). |

### Alerts and status

| Entity | What it shows |
|---|---|
| `event.jma_<place>_alerts` | The alerts an automation can react to (see [Events](#events)). |
| `binary_sensor.jma_<place>_feed_problem` | On when a main feed has been failing for 30 minutes. The attributes name the feed and the reason (see [Troubleshooting](#troubleshooting)). |

## Events

`event.jma_<place>_alerts` fires the event types below. The event data is in the entity's attributes.

### Warnings and rain

| Event type | When it fires | Data |
|---|---|---|
| `warning_issued`, `warning_raised`, `warning_lowered`, `warning_cleared` | A warning for the municipality appears, goes up, goes down or ends. | `phenomenon`, `old_level`, `new_level`, `code`, `name_ja`, `name_en`, `status`, `headline`, `area`, `report_time` |
| `warning_in_force` | On the first poll after Home Assistant starts, for each level 4 or 5 warning that is already in force. | as above |
| `rain_arriving` | Rain of 1 mm/h or more is forecast at your home location within 30 minutes while it is dry now. It fires once per rain episode and fires again only after 60 dry minutes. | `minutes`, `band`, `mmh_upper`, `time`, `max_mmh` |

### Earthquakes

| Event type | When it fires | Data |
|---|---|---|
| `earthquake` | A report of a quake includes a station in your scope: the area `東京都２３区` in the 23 special wards of Tokyo, the prefecture everywhere else. It fires again when the scope's maximum rises, when the local intensity rises or when the epicentre becomes known. A quake more than 10 minutes old, replayed from the history after a reconnect, does not fire. See [Earthquake values](#earthquake-values). | the [earthquake values](#earthquake-values), and `change` (`new`, `scale` or `detail`) |
| `eew` | An early warning (緊急地震速報) names an area of your prefecture (in the 23 special wards of Tokyo: the area `東京都２３区`). It fires again for the same warning only if the forecast intensity rises. A warning issued more than 2 minutes ago does not fire. | `event_id`, `serial`, `origin_time`, `hypocenter`, `magnitude`, `pref`, `area` (your area; empty outside the 23 wards), `areas` (the matched areas of the warning), `predicted_scale`, `predicted_shindo`, `scale_from`, `scale_to`, `arrival_time`, `update` |
| `eew_cancelled` | An early warning that was announced is cancelled. | `event_id`, `serial`, `origin_time`, `hypocenter`, `magnitude`, `pref`, `area` |

### Tsunami

| Event type | When it fires | Data |
|---|---|---|
| `tsunami` | The picture for your coast area changes, or a warning or major warning is in force anywhere in Japan. | `id`, `issued`, `home_grade`, `home_grade_ja`, `home_area`, `home_height`, `home_height_m`, `home_arrival`, `home_condition`, `home_immediate`, `max_grade`, `areas_major`, `areas_warning`, `count_major`, `count_warning`, `count_watch`, `previous_home_grade`, `previous_max_grade`, `backfill` |
| `tsunami_cancelled` | The tsunami information that was announced is cancelled or has expired. | `id`, `issued`, `home_area`, `previous_home_grade`, `previous_max_grade`, `cancelled`, `expired`, `backfill` |

`backfill` is `true` when the event comes from the tsunami list read on connect and the bulletin is more than
10 minutes old. A bulletin that is never cancelled expires 48 hours after it was issued.

### Typhoons and heat

| Event type | When it fires | Data |
|---|---|---|
| `typhoon_approach` | A storm's approach stage for your home rises. See the stages below. | `storm_id`, `number`, `number_short`, `name_en`, `name_ja`, `category`, `stage`, `previous_stage`, `closest_km`, `closest_time`, `closest_hours`, `zone_enters_time`, `zone_enters_hours`, `distance_km`, `pressure_hpa`, `max_wind_ms`, `issue` |
| `typhoon_cleared` | A storm that was announced has passed, or JMA no longer lists it. `reason` is `passed` or `gone`. "Passed" means its closest approach is behind it and three newer forecasts in a row show no approach stage. | `storm_id`, `number`, `number_short`, `name_en`, `name_ja`, `previous_stage`, `reason` |
| `heat_alert` | A new 熱中症警戒アラート or 熱中症特別警戒アラート is issued for today or tomorrow. | `grade` (`alert` or `special`), `day`, `target_date`, `area`, `wbgt_max`, `wbgt_home`, `special`, `report_time` |

Typhoon approach stages:

| Stage | Condition |
|---|---|
| `notice` | Within 72 hours, the forecast track passes within 300 km of your home, or your home enters the storm-warning zone (暴風警戒域). |
| `urgent` | Within 24 hours, your home enters the storm-warning zone, or it is already inside. |

### After a restart

Typhoon stages, heat alerts and tsunami information are remembered across restarts, so a restart does not
announce them again. Earthquakes, early warnings and warnings are not remembered, on purpose: they are announced
again after a restart.

## Earthquake values

The earthquake data comes from P2P地震情報 (P2P Earthquake Information), which relays JMA's reports. A report
lists observation points (JMA's seismic intensity stations), each with a prefecture and an address. The
integration sorts the points into up to three scopes for your place:

| Scope | A point is in it when | Who has it |
|---|---|---|
| Prefecture | its prefecture is the prefecture of your forecast office | every place |
| Area | it is JMA's area-level row for the 23 wards (address `東京都２３区`), or its address starts with the name of one of the 23 wards | only places in the 23 special wards of Tokyo |
| Local | it is in your prefecture and its address contains the Japanese name of your municipality | every place, where the name matches (see [Known limits](#known-limits)) |

Details of the name match:

- Addresses are compared as plain text.
- For a ward of a designated city, the name is also tried without its first `市` (city), because JMA writes it
  that way. For example, `神戸市中央区` (Chuo ward, Kobe) matches both `神戸市中央区` and `神戸中央区`.
- For the area, a leading `東京都` or `東京` (Tokyo) is ignored, and the ward can be any of the 23. For example,
  `東京千代田区丸の内` is in the area.
- The rest of Tokyo prefecture (the Tama cities and the islands) is not in the area. Nor is a point whose address
  names no ward, such as `東京国際空港` (Tokyo International Airport).
- A place anywhere else has no area, including a Tokyo municipality outside the 23 wards such as `八王子市`
  (Hachioji). For such a place `area` is empty and everything follows the prefecture.

**Which scope fires events.** For a place with an area, the `earthquake` and `eew` events follow the area. For
every other place they follow the prefecture. For a ward of Tokyo, only the early warning's area `東京都２３区`
counts. A report that raises only the prefecture's maximum fires nothing for a place with an area, so a quake
felt only in Tama or on the islands fires nothing for a ward. `sensor.jma_<place>_last_earthquake` still shows
the newest quake, whatever its scope.

**`change`** in the `earthquake` event says why it fired:

- `new`: the first report of this quake.
- `scale`: the scope's maximum rose.
- `detail`: the scope's maximum is unchanged, but the local intensity rose or the epicentre became known.

The sensor attributes and the `earthquake` event carry these values:

| Name | Meaning |
|---|---|
| `pref`, `local_name` | The prefecture and the municipality name the values are for. |
| `area` | `東京都２３区` for a place in the 23 special wards of Tokyo; empty for every other place. |
| `pref_max_scale`, `pref_shindo` | The highest intensity reported by a point in the prefecture, as the P2P code (10 to 70) and as the shindo label (`1`, `2`, `3`, `4`, `5-`, `5- or more`, `5+`, `6-`, `6+`, `7`). |
| `area_max_scale`, `area_shindo` | The same for the points of the area. Unknown when none reported, and always unknown for a place without an area. |
| `local_scale`, `local_shindo` | The same for the local points. Unknown when none reported. |
| `max_scale`, `max_shindo` | The highest intensity anywhere. |
| `id`, `hypocenter`, `magnitude`, `depth`, `time`, `issue_type`, `domestic_tsunami` | As P2P reports them. |

The sensor also has `connected`: whether the connection to P2P地震情報 is open.

## Update intervals

| Feed | Interval | Constant in `const.py` |
|---|---|---|
| Warnings (JMA) | 60 seconds | `INTERVAL_WARNINGS` |
| Cross-check of the warnings against JMA's XML feed | 5 minutes | `INTERVAL_XML` |
| Rain nowcast (JMA tiles) | 5 minutes | `INTERVAL_NOWCAST` |
| Map-layer frame lists, キキクル, rain outlook (JMA) | 5 minutes | `INTERVAL_LAYERS` |
| Forecast and overview (JMA) | 10 minutes | `INTERVAL_FORECAST` |
| AMeDAS observations (JMA) | 10 minutes | `INTERVAL_AMEDAS` |
| Warning outlook (JMA) | 10 minutes | `INTERVAL_TIMELINE` |
| Heat-stroke alert (Ministry of the Environment; new files at 05, 10, 14 and 17 JST in season) | 20 minutes | `INTERVAL_HEAT_ALERT` |
| Typhoons (JMA) | 30 minutes | `INTERVAL_TYPHOON` |
| Nationwide city forecast (JMA) | 30 minutes | `INTERVAL_JAPAN` |
| Hourly forecast (Open-Meteo) | 60 minutes | `INTERVAL_HOURLY` |
| Heat index (Ministry of the Environment) | 60 minutes | `INTERVAL_WBGT` |
| Earthquakes, early warnings and tsunami information (P2P地震情報) | Pushed. Each place keeps one WebSocket open (at most two places per home, see [Known limits](#known-limits)). The recent history and the tsunami list are read on every connect. | `URL_P2P_WS`, `URL_P2P_HISTORY`, `URL_P2P_TSUNAMI` |

When a poll fails, the entities keep the last good data until it is older than three times the interval
(`STALE_FACTOR`). After that they become unavailable. A feed whose file changes shape fails the same way, without
a traceback in the log.

## Known limits

**Reliability**

- JMA's `/bosai/` JSON files and tiles are not a documented API. Their shape can change without notice. A changed
  file makes that one feed fail (the feed-problem sensor shows it), not the whole integration.
- Earthquake and tsunami information comes through P2P地震情報, a volunteer service that does not guarantee
  accuracy (「情報の正確性も一切保証しません」, "we make no guarantee at all of the accuracy of the information").
  **Do not use this integration as your only way to learn of a danger.**
- The hourly forecast depends on Open-Meteo, whose free API is for **non-commercial use only**.

**Coverage**

- Weather, forecasts, warnings, rain and typhoons work for every municipality (class20 area) in JMA's list.
- River-flood warnings are not read. JMA keeps them in a separate map.
- **The tsunami coast area (津波予報区) is known only for Tokyo prefecture**, by municipality: 東京湾内湾 (inner
  Tokyo Bay: the 23 wards and the mainland), 伊豆諸島 (the Izu islands) and 小笠原諸島 (the Ogasawara islands).
  Everywhere else `sensor.jma_<place>_tsunami` stays `none`. Its `max_grade` attribute and the `tsunami` event
  still report a warning or major warning anywhere in Japan, with the areas named.
- **The heat index (WBGT) sensor exists only for the Tokyo office (`130000`)**, the only office whose file name
  the integration knows. For every other office the sensor is not created. The heat-stroke alert sensor works for
  every office.
- **Only the 23 special wards of Tokyo have an area.** Everywhere else, including the Tama cities and the islands
  of Tokyo, earthquake events and early warnings follow the whole prefecture. The finer areas of other
  prefectures are not mapped.
- The earthquake values for the prefecture work everywhere. The `local_*` values depend on the name match in
  [Earthquake values](#earthquake-values), which has the limits listed below.
- The overview text and the warning names are in Japanese. The integration's own interface text is in English
  and Japanese.

**Earthquake connections**

- P2P地震情報 allows two connections per IP address, and each place keeps one open. So at most two places per
  home receive earthquake information. A third place opens no connection and logs one line saying so. Its
  earthquake and tsunami sensors stay unavailable.

<details>
<summary>How well the municipality name match works (counted over <code>tests/fixtures/area.json</code>)</summary>

These counts compare JMA's list in `tests/fixtures/area.json` (1,805 areas) with the roughly 1,500 station
addresses in the two P2P files in `tests/fixtures`:

- An area that is a **whole designated city**, for example `千葉市` (Chiba City), never matches. JMA writes its
  stations with the ward (`千葉中央区中央港`). Its `local_*` values stay unknown.
- **Split areas** whose name does not end in `市`, `区`, `町` or `村` (city, ward, town or village) never match.
  There are 84 of these in the 1,805, for example `仙台市東部` (eastern Sendai).
- A name **inside a longer name** in the same prefecture also matches the longer one's stations, so `local_*` can
  be too high. There are 23 such pairs in the list, for example `相馬市` (0720900) inside `南相馬市` (0721200), and
  `大阪市` (2710000) inside `東大阪市` (2722700).
- About 1,300 of those addresses match at least one area in the list. The others match none (for example about
  two thirds of the roughly 100 Kanagawa addresses match none).
- For the early warning (`eew`), the prefecture of an area is compared as P2P writes it. It is not verified
  whether P2P writes a prefecture or a forecast-region name for Hokkaido, Okinawa or the Amami islands: the tests
  have no real early-warning sample for them.

</details>

## Data sources and attribution

Every entity that shows data from these services carries the service's credit in its `attribution` attribute.
If you show the data in a public place, give the credit the service asks for. The services' terms apply to you as
the user.

Where a service states no rate limit, none is given here. The intervals in
[Update intervals](#update-intervals) are the ones this integration uses.

| Service | Used for | Licence |
|---|---|---|
| [Japan Meteorological Agency](#japan-meteorological-agency) | forecasts, warnings, AMeDAS, nowcast and risk tiles, typhoons | Public Data License 1.0, unless a page says otherwise |
| [Open-Meteo](#open-meteo) | hourly forecast (JMA MSM model) | CC BY 4.0; free API for non-commercial use only |
| [P2P地震情報](#p2p地震情報) | earthquake information, early warnings, tsunami information | free for commercial and non-commercial use; no warranty |
| [Ministry of the Environment](#ministry-of-the-environment) | heat index (WBGT) and heat-stroke alerts | the ministry's terms |
| [Geospatial Information Authority of Japan](#geospatial-information-authority-of-japan) | flood-risk map layer listed for map cards | Public Data License 1.0, unless stated otherwise |

### Japan Meteorological Agency

- **Terms:** <https://www.jma.go.jp/jma/kishou/info/coment.html>
- **Credit asked for:** 「コンテンツを利用する際は出典を記載してください。」 (give the source when you use the
  content). The page's examples: 「出典：気象庁ホームページ　（当該ページのURL）」 and
  「「図・写真等の名称」（気象庁ホームページより）」.
- **Edited content:** 「コンテンツを編集・加工等して利用する場合は、上記出典とは別に、編集・加工等を行ったことを記載してください。」
  (if you edit or process the content, state that you did, separately from the source above).
- **What the entities carry:** 「出典：気象庁ホームページ（https://www.jma.go.jp/bosai/）」 and
  「気象庁の情報を加工して作成」 (made by processing JMA's information). The integration edits the data: it derives
  levels, frames and forecasts. Where you display the data, give the address of the page it came from.
- **Licence:** Public Data License 1.0 (「公共データ利用規約（第1.0版）」), unless a page says otherwise. No
  commercial ban is stated. Some content is restricted by law (気象業務法, the Meteorological Service Act). The
  page states no request-rate limit.

### Open-Meteo

- **Terms:** <https://open-meteo.com/en/terms>. Licence: CC BY 4.0, <https://open-meteo.com/en/licence>.
- **Credit asked for:** "You must give appropriate credit, provide a link to the licence, and indicate if changes
  were made" and "You must include a link next to any location Open-Meteo data are displayed", for example
  "Weather data by Open-Meteo.com" with a link to <https://open-meteo.com/>. The weather entity's `attribution`
  writes the address out.
- **Changes the integration makes:**
  - it maps the hourly values to Home Assistant's forecast fields (the wind speed is requested in metres per
    second, so no unit conversion is made);
  - it leaves out variables that have no value for any hour;
  - it derives the weather condition from the service's weather code and its day flag;
  - it leaves out rows without a temperature value;
  - it serves only the hours that have not yet passed.
- **Use:** "You may only use the free API services for non-commercial purposes."
- **Limits:** free tier "Less than 10'000 API calls per day, 5'000 per hour and 600 per minute." The integration
  asks once an hour.

### P2P地震情報

- **Terms:** <https://www.p2pquake.net/develop/json_api_v2/>
- **Credit asked for:** the service gives no credit wording of its own. For JMA's earthquake and tsunami
  information up to 4 April 2021 it asks for this statement:
  「2021 年 4 月 4 日までの気象庁 地震情報・津波予報: CC BY 4.0 準拠であり、気象庁の情報を利用している旨の表示が必要です」
  (CC BY 4.0 applies, and you must state that JMA's information is used). It gives no sentence for later
  information. The entities carry 「P2P地震情報（https://www.p2pquake.net/） · 気象庁の地震情報を利用」.
- **Use:** free for commercial and non-commercial use (「商用・非商用問わず無償（無料）でご利用になれます」). No
  warranty (「無保証」).
- **Limits (per IP address):** /history 60 requests per minute, /jma 10 requests per minute, /ws 2 connections.
  This integration uses /ws and /history, and reads `/jma/tsunami` once on every connect. If that list does not
  answer, it asks again once a minute until it does.

### Ministry of the Environment

- **Service:** heat-stress information, <https://www.wbgt.env.go.jp/en/tos.php>
- **Credit asked for:** "Source: Ministry of the Environment Heat Illness Prevention Information website (URL of
  the relevant page)" and, for edited content, "Created by editing the XX Survey (Ministry of the Environment) (URL
  of the relevant page)". The WBGT sensor's value is a daily maximum computed by the integration. So the
  `attribution` of the WBGT and heat-alert sensors carries both sentences with the site address, with "XX Survey"
  written as the site's name.
- **Terms:** see the ministry's page <https://www.wbgt.env.go.jp/en/tos.php>.
- **Files used:** `https://www.wbgt.env.go.jp/prev15WG/dl/yohou_<pref>.csv` (heat index) and
  `https://www.wbgt.env.go.jp/alert/dl/<year>/alert_<yyyymmdd>_<hh>.csv` (alerts).

### Geospatial Information Authority of Japan

- **Service:** the hazard-map portal, <https://disaportal.gsi.go.jp/>
- **Credit asked for:** 「出典：「ハザードマップポータルサイト」」 (source: the Hazard Map Portal Site), shown in the
  layer's attribution. For edited content: 「「ハザードマップポータルサイト」を加工して作成」.
- **Licence:** Public Data License 1.0, unless stated otherwise. Third-party rights are for the user to check
  (「利用者の責任において確認」).

## Privacy

- State attributes stay inside your Home Assistant installation.
- The radar-frames sensor has an attribute `home` that holds the home coordinates of Home Assistant.
- The typhoon attributes hold the distance from your home location, and the closest point of the forecast track to
  it, with its time.
- The hourly forecast is requested from Open-Meteo for your home location, rounded to four decimals. Open-Meteo
  receives that location with each request.
- Requests to JMA carry the map tile numbers of your home location (for the rain nowcast and the map layers) and
  the ids of the two weather stations (for the AMeDAS files).
- The diagnostics file hides the place: coordinates, area code and names, station ids and the entry title.

## Troubleshooting

- **Download diagnostics.** Go to **Settings > Devices & services**, open the integration, select the three dots
  and select **Download diagnostics**. The file holds the health and the last error of each feed. Your
  coordinates, the area code, the area names, both station ids and the entry title are redacted. This includes
  error messages, where the x and y numbers of a map tile are replaced too. The office code stays.
- **Check the feed-problem sensor.** `binary_sensor.jma_<place>_feed_problem` watches the main feeds: warnings
  (with the XML cross-check), AMeDAS observations (and each of the two stations), the hourly forecast, the rain
  nowcast, the daily forecast and the earthquake connection. Each feed's attribute shows `problem`, `since` and
  `reason`. The other feeds (outlook, typhoons, heat, map layers, nationwide forecast) are optional and are not
  watched.
- **Turn on debug logging.** Add `custom_components.jma: debug` under `logger:` in `configuration.yaml` to see the
  detail for each feed.
- **No heat-index sensor, or the tsunami sensor is always `none`.** This is expected outside Tokyo. The heat
  index needs the Tokyo office, and the coast area needs a Tokyo municipality. See [Known limits](#known-limits).
- **No earthquake values for a third place.** See [Known limits](#known-limits).

## Removing the integration

1. Go to **Settings > Devices & services > JMA Weather (Unofficial)**, select the three dots of the entry and
   select **Delete**. Repeat for every entry.
2. Remove the integration in HACS. If you installed it by hand, delete the folder `custom_components/jma`.
3. Restart Home Assistant.

## Reporting problems and contributing

Open an issue at <https://github.com/glockpete/ha-jma/issues>. Attach the diagnostics file (it hides your place)
and give the integration version from `manifest.json`. Please do not post your coordinates.

To run the tests, see [CONTRIBUTING.md](CONTRIBUTING.md). Report a security problem as described in
[SECURITY.md](SECURITY.md).

## Licence

MIT, see [LICENSE](LICENSE). The data the integration reads belongs to the services listed above and stays under
their terms.

Acknowledgement: some of the rain-tile palette colours were taken from the public project
[yucjk0120/jma_nowcast_dashboard](https://github.com/yucjk0120/jma_nowcast_dashboard).
