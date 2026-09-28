"""活动 ↔ 计划课自动关联 + 同步后置钩子。

高驰/佳明没有数据推送，同步管道把新活动落库的时刻就是「知道用户跑完了」的
最早时机——分析、点评、调课建议（workout_analysis / plan_drift）统一从
on_activities_changed 这个钩子获得触发入口。

1. auto_link：新活动与「当天±1 天」的未完成计划课按距离/时长容差配对，
   命中即置 completed/linked 并回填 completed_activity_id，对账不再依赖
   用户手动点「完成」；
2. 配对成功的课自动触发训练后点评（coach_comment.regenerate_async 线程内
   幂等且用独立会话，因此必须在 link 事务 commit 之后调用）。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models

logger = logging.getLogger(__name__)

# 配对容差（相对计划值）：实际距离/时长落在计划的一定比例窗口内才算同一堂课。
# 窗口故意放宽——户外 GPS 漂移、临时改路线都常见；真正防止的是把一次随意慢跑
# 错挂到长距离课上（计划一半的距离都不该算同一堂课）。日期约束（±1 天）才是
# 最强的判别条件。
KM_RATIO_WINDOW = (0.6, 1.5)
DUR_RATIO_WINDOW = (0.5, 1.6)
DATE_WINDOW_DAYS = 1


def _candidates(db: Session, athlete_id: int, act_date: date) -> list[models.PlanWorkout]:
    """未完成的计划课（planned/synced，含已推送到手表还没跑的），按日期邻近过滤。"""
    lo = act_date - timedelta(days=DATE_WINDOW_DAYS)
    hi = act_date + timedelta(days=DATE_WINDOW_DAYS)
    return db.scalars(
        select(models.PlanWorkout).where(
            models.PlanWorkout.athlete_id == athlete_id,
            models.PlanWorkout.status.in_(["planned", "synced"]),
            models.PlanWorkout.date >= lo,
            models.PlanWorkout.date <= hi,
        ).order_by(models.PlanWorkout.date)
    ).all()


def auto_link_activity(db: Session, activity: models.Activity) -> models.PlanWorkout | None:
    """把一条新活动与最匹配的计划课配对。命中返回该课（不 commit，由调用方统一提交）。"""
    # 力量课/跑步机外的交叉训练没有距离概念，误配率高于收益，v1 只对跑步开放
    if activity.sport != "run" or not activity.distance_m:
        return None
    if activity.start_time is None:
        return None
    # 已被其他课关联过（取消完成后重新同步等场景）不再重复配对
    taken = db.scalar(select(models.PlanWorkout.id).where(
        models.PlanWorkout.completed_activity_id == activity.id).limit(1))
    if taken is not None:
        return None

    act_km = activity.distance_m / 1000
    act_min = activity.duration_sec / 60
    best, best_score = None, None
    for wo in _candidates(db, activity.athlete_id, activity.start_time.date()):
        if not wo.distance_km:
            continue
        km_ratio = act_km / wo.distance_km
        if not (KM_RATIO_WINDOW[0] <= km_ratio <= KM_RATIO_WINDOW[1]):
            continue
        if wo.duration_min > 0 and activity.duration_sec:
            dur_ratio = act_min / wo.duration_min
            if not (DUR_RATIO_WINDOW[0] <= dur_ratio <= DUR_RATIO_WINDOW[1]):
                continue
        # 日期差优先（同天 > 前一天补跑 > 次日补跑），同分时取形状最接近的
        date_diff = abs((wo.date - activity.start_time.date()).days)
        score = (date_diff, abs(km_ratio - 1) + abs((act_min / wo.duration_min if wo.duration_min else 1) - 1))
        if best_score is None or score < best_score:
            best, best_score = wo, score
    if best is None:
        return None
    best.status = "completed"
    best.completed_source = "linked"
    best.completed_activity_id = activity.id
    return best


def link_new_activities(db: Session, athlete_id: int, activity_ids: list[int]) -> list[int]:
    """批量尝试关联，返回新命中的 workout id 列表（自带提交：点评线程读库前必须落盘）。"""
    linked: list[int] = []
    for aid in activity_ids:
        act = db.get(models.Activity, aid)
        if act is None:
            continue
        wo = auto_link_activity(db, act)
        if wo is not None:
            linked.append(wo.id)
    if linked:
        db.commit()
    return linked


def on_activities_changed(db: Session, athlete_id: int, activity_ids: list[int]) -> dict:
    """同步后置钩子：画像刷新 → 课表对账 → 训练后点评。

    后续的分析/调课阶段（workout_analysis / plan_drift）在此追加触发位；
    任何一步失败只记日志，不把错误抛回同步管道。
    """
    out: dict = {"linked_workouts": []}
    try:
        athlete = db.get(models.Athlete, athlete_id)
        if athlete is not None:
            from . import profile
            profile.refresh(db, athlete, commit=False)
            db.commit()   # 画像推导结果随对账事务一并落盘
    except Exception as e:
        logger.warning("同步后画像刷新失败（忽略）athlete=%s: %s", athlete_id, e)

    try:
        out["linked_workouts"] = link_new_activities(db, athlete_id, activity_ids)
    except Exception as e:
        logger.exception("活动↔课表自动关联失败 athlete=%s: %s", athlete_id, e)
        db.rollback()
        return out

    # regenerate_async 幂等（已有点评/已取消完成会跳过），线程内自建会话，
    # 上面已 commit，这里只是「点火」；单课分析是纯计算，同步做掉供调课引擎用
    from . import coach_comment, workout_analysis
    for wo_id in out["linked_workouts"]:
        try:
            coach_comment.regenerate_async(wo_id)
        except Exception as e:
            logger.warning("自动触发训练点评失败（workout %s）: %s", wo_id, e)
        try:
            workout_analysis.analyze_and_store(db, wo_id)
        except Exception as e:
            logger.warning("单课分析失败（workout %s）: %s", wo_id, e)
    return out
