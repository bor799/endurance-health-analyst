# COROS integration

## Priority order (don't rebuild what exists)

1. **COROS official MCP** — if the user has it connected, prefer it for pulling
   activities/streams. The skill's DuckDB remains the system of record; use MCP data
   as a source that lands in the same `workouts`/`series` tables.
2. **`.FIT` files from COROS export** — `scripts/ingest_fit.py`, pure-python decoder,
   no dependencies. Handles: file header, definition/data messages, compressed
   timestamps, developer-field skipping, CRC-tolerant parsing.

## What ingest_fit.py decodes

Record messages (global 20): timestamp (FIT epoch 1989-12-31 UTC), position
lat/lon (semicircles ×180/2³¹), heart rate, cadence, distance (cm→m), speed (mm/s→m/s),
power. Lap messages are counted; session metadata optional. Sport detection defaults by
median moving speed (<5 m/s → run, else ride) — override with `--sport`.

Second-level granularity is preserved in `series`.

## Workout matching

A `.fit` whose start falls within 10 min of an existing same-sport workout is treated
as the same session: logged + skipped unless `--force`. This is the COROS-vs-Apple-Watch
double-count guard. If both exist, COROS GPS wins on `source_priority`.

## Typical flows

```bash
# COROS app → 我的 → 设置 → 数据导出/下载 .fit
.venv/bin/python scripts/ingest_fit.py ~/Downloads/COROS/*.fit
.venv/bin/python scripts/enrich_weather.py --all-missing
.venv/bin/python scripts/analyze_workout.py --workout latest:run
```

If the COROS MCP is available in-session: fetch activity streams (time, latlng,
heartrate, cadence, altitude, distance), then insert via the same schema
(`series` + `route_points` + `workouts`) using `eha_lib.make_workout_id("COROS", ...)`
and the same 10-min duplicate guard — keep the DB canonical.
