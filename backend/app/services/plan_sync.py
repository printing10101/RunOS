"""高驰官方课表镜像：把 COROS 的训练日程落到本地 plan_workouts。

设计约定：
- 高驰是课表**事实源**，本地镜像做全量对齐（新增/更新/移除），但保留已完成
  课次——那是用户的真实执行历史，与「归档计划的 planned 课该删、completed
  课该留」是同一条纪律（data.archive_plan）。
- 镜像占用「当前有效计划」的位置：同步前把用户本地 AI 计划走
  data.archive_plan() 归档，绝不直接改 status（会把 planned 课残留进
  总览/打卡/执行率等按 athlete_id 的查询）。
- 官方课表只有模板码（如 S5808）与负荷预估，没有课型语义；session_type
  用透明的确定性规则映射：TL≥65 视为质量课、时长≥90 分钟视为长距离，
  其余按轻松跑。规则写在这里，AI 工具链直接复用，不另造一套。
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..data import archive_plan
from ..integrations.base import IntegrationError
from ..logging_config import get_logger

logger = get_logger(__name__)

MIRROR_PLAN_NAME = "高驰课表（官方镜像）"

# 镜像窗口：往前多看 2 天（处理今天已过的课），往后镜像半年
_PAST_DAYS = 2
_FORWARD_DAYS = 180


def _identity(row: dict) -> str:
    """官方课次的稳定身份键。idInPlan 是官方排课内的唯一序号，最可靠；
    缺失时退化为 模板码+日期（同天同码视为同一节课）。"""
    if row.get("plan_id") and row.get("id_in_plan") is not None:
        return f"{row['plan_id']}:{row['id_in_plan']}"
    return f"code:{row.get('code', '')}@{row.get('date')}"


def _session_type(row: dict) -> str:
    if (row.get("load_tl") or 0) >= 65:
        return "quality"
    if (row.get("duration_min") or 0) >= 90:
        return "long"
    return "easy"


def _title(row: dict) -> str:
    code = row.get("code") or "官方课"
    km = row.get("distance_km")
    return f"官方课表 {code}" + (f"（{km}km）" if km else "")


def _description(row: dict) -> str:
    parts = []
    if row.get("duration_min"):
        parts.append(f"预计 {round(row['duration_min'])} 分钟")
    if row.get("load_tl") is not None:
        parts.append(f"官方负荷 {row['load_tl']} TL")
    parts.append("来自高驰官方课表镜像，模板码 " + (row.get("code") or "-"))
    return "，".join(parts)


def _ensure_week(db: Session, plan: models.TrainingPlan, monday: date,
                 index_of: dict[date, int]) -> models.PlanWeek:
    week = db.scalar(select(models.PlanWeek).where(
        models.PlanWeek.plan_id == plan.id, models.PlanWeek.start_date == monday))
    if not week:
        week = models.PlanWeek(plan_id=plan.id, start_date=monday, week_index=1,
                               phase="base")
        db.add(week)
        db.flush()   # 后续插 workout 需要 week.id
    week.week_index = index_of[monday]
    return week


def sync_coros_schedule(db: Session, adapter) -> dict:
    """拉取官方训练日程并对齐本地镜像计划（不 commit，由调用方统一提交）。"""
    today = date.today()
    rows = adapter.fetch_training_schedule(today - timedelta(days=_PAST_DAYS),
                                           today + timedelta(days=_FORWARD_DAYS))
    if not rows:
        return {"ok": True, "count": 0, "added": 0, "updated": 0, "removed": 0,
                "archived_local": False}

    plan = db.scalar(select(models.TrainingPlan).where(
        models.TrainingPlan.name == MIRROR_PLAN_NAME
    ).order_by(models.TrainingPlan.id.desc()))
    archived_local = False
    if plan is None:
        athlete = db.scalar(select(models.Athlete).order_by(models.Athlete.id).limit(1))
        if not athlete:
            raise IntegrationError("请先完善个人档案再同步课表")
        plan = models.TrainingPlan(
            athlete_id=athlete.id, name=MIRROR_PLAN_NAME, race_type="other",
            start_date=today, race_date=today, source="coros")
        db.add(plan)
        db.flush()

    # 镜像成为当前有效计划；本地原计划按标准归档通道退位
    active = db.scalars(select(models.TrainingPlan).where(
        models.TrainingPlan.status == "active",
        models.TrainingPlan.id != plan.id)).all()
    for other in active:
        archive_plan(db, other)
        archived_local = True

    dates = [date.fromisoformat(r["date"]) for r in rows if r.get("date")]
    mondays = sorted({d - timedelta(days=d.weekday()) for d in dates})
    index_of = {m: i + 1 for i, m in enumerate(mondays)}

    existing_planned = db.scalars(
        select(models.PlanWorkout).join(models.PlanWeek,
                                        models.PlanWorkout.week_id == models.PlanWeek.id)
        .where(models.PlanWeek.plan_id == plan.id,
               models.PlanWorkout.status == "planned")).all()
    by_identity: dict[str, models.PlanWorkout] = {
        (wo.external_workout_id or ""): wo for wo in existing_planned}

    added = updated = 0
    seen: set[str] = set()
    for row in rows:
        if not row.get("date"):
            continue
        d = date.fromisoformat(row["date"])
        monday = d - timedelta(days=d.weekday())
        week = _ensure_week(db, plan, monday, index_of)
        ident = _identity(row)
        seen.add(ident)
        wo = by_identity.get(ident)
        fields = {"date": d, "session_type": _session_type(row), "title": _title(row),
                  "description": _description(row),
                  "distance_km": row.get("distance_km") or 0,
                  "duration_min": row.get("duration_min") or 0}
        if wo is None:
            db.add(models.PlanWorkout(
                week_id=week.id, athlete_id=plan.athlete_id, status="planned",
                external_workout_id=ident, pushed_platforms=[], structured=[], **fields))
            added += 1
        else:
            changed = any(getattr(wo, k) != v for k, v in fields.items())
            if wo.week_id != week.id:
                wo.week_id = week.id
                changed = True
            if changed:
                for k, v in fields.items():
                    setattr(wo, k, v)
                updated += 1

    removed = 0
    for wo in existing_planned:
        if (wo.external_workout_id or "") not in seen:
            db.delete(wo)
            removed += 1

    if dates:
        plan.start_date = min(mondays)
        plan.race_date = max(dates)   # 官方镜像没有真正的比赛日，用最后排课日占位

    result = {"ok": True, "count": len(rows), "added": added, "updated": updated,
              "removed": removed, "archived_local": archived_local}
    logger.info("高驰课表镜像完成: %s", result)
    return result
