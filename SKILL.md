---
name: endurance-health-analyst
description: >
  This skill should be used when the user asks to analyze a run, ride, or swim;
  says "明明和上次差不多，为什么今天跑起来特别难", "为什么今天心率这么高",
  "这次 Apple Watch 心率靠谱吗", or "最近有氧状态是不是变差了"; wants to
  import Apple Health export.zip or COROS FIT data; asks to "记录今天要放松的小腿",
  "今晚提醒我做跑后放松"; or asks to "用这张照片和 GPS 轨迹生成朋友圈运动卡".
  It explains workout fluctuations against the athlete's own history and environment,
  tracks gentle muscle-recovery reminders, and renders privacy-safe square share cards.
  It never provides a medical diagnosis or uses population averages as the yardstick.
version: 0.2.0
---

# Endurance Health Analyst

把一次运动从“我跑了多少”，翻译成“**今天身体为这次运动付出了什么、为什么和过去不一样、下一次该做什么**”。

始终保持这条证据顺序：

```text
运动记录 → 身体响应 → 环境影响 → 个人历史基线 → 主观感受 → 解释今天 → 放松/分享
```

## 不可违反的边界

1. **只做个人纵向比较**：使用 7/28/90/365 天和全历史基线，不用人群平均值判断好坏。
2. **环境必须进入解释**：高心率 + 湿热天气不等于有氧能力下降。先检查温度、湿度、露点、睡眠、近期负荷、坡度和训练结构。
3. **证据不足就明说**：相似训练不足、心率缺失或稳定段不足时，不编造趋势或因果。
4. **隐私先于绘图**：分享卡默认隐藏起终点；短路线或裁剪为空时完全不画，绝不回退原坐标。
5. **照片保持真实**：只做方向修正、比例裁切、元数据清除和必要遮罩，不重绘、扩图、美颜或生成照片内容。
6. **运动健康分析，不是疾病诊断**：不输出心脏、心律失常、损伤或疾病结论。异常数据 + 明显警示症状时，只输出：
   “该表现无法仅通过运动数据解释，建议停止高强度运动并考虑专业医学评估。”
7. **跑步优先**：V0.x 优先解释跑步；骑行和游泳按通用口径处理。

## 环境准备

```bash
~/.agents/skills/endurance-health-analyst/setup.sh
PY=~/.agents/skills/endurance-health-analyst/.venv/bin/python
S=~/.agents/skills/endurance-health-analyst/scripts
```

数据库：`data/health.duckdb`。<br>
配置：复制 `data/config.example.json` 为 `data/config.json`，按需填写 `max_hr`、`lthr`、`age`、来源优先级和隐私半径。

## 按用户意图执行

| 用户意图 | 执行流程 |
|---|---|
| 首次或更新 Apple Health | `$PY $S/ingest_apple_health.py ~/Downloads/export.zip` |
| 导入 COROS FIT | `$PY $S/ingest_fit.py <files.fit> [--sport run]` |
| “今天为什么比上次难/轻松？” | 补主观输入 → `enrich_weather.py` → `analyze_workout.py` → 按固定结构回答 |
| “和过去相似训练相比呢？” | `$PY $S/compare_history.py --workout latest:run` |
| “最近有氧是不是变差？” | `$PY $S/compare_history.py --trend`，必要时加 `--windows` |
| “记录小腿放松，今晚提醒我” | `manage_recovery.py create`，明确肌群、时间和提醒后端 |
| “我做完放松了” | `manage_recovery.py complete --plan <id> --soreness-after <0-10>` |
| “用这张照片生成朋友圈卡” | 确认照片路径 → `render_card.py --photo <path> --privacy on` |

`--workout` 接受 workout_id 前缀、`latest`、`latest:run` 或 `YYYY-MM-DD`。

## 解释一次状态起伏

先收集必要主观信息，再运行分析：

```bash
$PY $S/enrich_weather.py --workout latest:run
$PY $S/analyze_workout.py --workout latest:run \
  --rpe 7 \
  --breathing hard \
  --legs tired \
  --pain none \
  --feeling "配速差不多，但后半程明显更累"
```

读取 `output/analysis.json`，严格按以下结构回答：

1. **一句话**：复述 `one_liner` 的因果主线，区分环境/恢复成本与真实趋势变化。
2. **三个信号**：❤️ 心血管、🌡 环境、⚡ 表现，各写一个结论。
3. **下一步**：最多 2 个可执行动作。

解释守则：

- `hr_vs_baseline_bpm ≥ +5`：描述为“同配速心血管成本偏高”，结合天气、睡眠、近 7 天负荷和主观感受再解释。
- HR Drift 只有在 `interpretable: true` 时才能作为证据；跑走间歇只引用识别出的稳定段。
- EF 只用于个人趋势，不作跨人指标。
- `enough_for_baseline: false`：明确说“历史相似训练不足，暂时无法判断是否偏离基线”。
- 呼吸吃力而腿尚可：只描述“本次限制更偏心肺/环境侧”；腿疲劳而呼吸相对轻松：只描述“外围肌肉负担更突出”。两种情况都不推断疾病。

产物：

- `output/workout.json`：统一 Workout Schema；
- `output/analysis.json`：一句话、三个信号、建议、指标与基线；
- `output/report.html`：Pace × HR 时间线、心率区间和相似历史训练。

## 记录肌肉放松与提醒

在用户明确肌群和时间后创建计划。时间不明确时先询问，不猜测。

```bash
$PY $S/manage_recovery.py create \
  --workout latest:run \
  --muscles calves,quads,glutes \
  --routine post_run_basic \
  --duration-min 10 \
  --soreness-before 5 \
  --remind-at 2026-08-24T21:30:00+08:00 \
  --reminder-backend macos \
  --json
```

macOS 提醒成功后会同步到启用 iCloud 的 iPhone/Apple Watch。首次调用可能触发系统 Automation 和 Reminders 权限确认。只有 `osascript` 返回成功与提醒 ID 后，才能告诉用户提醒已设置。

完成、跳过或取消：

```bash
$PY $S/manage_recovery.py complete --plan <id> --soreness-after 2 --note "小腿放松了"
$PY $S/manage_recovery.py skip --plan <id> --note "今天不舒服，暂不做"
$PY $S/manage_recovery.py cancel --plan <id>
$PY $S/manage_recovery.py list --since 28d --json
```

只使用 `post_run_basic`、`lower_leg_gentle`、`hips_glutes_gentle` 三套保守清单。不要把评分变化解释成治疗效果或损伤恢复。尖锐疼痛、明显肿胀、动作使症状加重时，不推荐强力拉伸、滚压或按摩。

详细规则见 `references/muscle-recovery.md`。

## 生成照片 × 轨迹方形卡

```bash
$PY $S/render_card.py \
  --workout latest:run \
  --photo /absolute/path/to/today-run.HEIC \
  --privacy on
```

输出 `output/workout-card.png`（2160×2160）和 `output/workout-card.html`。

执行顺序不可交换：

```text
读取原始路线 → 严格隐私裁剪 → 局部 XY 投影 → 删除经纬度
读取照片 → EXIF 方向修正 → 比例裁切 → 无元数据 PNG
安全 XY + 净化照片 → 透视叠加 → 方形卡
```

- 默认照片为主视觉，距离为主数字，平均心率为第二层级；保留时长、配速和最多两行结论。
- 路线透视只是一种视觉投影，不代表真实三维地形。
- 无照片时生成方形路线版；损坏照片返回错误，不静默降级。
- 分享前提醒用户检查照片画面是否包含人物、门牌或可识别地标；自动去 EXIF 不能消除画面内容本身的隐私。

详细规则见 `references/share-card.md`。

## 数据说明

- Apple Health `export.zip` 使用流式解析；支持 HealthKit Export v14 的长 DOCTYPE、分钟时长、`WorkoutStatistics` 距离、`WorkoutRoute/FileReference` 和厘米海拔 metadata。
- 同运动且开始时间相近的跨来源记录按 `source_priority` 去重，详情写入 `ingest_log`。
- COROS 经 Apple Health 同步通常只有训练摘要；秒级心率与轨迹需要原始 FIT。
- Open-Meteo 不需要 Key。查询会发送运动时间与一个坐标：优先使用本次路线中部；无路线时自动借用前后 60 天内最近的已定位训练坐标，并在 `weather.raw.position_source` 标明来源；仍无法定位时跳过天气。
- 所有估计心率区间依据都标记 `estimated: true`，回答时必须明示。

## References

- `references/metrics.md`：EF、HR Drift、HRR、Zones、相似训练与有效性条件
- `references/apple-health.md`：HealthKit 导出格式、路由、来源去重
- `references/coros.md`：FIT 导入与 COROS 数据边界
- `references/scientific-notes.md`：生理解释与医疗措辞
- `references/muscle-recovery.md`：放松记录、提醒状态与安全边界
- `references/share-card.md`：照片处理、轨迹隐私和视觉规则

## 验收

```text
export.zip → GPS+HR+Pace → 天气 → 指标 → 个人历史 → 一句话
          → 放松记录/提醒
          → 照片净化 + 隐私轨迹 → 2160×2160 分享卡
```

运行：

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
.venv/bin/python tests/e2e_test.py
```
