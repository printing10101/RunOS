"""预测演变曲线：本地引擎逐日快照 + 官方快照对照，按比赛分组。

race_predictions 表在预测装配入口按天落本地预测快照（每个进行中的目标一场，
幂等键含 predicted_on），fitness_snapshots.race_predictions 是高驰官方当期
预测。两条曲线放在一起，「练得有没有效果」不再只看比赛日一锤子买卖；
赛后实测回填（actual_sec / delta_pct）一并带出，预测校准报告共用本取数面。

官方快照是「当期能力」预测、不挂比赛日，只按项目键对照到同 race_type 的
比赛组上；官方键 half_marathon 与本地键 hm 不同，入口处归一。
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from .vdot import time_str

# 官方快照键 → 本地项目键（其余键同名直通；800m/1k 等官方不提供）
_OFFICIAL_KEY_MAP = {"half_marathon": "hm"}

# 校准偏差（实测 vs 预测，%）的「可信」判定线：±2% 以内视为预测贴合
_CALIB_FINE_PCT = 2.0


def build_prediction_trend(db: Session, athlete_id: int,
                           race_type: str | None = None, days: int = 180) -> dict:
    """本地预测快照按 (race_date, race_type) 分组 + 官方预测对照线。"""
    since = date.today() - timedelta(days=days)

    groups: dict[tuple, dict] = {}
    rows = db.scalars(select(models.RacePrediction).where(
        models.RacePrediction.athlete_id == athlete_id,
        models.RacePrediction.predicted_on >= since)
        .order_by(models.RacePrediction.predicted_on)).all()
    for r in rows:
        if race_type and r.race_type != race_type:
            continue
        key = (r.race_date, r.race_type, r.distance_m)
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "race_date": r.race_date.isoformat(), "race_type": r.race_type,
                "race_name": r.race_name, "distance_m": r.distance_m,
                "local_series": [], "official_series": [], "actual": None,
            }
        g["local_series"].append({"date": r.predicted_on.isoformat(),
                                  "predicted_sec": r.predicted_sec,
                                  "time_str": time_str(r.predicted_sec)})
        if r.actual_sec is not None and g["actual"] is None:
            g["actual"] = {"actual_sec": r.actual_sec, "time_str": time_str(r.actual_sec),
                           "delta_pct": r.delta_pct}

    # 官方当期预测：按项目键聚成对照线（不挂比赛日）
    official_by_type: dict[str, list] = {}
    snaps = db.scalars(select(models.FitnessSnapshot).where(
        models.FitnessSnapshot.athlete_id == athlete_id,
        models.FitnessSnapshot.date >= since)
        .order_by(models.FitnessSnapshot.date)).all()
    for s in snaps:
        for k, sec in (s.race_predictions or {}).items():
            if not isinstance(sec, int) or sec <= 0:
                continue
            local_key = _OFFICIAL_KEY_MAP.get(k, k)
            official_by_type.setdefault(local_key, []).append(
                {"date": s.date.isoformat(), "predicted_sec": sec, "time_str": time_str(sec)})
    for g in groups.values():
        g["official_series"] = official_by_type.get(g["race_type"], [])

    races = sorted(groups.values(), key=lambda g: g["race_date"])
    return {"empty": not races, "races": races}


def calibration_summary(db: Session, athlete_id: int) -> dict:
    """预测校准汇总：历次「赛前预测 → 实测」偏差（race_prediction_review 的聚合版）。

    delta_pct = (实测 − 预测) / 预测 × 100：正 = 实测比预测慢（预测偏乐观）。
    给校准报告卡与赛时计划的可信度标注共用；没有实测点时如实说未校准。
    """
    rows = db.scalars(select(models.RacePrediction).where(
        models.RacePrediction.athlete_id == athlete_id,
        models.RacePrediction.actual_sec.is_not(None))
        .order_by(models.RacePrediction.race_date)).all()
    items = [{"race_date": r.race_date.isoformat(), "race_type": r.race_type,
              "race_name": r.race_name, "predicted_on": r.predicted_on.isoformat(),
              "predicted_str": time_str(r.predicted_sec),
              "actual_str": time_str(r.actual_sec), "delta_pct": r.delta_pct}
             for r in rows]
    deltas = [i["delta_pct"] for i in items if i["delta_pct"] is not None]
    bias = round(sum(deltas) / len(deltas), 1) if deltas else None
    mean_abs = round(sum(abs(d) for d in deltas) / len(deltas), 1) if deltas else None
    note = _calibration_note(len(items), bias)
    return {"empty": not items, "n": len(items), "items": items,
            "bias_pct": bias, "mean_abs_pct": mean_abs, "note": note}


def _calibration_note(n: int, bias: float | None) -> str:
    if not n or bias is None:
        return "还没有实测校准点：预测未经比赛检验，建议赛前用一场测试赛或热身赛校准"
    if abs(bias) <= _CALIB_FINE_PCT:
        return f"已用 {n} 场实测校准：平均偏差 {bias:+.1f}%（±{_CALIB_FINE_PCT:.0f}% 内），预测可信"
    if bias > 0:
        return (f"已用 {n} 场实测校准：预测平均偏乐观 {bias:+.1f}%（实测偏慢），"
                "按目标配速执行要预留余量，前半程压得更保守些")
    return (f"已用 {n} 场实测校准：预测平均偏保守 {bias:+.1f}%（实测更快），"
            "可按目标再放心半档，后程有提速空间")
