"""伤病结构化追踪：恢复期建议（档位+理由）与负荷关联分析（确定性、只读）。

与 checkin 建议引擎同一纪律：只出档位与理由，不直接改课表；课表变更仍走
「AI 提案 → 用户确认」。恢复期建议按伤病等级与起始天数给渐进档位，解释权
交给教练与用户；关联分析回答「我的伤病是不是都赶在负荷高位段出现」——
样本不足或负荷窗口覆盖不到的伤病如实排除，不硬凑。
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models

SEVERITY_LABELS = {1: "轻微", 2: "中等", 3: "严重"}

# 关联分析口径：伤病起始日前 7 天本地 ACWR 均值 ≥1.3 记「高位段起病」
ONSET_ACWR_HIGH = 1.3
_ONSET_WINDOW = 7
_ONSET_ACWR_WINDOW_DAYS = 200   # 负荷序列回看窗口：更早的伤病不参与关联
_MIN_CORRELATION_SAMPLE = 2


def injuries_dicts(db: Session, athlete_id: int, days: int = 365) -> list[dict]:
    """全部伤病记录（按起始日倒序），供列表与档案复盘。"""
    since = date.today() - timedelta(days=days)
    rows = db.scalars(select(models.InjuryLog).where(
        models.InjuryLog.athlete_id == athlete_id,
        models.InjuryLog.start_date >= since)
        .order_by(models.InjuryLog.start_date.desc())).all()
    return [_injury_dict(r) for r in rows]


def active_injuries(db: Session, athlete_id: int) -> list[dict]:
    """恢复中的伤病（end_date 为空），按起始日正序——最早的先被看见。"""
    rows = db.scalars(select(models.InjuryLog).where(
        models.InjuryLog.athlete_id == athlete_id,
        models.InjuryLog.end_date.is_(None))
        .order_by(models.InjuryLog.start_date)).all()
    return [_injury_dict(r) for r in rows]


def _injury_dict(r: models.InjuryLog) -> dict:
    return {"id": r.id, "start_date": r.start_date.isoformat(),
            "end_date": r.end_date.isoformat() if r.end_date else None,
            "body_part": r.body_part, "side": r.side, "severity": r.severity,
            "severity_label": SEVERITY_LABELS.get(r.severity, "轻微"),
            "cause": r.cause, "notes": r.notes,
            "days_since_start": (date.today() - r.start_date).days}


def return_to_run_advice(injury: dict | None, today: date | None = None) -> dict | None:
    """恢复期渐进档位：按等级 × 起病天数给「rest/easy/reduce/normal」+ 理由。

    档位词汇与 checkin_advice._LADDER 同源（用户看着是同一套语言）；
    疼痛监控红线单独给出，覆盖所有档位。
    """
    if not injury:
        return None
    today = today or date.today()
    start = date.fromisoformat(injury["start_date"])
    days = max(0, (today - start).days)
    sev = injury["severity"]
    part = injury["body_part"] + (f"（{injury['side']}）" if injury.get("side") else "")

    if sev >= 3:
        if days <= 7:
            level, why = "rest", f"{part} 严重伤病起病 {days} 天：头 7 天停跑，让组织先过炎症期"
        elif days <= 21:
            level, why = "easy", f"{part} 严重伤病第 {days} 天：跑走交替起步（如跑 1 分走 2 分 × 20 分钟），隔天一次"
        else:
            level, why = "reduce", f"{part} 严重伤病第 {days} 天：可渐进恢复轻松跑，若仍疼痛建议就医复评再继续"
    elif sev == 2:
        if days <= 3:
            level, why = "easy", f"{part} 中等伤病起病 {days} 天：只做轻松跑或 cross-training，避开疼痛动作"
        elif days <= 10:
            level, why = "reduce", f"{part} 中等伤病第 {days} 天：减量 30-50%，先恢复频率再恢复单次量"
        else:
            level, why = "normal", f"{part} 中等伤病第 {days} 天：可按计划练，带痛监控红线收紧"
    else:
        if days <= 2:
            level, why = "easy", f"{part} 轻微不适第 {days} 天：先轻松跑观察反应，不上强度"
        else:
            level, why = "normal", f"{part} 轻微不适第 {days} 天：可按计划练，质量课前后留意疼痛变化"

    return {"injury_id": injury["id"], "body_part": part, "severity": sev,
            "days_since_start": days, "level": level, "why": why,
            "pain_rule": "训练中或次日疼痛超过 3/10、或晨起第一步痛 → 立即降一档并休跑 2 天"}


def load_correlation(db: Session, athlete_id: int,
                     daily_loads: list[dict] | None = None) -> dict:
    """伤病起始 × 负荷水平关联：起病前 7 天 ACWR 均值处于高位段的比例。

    daily_loads 缺省时从活动表现算（逐日 training_load 求和，与 load_status
    同口径）；负荷窗口覆盖不到的伤病不计入 measured。样本 <2 如实 insufficient。
    """
    from .crosscheck import acwr_series

    rows = db.scalars(select(models.InjuryLog).where(
        models.InjuryLog.athlete_id == athlete_id)).all()
    if len(rows) < _MIN_CORRELATION_SAMPLE:
        return {"n": len(rows), "measured": 0, "onset_high_acwr": 0,
                "insufficient": True, "detail": None}

    if daily_loads is None:
        since = date.today() - timedelta(days=_ONSET_ACWR_WINDOW_DAYS)
        acts = db.scalars(select(models.Activity).where(
            models.Activity.athlete_id == athlete_id,
            models.Activity.start_time >= since)).all()
        by_day: dict[date, float] = {}
        for a in acts:
            d = a.start_time.date()
            by_day[d] = by_day.get(d, 0.0) + (a.training_load or 0)
        daily_loads = [{"date": d.isoformat(), "load": v} for d, v in sorted(by_day.items())]
    series = acwr_series(daily_loads)

    measured, high = 0, 0
    for r in rows:
        vals = [series.get(r.start_date - timedelta(days=k)) for k in range(1, _ONSET_WINDOW + 1)]
        vals = [v for v in vals if v is not None]
        if not vals:
            continue
        measured += 1
        if sum(vals) / len(vals) >= ONSET_ACWR_HIGH:
            high += 1
    if measured < _MIN_CORRELATION_SAMPLE:
        return {"n": len(rows), "measured": measured, "onset_high_acwr": high,
                "insufficient": True, "detail": None}

    if high:
        detail = (f"{measured} 次伤病里 {high} 次起病于前 {_ONSET_WINDOW} 天 ACWR ≥"
                  f"{ONSET_ACWR_HIGH} 的负荷高位段——负荷爬坡阶段把增幅压住是最直接的预防")
    else:
        detail = f"{measured} 次伤病里 0 次起病于负荷高位段，病因更可能在负荷之外（动作/装备/意外）"
    return {"n": len(rows), "measured": measured, "onset_high_acwr": high,
            "insufficient": False, "detail": detail}
