# Apple Health export — parsing notes

Source: iPhone 健康 App → user avatar →「导出所有健康数据」→ `export.zip` (prepares
for minutes, arrives via Files/AirDrop/邮件). Multi-year exports can be 1–8 GB.

## Archive layout

```
export.zip
├── 导出.xml | export.xml | Export.xml   ← root <HealthData>; name is LOCALIZED,
│                                          and some zip tools show mojibake
├── export_cda.xml                        ← clinical records; NOT used
└── workout-routes/
    └── route_2026-08-21_18.30_run_xxxx.gpx   ← one per GPS workout (iOS 16+)
```

- Main-file detection: scan depth ≤ 1 XMLs, read first 2 KB, take the one containing
  `<HealthData` — never trust the filename.
- Legacy exports ship binary `.perf` route files instead of GPX — unsupported; ask the
  user to re-export from a device on iOS 16+.
- We stream with `iterparse` + `root.clear()` — memory stays flat on multi-GB files.
- Re-ingest is idempotent for workouts (`INSERT OR REPLACE` on stable id) and
  near-idempotent for samples (dedup happens at query time).

## Record types ingested

| Apple type | metric key | notes |
|---|---|---|
| HeartRate | hr_samples | ~5 s cadence from Watch |
| RestingHeartRate | resting_hr | daily |
| HeartRateVariabilitySDNN | hrv_sdnn | ms, watch daytime+sleep samples |
| VO2Max | vo2max | ml/kg/min, watch estimated |
| RespiratoryRate | respiratory_rate | sleep-derived |
| ActiveEnergyBurned | active_energy | kcal |
| AppleExerciseTime | exercise_time | min |
| RunningSpeed / RunningCadence / WalkingSpeed | running_speed / running_cadence / walking_speed | per-second during watch workouts; used as series fallback when no GPX route |
| SleepAnalysis (asleep*) | sleep_asleep | interval minutes; overlaps resolved by night-union at query time |

## Workout element

```xml
<Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="52.5"
         totalDistance="8.2" startDate="2026-08-21 18:30:12 +0800" endDate="..."
         sourceName="Apple Watch" device="&lt;&lt;HKDevice: name:Apple Watch...&gt;&gt;">
  <WorkoutStatistics type="...HeartRate" average="158.3" maximum="175" unit="count/min"/>
  <WorkoutStatistics type="...ElevationAscended" sum="86" unit="m"/>
  <MetadataEntry key="com.apple.health.workout-route" value="../workout-routes/route_xxx.gpx"/>
</Workout>
```

- `totalDistance` is km. avg/max pace derive from RunningSpeed stats (km/h → s/km),
  else distance/duration.
- Date strings carry explicit offsets (`+0800`); everything is stored as UTC.

## Source deduplication

The export can contain the same session from Apple Watch, iPhone, COROS app, Strava…

1. **Workout matching**: same sport + start within 10 min ⇒ same session. Winner by
   `source_priority` (COROS > Apple Watch > Garmin > Strava > iPhone > other), then
   longer duration. Losers go to `ingest_log` as `duplicate_suppressed` rows.
2. **Sample dedup**: `preferred_hr` view keeps one sample per minute bucket,
   highest-priority source wins (`source_rank()` macro, generated from config).
3. Same-source re-exports collapse on the stable workout id hash.

## Weather attach

Route median coordinate + start/end window → Open-Meteo hourly (archive API, forecast
fallback inside ~7 days of "today"), averaged over the workout hours. No route ⇒ no
weather (we don't know where you were — don't guess).

## Real-export quirks (HealthKit Export Version 14, verified 2026-08)

A real 中文 export (`导出.zip`, 44 MB → 1.25 GB) surfaced these differences from
older/naive assumptions — all handled by `ingest_apple_health.py`:

1. **DOCTYPE prolog**: the main XML opens with a long `<!DOCTYPE HealthData [...]>`
   block; `<HealthData` appears well past the first 2 KB. The main-file scan reads
   up to 1 MB in chunks instead of a fixed head window (and skips CDA files whose
   root is `<ClinicalDocument`).
2. **`durationUnit="min"`**: `<Workout duration="32.44" durationUnit="min">` —
   seconds = duration × 60. Older exports without the attribute stay seconds.
3. **Distance/energy live in `WorkoutStatistics` sums** (`…DistanceWalkingRunning
   sum="3.92" unit="km"`), not in legacy `totalDistance` attributes. Both are read.
4. **Route links are `<WorkoutRoute><FileReference path="/workout-routes/….gpx"/>`
   elements** (not the legacy `com.apple.health.workout-route` MetadataEntry).
   Both formats supported, plus a fallback that matches remaining GPX files to
   workouts by the first `<trkpt>` timestamp (both UTC → no tz math).
5. **Elevation** often appears only as `MetadataEntry HKElevationAscended` with a
   `"3230 cm"` style value (parsed cm → m).
6. **COROS workouts synced via Health are summary-only**: distance/duration yes,
   HR curve no (all-day sparse samples ~1/15 min at best). Second-level HR for the
   COROS era must come from `.FIT` exports (P1) — until then those runs carry no
   HR analysis, by design rather than by bug.
7. Same zip can contain several watch generations (an older device and a newer one,
   e.g. after an upgrade); dedup keys on sport + start time, not source name.
