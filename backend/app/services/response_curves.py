"""个人响应曲线：课型 → 次日身体反应 + 硬课后的 HRV 恢复时长（只读、确定性）。

数据资产的复利：记录越久，越能回答通用 App 回答不了的「对我有效的是什么」。
两条曲线都只以「关联了课表课的活动」为样本（未关联的不猜课型——从标题猜
课型是编数据）；HRV 以档案基线为锚（Health 页展示的同一个值，口径对用户可见）。
样本量不足（<3 次）时如实标 insufficient，不硬凑结论。

恢复时长的口径：只统计「次日 HRV 明显抑制」（< 基线 90%）的硬课，从硬课日
到 HRV 回到基线 95% 以上的天数——没有抑制就无所谓恢复，把「没抑制」混进
均值会稀释真实恢复时长。手表给的 recovery_hours 作为对照一并列出。
"""
from __future__ import annotations

from datetime import date, timedelta
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from . import vocab

_MIN_SAMPLE = 3
_SUPPRESSION_RATIO = 0.9     # 次日 HRV < 基线 90% 视为明显抑制
_RECOVER_RATIO = 0.95        # 回到基线 95% 以上算恢复
_RECOVERY_WINDOW_DAYS = 3    # 超过 3 天没回到基线就算「未恢复」，不无限追踪
# 次日睡眠分数反应的参照基线：硬课日前 28 天的中位数（与 baseline.py 的稳健
# 统计口径一致），样本 <7 不估基线——睡眠分数通道是 HRV 的小权重补充，不硬凑
_SLEEP_BASELINE_WINDOW = 28
_SLEEP_BASELINE_MIN = 7


def hard_session_days(db: Session, athlete_id: int) -> list[dict]:
    """关联了硬课（质量课 + 长距离）的活动：[{date, session_type, activity_id}]。"""
    rows = db.execute(
        select(models.Activity, models.PlanWorkout.session_type)
        .join(models.PlanWorkout,
              models.PlanWorkout.completed_activity_id == models.Activity.id)
        .where(models.PlanWorkout.athlete_id == athlete_id,
               models.PlanWorkout.session_type.in_(vocab.HARD_TYPES))
    ).all()
    out = [{"date": act.start_time.date(), "session_type": st, "activity_id": act.id}
           for act, st in rows]
    out.sort(key=lambda x: x["date"])
    return out


def _hrv_by_date(db: Session, athlete_id: int) -> dict[date, float]:
    rows = db.scalars(select(models.BodyMetric).where(
        models.BodyMetric.athlete_id == athlete_id,
        models.BodyMetric.hrv_rmssd.is_not(None))).all()
    return {r.date: float(r.hrv_rmssd) for r in rows}


def _watch_recovery_by_date(db: Session, athlete_id: int) -> dict[date, int]:
    rows = db.scalars(select(models.BodyMetric).where(
        models.BodyMetric.athlete_id == athlete_id,
        models.BodyMetric.recovery_hours.is_not(None))).all()
    return {r.date: int(r.recovery_hours) for r in rows}


def _sleep_score_by_date(db: Session, athlete_id: int) -> dict[date, float]:
    rows = db.scalars(select(models.BodyMetric).where(
        models.BodyMetric.athlete_id == athlete_id,
        models.BodyMetric.sleep_score.is_not(None))).all()
    return {r.date: float(r.sleep_score) for r in rows}


def _median_before(points: dict[date, float], d: date,
                   window: int = _SLEEP_BASELINE_WINDOW,
                   min_n: int = _SLEEP_BASELINE_MIN) -> float | None:
    """d 之前 window 天内的中位数，作个人参照基线；样本不足不猜（None）。"""
    vals = [v for dt, v in points.items() if d - timedelta(days=window) <= dt < d]
    if len(vals) < min_n:
        return None
    return median(vals)


def workout_type_response(db: Session, athlete: models.Athlete) -> list[dict]:
    """各课型的次日 HRV 反应：相对基线的平均偏移与明显抑制比例。多样本在前。

    附带次日睡眠分数反应（相对硬课日前 28 天中位数的小权重补充通道）：
    HRV 反映自主神经恢复，睡眠分数反映睡眠结构质量，两者互补；基线估不出
    或次日缺测就不进样本，宁缺勿猜。
    """
    baseline = float(athlete.hrv_baseline or 0)
    hrv = _hrv_by_date(db, athlete.id)
    sleep = _sleep_score_by_date(db, athlete.id)
    groups: dict[str, list] = {}
    for s in hard_session_days(db, athlete.id):
        groups.setdefault(s["session_type"], []).append(s)
    out = []
    for st, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        deltas, suppressed = [], 0
        sleep_deltas: list[float] = []
        for s in items:
            nxt = hrv.get(s["date"] + timedelta(days=1))
            if nxt is not None and baseline > 0:
                deltas.append(nxt - baseline)
                if nxt < baseline * _SUPPRESSION_RATIO:
                    suppressed += 1
            # 睡眠分数通道独立取样：HRV 缺测不连带跳过
            sleep_base = _median_before(sleep, s["date"])
            nxt_score = sleep.get(s["date"] + timedelta(days=1))
            if sleep_base is not None and nxt_score is not None:
                sleep_deltas.append(nxt_score - sleep_base)
        n = len(deltas)
        out.append({
            "session_type": st, "label": vocab.session_label(st),
            "sessions": len(items), "measured": n,
            "next_day_hrv_delta": round(sum(deltas) / n, 1) if n else None,
            "suppression_rate": round(suppressed / n, 3) if n else None,
            "insufficient": n < _MIN_SAMPLE,
            "sleep_measured": len(sleep_deltas),
            "next_day_sleep_delta": round(sum(sleep_deltas) / len(sleep_deltas), 1)
            if sleep_deltas else None,
            "sleep_insufficient": len(sleep_deltas) < _MIN_SAMPLE,
        })
    return out


def recovery_profile(db: Session, athlete: models.Athlete) -> dict:
    """硬课后的 HRV 恢复天数（明显抑制 → 回到基线 95%），手表恢复时间作对照。"""
    baseline = float(athlete.hrv_baseline or 0)
    hrv = _hrv_by_date(db, athlete.id)
    watch = _watch_recovery_by_date(db, athlete.id)
    spans, watch_hours = [], []
    for s in hard_session_days(db, athlete.id):
        if baseline <= 0:
            break
        nxt = hrv.get(s["date"] + timedelta(days=1))
        if nxt is None or nxt >= baseline * _SUPPRESSION_RATIO:
            continue   # 没有抑制就没有「恢复时长」可言
        for offset in range(1, _RECOVERY_WINDOW_DAYS + 1):
            v = hrv.get(s["date"] + timedelta(days=offset))
            if v is not None and v >= baseline * _RECOVER_RATIO:
                spans.append(offset)
                break
        if s["date"] in watch:
            watch_hours.append(watch[s["date"]])
    sample = len(spans)
    return {
        "sample": sample,
        "median_days_to_baseline": float(median(spans)) if spans else None,
        "watch_recovery_hours_median": float(median(watch_hours)) if watch_hours else None,
        "insufficient": sample < _MIN_SAMPLE,
    }
