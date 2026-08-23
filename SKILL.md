---
name: endurance-health-analyst
description: >
  Personal endurance sports analyst + daily Show Up system for runners, swimmers and
  cyclists. Use when the user asks to analyze a run/ride/swim ("分析我今天的跑步"),
  explain why heart rate felt high ("为什么今天7分配心率这么高"), compare against their
  own history ("和过去一年相似跑步比较", "最近是不是心率越来越高"), review aerobic state
  ("分析最近一个月的有氧状态"), ingest Apple Health exports / COROS FIT files /
  workout routes, enrich weather, or generate a shareable workout card
  ("生成今天的运动分享卡"). Explains what the body paid for a workout and why —
  never a medical diagnosis, never population averages as the yardstick.
version: 0.1.0
---

# Endurance Health Analyst

把一次运动从"我跑了多少"，翻译成"**今天我的身体为了这次运动付出了什么，以及为什么**"。

Core chain (never break this order):

```
运动记录 → 身体响应 → 环境影响 → 个人历史基线 → 解释今天 → 分享卡
```

## Non-negotiables

1. **纵向比较 only** — compare the user to their own baseline (7/28/90/365d), never to population averages.
2. **环境必须进入解释** — high HR + hot/humid weather ≠ "有氧能力下降". Consider heat, humidity, dew point, sleep, recent load, elevation before blaming fitness.
3. **Privacy first** — share cards default to privacy mode (start/finish GPS trimmed ~300 m).
4. **医疗边界** — sports analysis, not diagnosis. Never output cardiac/arrhythmia/disease conclusions. On abnormal data + symptoms: 建议停止高强度运动并考虑专业医学评估.
5. **跑步优先** — V0.x focuses running; map other sports generically.

## Environment setup (once per machine)

```bash
~/.agents/skills/endurance-health-analyst/setup.sh   # creates .venv + installs duckdb
PY=~/.agents/skills/endurance-health-analyst/.venv/bin/python
S=~/.agents/skills/endurance-health-analyst/scripts
```

Database: `data/health.duckdb` (local-first, all data stays on this Mac).
User config: `data/config.json` (create by copying `data/config.example.json`; set
`athlete.max_hr` / `lthr` / `age` for accurate zones, and `source_priority`).

## Pipelines by user intent

| 用户说 | 跑什么 |
|---|---|
| 导入 Apple Health（首次/更新） | `$PY $S/ingest_apple_health.py ~/Downloads/export.zip` |
| 导入 COROS .fit | `$PY $S/ingest_fit.py <files.fit> [--sport run]` |
| 分析我今天的跑步 / 为什么心率这么高 | `enrich_weather.py --workout latest:run` → `analyze_workout.py --workout latest:run`（可加 `--rpe 7 --breathing hard --legs fresh --feeling ...`）→ 回答用户 |
| 和过去一年/一段时间相似跑步比较 | `$PY $S/compare_history.py --workout latest:run` |
| 最近是不是心率越来越高 / 有氧状态 | `$PY $S/compare_history.py --trend`（+ `--windows` 看 7/28/90/365 滚动基线） |
| 生成今天的运动分享卡 | `$PY $S/render_card.py --workout latest:run`（产出 `output/workout-card.png` + `report.html`） |

Outputs land in `output/`: `workout.json` (unified schema), `analysis.json`
(one-liner + 3 signals + next steps + metrics + baseline), `workout-card.png`,
`report.html` (pace × HR timeline).

`--workout` accepts: a workout_id prefix, `latest`, `latest:run`, or `YYYY-MM-DD`.

## How to answer "为什么今天身体感觉是这样"

Read `output/analysis.json` and compose the answer with this fixed structure:

1. **一句话** — `one_liner` field (may rephrase, keep the causal story: 环境/恢复 vs 跑力).
2. **三个信号** — `signals`: ❤️ Cardiovascular (`hr_vs_baseline_bpm`), 🌡 Environment
   (temperature/humidity/dew point), ⚡ Performance (HR drift). One conclusion each.
3. **下一步** — `next_steps`, max 2, actionable.

Interpretation guardrails:
- `hr_vs_baseline_bpm ≥ +5` → "同配速心血管成本偏高";结合 `heat_load` (high ⇒ 环境解释优先)、
  HR drift、前一晚睡眠 (`sleep_prev_night_min`)、近 7 天负荷 (`km_last_7d`) 再下结论。
- HR drift 仅当 `interpretable: true`（配速前后差 ≤5% 且非间歇课）才作为证据引用。
- EF (speed per heartbeat) 只做个人纵向趋势，绝不当作跨人指标。
- 基线不足 (`enough_for_baseline: false`) 时明说"历史相似跑步还不够，暂不能对比"。

## Data notes

- Apple Health `export.zip`: streaming parse; route = `workout-routes/*.gpx`; legacy
  `.perf` routes are unsupported → suggest re-export from a newer iOS device.
- Dedup: same sport + start within 10 min ⇒ keep one by `source_priority`
  (COROS > Apple Watch > Strava > iPhone), duplicates logged to `ingest_log`.
- Weather: Open-Meteo (no key). Archive API lags ~5 days; recent workouts auto-fall
  back to the forecast API. No GPS route ⇒ no weather (coords unknown).
- All estimated HR-zone bases are flagged `"estimated": true` — surface that to the user.

## References (read when needed)

- `references/metrics.md` — formulas, thresholds, valid flags
- `references/apple-health.md` — export quirks, localized filenames, source dedup
- `references/coros.md` — FIT ingestion + COROS MCP notes
- `references/scientific-notes.md` — drift/EF/HRR physiology + medical boundary phrasing

## V0.1 acceptance (already validated by tests/e2e_test.py)

```
export.zip → 解析户外跑 → GPS+HR+Pace → 天气 → HR Drift → 历史相似跑比较 → 一句话 → workout-card.png
```
