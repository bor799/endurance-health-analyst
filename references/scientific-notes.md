# Scientific notes — physiology behind the interpretation rules

These notes justify the causal language the skill uses. They are heuristics for
**sport context**, not clinical claims.

## Cardiovascular drift

During prolonged steady exercise, HR slowly rises at constant workload —
cardiovascular drift. Main drivers: rising core/skin temperature, dehydration-driven
plasma volume loss, peripheral vasodilation in heat, catecholamines late in sessions.

Practical read (easy-steady runs, personal context):
- ~+1–5% HR drift per hour: ordinary.
- >8%/h in hot/humid conditions with flat pace: **expected environment cost**, not
  fitness loss. The narrative should say exactly this.
- Same drift in cool conditions, well-hydrated, repeated across weeks: worth watching
  recovery status (sleep, RHR, HRV) before touching training volume.

Hence the rule: `interpretable` drift requires |pace change| ≤ 5% and non-interval
sessions; heat load is always co-reported.

## Heat & humidity

- Performance decrement starts around wet-bulb-globe ~18–20 °C for sustained efforts;
  perceptible for easy runs when air temp ≥ ~27 °C or dew point ≥ ~21 °C.
- Dew point matters more than RH at high temps (evaporation limit). The card shows
  temp + RH; `dew_point_c` sits in the environment block.
- Typical response: +3–10 bpm at the same pace, larger drift, higher RPE. If the
  user's HR-vs-baseline delta falls in that band on a hot day, the causal story is
  "environment + recovery raised the cost", not "aerobic capacity dropped".

## Aerobic efficiency (EF)

Speed per heartbeat over the steady segment. Classic aerobic-development marker:
as stroke volume grows, the same speed costs fewer beats. Confounds: heat, hills,
fatigue, dehydration, cadence changes — which is why EF is only read as a rolling
personal trend (28 d vs 90 d), never a single-day verdict.

## Heart rate recovery

HRR60 reflects parasympathetic reactivation. Trends matter: a sustained drop of
several bpm (with rising RHR / falling HRV) can accompany accumulated fatigue.
Single values vary with cooldown behavior — require the trend, never flag one workout.

## Zone basis honesty

Max-HR-from-history is a **lower bound** of true max (you only see the max you've
produced). Age formulas miss individuals by ±10–15 bpm. That's why every estimated
basis is labeled `estimated` and the config accepts lab/field LTHR values that
silently upgrade all zone math.

## Subjective data closes the loop

Sensors + body feel = complete data. Pattern worth calling out in the narrative:
breathing hard + legs fresh + elevated HR → cardiopulmonary-limited session (heat,
allergies, fatigue, illness risk — phrase neutrally: "心肺限制为主"); legs tired +
breathing easy → muscular/peripheral fatigue (training load, surfaces, fueling).

## Medical boundary (mandatory phrasing)

This is a sports analysis tool. We do not diagnose, and we do not emit probability
statements about arrhythmia or disease. When the data looks anomalous (e.g., HR
plateau far beyond personal max, extreme HRR anomaly) **and** the user reports
symptoms (chest pain, palpitations, dizziness, unexplained dyspnea), respond with:

> 该表现无法仅通过运动数据解释，建议停止高强度运动并考虑专业医学评估。

Nothing stronger, nothing weaker.

## HRR anchoring (updated after real interval data)

Textbook HRR assumes effort stops dead at workout end. Two real patterns break it:

- **Interval session ending with walk recovery**: HR has already recovered at the
  official end → end-anchored HRR reads ≈ 0. Fix: when the last-10-min HR peak is
  followed by a sharp drop (≥ 12 bpm within 20–70 s — the walk-recovery
  signature), anchor HRR on that peak (`anchored_on: last_effort_peak`, offsets
  measured from the peak).
- **Continuous run with gradual cooldown ramp**: the peak sits atop a slow
  decline — peak-anchoring would read near-zero (verified against the synthetic
  fixture). Here we keep the end anchor and accept the conservative reading.

The steep-drop discriminator separates the two; `anchor_offset_s` and `note` are
exported so downstream consumers can see which rule fired.
