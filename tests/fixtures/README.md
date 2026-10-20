# Test fixtures

Samples of the files the integration reads, used by the tests instead of the network. The terms of each service
are in the main README (Data sources and attribution). "Sample of a published file (may be trimmed)" means the
file is a copy of what the service published, possibly cut down; it is not edited otherwise. "Synthetic" means
the file was written for the tests, with invented values, in the shape of the service's answer.

| File | Published by | Kind |
|---|---|---|
| `amedastable.json` | Japan Meteorological Agency (AMeDAS station table) | sample of a published file (may be trimmed) |
| `area.json` | Japan Meteorological Agency (area list) | sample of a published file (may be trimmed) |
| `extra.xml` | Japan Meteorological Agency (XML feed) | sample of a published file (may be trimmed) |
| `forecast_010000.json`, `forecast_130000.json`, `overview_130000.json` | Japan Meteorological Agency (forecast) | sample of a published file (may be trimmed) |
| `himawari_targetTimes_jp.json`, `rasrf_targetTimes.json`, `rasrf_targetTimes_b.json`, `risk_targetTimes.json`, `targetTimes_N1.json`, `targetTimes_N2.json`, `targetTimes_N3.json`, `targetTimes_N3_b.json` | Japan Meteorological Agency (map-tile frame lists) | sample of a published file (may be trimmed) |
| `tile_hrpns_rain_z6_54_25.png`, `tile_hrpns_z10_909_403.png`, `tile_rasrf_z10_sample.png`, `tile_risk_transparent_z10.png` | Japan Meteorological Agency (map tiles). `tile_hrpns_rain_z6_54_25.png` is a zoom-6 tile with rain in it, used for the palette test; it does not contain the tests' place (which lies in tile 56/25 at zoom 6), so it is not that place's tile | sample of a published file (may be trimmed) |
| `typhoon_TC2632_forecast.json`, `typhoon_TC2632_specs.json`, `typhoon_targetTc.json`, `typhoon_TC2633_forecast.json`, `typhoon_TC2633_specs.json`, `typhoon_targetTc_20261003.json` | Japan Meteorological Agency (typhoon) | sample of a published file (may be trimmed) |
| `warning_r8_130000.json`, `warning_r8_map.json`, `warning_stale_130000.json`, `warning_timeline_130000.json` | Japan Meteorological Agency (warnings; `warning_stale_130000.json` is a file in the older format that is no longer updated) | sample of a published file (may be trimmed) |
| `point_44132_12.json` | Japan Meteorological Agency (AMeDAS observations) | sample of a published file (may be trimmed) |
| `point_62016_12.json` | none: written for the tests, in the shape of `point_44132_12.json`, without the pressure keys (`pressure`, `normalPressure`) and without the snow keys (`snow`, `snow1h`, `snow6h`, `snow12h`, `snow24h`), for station 62016, the first Osaka-prefecture station of `amedastable.json` whose element list has no pressure | synthetic |
| `openmeteo_msm.json` | none: written for the tests, in the shape of an Open-Meteo answer (72 hourly values; of the variables the integration requests it leaves out `cloud_cover` and `dew_point_2m`, and `precipitation_probability` holds only nulls) | synthetic |
| `p2p_551_sample_a.json`, `p2p_551_sample_b.json` | P2P地震情報 (earthquake records) | sample of a published file (may be trimmed) |
| `p2p_552_cancelled.json`, `p2p_552_majorwarning.json`, `p2p_552_many_areas_40.json`, `p2p_552_tokyo_bay.json`, `p2p_552_warning.json`, `p2p_552_watch.json`, `p2p_jma_tsunami_history.json` | P2P地震情報 (tsunami information, code 552, and the `/v2/jma/tsunami` list) | sample of a published file (may be trimmed) |
| `alert_20240411_17.csv`, `alert_20240412_05.csv`, `alert_20260720_17.csv`, `alert_20260721_05.csv`, `alert_20261003_05.csv`, `alert_20261003_10.csv`, `alert_20261003_17.csv` | Ministry of the Environment (heat-stroke alerts, one file per issue) | sample of a published file. Source: Ministry of the Environment Heat Illness Prevention Information website (https://www.wbgt.env.go.jp/). Public Data License 1.0. |
| `wbgt_yohou_tokyo.csv` | Ministry of the Environment (heat index forecast) | sample of a published file (may be trimmed). Source: Ministry of the Environment Heat Illness Prevention Information website (https://www.wbgt.env.go.jp/). Trimmed sample; Public Data License 1.0. |
