# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [semantic versioning](https://semver.org/).

## Unreleased

### Added

- **Release asset.** Every published release carries `jma.zip`, the integration folder zipped by the new
  `release.yml` workflow. `hacs.json` names it (`zip_release`, `filename`), so HACS and the Home Assistant
  Marketplace fetch one file instead of every file of the folder, and count the downloads. The asset was added to
  the 0.6.0 to 0.7.0 releases by hand, so those versions stay installable.

### Changed

- **Only releases are offered.** `hide_default_branch` in `hacs.json` removes the `main` branch from the version
  picker, so users only install a tested release.

## 0.7.0 - quality scale

Work towards the Home Assistant Integration Quality Scale.

### Added

- **Reconfigure.** The entry menu has a **Reconfigure** item. It picks the two AMeDAS stations again from JMA's
  current tables and the home of Home Assistant, and refreshes the area names stored with the entry (an entry made
  before 0.6 gains its Japanese area name). The municipality and the entry title do not change, so a name you gave
  the entry stays. A station id typed under Configure is dropped; the `jma.simulate` setting stays. The entry
  reloads only when it is set up and a setting changed.
- **Repair issues** under **Settings > Repairs**: `no_quake_socket` for an entry that cannot get one of the two
  earthquake connections, and `station_silent` for an AMeDAS station that answers nothing while the other one does.
  Each goes by itself when its cause goes: the connection is free, or the station answers, or the entry is set up
  again with other stations.
- **"Retrying setup".** When none of the four main feeds (forecast, warnings, AMeDAS observations, rain nowcast)
  can be fetched, the set-up is not completed and Home Assistant retries with growing waits. One feed that answers,
  or a file that arrives but does not parse, still sets up, and the entities wait for their feed.
- **A check of the station ids in Configure against JMA's station table.** An id of five digits that no station has
  shows `unknown_station`. When the table cannot be fetched, the ids are accepted as typed.
- **`icons.json`.** Entity icons moved from Python to `icons.json`, with state icons for the tsunami and heat-alert
  sensors, a range of icons for the warning level and an icon for the `jma.simulate` action.
- **Help text under every form field** (`data_description`) in English and Japanese, and the abort text
  `already_in_progress`.
- **The version of the integration on the device page** (`sw_version`).
- **Test coverage.** Every module is above 95 % line coverage. New tests cover the config-flow aborts, the options
  flow, Reconfigure, the repair issues, the retrying setup, the home-moved reload, the device titles for all 1,805
  municipalities, the station-id checks, the unavailable earthquake sensors, the log lines and the reason length.

- **Quality scale.** `custom_components/jma/quality_scale.yaml` lists every rule of the Home Assistant Integration
  Quality Scale with where it is met or why it does not apply, and `manifest.json` declares `platinum`. Home
  Assistant checks this file only for its own integrations, so `tests/test_quality_scale_file.py` does it here: all
  54 rules once, in hassfest's order, no `todo`, a reason on every exemption, and the declared tier earned.
- **Continuous integration.** Two coverage gates: pytest-cov fails the run when the total line coverage of
  `custom_components/jma` is under 95 %, and coverage-threshold fails it when any one module is. A mypy job runs
  `mypy --strict` over the package (mypy 2.4.0, pinned in `requirements_test.txt`).

### Changed

- `binary_sensor.jma_<place>_feed_problem` has the **diagnostic** entity category.
- `sensor.jma_<place>_radar_frames` and `sensor.jma_<place>_japan` are **disabled by default** on a new place.
  Entities that exist keep their state. Enable them in the entity settings when a map card or the nationwide
  forecast needs them.
- `sensor.jma_<place>_last_earthquake` and `sensor.jma_<place>_tsunami` become **unavailable** when the P2P地震情報
  connection has been down for more than 10 minutes, the limit the feed-problem sensor uses, and are available
  again once the connection has been up for 60 seconds (a connection that drops sooner is part of the same outage).
  A shorter drop changes nothing. The alerts event entity stays available.
- **The log is written once per outage.** The earthquake connection writes one WARNING when an outage begins
  (`P2P earthquake connection lost` or `P2P earthquake connection cannot be opened`), debug lines for the retries and
  one INFO when a connection has been open for 60 seconds (a connection that drops sooner is part of the same
  outage, so a server that closes every connection at once gives one WARNING, not one pair per connection). A feed
  writes one WARNING for a run of unexpected payloads and one INFO when its data is back after N failed polls.
- **The first refresh runs in two phases.** The four main feeds first. The other feeds, the heat-alert and typhoon
  files among them, only after the check for "Retrying setup" has passed, so that an alert decided in a set-up that
  is then retried is not remembered as announced.
- **Strict typing.** `mypy --strict` passes on all 29 modules, with no `type: ignore` and no `cast`. The settings
  are in `[tool.mypy]` in `pyproject.toml`. The coordinator sensors are generic over their coordinator's data, and
  the hourly and daily rows are Home Assistant `Forecast` rows (the daily one with JMA's code, icon, reliability and
  the temperature ranges as extra keys). Three spellings became explicit: a merge with no primary observation returns
  the secondary one, a warning with no data type is not looked up in the headlines, and a map-tile pixel of an
  impossible shape raises a parse error instead of a `TypeError`.
- **Schemas import `probatio`**, Home Assistant's validation engine since 2026.9 (`voluptuous` is an alias of it at
  run time and is no longer installed). Importing it directly lets mypy check the schemas against Home Assistant
  2026.10's signatures; the behaviour is the same.
- The success text of Reconfigure no longer says that the entry is reloading ("The stations and names were
  updated."), because it reloads only when it is set up and a setting changed.
- The README describes the set-up dialogs with their texts, Options, Reconfigure, the move of the home, the device
  titles, the retrying setup, the repair issues, the disabled and diagnostic entities, the health limits of the
  feed-problem sensor, the log lines to look for, and what is deleted with an entry. `README.ja.md` has short
  bullets for the same.

### Fixed

- `sensor.jma_<place>_warning_level` is `unknown` instead of 0 when the warnings file has no row for the
  municipality. 0 reads as "nothing in force" when nothing is known. The attribute `area_missing` still says why.
- The weather entity picks the current row of the hourly forecast, and starts the hourly forecast, at Japan's hour.
  The rows are in JST. In a time zone with a half-hour offset (Asia/Kolkata, Australia/Adelaide) the hour was floored
  to half past the JST hour, so from half past the hour on the next hour's row was used.
- The error text in a `reason` of the feed-problem sensor is cut to 200 characters. A stored error, such as a
  server's whole HTML page, no longer fills an attribute that is written to the recorder every minute.
- Two municipalities with one English name, such as Chuo in Tokyo and in Yamanashi, got the same device name and
  entity ids ending in `_2`. A shared name is now followed by the forecast office: `JMA Chuo (Tokyo)` and
  `JMA Chuo (Yamanashi)`. The city among towns keeps the plain name (`JMA Chiyoda`; Chiyoda Town in Gunma becomes
  `JMA Chiyoda (Gunma)`). This applies to places added from now on. Existing entries keep their title.
- Moving the home in Settings left the rain tile on the old place until a restart. The entry now reloads when the
  latitude or longitude changes in **Settings > System > General**. A home changed in `configuration.yaml` and
  applied with `homeassistant.reload_core_config` fires no event, so reload the entry by hand. Any other change to
  the core configuration leaves it alone.

## 0.6.2 - plain-English README

### Changed

- The README is rewritten in plain English: shorter sentences, the same facts, reference material in tables, and
  numbered steps for installing and setting up. The Japanese summary moved to `README.ja.md`.
- The README now says exactly which feeds the feed-problem sensor watches and states the rule of `rain_arriving`;
  the event data lists name every field the code sends. The counts for the municipality name match are rounded.
- The GitHub Actions used by the checks are updated to checkout 7.0.1, setup-python 7.0.0 and ruff-action 4.1.0
  (still pinned by commit SHA).

No change to the integration's behaviour.

## 0.6.1 - earthquake area for the 23 wards of Tokyo

### Added

- **An area tier between the prefecture and the municipality, for the 23 special wards of Tokyo.** An entry in
  one of the 23 wards is scoped to the area `東京都２３区`. New values: `area`, `area_max_scale` and `area_shindo`
  (in the `earthquake` event data and in the attributes of `sensor.jma_<place>_last_earthquake`; the sensor shows
  `area` before any quake too), and `area` in the data of the `eew` and `eew_cancelled` events. Entries anywhere
  else, including the Tama cities and the islands of Tokyo, have no area (`area` is empty) and are unchanged.

### Changed

- For an entry in one of the 23 wards, **`earthquake` events and early warnings (`eew`) no longer fire for the rest
  of the prefecture**: a quake or a warning that names only the Tama cities or the islands raises nothing. An
  `earthquake` event needs a point of the 23 wards (a ward address or the area-level row `東京都２３区`), and a
  rise of the prefecture's maximum alone is no longer a `scale` change for such an entry. 0.6.0 used the whole
  prefecture for every entry.
- Entries made before 0.6 stored the Japanese municipality name only under `area_name`; the quake part now finds
  it there when `area_name_ja` is missing (a numeric code under `area_name` is never taken as a name), so their
  `local_*` values are known.
- `jma.simulate` is one action for all entries, always listed, with an optional `config_entry_id`. Without it, it
  fires at every entry that has the test option on, and it says why when none has. Reloading or unloading one entry
  no longer removes it for the others.

### Fixed

Found by a hostile review of the feeds, the sockets and the setup (each fix has a regression test):

- A tsunami bulletin whose areas all have the grade "Unknown" no longer clears an active warning, and a wave height
  that is not a finite number reads as unknown instead of dropping the whole bulletin.
- An earthquake report dated in the future no longer pins the "last earthquake" sensor, and one malformed station
  in a report no longer drops the whole report. An early-warning area with an invalid intensity value still raises
  the alert.
- Oversized, deeply nested or redirected answers from JMA and the P2P service are refused instead of exhausting
  memory or following another host. A failing earthquake-history lookup no longer drops the live connection or
  delays early warnings. A flood of malformed live messages writes one log warning every five minutes. The
  early-warning memory is bounded.
- One unreadable timestamp in the warnings file no longer freezes the warnings sensor. A heat-alert file dated in
  the future is ignored, so it cannot block the next real alert.
- Absurd numbers (infinity, huge integers) or an oversized CSV field in a feed keep the last good data and log one
  warning line instead of making the integration unavailable at once. A typhoon with a nonsensical storm-warning
  radius no longer triggers a false "typhoon approaching".
- A map tile that is not exactly 256 x 256 or not a PNG is refused before it is decoded. A solid black or grey
  tile served in place of a rain or キキクル tile reads as "no data" instead of heavy rain or a high level (a solid
  white tile is the real level 1 of the land and inundation layers, and stays so). One poll reads at most 8 storms,
  13 nowcast forecast frames and 16 outlook frames, and each of those groups of requests has at most 6 in flight.
  The same station as nearest and second station is fetched once.
- The weather entity uses Japan's date for "today" whatever time zone Home Assistant is set to; before, the forecast
  day and today's high and low could be a day out for some hours.
- When an entry is removed or disabled, its earthquake connection passes to an entry that has none, with no restart.
- A damaged or maintenance-page area list or station table ends setup with "could not reach the server" instead of
  "Unknown error"; an empty office list no longer shows an empty drop-down.
- Diagnostics hide the tsunami coast area, which is the area code in words.
- The bug-report form no longer asks for a municipality and says which parts of a log can point to your area.

## 0.6.0 - first public release

First release published on GitHub and in HACS. It builds on a private 0.5.x line that ran in a single home; this
release makes every feature work for any place in Japan.

### Added

- **Tsunami information** (P2P地震情報, code 552): `sensor.jma_<place>_tsunami` and the `tsunami` and
  `tsunami_cancelled` events. A warning or major warning anywhere in Japan is reported; the home coast area is
  known for Tokyo prefecture only, chosen by municipality: 東京湾内湾, 伊豆諸島 or 小笠原諸島.
- **Typhoon approach**: a storm stage per storm (`none`, `notice`, `urgent`) in the typhoon sensor's `storms`
  attribute, and the `typhoon_approach` and `typhoon_cleared` events.
- **Heat-stroke alerts** (環境省 熱中症警戒アラート and 熱中症特別警戒アラート): `sensor.jma_<place>_heat_alert` and the
  `heat_alert` event, for every forecast office.
- **Announce-once memory**: typhoon stages, heat alerts and tsunami information are remembered across restarts
  (stored by Home Assistant), so a restart does not announce them again.
- Japanese translations of the new entities and events; the Japanese title of the integration.

### Changed

- The integration is named **JMA Weather (Unofficial)** in HACS and in the Home Assistant interface (the domain
  `jma` is unchanged).
- Earthquake values follow the entry's prefecture and municipality, not a fixed place.
- Requires Home Assistant 2026.9.0 or newer. No extra Python packages are needed.

### Fixed

- The options form no longer answers HTTP 500 on Home Assistant 2026.9.
- A warning code the integration does not know is no longer announced as a level-0 warning.
- An hour of failed rain-tile requests no longer re-arms the `rain_arriving` event in the middle of an episode.
- A live earthquake early warning is no longer dropped silently when the clock of the host is slow (up to ten
  minutes) or when the message has no issue time. A warning dated further ahead than that is logged once as a clock
  problem. A replay after a reconnect without a readable issue time is still ignored, since its age cannot be
  checked, and a cancelled warning is not announced again by such a replay.
- The tsunami coast area of a place on the Izu or Ogasawara islands is the island coast, not Tokyo Bay: it is chosen
  by the municipality's area code, not by the forecast office.
- The tsunami sensor of a third place (which opens no earthquake connection) is now unavailable, like the earthquake
  sensor, instead of unknown for ever.
- Station ids typed with a Japanese input method (full-width digits, `４４１３２`) are accepted and stored as ASCII;
  other non-ASCII digits are refused with the invalid-station message.
- The tsunami clock tick no longer rewrites the sensors when nothing changed, and does not run for a place without an
  earthquake connection.

## 0.1.0

Unreleased first version of the repository: a weather entity, warning, rain, typhoon, earthquake and heat-index
sensors, an alert event entity, a feed-problem sensor, diagnostics that redact the place, and English and Japanese
translations.
