"""力量测试录入与评估：五大项相对力量 → 水平分档与均衡度。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..data import get_default_athlete, strength_tests_dicts
from ..db import get_db
from ..services.strength import assess_strength

router = APIRouter(prefix="/api/strength", tags=["strength"])


@router.get("")
def strength_overview(db: Session = Depends(get_db)):
    athlete = get_default_athlete(db)
    if not athlete:
        return {"empty": True, "tests": [], "analysis": None}
    tests = strength_tests_dicts(db, athlete.id)
    return {"empty": not tests, "tests": tests,
            "analysis": assess_strength(tests, athlete.sex, athlete.weight_kg)}


@router.post("")
def add_test(data: schemas.StrengthTestIn, db: Session = Depends(get_db)):
    athlete = get_default_athlete(db)
    if not athlete:
        raise HTTPException(404, "请先完善个人档案")
    row = models.StrengthTest(
        athlete_id=athlete.id, date=data.date, exercise=data.exercise,
        best_weight_kg=data.best_weight_kg, reps=data.reps,
        bodyweight_kg=data.bodyweight_kg or athlete.weight_kg, notes=data.notes)
    db.add(row)
    db.commit()
    return {"ok": True, "id": row.id}


@router.delete("/{test_id}")
def delete_test(test_id: int, db: Session = Depends(get_db)):
    row = db.get(models.StrengthTest, test_id)
    if not row:
        raise HTTPException(404, "测试记录不存在")
    db.delete(row)
    db.commit()
    return {"ok": True}
