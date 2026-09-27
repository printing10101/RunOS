"""训练方法知识库 API：检索、详情、按跑者画像推荐、模板配速解析。

全部为只读端点（知识库内容由 seed 脚本维护），不涉及写入与删除，
因此不需要 CSRF 之外的变更校验。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..data import get_default_athlete
from ..db import get_db
from ..services import method_library

router = APIRouter(prefix="/api/methods", tags=["methods"])


@router.get("")
def list_methods(
    origin: str | None = Query(None, description="japan/asia/western/global"),
    category: str | None = Query(None, description="system=体系 / session=单课型"),
    level: str | None = Query(None, description="beginner/intermediate/advanced/elite"),
    race: str | None = Query(None, description="5k/10k/hm/marathon"),
    phase: str | None = Query(None, description="base/build/peak/taper"),
    min_evidence: str | None = Query(None, description="最低证据等级 A/B/C/D"),
    q: str | None = Query(None, description="关键词（名称/摘要/标签）"),
    db: Session = Depends(get_db),
):
    rows = method_library.list_methods(db, origin=origin, category=category, level=level,
                                       race=race, phase=phase, min_evidence=min_evidence, q=q)
    return {"items": [method_library.method_summary(m) for m in rows], "total": len(rows)}


@router.get("/recommend")
def recommend(top_n: int = Query(5, ge=1, le=10), db: Session = Depends(get_db)):
    """按跑者画像（真实数据推导）推荐训练方法，返回得分与命中理由。"""
    athlete = get_default_athlete(db)
    if not athlete:
        raise HTTPException(404, "请先完善个人档案")
    return method_library.recommend_payload(db, athlete, top_n)


@router.get("/templates")
def list_templates(method: str | None = Query(None, description="按方法 code 过滤"),
                   db: Session = Depends(get_db)):
    rows = db.scalars(select(models.WorkoutTemplate)).all()
    if method:
        rows = [w for w in rows if w.method and w.method.code == method]
    return {"items": [method_library.workout_dict(w, with_structure=False) for w in rows],
            "total": len(rows)}


@router.get("/templates/{code}/resolve")
def resolve_template(code: str, vdot: float = Query(..., gt=20, le=90),
                     db: Session = Depends(get_db)):
    """把课表模板解析为真实配速的结构化步骤（格式与排课引擎一致，可下发手表）。"""
    w = db.scalar(select(models.WorkoutTemplate).where(models.WorkoutTemplate.code == code))
    if not w:
        raise HTTPException(404, "课表模板不存在")
    try:
        steps = method_library.resolve_template_steps(w.structure, vdot)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"template": method_library.workout_dict(w, with_structure=False),
            "vdot": vdot, "structured": steps}


@router.get("/principles")
def list_principles(category: str | None = Query(None, description="periodization/intensity/load/recovery/specificity/strength/nutrition/heat/technique/psychology"),
                    min_evidence: str | None = Query(None), q: str | None = Query(None),
                    db: Session = Depends(get_db)):
    rows = method_library.list_principles(db, category=category, min_evidence=min_evidence, q=q)
    return {"items": [method_library.principle_dict(db, p) for p in rows], "total": len(rows)}


@router.get("/plans/recommend")
def recommend_plans(top_n: int = Query(4, ge=1, le=8), db: Session = Depends(get_db)):
    """按跑者画像匹配参考训练计划（含命中理由与出处）。"""
    athlete = get_default_athlete(db)
    if not athlete:
        raise HTTPException(404, "请先完善个人档案")
    return method_library.recommend_plans_payload(db, athlete, top_n)


@router.get("/plans")
def list_plans(race: str | None = Query(None, description="5k/10k/hm/marathon/any"),
               level: str | None = Query(None), q: str | None = Query(None),
               db: Session = Depends(get_db)):
    rows = method_library.list_plan_templates(db, race=race, level=level, q=q)
    return {"items": [method_library.plan_dict(db, pl) for pl in rows], "total": len(rows)}


@router.post("/{code}/apply-week")
def apply_method_week(code: str, db: Session = Depends(get_db)):
    """把方法体系按跑者真实数据（周跑量/时段/VDOT）降档生成一周体验课表并落库为当前计划。

    这是唯一的写端点：与 /plan/generate 相同的单 active 语义（旧计划归档）。
    """
    athlete = get_default_athlete(db)
    if not athlete:
        raise HTTPException(404, "请先完善个人档案")
    try:
        return method_library.apply_method_week(db, athlete, code)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/{code}")
def get_method(code: str, db: Session = Depends(get_db)):
    m = method_library.get_method(db, code)
    if not m:
        raise HTTPException(404, "训练方法不存在")
    return method_library.method_full(m)
