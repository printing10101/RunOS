"""比赛实测成绩：成绩预测的「真值锚点」录入与维护。

每次录入后预测自动校准（predictor.calibrate_riegel 每次计算时重跑），
所以这里只做纯 CRUD，不缓存任何派生值。唯一例外是偏差复盘：录入/修正
成绩时回填赛前预测快照（race_predictions 表）——这一步不回填，复盘
就永远没有真值可比。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..data import get_default_athlete
from ..db import get_db
from ..services.vdot import pace_label, time_str, vdot_from_performance

router = APIRouter(prefix="/api/races", tags=["races"])


def _race_dict(r: models.RaceResult, review: dict | None = None) -> dict:
    try:
        v = round(vdot_from_performance(r.distance_m, r.time_sec), 1)
    except ValueError:
        v = None
    pace = round(r.time_sec / (r.distance_m / 1000)) if r.distance_m else None
    out = {
        "id": r.id, "date": r.date.isoformat(), "race_name": r.race_name,
        "race_type": r.race_type, "distance_m": r.distance_m,
        "time_sec": r.time_sec, "time_str": time_str(r.time_sec),
        "pace_str": pace_label(pace) if pace else "-",
        "vdot": v, "avg_hr": r.avg_hr, "is_official": r.is_official,
        "notes": r.notes,
    }
    if review is not None:
        hit = review.get((r.date.isoformat(), r.race_type))
        if hit:
            out["prediction"] = hit
    return out


def _review_map(db: Session, athlete_id: int) -> dict:
    from ..data import race_prediction_review
    return race_prediction_review(db, athlete_id)


@router.get("")
def list_races(db: Session = Depends(get_db)):
    athlete = get_default_athlete(db)
    if not athlete:
        return {"races": []}
    rows = db.scalars(select(models.RaceResult).where(
        models.RaceResult.athlete_id == athlete.id
    ).order_by(models.RaceResult.date.desc())).all()
    review = _review_map(db, athlete.id)
    return {"races": [_race_dict(r, review) for r in rows]}


@router.post("")
def add_race(data: schemas.RaceResultIn, db: Session = Depends(get_db)):
    athlete = get_default_athlete(db)
    if not athlete:
        raise HTTPException(404, "请先完善个人档案")
    if data.distance_m <= 0 or data.time_sec <= 0:
        raise HTTPException(400, "距离与成绩必须为正数")
    row = models.RaceResult(athlete_id=athlete.id, **data.model_dump())
    db.add(row)
    db.flush()   # 拿到 row.id 供回填匹配；commit 统一在末尾
    from ..data import backfill_race_prediction
    backfill_race_prediction(db, athlete.id, row)
    db.commit()
    return {"ok": True, "id": row.id, "race": _race_dict(row)}


@router.put("/{race_id}")
def update_race(race_id: int, data: schemas.RaceResultIn, db: Session = Depends(get_db)):
    row = db.get(models.RaceResult, race_id)
    if not row:
        raise HTTPException(404, "比赛记录不存在")
    for k, v in data.model_dump().items():
        setattr(row, k, v)
    from ..data import backfill_race_prediction
    backfill_race_prediction(db, row.athlete_id, row)
    db.commit()
    return {"ok": True}


@router.delete("/{race_id}")
def delete_race(race_id: int, db: Session = Depends(get_db)):
    row = db.get(models.RaceResult, race_id)
    if not row:
        raise HTTPException(404, "比赛记录不存在")
    db.delete(row)
    db.commit()
    return {"ok": True}
