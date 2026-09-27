"""饮食记录与分析。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..data import diet_analysis_payload, get_default_athlete
from ..db import get_db
from ..services import ai_coach

router = APIRouter(prefix="/api/diet", tags=["diet"])


@router.get("")
def diet_overview(db: Session = Depends(get_db)):
    athlete = get_default_athlete(db)
    if not athlete:
        return {"targets": None, "analysis": None, "logs": []}
    return diet_analysis_payload(db, athlete)


@router.post("/logs")
def add_log(data: schemas.DietLogIn, db: Session = Depends(get_db)):
    athlete = get_default_athlete(db)
    if not athlete:
        raise HTTPException(404, "请先完善个人档案")
    r = models.DietLog(athlete_id=athlete.id, **data.model_dump())
    db.add(r)
    db.commit()
    return {"ok": True, "id": r.id}


@router.put("/logs/{log_id}")
def update_log(log_id: int, data: schemas.DietLogIn, db: Session = Depends(get_db)):
    r = db.get(models.DietLog, log_id)
    if not r:
        raise HTTPException(404, "记录不存在")
    for k, v in data.model_dump().items():
        setattr(r, k, v)
    db.commit()
    return {"ok": True}


@router.post("/estimate")
def estimate(data: schemas.DietEstimateIn):
    """食物描述 → 本地模型估算营养素（kcal/三大营养素）。模型不可用时返回错误，前端可退回手填。"""
    text = (data.description or "").strip()
    if not text:
        raise HTTPException(400, "请先描述吃了什么")
    try:
        return ai_coach.estimate_meal_nutrition(text)
    except ValueError as e:
        raise HTTPException(503, str(e)) from e


@router.delete("/logs/{log_id}")
def delete_log(log_id: int, db: Session = Depends(get_db)):
    r = db.get(models.DietLog, log_id)
    if not r:
        raise HTTPException(404, "记录不存在")
    db.delete(r)
    db.commit()
    return {"ok": True}
