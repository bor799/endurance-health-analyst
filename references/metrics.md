# Metrics — formulas, thresholds, validity flags

V0.x keeps a deliberately small metric set. Each metric carries validity flags in
`analysis.json`; cite a metric only when its flags allow.

## Aerobic Efficiency (EF)

```
EF = avg_speed(m/s) ÷ avg_HR   over the steady aerobic segment
```

- Unitless personal index ("m/s per heartbeat"). **Longitudinal self-comparison only** —
  never compare absolute EF between athletes.
- Higher over time = more speed per heartbeat = aerobic improvement.
- `whole_workout_ef` variant uses whole-workout avg (includes warm-up) — use the steady
  `steady_ef` for trend claims.

## Heart Rate Drift

Steady aerobic segment selection:
1. Drop warm-up (first `steady_state.warmup_min`, default 5) and cool-down (last 3 min).
2. Keep only moving points (speed ≥ `min_speed_mps` 1.8) within ±15% of median speed
   (filters stops, traffic lights, surges).
3. Drop |grade| > 3% over a ~150 m rolling window (filters climbs).
4. If too little data survives, the grade filter relaxes first (`grade_relaxed: true`).
5. `pace_cv_pct > 12` → `interval_like: true`.

```
drift% = (HR_second_half − HR_first_half) ÷ HR_first_half × 100
interpretable = |pace_change%| ≤ 5 AND NOT interval_like
```

- Report `hr_slope_bpm_per_10min` (OLS slope) alongside.
- Typical easy runs: +1–5% per hour is normal; >8%/h with heat/humidity points to
  environment-driven cardiovascular strain (see scientific-notes.md).
- Never cite drift when `interpretable: false`.

## Heart Rate Recovery (HRR)

```
HR_end   = mean HR over last 30 s of recording
HRR60    = HR_end − HR(60 s after end)      (nearest sample ±25 s)
HRR120   = HR_end − HR(120 s after end)
```

- Requires the watch to keep recording ≥1–2 min after stopping (rare in Apple Health;
  often absent → `available: false, reason: no_post_workout_hr`).
- Longitudinal trend only. Low/blunted HRR trends can accompany overreaching — but we
  never diagnose; we flag "低于个人基线趋势" at most.

## HR Zones Z1–Z5

Basis resolution priority (stored in `hr_zones.basis`, `estimated` flag surfaced):

```
lab_threshold_hr (config)           → LTHR zones (Friel %LTHR: 81/89/96/100)
lthr (config, from COROS/Apple)     → LTHR zones
max_hr (user config)                → %max zones (60/70/80/90), configured provenance
observed max HR in 365 d history    → %max zones (60/70/80/90) — estimated, lower bound
220 − age (config)                  → %max zones — estimated, least reliable
```

- Zone seconds count **moving time only** (speed ≥ 1 m/s, HR present).
- Any basis other than lab/config values is an estimate — say so when quoting zones.

## Baseline comparison (compare_history)

Similar-workout filter chain:
1. same sport, start < current, ≤ 365 d lookback
2. avg pace within ±12%, distance within ±25%
3. strict tier: temperature ±4 °C AND humidity ±15% (both known)
4. strict tier < `min_similar` (3) → relax env filter, set `environment_relaxed`

Reported: median / P25 / P75 of avg HR, `hr_vs_baseline_bpm` (today − median),
`hr_percentile_in_history`, EF delta, drift baseline.

Effect sizes used in narrative templates:
- `hr_vs_baseline_bpm ≥ +5` → "cost elevated"; `≤ −5` → "cost lower"; else "within baseline"
- heat load high: temp ≥ 27 °C OR dew point ≥ 21 °C OR apparent ≥ 31 °C

## Aerobic trend verdict

Votes over three signals (28 d vs 90 d):
- EF delta ≥ +1% → +1 (improving) / ≤ −1% → −1
- HR-at-similar-pace delta ≥ +2 bpm → −1 / ≤ −2 → +1
- resting HR delta ≥ +2 bpm → −1 / ≤ −2 → +1
Sum ≥ 1 → improving; ≤ −1 → declining; else stable. Always list the votes (`reasons`).
