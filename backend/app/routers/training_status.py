"""训练状态：负荷趋势 / 负荷重点 / 训练状态标签 / 训练准备度 / 恢复时间。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..data import (
    active_plan,
    activities_dicts,
    athlete_dict,
    build_prediction,
    build_training_status_for,
    get_default_athlete,
    planned_workouts,
)
from ..db import get_db

router = APIRouter(prefix="/api/training-status", tags=["training-status"])


@router.get("")
def training_status(db: Session = Depends(get_db)):
    athlete = get_default_athlete(db)
    if not athlete:
        return {"empty": True}
    payload, _ = build_training_status_for(db, athlete)
    payload["athlete"] = {"name": athlete.name, "max_hr": athlete.max_hr,
                          "resting_hr": athlete.resting_hr, "hrv_baseline": athlete.hrv_baseline}
    # 官方↔本地交叉校验（未连接官方平台时 empty，前端不显示卡片）
    from ..services.crosscheck import build_crosscheck, snapshots_dicts
    payload["crosscheck"] = build_crosscheck(
        snapshots_dicts(db, athlete.id), payload.get("daily") or [])
    return payload


@router.get("/pro-insights")
def pro_insights(db: Session = Depends(get_db)):
    """专业洞察（有氧解耦 / 强度分布 / 乳酸阈值 / 疲劳抗性 / 步频经济性）。

    独立端点而非并入 /：洞察引擎需要带设备明细（raw）的活动数据，
    比常规训练状态查询重一个量级，不值得让状态页每次都背着它。
    """
    from ..services.pro_insights import build_pro_insights

    athlete = get_default_athlete(db)
    if not athlete:
        return {"empty": True}
    acts = activities_dicts(db, athlete.id, days=120, include_raw=True)
    # current_vdot 与官方快照不传的话，阈值卡的 Daniels 交叉分支和 VO2max 互证卡
    # 永远不可达（参数定义了却没人喂）——这里补上两条数据线
    pred = build_prediction(db, athlete.id)
    from ..services.crosscheck import snapshots_dicts
    snaps = snapshots_dicts(db, athlete.id)
    return build_pro_insights(acts, athlete_dict(athlete),
                              current_vdot=pred.current_vdot or None,
                              now=datetime.now(),
                              fitness_snapshot=snaps[-1] if snaps else None)


@router.get("/forecast")
def load_forecast(db: Session = Depends(get_db)):
    """前瞻态负荷规划：把当前有效计划代入 CTL/ATL/TSB，预测未来 12 周体能/疲劳/形态。"""
    from ..services import load_project

    athlete = get_default_athlete(db)
    if not athlete:
        return {"empty": True}
    plan = active_plan(db)
    if not plan:
        return {"empty": True, "reason": "暂无有效训练计划，先生成计划后可预览未来负荷曲线"}

    st = training_status(db)
    if st.get("empty"):
        return {"empty": True, "reason": "暂无训练数据，负荷曲线需要先有历史训练记录"}

    planned, planned_race = planned_workouts(db, plan.id)
    race_date = planned_race or plan.race_date

    ad = {"max_hr": athlete.max_hr, "resting_hr": athlete.resting_hr, "sex": athlete.sex}
    payload = load_project.project_load(
        st["daily"], planned, ad, fitness=st["fitness"], fatigue=st["fatigue"],
        race_date=race_date)
    payload["plan"] = {"id": plan.id, "name": plan.name, "race_date": plan.race_date.isoformat()}
    return payload
