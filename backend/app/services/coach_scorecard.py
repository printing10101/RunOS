"""教练成绩单：提案采纳与执行落地的问责聚合（只读、纯确定性）。

所有跑步 App 都在度量跑者，这里度量教练自己。系统里已经躺着三类「教练输出」——
引擎建议（ai_proposals）、建议对应的课次（plan_workouts）、每周的执行情况——
聚合成一张可对答案的成绩单：
- 提案采纳率：发出 → 采纳/忽略。自动撤回（withdrawn）是系统行为，不算用户的决定；
- 采纳后执行：带 workout_id 的采纳提案，对应那节课最终落地了没有（跳课提案的
  「落地」就是课被跳过——应用函数把 status 置为 skipped，按落地计）;
- 逐周对照：近 8 周每周执行率 + 当周采纳/忽略了几条建议。两组数字并排放着让
  数据自己说话，不做因果断言（样本量不支持）。

口径约定（与既有引擎同源，不自造）：
- 周执行率分母同 data.plan_adherence_dict：已到期、非 rest 课；completed/synced 算完成；
- 决定归属周用 [周一-1天, 下周一-1天) 的 UTC 松弛窗——decided_at 存 UTC 而
  week_start 是本地周一，松弛吸收时区差（同 proposal_store.week_decision_summary，
  极端情况下当地周日前夜的决定会多算进后一周一条，对汇总无伤）。
- 对话提案的卡片只存消息 JSON 里、apply 走无卡通道，无法回溯决定，只统计
  「发出过几张卡」作信息参考，不进采纳率。
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from .planner import week_start

_LANDED_WORKOUT_STATUS = ("completed", "synced")


def proposal_stats(db: Session, athlete: models.Athlete) -> dict:
    """引擎提案的采纳漏斗（全量 + 按类型分组）。"""
    rows = db.scalars(select(models.AiProposal).where(
        models.AiProposal.athlete_id == athlete.id)).all()
    applied = sum(1 for p in rows if p.status == "applied")
    dismissed = sum(1 for p in rows if p.status == "dismissed")
    decided = applied + dismissed

    kinds: dict[str, dict] = {}
    for p in rows:
        slot = kinds.setdefault(p.kind, {"kind": p.kind, "emitted": 0, "applied": 0,
                                         "dismissed": 0, "withdrawn": 0, "pending": 0})
        slot["emitted"] += 1
        if p.status != "pending":
            slot[p.status] += 1
    by_kind = sorted(kinds.values(), key=lambda x: -x["emitted"])
    for k in by_kind:
        k_decided = k["applied"] + k["dismissed"]
        k["acceptance_rate"] = round(k["applied"] / k_decided, 3) if k_decided else None

    # 对话提案：卡片存消息 JSON，apply 不落 ai_proposals，只能数「发过几张」
    conv_ids = db.scalars(select(models.AiConversation.id).where(
        models.AiConversation.athlete_id == athlete.id)).all()
    chat_emitted = 0
    if conv_ids:
        msgs = db.scalars(select(models.AiMessage).where(
            models.AiMessage.conversation_id.in_(conv_ids),
            models.AiMessage.role == "assistant")).all()
        chat_emitted = sum(len(m.proposals or []) for m in msgs)

    return {
        "total": len(rows), "pending": sum(1 for p in rows if p.status == "pending"),
        "applied": applied, "dismissed": dismissed,
        "withdrawn": sum(1 for p in rows if p.status == "withdrawn"),
        "decided": decided,
        "acceptance_rate": round(applied / decided, 3) if decided else None,
        "by_kind": by_kind,
        "chat_emitted": chat_emitted,
    }


def execution_after_adoption(db: Session, athlete: models.Athlete) -> dict:
    """采纳提案的执行落点：带 workout_id 的提案对应课次的最终状态。

    profile_update / goal_update / add_workout 没有单课可对答案，不进这个口径；
    课表重建后 workout 查不到的行跳过（不编造落点）。
    """
    rows = db.scalars(select(models.AiProposal).where(
        models.AiProposal.athlete_id == athlete.id,
        models.AiProposal.status == "applied")).all()
    details = []
    for p in rows:
        workout_id = (p.payload or {}).get("workout_id")
        if not workout_id:
            continue
        wo = db.get(models.PlanWorkout, workout_id)
        if wo is None:
            continue
        if wo.status in _LANDED_WORKOUT_STATUS:
            outcome = "completed"
        elif wo.status == "skipped":
            outcome = "skipped"   # skip 类提案的落地形态
        else:
            outcome = "pending_execution"
        details.append({"proposal_id": p.id, "kind": p.kind, "title": p.title,
                        "workout_date": wo.date.isoformat(),
                        "workout_status": wo.status, "outcome": outcome})
    landed = sum(1 for d in details if d["outcome"] != "pending_execution")
    details.sort(key=lambda d: d["workout_date"], reverse=True)
    return {
        "tracked": len(details), "landed": landed,
        "pending_execution": len(details) - landed,
        "landed_rate": round(landed / len(details), 3) if details else None,
        "details": details,
    }


def weekly_series(db: Session, athlete: models.Athlete, weeks: int = 8) -> list[dict]:
    """近 N 周每周：执行率（已到期非 rest 课）+ 当周采纳/忽略的提案数。老→新。"""
    today = date.today()
    rows = db.scalars(select(models.PlanWorkout).where(
        models.PlanWorkout.athlete_id == athlete.id,
        models.PlanWorkout.date >= today - timedelta(weeks=weeks),
        models.PlanWorkout.date <= today,
        models.PlanWorkout.session_type != "rest")).all()
    buckets: dict[date, dict] = {}
    for w in rows:
        ws = week_start(w.date)
        b = buckets.setdefault(ws, {"week_start": ws.isoformat(), "planned": 0,
                                    "completed": 0, "applied": 0, "dismissed": 0})
        b["planned"] += 1
        if w.status in _LANDED_WORKOUT_STATUS:
            b["completed"] += 1

    decisions = db.scalars(select(models.AiProposal).where(
        models.AiProposal.athlete_id == athlete.id,
        models.AiProposal.status.in_(["applied", "dismissed"]),
        models.AiProposal.decided_at.is_not(None))).all()
    for p in decisions:
        # UTC 决定时刻 +1 天再归周：把「UTC 周日晚 = 本地周一晨」拉回本周
        d = (p.decided_at + timedelta(days=1)).date()
        ws = week_start(d)
        b = buckets.get(ws)
        if b is not None:
            b[p.status] += 1

    series = [buckets[ws] for ws in sorted(buckets)][-weeks:]
    for b in series:
        b["execution_rate"] = round(b["completed"] / b["planned"], 3) if b["planned"] else None
    return series


def _pct(rate: float | None) -> str:
    return f"{round(rate * 100)}%" if rate is not None else "—"


def _headline(stats: dict, execution: dict, series: list[dict]) -> str:
    if not stats["total"] and not stats["chat_emitted"]:
        return ("还没有提案记录：引擎与 AI 教练的调整建议会在这里留档，"
                "采纳与执行情况一目了然。")
    parts = []
    if stats["decided"]:
        parts.append(f"累计 {stats['total']} 条调整建议，采纳 {stats['applied']} 条"
                     f"（采纳率 {_pct(stats['acceptance_rate'])}）")
    elif stats["total"]:
        parts.append(f"累计 {stats['total']} 条建议待处理 {stats['pending']} 条")
    if execution["tracked"]:
        parts.append(f"采纳建议对应课次已落地 {execution['landed']}/{execution['tracked']}")
    due = [b for b in series if b["planned"]]
    if due:
        cur = due[-1]
        parts.append(f"本周已到期课完成 {cur['completed']}/{cur['planned']}")
    return "；".join(parts) + "。"


def build_scorecard(db: Session, athlete: models.Athlete) -> dict:
    stats = proposal_stats(db, athlete)
    execution = execution_after_adoption(db, athlete)
    series = weekly_series(db, athlete)
    return {
        "proposals": stats,
        "execution": execution,
        "weeks": series,
        "headline": _headline(stats, execution, series),
        "empty": stats["total"] == 0 and stats["chat_emitted"] == 0,
    }
