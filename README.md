# 耐力运动解释引擎

> **A local-first endurance workout explanation engine built around your own history.**

输入 Apple Health 导出（或 COROS FIT），输出的不是另一个数据仪表盘，而是一句因果结论：**「今天你的身体为这次运动付出了什么，以及为什么」**，外加一张保护路线隐私的分享卡。

```mermaid
flowchart LR
    A[运动记录<br/>Apple Health / COROS FIT] --> B[身体响应<br/>HR 曲线 · 配速 · 爬升]
    B --> C[环境影响<br/>温度 · 湿度 · 露点 · 风]
    C --> D[个人历史基线<br/>相似训练 · 滚动窗口]
    D --> E[解释今天<br/>结论 · 信号 · 建议]
    E --> F[分享卡<br/>路线隐私默认开启]
    style E fill:#D8E8E4,stroke:#769A92,stroke-width:2px
    style F fill:#E7EFED,stroke:#91AAA5,stroke-width:2px
```

一个典型输出长这样（而不是"今天跑了 3.9 km，平均心率 153"）：

> 相同配速下的心率高于你的个人基线，后半程存在心率漂移；当天 31°C / 湿度 73% /
> 体感 36.5°C——更可能是湿热环境与恢复状态推高了生理成本，而不是跑力下降。

---

## 为什么需要它（三个分论点）

### 论点一：展示数据 ≠ 解释训练

心率 160 在 31°C 湿热傍晚和在 15°C 干爽清晨，含义完全不同；间歇课的"心率漂移"
和匀速课的漂移也不是同一回事。所以本工具的解释链路是**固定的**（上图），
环境（天气、睡眠、近 7 天负荷）必须进入解释模型，而不是事后附注。
所有会污染判读的段落——热身、冷身、红灯停车、间歇的走跑切换——在计算前就被
过滤或单独锚定，配速不稳定时直接标记"本次漂移不可解释"，不硬编故事。

### 论点二：标尺只有一个——过去的你

- **纵向比较 only**：EF、同配速心率、HR Drift、HRR60/120 只和自己的历史比，
  绝不拿"人群平均值"当标尺（心率区间的阈值来源也按优先级：实验室 > LTHR >
  个人历史最大 HR > 220−age，最后两种一律打 `estimated` 标记）。
- **相似训练检索**：同运动、距离 ±25%、配速 ±12%、温度 ±4°C、湿度 ±15%，
  样本不足时自动放宽并注明；7/28/90/365 天/全历史滚动基线。
- **诚实优先**：基线样本不够（比如刚换表、刚重启训练）就直说
  "历史相似训练还不够，暂不能对比"。

### 论点三：三条红线不可越

1. **本地优先**：DuckDB 单文件数据库（`data/health.duckdb`），健康数据不出本机；
   唯一的外部请求是 Open-Meteo 天气（免 Key），只发送路线坐标与时间戳。
2. **隐私默认开启**：分享卡默认裁掉起点/终点附近 ~300 m GPS，绝不默认暴露家庭位置。
3. **医学边界**：这是运动健康分析工具，**不是疾病诊断工具**。出现异常数据 +
   明显症状时，只输出"建议停止高强度运动并考虑专业医学评估"，绝不给心脏/疾病结论。

---

## 30 秒上手

```bash
git clone https://github.com/bor799/endurance-health-analyst.git
cd endurance-health-analyst
./setup.sh                                    # 一次性：创建 .venv 并安装 duckdb
cp data/config.example.json data/config.json  # 可选：填 age / max_hr / lthr

PY=.venv/bin/python
# iPhone 健康 App → 头像 → 导出所有健康数据 → 得到 export.zip
$PY scripts/ingest_apple_health.py ~/Downloads/export.zip
$PY scripts/enrich_weather.py --all-missing    # Open-Meteo 补天气
$PY scripts/analyze_workout.py --workout latest:run
$PY scripts/render_card.py  --workout latest:run
open output/workout-card.png output/report.html
```

日常只需后三步（ingest 是偶尔一次的全量导入）。带主观感受让解释更准：

```bash
$PY scripts/analyze_workout.py --workout latest:run \
    --rpe 7 --breathing hard --legs fresh --pain none --feeling "最后两公里有点顶"
```

COROS 用户：`$PY scripts/ingest_fit.py ~/Downloads/*.fit`（自动与 Apple 记录去重）。

## 产出物

| 文件 | 内容 |
|---|---|
| `output/workout.json` | 统一 Workout Schema（identity / performance / cardiovascular / environment / recovery_context / route） |
| `output/analysis.json` | 一句话结论 + 三个信号（❤️ 心血管 · 🌡 环境 · ⚡ 表现）+ 下一步建议 + 全部指标与基线对比 |
| `output/workout-card.png` | Show Up 分享卡：路线为主角，隐私模式默认开启 |
| `output/report.html` | Pace × HR 同轴时间线 + 心率区间 + 相似训练对比表 |

## 指标口径（V0.1 —— 刻意的克制清单）

| 指标 | 定义 | 判读规则 |
|---|---|---|
| EF 有氧效率 | 稳定段 speed/HR | 仅个人纵向趋势，绝不跨人比较 |
| HR Drift | 稳定段后半 vs 前半 HR% | 过滤热身/冷身/停车/间歇；配速前后差 >5% 或间歇课 → `interpretable: false` |
| HRR60/120 | 锚定最后冲刺峰值后的心率回落 | 间歇课锚点自动切换（峰值后 20–70s 陡降 ≥12 bpm = 走恢复签名）；不用于诊断 |
| Z1–Z5 | 阈值来源优先级链 | 非实测基础全部标 `estimated: true` 并明示用户 |

## 已验证

- **合成导出 E2E 35/35 全绿**（`tests/e2e_test.py`）：
  export.zip → 解析户外跑 → GPS+HR+配速 → 天气 → HR Drift → 历史相似跑比较 → 结论 → 分享卡 PNG。
- **真实 HealthKit Export v14 端到端跑通**：4 年 / 487 次训练（跨源去重压制 11 条）/
  30 万+ 心率样本 / 100 条 GPS 路线 / 近期训练全部补齐天气。v14 的真实格式差异
  （超长 DOCTYPE、`durationUnit="min"`、距离藏在 `WorkoutStatistics` 求和、
  `WorkoutRoute/FileReference` 新路由链接、海拔只在 metadata、COROS 经 Health 同步
  只有摘要）已全部固化在解析器与 `references/apple-health.md`。

## 作为 Agent Skill 使用

本仓库同时是一个标准的 Agent Skill：`SKILL.md` 描述了命令路由与解释守则
（什么用户意图跑什么管道、"为什么心率这么高"如何按固定结构回答）。
放到任意支持 SKILL.md 约定的 agent（Claude Code / Cindy 等）技能目录即可，
也可以完全当独立 CLI 使用——脚本之间只通过 DuckDB 与 `output/` 通信。

## 目录结构

```
endurance-health-analyst/
├── SKILL.md            # agent 入口（命令路由 + 解释守则）
├── references/         # 指标公式 / Apple Health v14 陷阱 / COROS / 生理学注记
├── scripts/            # ingest / enrich_weather / analyze / compare_history / render_card
├── templates/          # workout-card.html / report.html
├── tests/e2e_test.py   # 全链路验收（合成 Apple Health 导出，含真值断言）
├── data/               # config.example.json（真实数据库不入库，见 .gitignore）
└── output/             # 每日产物（运行时生成）
```

## 边界与路线图

- V0.x **跑步优先**；骑行/游泳按通用口径映射。Apple legacy `.perf` 路线不支持
  （需从较新 iOS 重新导出）。
- Roadmap：COROS FIT 秒级心率回填 2024 时代基线 · 更多主观恢复信号 · 周度课表结构分析。

## License

MIT
