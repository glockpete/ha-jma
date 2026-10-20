# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [semantic versioning](https://semver.org/).

## 0.6.0 - first public release

First release published on GitHub and in HACS. It builds on a private 0.5.x line that ran in a single home; this
release makes every feature work for any place in Japan.

### Added

- **Tsunami information** (P2P地震情報, code 552): `sensor.jma_<place>_tsunami` and the `tsunami` and
  `tsunami_cancelled` events. A warning or major warning anywhere in Japan is reported; the home coast area is
  known for the Tokyo office only.
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

## 0.1.0

Unreleased first version of the repository: a weather entity, warning, rain, typhoon, earthquake and heat-index
sensors, an alert event entity, a feed-problem sensor, diagnostics that redact the place, and English and Japanese
translations.
