"""每日晨间打卡：主观状态录入 + 今日训练档位建议。

建议引擎是确定性的（services/checkin_advice + services/diet.fueling_status），
只输出档位与理由，不直接改课表；要改课表必须走 AI 提案 → 用户确认。
"""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..data import body_metrics_dicts, diet_analysis_payload, get_default_athlete
from ..db import get_db
from ..services.baseline import objective_cap
from ..services.checkin_advice import build_checkin_advice
from ..services.vocab import is_hard_or_long

router = APIRouter(prefix="/api/checkin", tags=["checkin"])


def _checkin_dict(row: models.DailyCheckin) -> dict:
    return {"id": row.id, "date": row.date.isoformat(),
            "sleep_quality": row.sleep_quality, "muscle_soreness": row.muscle_soreness,
            "energy_level": row.energy_level, "motivation": row.motivation,
            "pain_area": row.pain_area, "note": row.note}


def _today_workout(db: Session, athlete_id: int) -> dict | None:
    """今天的计划课。同天多节时质量课/长距离优先展示——它们才是
    最可能需要按打卡状态调整的课（vocab 统一口径）。"""
    rows = db.scalars(select(models.PlanWorkout).where(
        models.PlanWorkout.athlete_id == athlete_id,
        models.PlanWorkout.date == date.today(),
        models.PlanWorkout.status == "planned")).all()
    if not rows:
        return None
    rows.sort(key=lambda w: 0 if is_hard_or_long(w.session_type) else 1)
    wo = rows[0]
    return {"id": wo.id, "session_type": wo.session_type, "title": wo.title,
            "distance_km": wo.distance_km, "duration_min": wo.duration_min}


def _advice(db: Session, athlete: models.Athlete,
            checkin: models.DailyCheckin | dict | None) -> dict:
    wo = _today_workout(db, athlete.id)
    fueling = diet_analysis_payload(db, athlete).get("fueling")
    # 建议引擎只吃 dict；ORM 行先序列化（测试夹具直接传 dict，故两种都收）
    if checkin is not None and not isinstance(checkin, dict):
        checkin = _checkin_dict(checkin)
    objective = objective_cap(body_metrics_dicts(db, athlete.id, days=35))
    return build_checkin_advice(checkin, wo, fueling, objective)


def _get_or_none(db: Session, athlete_id: int, day: date) -> models.DailyCheckin | None:
    return db.scalar(select(models.DailyCheckin).where(
        models.DailyCheckin.athlete_id == athlete_id,
        models.DailyCheckin.date == day))


@router.get("/today")
def today_checkin(db: Session = Depends(get_db)):
    athlete = get_default_athlete(db)
    if not athlete:
        return {"checkin": None, "advice": None, "today_workout": None}
    row = _get_or_none(db, athlete.id, date.today())
    advice = _advice(db, athlete, row)
    return {"checkin": _checkin_dict(row) if row else None, "advice": advice,
            "today_workout": _today_workout(db, athlete.id)}


@router.post("")
def upsert_checkin(data: schemas.CheckinIn, db: Session = Depends(get_db)):
    """按日期 upsert：每人每天一条，重复提交即修正。"""
    athlete = get_default_athlete(db)
    if not athlete:
        raise HTTPException(404, "请先完善个人档案")
    row = _get_or_none(db, athlete.id, data.date)
    if not row:
        row = models.DailyCheckin(athlete_id=athlete.id, date=data.date)
        db.add(row)
    for k, v in data.model_dump().items():
        setattr(row, k, v)
    db.commit()
    return {"ok": True, "checkin": _checkin_dict(row),
            "advice": _advice(db, athlete, row)}


@router.get("/history")
def checkin_history(days: int = 30, db: Session = Depends(get_db)):
    """近 N 天打卡序列（升序），供趋势图与连续天数统计。"""
    athlete = get_default_athlete(db)
    if not athlete:
        return {"items": []}
    since = date.today() - timedelta(days=max(1, min(days, 365)))
    rows = db.scalars(select(models.DailyCheckin).where(
        models.DailyCheckin.athlete_id == athlete.id,
        models.DailyCheckin.date >= since).order_by(models.DailyCheckin.date)).all()
    return {"items": [_checkin_dict(r) for r in rows]}
