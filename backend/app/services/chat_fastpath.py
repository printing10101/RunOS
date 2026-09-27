"""AI 教练第〇层·直通：确定性数据查询不进 LLM。

「今天练什么」「这周课表」「配速表」这类纯查询毫秒级从引擎取数返回，
不打模型。判定刻意保守：严格短句白名单 + 疑问诉求排除——白名单外一律
回落到完整链路（能力闸门 → 工具路由 → 循环护栏），宁可慢一点也不能把
需要推理的问题截在直通层。
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..data import get_default_athlete
from .vocab import session_label

# 疑问/请求信号：出现即不走直通（这些需要 LLM 的推理或工具循环）
_QUESTION_MARKS = ("如何", "怎么", "为什么", "为何", "吗", "请", "帮我",
                   "能不能", "要不要", "该不该", "行不行", "好不好")

# 今天：练什么 / 什么课 / 有课吗的肯定式问法
_TODAY_PATTERNS = ("今天练什么", "今天练啥", "今天什么课", "今天的课",
                   "今日课表", "今天安排", "今日安排")
# 本周：课表 / 练什么 / 跑量
_WEEK_PATTERNS = ("这周课表", "本周课表", "这周练什么", "本周练什么",
                  "这周安排", "本周安排", "这周跑量", "本周跑量")
# 配速表
_PACE_PATTERNS = ("配速表", "我的配速", "训练配速", "配速区间")


def _is_question(text: str) -> bool:
    return ("？" in text or "?" in text
            or any(mark in text for mark in _QUESTION_MARKS))


def answer(db: Session, text: str) -> str:
    """命中直通白名单返回现成答案，否则返回空串（调用方走完整链路）。"""
    t = (text or "").strip().strip("。.！!，,？? ")
    if not t or len(t) > 24 or _is_question(t):
        return ""
    if any(p in t for p in _TODAY_PATTERNS):
        return _today(db)
    if any(p in t for p in _WEEK_PATTERNS):
        if "跑量" in t:
            return _week_summary(db)
        return _week_plan(db)
    if any(p in t for p in _PACE_PATTERNS):
        return _pace_table(db)
    return ""


def _pace_table(db: Session) -> str:
    athlete = get_default_athlete(db)
    if not athlete:
        return ""
    from ..data import build_prediction
    from .vdot import daniels_paces
    p = build_prediction(db, athlete.id)
    if not p.current_vdot:
        return ""
    lines = [f"当前 VDOT {p.current_vdot:.1f} 的训练配速表："]
    for z in daniels_paces(p.current_vdot):
        lines.append(f"· {z['label']}：{z['pace_from']} ~ {z['pace_to']}（{z['purpose']}）")
    return "\n".join(lines)


def _today(db: Session) -> str:
    athlete = get_default_athlete(db)
    if not athlete:
        return ""
    rows = db.scalars(select(models.PlanWorkout).where(
        models.PlanWorkout.athlete_id == athlete.id,
        models.PlanWorkout.date == date.today(),
        models.PlanWorkout.status.in_(["planned", "synced"]))).all()
    if not rows:
        return "今天没有安排训练课，可以休息或安排 30 分钟轻松跑。"
    lines = []
    for wo in rows:
        parts = [f"{session_label(wo.session_type)}「{wo.title}」"]
        if wo.distance_km:
            parts.append(f"{wo.distance_km:g}km")
        if wo.duration_min:
            parts.append(f"约 {round(wo.duration_min)} 分钟")
        if wo.start_time:
            parts.append(wo.start_time)
        lines.append("，".join(parts))
    return "今天的安排：" + "；".join(lines) + "。"


def _week_rows(db: Session, athlete_id: int) -> list[models.PlanWorkout]:
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    return db.scalars(select(models.PlanWorkout).where(
        models.PlanWorkout.athlete_id == athlete_id,
        models.PlanWorkout.date >= monday,
        models.PlanWorkout.date <= monday + timedelta(days=6),
        models.PlanWorkout.status.in_(["planned", "synced"]))
        .order_by(models.PlanWorkout.date)).all()


def _week_plan(db: Session) -> str:
    athlete = get_default_athlete(db)
    if not athlete:
        return ""
    rows = _week_rows(db, athlete.id)
    if not rows:
        return "本周还没有安排课表。"
    wd = "一二三四五六日"
    return "本周课表：" + "；".join(
        f"周{wd[w.date.weekday()]} {session_label(w.session_type)}「{w.title}」" for w in rows) + "。"


def _week_summary(db: Session) -> str:
    athlete = get_default_athlete(db)
    if not athlete:
        return ""
    rows = _week_rows(db, athlete.id)
    km = sum(w.distance_km or 0 for w in rows)
    return f"本周计划跑量约 {km:g}km，共 {len(rows)} 节课。"
