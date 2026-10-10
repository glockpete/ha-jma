# Changelog

## 0.1.0 - first public release

Provides, for any municipality in Japan:

- A weather entity with current conditions (AMeDAS observations and the rain nowcast), a daily forecast and
  an hourly forecast (Open-Meteo, JMA MSM model).
- Warning sensors: warning level, 3-hourly warning outlook, and the alert event entity (`warning_issued`,
  `warning_raised`, `warning_lowered`, `warning_cleared`, `warning_in_force`, `rain_arriving`, `earthquake`,
  `eew`, `eew_cancelled`).
- Rain and map sensors: rain in the next hour, rain outlook, radar and layer frame lists, キキクル risk level.
- Typhoon sensor with tracks and the closest approach to the home location, and a nationwide city forecast.
- Earthquake information from P2P地震情報: the highest scale in the entry's prefecture (`pref_max_scale`,
  `pref_shindo`) and at the entry's municipality (`local_scale`, `local_shindo`), and early warnings for the
  prefecture.
- Heat index (WBGT) for the offices whose file name is known (the Tokyo office).
- A feed-problem binary sensor, diagnostics that redact the place, and a test action `jma.simulate`.
- English and Japanese translations.
