"""综合评估引擎：把历史训练、身体状态、力量、饮食折算为多维度得分。

维度与权重：
  有氧能力 25% | 力量水准 15% | 意志品质 20% | 恢复能力 15% | 天赋水准 25%
每个维度输出 0-100 分 + 证据明细，供短板分析(weakness.py)引用。
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta

from . import predictor, vdot

# ---------------------------------------------------------------- 工具


def _weeks_ago(n: int) -> datetime:
    return datetime.now() - timedelta(weeks=n)


# 人群参照模型的参数（不是「用户兜底值」）。
# 这是把 VDOT 映射到人群百分位的 logistic 曲线参数，来源于耐力项目的整体分布，
# 对任何用户都成立；个人的年龄/性别差异由下面的 corp修正 承担。
# 之所以命名为常量并集中于此，是为了让「哪部分是与用户无关的人群先验」一目了然
# ——评估里其余数字都必须是本人数据的函数。
POP_VDOT_MIDPOINT = 46.0     # logistic 中点（VDOT）
POP_VDOT_SCALE = 7.2         # logistic 斜率尺度的倒数
FEMALE_VDOT_OFFSET = 4.5     # 女性在该分布上的等效平移


def _percentile_from_vdot(vdot_value: float, age: float, sex: str) -> float:
    """同年龄同性别人群百分位的工程近似（logistic 映射，按年龄 graded 修正）。

    这是**人群参照模型**，不随用户变化的部分只有上面三个分布参数；
    用户的年龄与性别通过 graded 校正与性别平移进入，因此不同用户结果不同。
    """
    v = vdot_value
    if age > 35:
        v = v / (1 - 0.004 * min(45, age - 35))   # 年龄graded等效
    elif age < 20:
        v = v / (1 - 0.003 * (20 - age))
    if sex == "female":
        v = v + FEMALE_VDOT_OFFSET                # 女性标准等效换算
    p = 100.0 / (1 + math.exp(-(v - POP_VDOT_MIDPOINT) / POP_VDOT_SCALE))
    return round(min(99.5, max(0.5, p)), 1)


def weekly_km_series(activities: list[dict], weeks: int = 12) -> list[float]:
    """按周聚合跑量（km）。"""
    now = datetime.now()
    series = []
    for w in range(weeks - 1, -1, -1):
        start = now - timedelta(weeks=w + 1)
        end = now - timedelta(weeks=w)
        km = sum((a.get("distance_m") or 0) / 1000 for a in activities
                 if start <= a.get("start_time", now) < end and a.get("sport") == "run")
        series.append(round(km, 1))
    return series


# ---------------------------------------------------------------- 有氧能力


def eval_aerobic(pred: predictor.PredictionResult, age: float, sex: str,
                 activities: list[dict]) -> dict:
    pct = _percentile_from_vdot(pred.current_vdot, age, sex)
    score = pct
    evidence = [f"当前 VDOT {pred.current_vdot}，位于同龄同性别第 {pct} 百分位",
                f"临界速度 {pred.critical_speed} m/s" if pred.critical_speed else "临界速度数据不足",
                f"数据质量：{pred.data_quality}"]
    hm = pred.predictions.get("hm")
    if hm:
        evidence.append(f"半马当前预测 {hm['time_str']}（配速 {hm['pace']}）")
    # 有氧趋势：比较近8周与之前8周的等效VDOT
    trend = vdot_trend(activities, age, sex)
    if trend is not None:
        evidence.append(f"近 16 周 VDOT 趋势：{'上升' if trend > 0.1 else '下降' if trend < -0.1 else '平稳'} "
                        f"（每 8 周 {trend:+.1f}）")
        score = min(99, max(1, score + min(4, trend * 2)))
    return {"score": round(score), "evidence": evidence, "percentile": pct}


def vdot_trend(activities: list[dict], age: float, sex: str) -> float | None:
    """用近 8 周与之前 8 周的最好「阈值档努力」估算 VDOT 变化。"""
    def best_vdot_since(days: int, before: datetime | None = None) -> float | None:
        end = before or datetime.now()
        start = end - timedelta(days=days)
        vals = []
        for a in activities:
            t = a.get("start_time")
            if not (start <= t <= end) or a.get("sport") != "run":
                continue
            d, dur = a.get("distance_m") or 0, a.get("duration_sec") or 0
            # 只取 3km 以上、且强度较高（配速快于 5:30/km 或 HR 高）的努力
            if d >= 3000 and dur > 0 and (d / dur) > 3.0:
                vals.append(vdot.vdot_from_performance(d, dur))
        return max(vals) if vals else None

    recent = best_vdot_since(56)
    prior = best_vdot_since(56, before=_weeks_ago(8))
    if recent and prior:
        return round(recent - prior, 1)
    return None


# ---------------------------------------------------------------- 意志品质


def eval_willpower(activities: list[dict], plan_adherence: dict | None) -> dict:
    """意志品质 = 计划执行率 + 连续性 + 质量课完成 + 晨晚训自律 + 长距离坚持 + 中断后恢复速度。"""
    runs = [a for a in activities if a.get("sport") == "run"]
    now = datetime.now()

    # 1) 计划执行率（8 周）
    adherence = None
    if plan_adherence and plan_adherence.get("total", 0) >= 5:
        adherence = plan_adherence["completed"] / plan_adherence["total"]

    # 2) 连续训练周数：连续 ≥3 次训练/周
    streak = 0
    for w in range(0, 26):
        start = now - timedelta(weeks=w + 1)
        end = now - timedelta(weeks=w)
        cnt = sum(1 for a in runs if start <= a.get("start_time", now) < end)
        if cnt >= 3:
            streak += 1
        elif w > 0:
            break

    # 3) 晨/晚训自律：07:00 前 或 21:00 后
    offhour = sum(1 for a in runs[-80:] if a["start_time"].hour < 7 or a["start_time"].hour >= 21)

    # 4) 长距离坚持：近 12 周每周最长距离 ≥ 当周中位距离 1.6 倍
    # 阈值随本人当周距离分布浮动，此前「≥ max(5.0, ...)」的绝对 5km 硬底会让
    # 周跑量本就 <5km 的初跑者恒判 0/12，等于用别人的尺度量他
    long_runs = 0
    for w in range(0, 12):
        start = now - timedelta(weeks=w + 1)
        end = now - timedelta(weeks=w)
        wk = [a for a in runs if start <= a.get("start_time", now) < end]
        if len(wk) < 2:
            continue
        dists = sorted((a.get("distance_m") or 0) / 1000 for a in wk)
        mid = dists[len(dists) // 2]
        if mid > 0 and dists[-1] >= mid * 1.6:
            long_runs += 1

    # 5) 中断后恢复速度：≥7 天空窗后 3 天内恢复训练的比率
    dates = sorted({a["start_time"].date() for a in runs})
    comebacks, gaps_cnt = 0, 0
    for i in range(1, len(dates)):
        gap = (dates[i] - dates[i - 1]).days
        if gap >= 7:
            gaps_cnt += 1
            if gap <= 10:   # 空窗 7-10 天即恢复 = 意志恢复快
                comebacks += 1
    comeback_rate = comebacks / gaps_cnt if gaps_cnt else None
    history_weeks = ((now.date() - dates[0]).days / 7) if dates else 0

    parts, gaps = {}, []
    if adherence is not None:
        parts["计划执行率"] = (adherence, 0.35, f"近 8 周计划课完成 {plan_adherence['completed']}/{plan_adherence['total']}")
    else:
        parts["周训练规律"] = (min(1.0, streak / 12), 0.35, f"连续达标周（≥3次/周）{streak} 周")
        gaps.append("无可判定的计划执行记录（近 8 周计划课不足 5 节），改用「周训练规律」计分")
    parts["训练连续性"] = (min(1.0, streak / 16), 0.15, f"最长连续达标 {streak} 周")
    parts["晨晚训自律"] = (min(1.0, offhour / 12), 0.15, f"清晨/夜间训练 {offhour} 次（近 80 次）")
    parts["长距离坚持"] = (long_runs / 12, 0.20, f"近 12 周完成长距离 {long_runs}/12 周")
    if comeback_rate is not None:
        parts["中断恢复力"] = (comeback_rate, 0.15, f"空窗后快速恢复 {comebacks}/{gaps_cnt} 次")
    elif history_weeks >= 8:
        # 有足够长的观察窗口且一次 ≥7 天中断都没有，这本身就是连续性的证据
        parts["中断恢复力"] = (1.0, 0.15, f"近 {history_weeks:.0f} 周无 ≥7 天中断")
    else:
        gaps.append("训练史不足 8 周，无法评估中断恢复力（该项已从总分中剔除）")

    # 权重重归一：不把「没数据」当成「表现中等」
    wsum = sum(w for _, w, _ in parts.values())
    score = round(sum(v * w for v, w, _ in parts.values()) / wsum * 100) if wsum else None
    evidence = [f"{k}：{d}" for k, (_, _, d) in parts.items()]
    return {"score": score, "evidence": evidence, "gaps": gaps,
            "streak_weeks": streak, "adherence": adherence}


# ---------------------------------------------------------------- 恢复能力


def eval_recovery(metrics: list[dict], activities: list[dict]) -> dict:
    """HRV/静息心率/睡眠 + 训练负荷比 ACWR。

    缺哪一项就**不给那一项计分**，并把总分按「实际可用项的权重」重新归一，
    同时在 ``gaps`` 里说明缺了什么。此前缺数据时填 0.6/0.7 这类「中等」值，
    等于替用户编造恢复状态——所有缺数据的用户都会拿到同一个虚高分数。
    """
    recent = [m for m in metrics if m["date"] >= (date.today() - timedelta(days=28))]
    older = [m for m in metrics if (date.today() - timedelta(days=56)) <= m["date"] < (date.today() - timedelta(days=28))]
    evidence, score_parts, gaps = [], [], []

    def avg(ms, key):
        vals = [m[key] for m in ms if m.get(key) is not None]
        return sum(vals) / len(vals) if vals else None

    hrv_now, hrv_prev = avg(recent, "hrv_rmssd"), avg(older, "hrv_rmssd")
    if hrv_now and hrv_prev:
        delta = (hrv_now - hrv_prev) / hrv_prev
        score_parts.append((min(1.0, max(0.0, 0.5 + delta * 4)), 0.35))
        evidence.append(f"HRV 近 28 天均值 {hrv_now:.0f}ms，较前 4 周 {delta:+.0%}")
    elif hrv_now:
        gaps.append(f"HRV 有近期均值 {hrv_now:.0f}ms 但缺前 4 周对照，本项未计分")
    else:
        gaps.append("没有 HRV 记录，恢复评估缺一项")

    rhr_now, rhr_prev = avg(recent, "resting_hr"), avg(older, "resting_hr")
    if rhr_now and rhr_prev:
        delta = rhr_now - rhr_prev
        score_parts.append((min(1.0, max(0.0, 0.5 - delta / 8)), 0.2))
        evidence.append(f"静息心率 {rhr_now:.0f}bpm，较前 4 周 {delta:+.0f}")
    else:
        gaps.append("静息心率缺前后对照，本项未计分")

    sleep = avg(recent, "sleep_hours")
    if sleep:
        s = 1.0 if 7 <= sleep <= 9 else max(0.3, 1 - abs(sleep - 8) / 3)
        score_parts.append((s, 0.25))
        evidence.append(f"平均睡眠 {sleep:.1f}h" + ("（充足）" if s >= 0.9 else "（不足，恢复受限）"))
    else:
        gaps.append("没有睡眠记录，本项未计分")

    # ACWR：近 7 天日均负荷 / 前 28 天日均负荷（两端必须同为「日均」，否则量纲不一致会虚高约 7 倍）
    def load_since(days):
        start = datetime.now() - timedelta(days=days)
        return sum(a.get("training_load") or 0 for a in activities if a.get("start_time", datetime.now()) >= start)

    acute_daily = load_since(7) / 7
    chronic_daily = load_since(28) / 28
    acwr = None
    if chronic_daily > 0:
        acwr = acute_daily / max(1e-6, chronic_daily)
        s = 1.0 if 0.8 <= acwr <= 1.3 else max(0.2, 1 - abs(acwr - 1.05) / 1.2)
        score_parts.append((s, 0.2))
        evidence.append(f"急慢性负荷比 ACWR {acwr:.2f}" + ("（合理）" if 0.8 <= acwr <= 1.3 else
                        ("（负荷突增，伤病风险上升）" if acwr > 1.3 else "（负荷骤降）")))
    else:
        gaps.append("近 28 天无可计算的训练负荷，ACWR 未计入")

    # 权重重归一：只在可用项之间分配总分，缺项不拉低也不虚高
    if score_parts:
        wsum = sum(w for _, w in score_parts)
        score = round(sum(v * w for v, w in score_parts) / wsum * 100)
    else:
        score = None
    # acwr 一并返回：review_method_week 等消费方靠它做负荷风险警示
    return {"score": score, "evidence": evidence, "gaps": gaps,
            "coverage": round(sum(w for _, w in score_parts), 2),
            "acwr": round(acwr, 2) if acwr is not None else None}


# ---------------------------------------------------------------- 天赋水准


def eval_talent(pred: predictor.PredictionResult, activities: list[dict],
                age: float, sex: str, training_age: float,
                weight_kg: float, height_cm: float) -> dict:
    """天赋 = 基础起点(40%) + 训练响应率(30%) + 经济性趋势(20%) + 身体条件(10%)。

    注意：天赋分刻画的是「可开发空间」，不是当前水平的评价。
    """
    evidence, gaps = [], []
    comps: dict[str, tuple[float, float]] = {}     # 项 -> (得分0-1, 权重)
    vdot_value = pred.current_vdot
    training_age = float(training_age or 0.0)

    # 1) 基础起点：当前百分位 vs 系统训练年限对应的期望百分位
    current_pct = _percentile_from_vdot(vdot_value, age, sex)
    expected_pct = 70 * (1 - math.exp(-training_age / 4.0)) + 8   # 训练0年≈8%，10年≈70%
    base_raw = current_pct - expected_pct                        # -70 ~ +90
    base_score = max(0.0, min(1.0, 0.5 + base_raw / 60))
    comps["基础起点"] = (base_score, 0.4)
    evidence.append(f"系统训练 {training_age:.1f} 年达到第 {current_pct} 百分位"
                    f"（同训练年限期望约第 {expected_pct:.0f}）→ 起点天赋"
                    f"{'高于预期' if base_raw > 8 else '符合预期' if base_raw > -8 else '靠训练弥补型'}")

    # 2) 训练响应率：近 16 周 VDOT 增速（对训练量归一）
    trend = vdot_trend(activities, age, sex)
    weeks_km = weekly_km_series(activities, 16)
    avg_km = sum(weeks_km) / max(1, len([k for k in weeks_km if k > 0]))
    if trend is not None and avg_km > 0:
        normalized = trend / max(1.0, avg_km / 40)   # 跑量大者同等增速更难得
        comps["训练响应率"] = (max(0.0, min(1.0, 0.5 + normalized / 3.0)), 0.3)
        evidence.append(f"近 16 周 VDOT 变化 {trend:+.1f}（周均跑量 {avg_km:.0f}km 下）→ "
                        f"{'对训练响应良好' if normalized > 0.5 else '响应中等' if normalized > -0.2 else '响应偏慢，需检视恢复与强度'}")
    else:
        gaps.append("训练响应数据不足（需 ≥4 个月、含阈值以上强度的历史），「训练响应率」未计分")

    # 3) 经济性趋势：同档轻松跑的心率漂移对比（前后 1/3 时段）
    eco_score = _economy_trend(activities)
    if eco_score is not None:
        comps["跑步经济性"] = (eco_score, 0.2)
        evidence.append("同配速下心率呈下降趋势 → 跑步经济性在改善" if eco_score > 0.6
                        else "同配速心率未见改善，注意有氧占比是否足够")
    else:
        gaps.append("经济性数据不足（需 ≥8 次带心率的轻松跑），「跑步经济性」未计分")

    # 4) 身体条件：BMI 18.5-22 为长跑最优区间。缺身高/体重时整项剔除，
    #    不填 0.4 这样的「非最优」默认值——那是拿没填档案当身体条件差。
    if height_cm and weight_kg and height_cm > 0:
        bmi = weight_kg / (height_cm / 100) ** 2
        if 18.5 <= bmi <= 22:
            bmi_score = 1.0
        elif 22 < bmi <= 25 or 17 <= bmi < 18.5:
            bmi_score = 0.7
        else:
            bmi_score = 0.4
        comps["身体条件"] = (bmi_score, 0.1)
        evidence.append(f"BMI {bmi:.1f}（长跑理想 18.5-22）")
        bmi = round(bmi, 1)
    else:
        bmi = None
        gaps.append("缺身高或体重，「身体条件(BMI)」未计分")

    # 权重重归一：只在可算的项之间分配，缺项不当作「中等」也不拉低总分
    wsum = sum(w for _, w in comps.values())
    score = round(sum(v * w for v, w in comps.values()) / wsum * 100) if wsum else 0

    # 快慢肌倾向（速度储备）：5k 配速 vs 全马配速的比值（用配速而非总时间）
    speed_reserve = None
    p5, pm = pred.predictions.get("5k"), pred.predictions.get("marathon")
    if p5 and pm and p5["time_sec"] > 0:
        pace_5k = p5["time_sec"] / 5.0            # sec/km
        pace_m = pm["time_sec"] / 42.195
        speed_reserve = round(pace_m / pace_5k - 1, 3)
        if speed_reserve > 0.22:
            tendency = "快肌偏优势（速度型）：短距离潜力大，长距离需补有氧底子"
        elif speed_reserve < 0.13:
            tendency = "慢肌偏优势（耐力型）：长距离有优势，速度上限需刻意开发"
        else:
            tendency = "均衡型：两项均可发展，视目标侧重"
        evidence.append(f"速度储备指数 {speed_reserve} → {tendency}")

    label = ("天赋出众" if score >= 85 else "天赋优秀" if score >= 70 else
             "天赋良好" if score >= 55 else "中等" if score >= 40 else "尚待开发")
    resp = comps.get("训练响应率")
    return {"score": score, "label": label, "evidence": evidence, "gaps": gaps,
            "components": {k: round(v * 100) for k, (v, _) in comps.items()},
            "coverage": round(wsum, 2),
            "speed_reserve": speed_reserve, "bmi": bmi,
            "base_score": round(base_score * 100),
            "response_score": round(resp[0] * 100) if resp else None}


def _economy_trend(activities: list[dict]) -> float | None:
    """轻松跑（HR<160）按时间前后分半，比较配速-心率效率。"""
    runs = [a for a in activities if a.get("sport") == "run" and a.get("avg_hr")
            and (a.get("distance_m") or 0) >= 4000 and a["avg_hr"] < 165
            and a.get("start_time") >= _weeks_ago(16)]
    if len(runs) < 8:
        return None
    runs.sort(key=lambda a: a["start_time"])
    half = len(runs) // 2

    def eff(rs):
        pts = [((a["distance_m"]) / (a["duration_sec"] / 3600) / 1000, a["avg_hr"]) for a in rs
               if a.get("duration_sec")]
        if not pts:
            return None
        # 固定速度 10km/h 下的等效心率（线性外推）
        k = sum((hr - 130) / max(0.5, v - 8) if v > 8.5 else 0 for v, hr in pts) / len(pts)
        return 150 - k * (10 - 8.5) * 3  # 粗略效率指数（越低越好）

    e1, e2 = eff(runs[:half]), eff(runs[half:])
    if e1 is None or e2 is None:
        return None
    improvement = (e1 - e2) / max(1.0, e1)      # 正值=心率下降=改善
    return max(0.0, min(1.0, 0.5 + improvement * 6))


# ---------------------------------------------------------------- 汇总

DIM_WEIGHTS = {"aerobic": 0.25, "strength": 0.15, "willpower": 0.20, "recovery": 0.15, "talent": 0.25}
DIM_LABELS = {"aerobic": "有氧能力", "strength": "力量水准", "willpower": "意志品质",
              "recovery": "恢复能力", "talent": "天赋水准"}


def grade_of(total: float) -> str:
    return "S" if total >= 85 else "A" if total >= 72 else "B" if total >= 60 else "C" if total >= 45 else "D"


def percentile_of_total(total: float) -> float:
    return round(min(99.5, max(1, 100 / (1 + math.exp(-(total - 58) / 13)))), 1)
