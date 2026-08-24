# 肌肉放松记录与提醒

## 边界

该功能记录一次短而温和的训练后自我放松，不诊断酸痛、损伤或疾病，也不提供康复治疗方案。

如果动作引起尖锐疼痛、明显肿胀或不适持续加重，立即停止。严重、无法解释或伴随警示症状的情况不应继续高强度训练，应考虑专业医学评估。

## 三套保守清单

- `post_run_basic`：轻松走动，小腿/股四头肌舒适放松，髋部与踝部轻柔活动。
- `lower_leg_gentle`：慢走、踝关节舒适活动、小腿轻柔放松；泡沫轴只用轻压力。
- `hips_glutes_gentle`：小幅髋部环绕、臀肌和髋屈肌舒适活动，不追求动作深度。

清单只适用于舒服的日常自我管理。一个清单存在，不代表其中动作适合某个具体症状。

## CLI

```bash
PY=.venv/bin/python

$PY scripts/manage_recovery.py create \
  --workout latest:run \
  --muscles calves,quads,glutes \
  --routine post_run_basic \
  --duration-min 10 \
  --soreness-before 5 \
  --remind-at 2026-08-24T21:30:00+08:00 \
  --reminder-backend macos \
  --json

$PY scripts/manage_recovery.py complete \
  --plan recovery_ab12 --soreness-after 2 --note "小腿明显放松" --json
$PY scripts/manage_recovery.py skip \
  --plan recovery_ab12 --note "今天不舒服，暂不做" --json
$PY scripts/manage_recovery.py cancel --plan recovery_ab12 --json
$PY scripts/manage_recovery.py list --since 28d --status completed --json
$PY scripts/manage_recovery.py due --json
$PY scripts/manage_recovery.py due --mark-fired --json
```

紧张/酸胀评分是可选的 0-10 整数观察，只保存，不做医疗解释。

没有 UTC offset 的提醒时间必须同时提供 IANA timezone：

```bash
$PY scripts/manage_recovery.py create --muscles calves \
  --remind-at 2026-08-24T21:30:00 --timezone Asia/Shanghai --json
```

夏令时切换造成的不存在时间会被拒绝；回拨时重复出现的时间也会被拒绝，此时应提供明确 UTC offset。提醒时间和审计时间都以 UTC 写入现有的无时区 `TIMESTAMP` 字段，同时保留原时区用于显示；不会重写既有记录。

## macOS 提醒事项

`--reminder-backend macos` 会通过 `scripts/reminder_macos.scpt` 将提醒写入 macOS“提醒事项”。启用 iCloud 同步后，提醒可出现在 iPhone 与 Apple Watch。

第一次调用可能触发 Automation 和 Reminders 权限确认。Python 使用 argv 数组传递标题、备注、列表名和 ID，不把用户文本拼入 AppleScript 源码。

状态更新规则：

1. `osascript` 成功并返回外部提醒 ID 后，才标记 `scheduled`。
2. 创建失败时标记 `failed` 并返回权限提示，不能告诉用户“已经设好”。
3. 完成、跳过或取消前先提交 `completing/skipping/canceling` 中间状态，再同步外部提醒，最后更新计划状态。
4. 外部同步失败时保留可见的 `failed` 状态，不假装两个系统一致。
5. 如果进程在外部操作成功后中断，重新执行同一个完成/跳过/取消命令即可对账；外部提醒已经不存在时按幂等成功处理。

## 状态

- 计划：`planned`、`completed`、`skipped`、`canceled`
- 提醒：`pending`、`scheduled`、`fired`、`completing`、`skipping`、`canceling`、`canceled`、`failed`

`due` 只列出时间已到、计划仍处于 `planned` 且提醒状态为 `pending/scheduled` 的记录。调用方实际发出提醒后，才使用 `--mark-fired`。
