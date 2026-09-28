"""提案持久层：漂移引擎建议的挂卡 / 撤回 / 列表（应用在路由层走两段式重检）。

与 AI 对话提案的关系：对话提案仍走消息内卡片 + /api/ai/proposals/apply，
引擎建议在这里持久化成 ai_proposals 行——引擎没有会话上下文，卡片必须
能跨请求存活到用户决定的那一刻。

幂等规则（dedup_key，如 easy_replacement:42）：
- 同一节课的同类建议只挂一张 pending 卡片，重复刷新只更新理由；
- 下次引擎重跑时条件已消失的建议自动标记 withdrawn（撤回≠拒绝，
  时间线上与用户主动忽略区分开）；已 applied/dismissed 的决定不动。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models


def _serialize(p: models.AiProposal) -> dict:
    return {
        "id": p.id, "kind": p.kind, "title": p.title, "reasons": p.reasons or [],
        "preview": p.preview or {}, "severity": p.severity, "payload": p.payload,
        "status": p.status, "source": p.source,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "decided_at": p.decided_at.isoformat() if p.decided_at else None,
    }


def refresh_engine_proposals(db: Session, athlete: models.Athlete) -> None:
    """跑一遍漂移引擎，把建议 upsert 成 pending 卡片；条件消失的自动撤回。"""
    from . import plan_drift

    adjustments = plan_drift.build_adjustments(db, athlete)
    current = {s["dedup_key"]: s for s in adjustments["suggestions"] if s.get("dedup_key")}

    pending = db.scalars(select(models.AiProposal).where(
        models.AiProposal.athlete_id == athlete.id,
        models.AiProposal.source == "engine",
        models.AiProposal.status == "pending")).all()
    by_key = {p.dedup_key: p for p in pending if p.dedup_key}

    # 用户已经决定过的 dedup_key 不再挂新卡：dismissed 是「别再提了」，
    # applied 是「已经改过了」；withdrawn 是系统自动撤回，条件重现允许再提
    decided_keys = set(db.scalars(select(models.AiProposal.dedup_key).where(
        models.AiProposal.athlete_id == athlete.id,
        models.AiProposal.source == "engine",
        models.AiProposal.status.in_(["applied", "dismissed"]),
        models.AiProposal.dedup_key != "")).all())

    for key, s in current.items():
        if key in decided_keys:
            continue
        row = by_key.get(key)
        if row is None:
            db.add(models.AiProposal(
                athlete_id=athlete.id, kind=s["kind"], payload=s["payload"],
                title=s["title"], reasons=s["reasons"], preview=s.get("preview") or {},
                severity=s["severity"], source="engine", dedup_key=key))
        else:
            # 条件还在但数字变了：刷新理由与严重度，不给同一件事堆卡片
            row.title = s["title"]
            row.reasons = s["reasons"]
            row.preview = s.get("preview") or {}
            row.severity = s["severity"]
            row.payload = s["payload"]

    for p in pending:
        if p.dedup_key and p.dedup_key not in current:
            p.status = "withdrawn"
            p.decided_at = models.utcnow_naive()
    db.commit()


def list_pending(db: Session, athlete: models.Athlete) -> list[dict]:
    """当前待确认的引擎建议（严重度高的在前，同严重度新的在前）。"""
    refresh_engine_proposals(db, athlete)
    rows = db.scalars(select(models.AiProposal).where(
        models.AiProposal.athlete_id == athlete.id,
        models.AiProposal.source == "engine",
        models.AiProposal.status == "pending")).all()
    rows.sort(key=lambda p: ({"high": 0, "medium": 1, "low": 2}.get(p.severity, 3),
                             p.created_at or models.utcnow_naive()), reverse=False)
    return [_serialize(p) for p in rows]


def list_history(db: Session, athlete: models.Athlete, limit: int = 20) -> list[dict]:
    """调整时间线：已决定（采纳/忽略/自动撤回）的引擎建议，新的在前。"""
    rows = db.scalars(select(models.AiProposal).where(
        models.AiProposal.athlete_id == athlete.id,
        models.AiProposal.source == "engine",
        models.AiProposal.status != "pending")
        .order_by(models.AiProposal.decided_at.desc(), models.AiProposal.id.desc())
        .limit(max(1, min(int(limit), 100)))).all()
    return [_serialize(p) for p in rows]


def week_decision_summary(db: Session, week_start: date) -> dict:
    """本周引擎决策汇总（周复盘用）：各状态条数 + 采纳的标题。

    decided_at 存的是 UTC（utcnow_naive），week_start 是本地周一：
    下限放宽 1 天吸收时区差（东八区本地周一凌晨 = UTC 还在周日），
    极端情况下会把「当地周日前夜」的决定多算进来一条，对汇总无伤。
    """
    rows = db.scalars(select(models.AiProposal).where(
        models.AiProposal.source == "engine",
        models.AiProposal.status.in_(["applied", "dismissed", "withdrawn"]),
        models.AiProposal.decided_at >= datetime.combine(week_start, time.min) - timedelta(days=1))).all()
    applied = [p for p in rows if p.status == "applied"]
    return {
        "applied": len(applied),
        "dismissed": sum(1 for p in rows if p.status == "dismissed"),
        "withdrawn": sum(1 for p in rows if p.status == "withdrawn"),
        "applied_titles": [p.title for p in applied[:5]],
    }


def get_pending(db: Session, athlete: models.Athlete, proposal_id: int) -> models.AiProposal:
    p = db.get(models.AiProposal, proposal_id)
    if p is None or p.athlete_id != athlete.id:
        from fastapi import HTTPException
        raise HTTPException(404, "提案不存在")
    if p.status != "pending":
        from fastapi import HTTPException
        raise HTTPException(409, f"提案已处理（{p.status}），不能重复处理")
    return p


def decide(db: Session, p: models.AiProposal, status: str) -> None:
    p.status = status
    p.decided_at = models.utcnow_naive()
    db.commit()
