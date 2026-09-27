"""专业洞察引擎：从已有训练数据推断进阶指标，每条都带完整证据链。

与训练状态页的分工：状态页回答「我现在状态怎么样」（负荷/恢复/准备度），
这里回答「我的跑步能力结构长什么样、短板在哪个生理系统」——每条洞察输出
「数值 → 判定 → 为什么 → 从哪些训练推出来的 → 怎么练」五段式，让用户看到的
不是黑盒分数，而是可以自己复核的推理过程。

指标口径（全部只依赖库内已有字段，不需要新采集）：
  - 有氧解耦 decoupling：前/后半程速度÷心率（EF）的漂移幅度，反映有氧耐力底子。
    只认设备逐段数据（Garmin/Strava laps、Strava streams），均心率算不出漂移。
  - 五区间强度分布：设备 hr_zones 精确值优先，均心率估算兜底
    （口径统一走 zones.estimate_zone_seconds，勿另写）。
  - 乳酸阈值：近 4 个月最优 30-70min 努力的实测配速 × Daniels T 区间交叉锚定。
  - 疲劳抗性：长距离后半程配速衰减 + 解耦合成 0-100 分。
  - 步频经济性：近 28 天 vs 前 28 天对照，步幅经济性提示。

数据可信度纪律：没有设备数据就明说「无法计算」，绝不拿均值合成数据编一套
看起来正常的漂移/衰减——那等于用假数据给用户下训练结论。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from . import vdot, zones

# 护栏常量与分半解析统一定在解析层（activity_detail），洞察层只引用
from .activity_detail import MAX_ELEV_PER_KM, activity_halves, decoupling_pct
from .fitness_metrics import _threshold_candidates, compare_vo2_sources, estimate_lthr


def build_pro_insights(acts: list[dict], athlete: dict,
                       current_vdot: float | None = None,
                       now: datetime | None = None,
                       fitness_snapshot: dict | None = None) -> dict:
    """入口：acts 需带 raw/dynamics（activities_dicts(include_raw=True)）。

    fitness_snapshot 是高驰官方体能快照（最新一行 FitnessSnapshot），
    没有（未连接高驰/还没同步）就不出 vo2_cross 卡，而不是出一张空卡。
    """
    now = now or datetime.now()
    runs = [a for a in acts if a.get("sport") == "run"]
    insights = [
        _decoupling_insight(runs, athlete, now),
        _zone_distribution_insight(runs, athlete, now),
        _threshold_insight(runs, athlete, current_vdot, now),
        _lthr_insight(runs, now),
    ]
    vo2_cross = _vo2_cross_insight(fitness_snapshot, current_vdot)
    if vo2_cross is not None:
        insights.append(vo2_cross)
    insights += [
        _fatigue_resistance_insight(runs, athlete, now),
        _cadence_insight(runs, now),
    ]
    gaps = [i["unavailable_reason"] for i in insights if not i.get("available")]
    return {
        "available": any(i.get("available") for i in insights),
        "insights": insights,
        "data_gaps": gaps,
        "generated_at": now.isoformat(),
    }


# ---------------------------------------------------------------- 洞察公共骨架

def _insight(key: str, title: str, what: str) -> dict:
    return {"key": key, "title": title, "what": what, "available": False,
            "unavailable_reason": "", "value": None, "display": "",
            "verdict": None, "verdict_label": "",
            "why": "", "evidence": [], "samples": [], "actions": []}


def _fail(ins: dict, reason: str) -> dict:
    ins["unavailable_reason"] = reason
    return ins


# ---------------------------------------------------------------- 设备逐段数据获取

# ---------------------------------------------------------------- 1. 有氧解耦

def _decoupling_insight(runs: list[dict], athlete: dict, now: datetime) -> dict:
    ins = _insight(
        "decoupling", "有氧耐力（心率解耦）",
        "把带心率的训练切成前后两半，比较「速度÷心率」的漂移幅度。漂移越小说明"
        "长时间维持同样输出的能力越强（有氧耐力底子越好）。只统计设备逐段心率数据，"
        "爬升 >12m/km 的课受坡度干扰已剔除。")
    samples = []
    for a in runs:
        if not (a.get("duration_sec") and a.get("avg_hr") and a.get("distance_m")):
            continue
        if not 2400 <= a["duration_sec"] <= 14400:   # 40min-4h 才有漂移信号
            continue
        if a.get("elevation_m") and a["elevation_m"] / (a["distance_m"] / 1000) > MAX_ELEV_PER_KM:
            continue
        halves = activity_halves(a)
        if not halves:
            continue
        ef1, ef2, p1, p2 = halves
        dec = decoupling_pct(ef1, ef2)
        if abs(dec) > 30:
            continue   # 心率带脱落/GPS 漂移类坏样本
        samples.append({
            "date": a["start_time"].date().isoformat(),
            "title": a.get("title") or "跑步",
            "decoupling_pct": round(dec, 1),
            "pace_decay_pct": round((p2 - p1) / p1 * 100, 1) if p1 > 0 else None,
            "duration_min": round(a["duration_sec"] / 60),
            "km": round(a["distance_m"] / 1000, 1),
        })
    recent = [s for s in samples if s["date"] >= (now - timedelta(days=56)).date().isoformat()]
    if len(recent) < 2:
        return _fail(ins, f"近 8 周只有 {len(recent)} 次带设备逐段心率的跑步（40min 以上），"
                          "样本不足无法评估。已同步佳明/Strava/高驰明细的活动才会进入本指标")
    vals = sorted(s["decoupling_pct"] for s in recent)
    median = vals[len(vals) // 2] if len(vals) % 2 else (vals[len(vals)//2 - 1] + vals[len(vals)//2]) / 2
    ins["available"] = True
    ins["value"] = round(median, 1)
    ins["display"] = f"{median:.1f}%"
    ins["samples"] = sorted(recent, key=lambda s: s["date"], reverse=True)
    if median < 5:
        ins["verdict"] = "good"
        ins["verdict_label"] = "优秀"
        ins["why"] = (f"近 8 周 {len(recent)} 次带心率训练的中位解耦 {median:.1f}%（<5% 优秀），"
                      "后半程心率几乎不漂移，长距离有氧底子扎实")
        ins["actions"] = ["有氧耐力不是当前短板，维持现有 Z2 跑量与长距离节奏即可",
                          "可以把训练重心转向阈值/最大摄氧量等上限能力"]
    elif median <= 8:
        ins["verdict"] = "watch"
        ins["verdict_label"] = "合格"
        ins["why"] = (f"中位解耦 {median:.1f}%（5-8% 合格区间），长时间输出心率会缓慢上漂，"
                      "有氧耐力可用但还有空间")
        ins["actions"] = ["轻松跑严格执行 Z2 上沿以下，宁慢勿快——漂移改善最快的杠杆",
                          "每 2 周一次 90min+ 全程不断速的长距离，训练脂肪供能效率"]
    else:
        ins["verdict"] = "bad"
        ins["verdict_label"] = "待提升"
        ins["why"] = (f"中位解耦 {median:.1f}%（>8% 偏高），后半程同配速心率显著升高，"
                      "说明有氧耐力限制了长距离表现——这不是意志问题，是慢肌与脂肪供能通路还没练够")
        ins["actions"] = ["把总跑量的 75-80% 压到 Z2 以下（储备心率 60-70%），坚持 6-8 周再复测",
                          "长距离课配速下调 15-30s/km，优先完成时间而不是完成配速",
                          "长距离超 90min 时每 30-40min 补 20-30g 碳水，排除补糖不足的干扰"]
    ins["evidence"] = [
        f"{s['date']} {s['title']}：{s['km']}km/{s['duration_min']}min，"
        f"解耦 {s['decoupling_pct']}%" + (f"，后半程配速衰减 {s['pace_decay_pct']}%" if s["pace_decay_pct"] is not None else "")
        for s in ins["samples"][:4]]
    ins["evidence"].append(f"判定基准：<5% 优秀，5-8% 合格，>8% 待提升（样本 {len(recent)} 次，取中位数）")
    return ins


# ---------------------------------------------------------------- 2. 五区间强度分布

def _zone_distribution_insight(runs: list[dict], athlete: dict, now: datetime) -> dict:
    ins = _insight(
        "zones", "强度分布（心率五区间）",
        "把近 28 天跑步时长按 Karvonen 储备心率落进 Z1-Z5：Z1+Z2 是低强度有氧底盘，"
        "Z3 是『不轻松也不够快』的灰色区间，Z4+Z5 是高强度刺激。设备 hr_zones 用精确值，"
        "其余按均心率估算（有标注）。对照极化 80/20 原则。")
    max_hr, rest_hr = athlete.get("max_hr"), athlete.get("resting_hr")
    if not max_hr or not rest_hr:
        return _fail(ins, "档案缺最大/静息心率，无法划分心率区间（到设置页补齐后即恢复）")

    def window_secs(days: int) -> tuple[dict, int, int] | None:
        cut = (now - timedelta(days=days)).date().isoformat()
        totals = {f"Z{i}": 0.0 for i in range(1, 6)}
        n_dev = n_est = 0
        for a in runs:
            if (a.get("start_time") or now).date().isoformat() <= cut:
                continue
            if a.get("sport") != "run":
                continue
            dev = _device_zone_secs(a)
            if dev:
                src, n_dev = dev, n_dev + 1
            else:
                est = zones.estimate_zone_seconds(a.get("duration_sec"), a.get("avg_hr"),
                                                  max_hr, rest_hr)
                if not est:
                    continue
                src, n_est = est, n_est + 1
            for k, v in src.items():
                totals[k] = totals.get(k, 0) + (v or 0)
        total = sum(totals.values())
        if total <= 0:
            return None
        return totals, n_dev, n_est

    cur = window_secs(28)
    if not cur:
        return _fail(ins, "近 28 天没有带心率数据的跑步，无法统计强度分布")
    totals, n_dev, n_est = cur
    hours = {k: v / 3600 for k, v in totals.items()}
    total_h = sum(hours.values())
    pct = {k: round(hours[k] / total_h * 100) for k in hours}
    easy, gray, hard = pct["Z1"] + pct["Z2"], pct["Z3"], pct["Z4"] + pct["Z5"]

    prev = window_secs(56)
    prev_easy = None
    if prev:
        pt = sum(prev[0].values())
        if pt > 0:
            prev_easy = round((prev[0]["Z1"] + prev[0]["Z2"]) / pt * 100)

    ins["available"] = True
    ins["value"] = easy
    ins["display"] = f"低强度 {easy}% · 灰色 {gray}% · 高强度 {hard}%"
    ins["samples"] = [{"zone": k, "hours": round(hours[k], 1), "pct": pct[k],
                       "label": zl} for k, zl in
                      zip(("Z1", "Z2", "Z3", "Z4", "Z5"),
                          ("恢复/热身", "有氧耐力", "灰色区间", "乳酸阈值", "最大摄氧"),
                          strict=True)]
    ins["evidence"] = [
        f"统计窗口 28 天、{n_dev + n_est} 次跑步、共 {total_h:.1f} 小时"
        + (f"（设备精确区间 {n_dev} 次，均心率估算 {n_est} 次）" if n_est else "，全部为设备精确数据"),
        f"构成：Z1 {hours['Z1']:.1f}h · Z2 {hours['Z2']:.1f}h · Z3 {hours['Z3']:.1f}h · "
        f"Z4 {hours['Z4']:.1f}h · Z5 {hours['Z5']:.1f}h",
        f"前 28 天（{prev_easy}%）对照" if prev_easy is not None else "前 28 天无对照样本",
        "判定基准：低强度 ≥75% 且高强度 3-30% 为健康的极化/金字塔结构",
    ]
    if gray > 25:
        ins["verdict"] = "bad"
        ins["verdict_label"] = "灰色区间偏多"
        ins["why"] = (f"Z3 灰色区间占 {gray}%（>25%），既不够轻松以积累有氧、又不够快以刺激上限，"
                      "同等跑量下进步更慢、疲劳换不来对应收益")
        ins["actions"] = ["轻松跑全部压到 Z2 上沿以下（储备心率 70% 以下），这是最优先的一刀",
                          "质量课集中到每周 2 次并明确目标（T 节奏 or I 间歇），其余全是轻松跑或休息"]
    elif hard < 3:
        ins["verdict"] = "watch"
        ins["verdict_label"] = "强度刺激不足"
        ins["why"] = (f"低强度占比 {easy}% 很健康，但 Z4+Z5 只有 {hard}%——上限能力（阈值/最大摄氧）"
                      "缺乏刺激，水平提升会逐渐停滞")
        ins["actions"] = ["每周加入 1-2 次质量课：T 节奏跑 20-30min 或 1km 间歇 ×6-8",
                          "质量课前后各留 1 天纯轻松日，避免强度连带"]
    elif easy < 70:
        ins["verdict"] = "watch"
        ins["verdict_label"] = "低强度不足"
        ins["why"] = (f"低强度占比 {easy}%（<70%），有氧底盘铺得不够，高强度课的收益建立不起来")
        ins["actions"] = ["增加纯 Z2 轻松跑或慢跑恢复日，把低强度占比拉回 75%+"]
    else:
        ins["verdict"] = "good"
        ins["verdict_label"] = "结构健康"
        ins["why"] = (f"低强度 {easy}% / 灰色 {gray}% / 高强度 {hard}%，符合极化 80/20 思想，"
                      "强度结构是可持续进步的形态" +
                      (f"，比前 28 天（低强度 {prev_easy}%）更集中" if prev_easy is not None and easy >= prev_easy else ""))
        ins["actions"] = ["保持现有强度节奏；每次质量课明确一个目标区间，别让质量课跑成 Z3"]
    return ins


def _device_zone_secs(a: dict) -> dict | None:
    """设备 hr_zones（raw 内，佳明实测）→ Z 秒数 dict；没有则 None。"""
    zr = (a.get("raw") or {}).get("hr_zones")
    if not isinstance(zr, dict) or not zr:
        return None
    out = {}
    for key, v in zr.items():
        z = "".join(ch for ch in str(key) if ch.isdigit())
        if z and v:
            out[f"Z{min(5, int(z))}"] = float(v)
    return out or None


# ---------------------------------------------------------------- 3. 乳酸阈值实测估计

def _threshold_insight(runs: list[dict], athlete: dict,
                       current_vdot: float | None, now: datetime) -> dict:
    ins = _insight(
        "threshold", "乳酸阈值（实测 × VDOT 交叉锚定）",
        "阈值配速是预测长距离成绩最灵的单指标。两条独立证据交叉：① 近 4 个月内最快的"
        "30-70min 连续跑实测均速（含热身，是保守下限）；② 当前 VDOT 按 Daniels 模型推算的 "
        "T 区间（83-88% vVDOT）。两者互相校验：实测明显慢于推算 → 阈值利用率不足；"
        "实测更快 → VDOT 已过时，配速表应上修。")
    cands = _threshold_candidates(runs, now)
    if not cands and not current_vdot:
        return _fail(ins, "近 4 个月没有 30-70min 带心率的连续跑，且无 VDOT 基线，无法估计阈值。"
                          "跑一次 5k 测试或 40min 节奏跑即可点亮本指标")
    daniels_t = None
    if current_vdot:
        v_fast = vdot.velocity_for_vo2(88, current_vdot) / 60   # km/min
        v_slow = vdot.velocity_for_vo2(83, current_vdot) / 60
        daniels_t = (round(1000 / v_fast), round(1000 / v_slow))  # (快端, 慢端) s/km

    ins["available"] = True
    if cands:
        best = cands[0]
        ins["value"] = best["pace_sec"]
        ins["samples"] = cands[:4]
        daniels_line = (f"Daniels T 区间（VDOT {current_vdot:.1f}）："
                        f"{vdot.pace_label(daniels_t[1])} - {vdot.pace_label(daniels_t[0])}"
                        ) if daniels_t else "Daniels T 区间：无 VDOT 基线"
        ins["evidence"] = [
            f"实测最快 {best['duration_min']}min 连续跑：{best['date']} {best['title']}，"
            f"均速 {vdot.pace_label(best['pace_sec'])}、均心率 {best['avg_hr']}",
            daniels_line,
        ]
        if daniels_t and best["pace_sec"] > daniels_t[1] + 15:
            ins["verdict"] = "watch"
            ins["verdict_label"] = "阈值利用率偏低"
            ins["display"] = f"{vdot.pace_label(best['pace_sec'])}（实测下限）"
            ins["why"] = (f"实测阈值配速 {vdot.pace_label(best['pace_sec'])} 明显慢于 Daniels 推算的 T 区间慢端 "
                          f"{vdot.pace_label(daniels_t[1])}——心肺上限（VDOT）不差，但『能用多久』的阈值利用率不足，"
                          "典型的基础期短板")
            ins["actions"] = ["每周 1 次 T 课从「3×10min @T 慢端、间歇 2min 轻松」起步，3 周后过渡到连续 20min",
                              "其余有氧跑维持轻松，避免用 T 配速跑日常课把灰色区间越滚越大"]
        elif daniels_t and best["pace_sec"] < daniels_t[0] - 15:
            ins["verdict"] = "good"
            ins["verdict_label"] = "强于推算"
            ins["display"] = f"{vdot.pace_label(best['pace_sec'])}（实测下限）"
            ins["why"] = (f"实测 {vdot.pace_label(best['pace_sec'])} 快于 Daniels T 快端 {vdot.pace_label(daniels_t[0])}，"
                          "阈值能力领先模型估计——VDOT/成绩预测偏保守，训练配速表可按新的测试成绩重算")
            ins["actions"] = ["跑一次全力 5k/10k 刷新 VDOT 基线，让全部训练配速跟上当前水平",
                              "阈值已是强项，把增量训练时间投向长距离耐力或最大摄氧量"]
        else:
            ins["verdict"] = "good" if daniels_t else "watch"
            ins["verdict_label"] = "两证一致" if daniels_t else "仅实测锚点"
            ins["display"] = f"{vdot.pace_label(best['pace_sec'])}（实测下限）"
            ins["why"] = ("实测与 Daniels 推算基本一致，阈值状态与有氧能力匹配，"
                          f"阈值心率参考 {best['avg_hr']} bpm" if daniels_t else
                          f"实测锚点 {vdot.pace_label(best['pace_sec'])}（含热身，真实阈值略快于此），"
                          "建议补一次全力测试与 VDOT 交叉验证")
            ins["actions"] = ["T 课配速按实测 ±3s/km 执行，心率落在均心率 ±3bpm",
                              "每 6-8 周用一次 40min 节奏跑复测，跟踪阈值漂移"]
    else:
        # 只有 VDOT 一条腿
        ins["value"] = daniels_t[1]
        ins["display"] = f"{vdot.pace_label(daniels_t[1])} - {vdot.pace_label(daniels_t[0])}"
        ins["verdict"] = "watch"
        ins["verdict_label"] = "仅模型推算"
        ins["evidence"] = [f"当前 VDOT {current_vdot}，按 Daniels 模型 T 区间 83-88% 推算"]
        ins["why"] = (f"暂无 30-70min 实测努力可比对，阈值按 VDOT 推算为 "
                      f"{vdot.pace_label(daniels_t[1])} - {vdot.pace_label(daniels_t[0])}——"
                      "推算值假设阈值利用率正常，缺实测校验")
        ins["actions"] = ["跑一次 40min 左右带心率的节奏跑或 5k 测试，即可完成实测交叉锚定"]
    return ins


# ---------------------------------------------------------------- 3.5 乳酸阈心率 / 官方 VO2 交叉

def _lthr_insight(runs: list[dict], now: datetime) -> dict:
    """乳酸阈心率卡：与阈值配速卡同一批实测努力，只是锚的是心率。"""
    est = estimate_lthr(runs, now)
    ins = _insight(
        "lthr", "乳酸阈心率（实测努力锚定）",
        "阈值心率是按心率执行 T 课的基准：心率落在阈值附近 ±3bpm 的训练正好压在"
        "「最大乳酸稳态」上，高了撑不久、低了刺激不足。与阈值配速卡同源：取近 4 个月"
        "最快的 30-70min 带心率努力，均心率加时长修正。这是下限估计——含热身稀释，"
        "真实阈值心率略高于此。")
    if not est.get("available"):
        return _fail(ins, est.get("unavailable_reason") or "数据不足，无法估计乳酸阈心率")
    ins["available"] = True
    ins["value"] = est["bpm"]
    ins["display"] = est["display"]
    ins["verdict"] = "neutral"
    confidence_label = {"high": "多证一致", "medium": "两证一致", "low": "单样本下限"}
    ins["verdict_label"] = confidence_label.get(est.get("confidence"), "估计")
    ins["samples"] = est.get("samples") or []
    ins["evidence"] = est.get("evidence") or []
    if est["confidence"] == "low":
        ins["why"] = (f"只有一个可用样本：{est['basis']}。单样本只能当下限参考，"
                      "再跑一次 40min 节奏跑即可交叉验证")
        ins["actions"] = ["T 课先按「该心率 -3 ~ -5bpm」执行，避免把下限当上限顶太狠",
                          "每 2 周复测一次，两次接近后可放心用作 T 课心率基准"]
    else:
        ins["why"] = f"{est['basis']}，多个接近强度的样本心率一致，可作 T 课心率基准"
        ins["actions"] = ["节奏跑心率控制在 LTHR ±3bpm；间歇间歇期恢复心率降到 LTHR -10 以下再启动",
                          "长距离末段若心率远超 LTHR 而配速未升，说明当次配速过激，应下调",
                          "每 6-8 周用一次 40min 节奏跑复测，跟踪阈值心率漂移"]
    return ins


def _vo2_cross_insight(snapshot: dict | None, current_vdot: float | None) -> dict | None:
    """官方 VO2max × 本地 VDOT 交叉锚定。没有官方快照时不出卡（返回 None）。"""
    if not snapshot or snapshot.get("vo2max") is None:
        return None
    official = snapshot["vo2max"]
    ins = _insight(
        "vo2_cross", "VO2max 口径互证（官方 × 成绩反推）",
        "同一个能力指数有两套独立来源：高驰官方值由日常配速/心率数据估计，"
        "本地 VDOT 由你的实测成绩按 Daniels 公式反推。两者互相校验——偏差本身就是"
        "信号：一致说明两套口径互证；官方更高通常意味着负荷撑得起摄氧上限、"
        "但成绩还没跟上（阈值利用率/耐力转化不足）；本地更高说明官方估计保守"
        "或心率档案（最大心率）设偏了。")
    ins["available"] = current_vdot is not None
    ins["value"] = official
    ins["display"] = f"官方 {official} · 本地 {current_vdot:.1f}" if current_vdot is not None else f"官方 {official}"
    ins["verdict"] = "neutral"
    ins["verdict_label"] = "仅官方值"
    comp = compare_vo2_sources(official, current_vdot)
    if current_vdot is None:
        ins["unavailable_reason"] = "暂无本地 VDOT 基线——跑一场比赛或全力测试后即可互证"
        return ins
    if comp["verdict"] == "aligned":
        ins["verdict"] = "good"
        ins["verdict_label"] = "两证一致"
        ins["why"] = (f"官方 {official} 与成绩反推 VDOT {current_vdot:.1f} 基本一致（差 {abs(comp['delta'])}），"
                      "两套独立口径互证，当前能力画像可信度高")
        ins["actions"] = ["维持当前训练结构，按现有配速表执行即可",
                          "下一次全力测试后两个数值应同步上移，若背离再回来看本卡"]
    elif comp["verdict"] == "official_higher":
        ins["verdict"] = "watch"
        ins["verdict_label"] = "官方更乐观"
        ins["why"] = (f"官方 VO2max {official} 高出成绩反推 VDOT {current_vdot:.1f} 约 {abs(comp['delta'])}——"
                      "日常负荷/配速数据撑得起更高的摄氧上限，但成绩没跟上。典型的心肺上限不差、"
                      "阈值利用率与耐力转化不足，与阈值卡互为印证")
        ins["actions"] = ["把训练重心从堆量转向 T/I 强度课，提高「能用多少上限」的转化率",
                          "长距离课加入末段渐进（最后 3km 提到 M 配速），练比赛末段的动员能力"]
    else:
        ins["verdict"] = "neutral"
        ins["verdict_label"] = "官方偏保守"
        ins["why"] = (f"成绩反推 VDOT {current_vdot:.1f} 高出官方 VO2max {official} 约 {abs(comp['delta'])}——"
                      "官方日估计偏保守，或手表的最大心率设置偏低压低了强度估计。"
                      "成绩口径的配速表可放心使用")
        ins["actions"] = ["核对档案最大心率：若明显高于手表默认值，官方 VO2max 会更准",
                          "训练配速以本地 VDOT 推算的配速表为准，官方值仅作参考"]
    ins["evidence"] = [
        f"高驰官方 VO2max：{official}（{snapshot.get('date', '当期')} 快照，由日常配速/心率估计）",
        f"本地 Daniels VDOT：{current_vdot:.1f}（由实测成绩反推）",
        "判定口径：|差值| ≤ 1.5 视为一致；这是工程口径，非生理测量",
    ]
    return ins


# ---------------------------------------------------------------- 4. 长距离疲劳抗性

def _fatigue_resistance_insight(runs: list[dict], athlete: dict, now: datetime) -> dict:
    ins = _insight(
        "fatigue_resistance", "长距离疲劳抗性",
        "用 75 分钟以上长距离跑的「后半程配速衰减 + 心率解耦」合成 0-100 分。"
        "衰减为正 = 后半程越跑越慢。这是马拉松成绩最直接的预测因子之一，"
        "只认设备逐段数据，爬升 >12m/km 的越野课已剔除。")
    cut = (now - timedelta(days=84)).date().isoformat()
    samples = []
    for a in runs:
        if not (a.get("duration_sec") and a.get("avg_hr") and a.get("distance_m")):
            continue
        if a["duration_sec"] < 4500:   # 75min
            continue
        if (a.get("start_time") or now).date().isoformat() <= cut:
            continue
        if a.get("elevation_m") and a["elevation_m"] / (a["distance_m"] / 1000) > MAX_ELEV_PER_KM:
            continue
        halves = activity_halves(a)
        if not halves:
            continue
        ef1, ef2, p1, p2 = halves
        dec = decoupling_pct(ef1, ef2)
        if abs(dec) > 30:
            continue
        decay = (p2 - p1) / p1 * 100
        samples.append({
            "date": a["start_time"].date().isoformat(),
            "title": a.get("title") or "长距离",
            "km": round(a["distance_m"] / 1000, 1),
            "duration_min": round(a["duration_sec"] / 60),
            "pace_decay_pct": round(decay, 1),
            "decoupling_pct": round(dec, 1),
            "score": _fatigue_score(decay, dec),
        })
    if not samples:
        return _fail(ins, "近 12 周没有 75min 以上带设备逐段心率的长距离跑，无法评估疲劳抗性。"
                          "下次长距离戴稳心率带，跑完即出分")
    samples.sort(key=lambda s: s["date"])
    latest = samples[-1]
    # 取最近 3 次均值，长距离单次波动大（天气/路线/补给），单次不能定结论
    recent = samples[-3:]
    score = round(sum(s["score"] for s in recent) / len(recent))
    ins["available"] = True
    ins["value"] = score
    ins["display"] = f"{score} 分"
    ins["samples"] = sorted(samples, key=lambda s: s["date"], reverse=True)
    ins["evidence"] = [
        f"{s['date']} {s['title']}：{s['km']}km/{s['duration_min']}min，"
        f"后半程配速衰减 {s['pace_decay_pct']}%，解耦 {s['decoupling_pct']}% → {s['score']} 分"
        for s in recent]
    ins["evidence"].append(f"评分口径：100 - max(0, 配速衰减%)×8 - max(0, 解耦%-3)×6，近 {len(recent)} 次取均值")
    if score >= 85:
        ins["verdict"] = "good"
        ins["verdict_label"] = "优秀"
        ins["why"] = f"长距离后程几乎不掉速、心率不漂，疲劳抗性是当前强项（最近一次 {latest['km']}km 衰减 {latest['pace_decay_pct']}%）"
        ins["actions"] = ["保持现有长距离频率；备赛期把长距离末端 20-30min 提到 M 配速做专项转化"]
    elif score >= 65:
        ins["verdict"] = "watch"
        ins["verdict_label"] = "合格"
        ins["why"] = f"长距离有可感知的后程衰减（最近一次配速 +{latest['pace_decay_pct']}%），在正常范围但未达优秀"
        ins["actions"] = ["长距离中后段每 5km 检查一次配速漂移，掉速超过 5% 说明前半程偏快",
                          "长距离前 2h 补足碳水，排除补给不足伪装成耐力不足"]
    else:
        ins["verdict"] = "bad"
        ins["verdict_label"] = "待提升"
        ins["why"] = f"长距离后程掉速/心率漂移明显（最近一次衰减 {latest['pace_decay_pct']}%、解耦 {latest['decoupling_pct']}%），马拉松后程崩盘风险高"
        ins["actions"] = ["先降长距离配速 20-30s/km 把心率稳在 Z2，用 6-8 周重建有氧底子再谈强度",
                          "周中补一次 60-75min 中长轻松跑，增加低强度暴露频次"]
    return ins


def _fatigue_score(pace_decay_pct: float, decoupling_pct: float) -> int:
    # 只罚正衰减：后半程更快（负分割）是好配速策略，不是疲劳信号
    score = 100 - max(0.0, pace_decay_pct) * 8 - max(0.0, decoupling_pct - 3) * 6
    return int(max(20, min(100, round(score))))


# ---------------------------------------------------------------- 5. 步频经济性

def _cadence_insight(runs: list[dict], now: datetime) -> dict:
    ins = _insight(
        "cadence", "步频经济性",
        "近 28 天跑步平均步频 vs 前 28 天，对照 170-180 的经验基准。步频过低通常伴随"
        "过大步幅：每步落地点远离重心，刹车效应+冲击增大。部分设备按单侧计数（数值约 85-100），"
        "引擎按 ×2 折算并在证据中标注。")

    def norm_cad(v: float) -> float | None:
        if v is None:
            return None
        return v * 2 if 80 <= v < 120 else (v if 120 <= v <= 220 else None)

    def window_avg(days_lo: int, days_hi: int) -> tuple[float, int, bool] | None:
        lo = (now - timedelta(days=days_lo)).date().isoformat()
        hi = (now - timedelta(days=days_hi)).date().isoformat()
        vals, folded = [], False
        for a in runs:
            d = (a.get("start_time") or now).date().isoformat()
            if not (lo <= d < hi):
                continue
            c = norm_cad(a.get("avg_cadence"))
            if c is None:
                continue
            vals.append(c)
            folded = folded or (a.get("avg_cadence") < 120)
        if not vals:
            return None
        return sum(vals) / len(vals), len(vals), folded

    recent = window_avg(28, -1)     # hi 设为未来日期，含今天
    if not recent:
        return _fail(ins, "近 28 天没有带步频的跑步记录（或步频口径异常），无法评估")
    prev = window_avg(56, 28)
    avg, n, folded = recent
    ins["available"] = True
    ins["value"] = round(avg)
    ins["display"] = f"{avg:.0f} spm"
    ins["samples"] = [{"date": (a.get("start_time") or now).date().isoformat(),
                       "cadence": round(norm_cad(a.get("avg_cadence")) or 0)}
                      for a in runs if norm_cad(a.get("avg_cadence"))][-12:]
    ins["evidence"] = [
        f"近 28 天 {n} 次跑步均值 {avg:.0f} 步/分" + ("（设备按单侧计数，已 ×2 折算）" if folded else ""),
        f"前 28 天均值 {prev[0]:.0f} 步/分（{prev[1]} 次）" if prev else "前 28 天无对照样本",
        "判定基准：≥180 优秀 · 170-180 良好 · 160-170 一般 · <160 冲击与刹车效应偏大",
    ]
    delta = avg - prev[0] if prev else None
    if avg >= 175:
        ins["verdict"] = "good"
        ins["verdict_label"] = "优秀"
        ins["why"] = f"步频 {avg:.0f} 处于经济区间，落地冲击分散良好" + \
            (f"，比前 28 天提高 {delta:+.0f}" if delta is not None and abs(delta) >= 2 else "")
        ins["actions"] = ["保持当前步频感觉；快步跑（strides）每周 2 次维持神经肌肉锐度"]
    elif avg >= 165:
        ins["verdict"] = "watch"
        ins["verdict_label"] = "良好"
        ins["why"] = f"步频 {avg:.0f} 在可接受区间、但距 175+ 的经济区间还有一步之遥" + \
            (f"（较前 28 天 {delta:+.0f}）" if delta is not None and abs(delta) >= 2 else "")
        ins["actions"] = ["每周 1-2 次 6-8×100m 快步跑，让高步频成为默认模式",
                          "轻松跑时偶发检查：若步频 <165，缩短步幅提高节奏，而不是加力迈腿"]
    else:
        ins["verdict"] = "bad"
        ins["verdict_label"] = "待提升"
        ins["why"] = f"步频 {avg:.0f}（<165）偏低，大概率步幅过大：着地点超出重心、每步都在刹车，伤膝伤胫骨"
        ins["actions"] = ["用节拍器 App 设 175-180 步/分跟跑 2 周，只提频不加力",
                          "步频上来的同时步幅会自然缩短，配速短期掉 5-10s/km 是正常的，忍住"]
    return ins
