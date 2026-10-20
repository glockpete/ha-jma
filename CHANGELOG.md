# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [semantic versioning](https://semver.org/).

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

- A tsunami bulletin whose areas all have the grade "Unknown" no longer clears an active warning.
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
