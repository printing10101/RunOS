"""成绩预测引擎：

1. Riegel 外推     T2 = T1 * (D2/D1)^exp        （exp≈1.06，耐力越弱略调高）
2. Daniels VDOT 等效换算（vdot.py）
3. 临界速度模型 CS  D = CS*t + D'                （≥2 个距离的近期最好成绩拟合）
4. 加权融合 → 当前各距离预测成绩
5. 生涯上限 = 当前水平 × 提升空间系数（由天赋分/训练年限/跑量余量/年龄决定）
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

from . import vdot

TARGET_DISTANCES = ["800m", "1500m", "3k", "5k", "10k", "hm", "marathon"]


@dataclass
class BestEffort:
    distance_m: float
    time_sec: float
    date: str = ""          # ISO 日期
    label: str = ""


@dataclass
class PredictionResult:
    predictions: dict[str, dict] = field(default_factory=dict)   # 距离 -> {time_sec, time_str, pace, sources, interval_sec?}
    current_vdot: float = 0.0
    critical_speed: float | None = None     # m/s
    d_prime: float | None = None            # m
    cs_r2: float | None = None              # 临界速度拟合优度（None = 未拟合或不可判）
    data_quality: str = ""
    quality: dict = field(default_factory=dict)   # 结构化质量：efforts / riegel_pairs / cs_r2
    calibration: dict | None = None         # Riegel 指数个性化校准信息


def riegel_predict(src: BestEffort, target_m: float, exp: float | None = None) -> float:
    exp = exp or _baseline_riegel_exp(src)
    return src.time_sec * (target_m / src.distance_m) ** exp


def _baseline_riegel_exp(src: BestEffort) -> float:
    """默认指数 1.06；速度越慢的选手耐力衰减更快，指数略增。"""
    pace_sec_per_km = src.time_sec / (src.distance_m / 1000)
    return 1.08 if pace_sec_per_km > 360 else 1.06


def calibrate_riegel(results: list[BestEffort]) -> dict | None:
    """用实测比赛成绩反推个人 Riegel 指数。

    对每对距离不同、且相隔 ≤90 天的比赛（避免水平漂移污染）：
        exp = ln(T2/T1) / ln(D2/D1)
    取中位数作为个人指数。仅当有效配对 ≥1 时返回，否则 None（继续用基线指数）。
    """
    exps = []
    ordered = sorted(results, key=lambda e: e.date)
    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            a, b = ordered[i], ordered[j]
            if abs(a.distance_m - b.distance_m) < 500:
                continue
            try:
                d1, d2 = date.fromisoformat(a.date), date.fromisoformat(b.date)
            except ValueError:
                continue
            if abs((d2 - d1).days) > 90:
                continue
            exp = math.log(b.time_sec / a.time_sec) / math.log(b.distance_m / a.distance_m)
            if 1.00 <= exp <= 1.16:      # 越界视为脏数据（如中途退赛）
                exps.append(exp)
    if not exps:
        return None
    exps.sort()
    n = len(exps)
    median = exps[n // 2] if n % 2 else (exps[n // 2 - 1] + exps[n // 2]) / 2
    return {"exponent": round(median, 3), "pairs": n, "baseline": 1.06,
            "note": ("个人耐力衰减" + ("快于" if median > 1.06 else "慢于") + "标准模型，预测已按实测校准")
            if abs(median - 1.06) > 0.01 else "与标准模型一致"}


def fit_critical_speed(efforts: list[BestEffort]) -> tuple[float, float, float | None] | None:
    """最小二乘拟合 D = CS*t + D'，返回 (CS m/s, D' m, R²)。至少 2 个不同距离。

    R² 衡量成绩点是否真的落在同一条 CS-D' 直线上（模型假设的成立程度），
    供融合层降权与展示层标注；全部点共线等退化情形给 None。
    """
    pts = [(e.time_sec, e.distance_m) for e in efforts if e.distance_m >= 800]
    dists = {round(d) for _, d in pts}
    if len(pts) < 2 or len(dists) < 2:
        return None
    n = len(pts)
    sx = sum(t for t, _ in pts)
    sy = sum(d for _, d in pts)
    sxx = sum(t * t for t, _ in pts)
    sxy = sum(t * d for t, d in pts)
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-9:
        return None
    cs = (n * sxy - sx * sy) / denom          # 斜率 = 临界速度 m/s
    d_prime = (sy - cs * sx) / n              # 截距 = D'
    if cs <= 0:
        return None
    mean_y = sy / n
    ss_tot = sum((d - mean_y) ** 2 for _, d in pts)
    if ss_tot <= 1e-9:
        return cs, max(0.0, d_prime), None
    ss_res = sum((d - (cs * t + d_prime)) ** 2 for t, d in pts)
    return cs, max(0.0, d_prime), max(0.0, 1 - ss_res / ss_tot)


def cs_predict(cs: float, d_prime: float, target_m: float) -> float | None:
    """临界速度模型预测时间；目标距离过短（<D'）时不适用。"""
    if target_m <= d_prime + 50:
        return None
    return (target_m - d_prime) / cs


def _estimate_interval(estimates: dict[str, float], weights: dict[str, float],
                       blended: float) -> tuple[float, float] | None:
    """多来源估计的加权分位区间 [P25, P75]（时间秒）。

    来源本质是同一能力的不同模型视角：Riegel 偏乐观于长距、VDOT 中性、
    CS 模型对超长距保守——它们的散布就是模型不确定性的直接度量。
    来源 <2 或散布极小（<1%）时区间无意义，返回 None；区间以混合点为锚，
    至少覆盖到 blended（单调性约束由调用方测试锁定）。
    """
    items = [(estimates[k], weights[k]) for k in estimates if k in weights]
    if len(items) < 2:
        return None
    items.sort(key=lambda x: x[0])
    total = sum(w for _, w in items)
    if total <= 0:
        return None
    lo, hi = _weighted_quantile_range(items, total)
    # 散布不足 1% 时视为来源高度一致，不展示区间
    if (hi - lo) / blended < 0.01:
        return None
    # blended 可能被实测下限钳到分位区间外（如来源一致偏乐观而下限更慢）——
    # 区间的语义是「单点的可信范围」，必须包含单点本身
    return min(lo, blended), max(hi, blended)


def _weighted_quantile_range(items: list[tuple[float, float]],
                             total: float) -> tuple[float, float]:
    """加权分位的 [P25, P75]：按估计值升序累加权重，首个越过 25%/75% 的值即分位点。"""
    acc = 0.0
    lo, hi = items[0][0], items[-1][0]
    for val, w in items:
        prev = acc
        acc += w
        if prev < total * 0.25 <= acc:
            lo = val
        if prev < total * 0.75 <= acc:
            hi = val
            break
    return lo, hi


def predict_performances(efforts: list[BestEffort], riegel_exp: float | None = None) -> PredictionResult:
    """输入近期各距离最好成绩（≥1 条），输出全部标准距离预测。

    riegel_exp: 个人校准指数（由实测比赛成绩反推，见 calibrate_riegel）；
    缺省时按配速基线取 1.06/1.08。
    """
    result = PredictionResult()
    if not efforts:
        result.data_quality = "无可用比赛/测试成绩"
        return result

    efforts = sorted(efforts, key=lambda e: e.date, reverse=True)
    if riegel_exp:
        result.calibration = {"exponent": riegel_exp, "source": "race_results"}

    # 当前 VDOT：取各成绩换算 VDOT 的最大值（最真实反映当前能力）
    vdots = []
    for e in efforts:
        try:
            vdots.append(vdot.vdot_from_performance(e.distance_m, e.time_sec))
        except ValueError:
            continue
    result.current_vdot = round(max(vdots), 1) if vdots else 0.0

    cs_fit = fit_critical_speed(efforts)
    if cs_fit:
        result.critical_speed = round(cs_fit[0], 3)
        result.d_prime = round(cs_fit[1])
        result.cs_r2 = round(cs_fit[2], 4) if cs_fit[2] is not None else None

    # 实测成绩 → 各目标距离的 VDOT 等效时间（不含任何外推来源），作为预测下限
    measured_floor: dict[int, float] = {}
    for target in TARGET_DISTANCES:
        d_m = vdot.RACE_DISTANCES[target]
        try:
            measured_floor[target] = min(
                vdot.time_from_vdot(d_m, vdot.vdot_from_performance(e.distance_m, e.time_sec))
                for e in efforts)
        except ValueError:
            continue

    for target in TARGET_DISTANCES:
        d_m = vdot.RACE_DISTANCES[target]
        estimates: dict[str, float] = {}   # 来源 -> 预测秒
        weights: dict[str, float] = {}

        for i, e in enumerate(efforts[:4]):  # 最近 4 个最好成绩
            t = riegel_predict(e, d_m, riegel_exp)
            # 时间越近权重越高；距离越接近目标权重越高
            recency = 1.0 / (1 + i * 0.35)
            proximity = 1.0 / (1 + abs(math.log(d_m / e.distance_m)))
            estimates[f"riegel:{e.label or int(e.distance_m)}m"] = t
            weights[f"riegel:{e.label or int(e.distance_m)}m"] = 0.2 * recency * (0.5 + proximity)

        for i, e in enumerate(efforts[:3]):
            try:
                t = vdot.time_from_vdot(d_m, vdot.vdot_from_performance(e.distance_m, e.time_sec))
            except ValueError:
                continue
            key = f"vdot:{e.label or int(e.distance_m)}m"
            estimates[key] = t
            # 用循环下标而非 efforts.index(e)：后者按 dataclass 全字段相等匹配，
            # 遇到重复成绩会错取第一条的索引，导致权重分配失真
            weights[key] = 0.3 / (1 + i * 0.35)

        if cs_fit:
            t = cs_predict(cs_fit[0], cs_fit[1], d_m)
            if t:
                estimates["critical_speed"] = t
                # 两点拟合权重压低；R²<0.9 说明成绩点不在一条 CS 直线上
                #（如 short sprint 混入、状态突变），再降一档
                if len(efforts) >= 3:
                    weights["critical_speed"] = 0.55 if (result.cs_r2 or 1.0) >= 0.9 else 0.3
                else:
                    weights["critical_speed"] = 0.2

        if not estimates:
            continue
        wsum = sum(weights[k] for k in estimates)
        blended = sum(estimates[k] * weights[k] for k in estimates) / wsum
        # 防外推失真：混合预测不得快于本人任何实测成绩的 VDOT 等效时间。
        if measured_floor:
            blended = max(blended, min(measured_floor.values()))

        interval = _estimate_interval(estimates, weights, blended)
        v = d_m / (blended / 60.0)   # m/min
        entry = {
            "time_sec": round(blended),
            "time_str": vdot.time_str(blended),
            "pace": vdot.pace_str(v),
            "vdot": round(vdot.vdot_from_performance(d_m, blended), 1),
            "sources": sorted(weights.keys()),
        }
        if interval:
            entry["interval_sec"] = [round(interval[0]), round(interval[1])]
        result.predictions[target] = entry

    # 结构化数据质量：展示层与 AI 层共用同一份事实（data_quality 字符串保留为人读的摘要）。
    # riegel_pairs 本函数不可知（只收到校准后的指数），由 build_prediction 查库后补充
    result.quality = {
        "efforts": len(efforts),
        "riegel_pairs": None,
        "cs_r2": result.cs_r2,
        "cs_reliable": bool(cs_fit and len(efforts) >= 3 and (result.cs_r2 or 0) >= 0.9),
    }
    if len(efforts) >= 3 and cs_fit and result.quality["cs_reliable"]:
        result.data_quality = "高（多距离成绩 + 临界速度拟合）"
    elif len(efforts) >= 3 and cs_fit:
        result.data_quality = "中（多距离成绩 + 临界速度拟合一致性欠佳）"
    elif len(efforts) >= 2:
        result.data_quality = "中（多距离成绩）"
    else:
        result.data_quality = "低（单一距离，建议补一场另一距离测试）"
    return result


# ---------------------------------------------------------------- 生涯上限

def _age_headroom(age: float) -> float:
    """年龄窗口：长跑峰值约 24-34 岁。

    这是「年龄」这一本人属性的函数（不是与用户无关的常数）：
    越年轻，可提升的空间越大。
    """
    if age <= 22:
        return 0.06
    if age <= 34:
        return 0.03
    if age <= 40:
        return 0.02
    if age <= 48:
        return 0.01
    return 0.005


# 各项目「高水平训练」的周跑量参照（km/周）。用于衡量本人当前跑量还有多少余量。
IDEAL_WEEKLY_KM = {"800m": 45, "1500m": 50, "3k": 55, "5k": 60,
                   "10k": 70, "hm": 80, "marathon": 90}


def _volume_headroom(weekly_km: float, race_type: str = "marathon") -> float:
    """当前周跑量相对该项目高水平训练所需跑量的差距。

    按本人参赛项目取参照值——此前无论什么项目都按全马的 90km 口径，
    会让 5k 目标的跑者被系统性判定为「跑量余量很大」。
    """
    ideal = IDEAL_WEEKLY_KM.get(race_type, 70)
    ratio = min(1.0, weekly_km / ideal)
    return (1 - ratio) * 0.12


def estimate_career_ceiling(
    pred: PredictionResult,
    talent_score: float,          # 0-100
    age: float,
    weekly_km: float,
    training_age_years: float,
    adherence: float | None,      # 0-1 训练一致性；None = 无计划执行数据
    race_type: str = "marathon",
) -> dict:
    """估算生涯能达到的最好成绩与所需条件。返回各距离生涯最佳预测 + 提升空间解析。

    ``adherence`` 为 None 时（没有可判定的计划执行记录）把「执行一致性」整块剔除并
    标注数据缺口，而不是填一个凭空的平均值——那等于替用户编造训练行为。
    """
    headroom_components = {
        "天赋提升空间": round(talent_score / 100 * 0.20, 3),
        "年龄窗口": _age_headroom(age),
        "跑量余量": _volume_headroom(weekly_km, race_type),
        "系统训练年限": round(max(0.0, (8 - training_age_years) / 8) * 0.06, 3),
    }
    gaps: list[str] = []
    if adherence is not None:
        headroom_components["执行一致性"] = round((1 - adherence) * 0.05, 3)
    else:
        gaps.append("缺计划执行记录，「执行一致性」项未计入（当前估算偏乐观）")

    total_headroom = min(0.35, sum(headroom_components.values()))

    ceiling: dict[str, dict] = {}
    for dist, p in pred.predictions.items():
        ceiling_time = p["time_sec"] * (1 - total_headroom)
        ceiling[dist] = {
            "current": p["time_str"],
            "career_best": vdot.time_str(ceiling_time),
            "career_best_sec": round(ceiling_time),
            "improvement_pct": round(total_headroom * 100, 1),
        }

    # 达到上限预计需要的年限：跑量余量越大、越年轻则越远
    years = round(2 + total_headroom * 16 - min(1.0, training_age_years / 8), 1)
    years = max(1.0, min(10.0, years))

    return {
        "total_headroom_pct": round(total_headroom * 100, 1),
        "headroom_components": {k: f"{v*100:.1f}%" for k, v in headroom_components.items()},
        "years_to_ceiling": years,
        "ceiling": ceiling,
        "gaps": gaps,
        "disclaimer": "生涯上限为基于天赋分、训练变量与年龄窗口的统计性估计，会随训练数据积累滚动修正",
    }
