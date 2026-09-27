"""综合评估：五维评分快照的计算与留档。

前端评估页/预测页每次打开都 POST /compute 全量重算（评估随时可重算）；
with_persist=True 时按「分数没变且一周内已留档就跳过」的规则写
assessments 快照，避免页面浏览把历史趋势表灌成「访问记录」。
AI 解读在 /api/ai/assessment-interpretation（ai 路由），不在本路由。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..data import build_evaluation, get_default_athlete
from ..db import get_db

router = APIRouter(prefix="/api/assessment", tags=["assessment"])


@router.post("/compute")
def compute(db: Session = Depends(get_db)):
    athlete = get_default_athlete(db)
    if not athlete:
        return {"empty": True}
    return build_evaluation(db, athlete, with_persist=True)
