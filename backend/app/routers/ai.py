"""AI 教练：本地大模型对话（工具调用 + 提案确认制）。

路线：
- GET  /api/ai/status              本地模型服务状态（前端据此显示连接指引）
- GET  /api/ai/conversations       会话列表（对话持久化，刷新/重开不丢）
- POST /api/ai/conversations       新建会话
- GET  /api/ai/conversations/{id}  会话消息回放
- DELETE /api/ai/conversations/{id} 删除会话
- POST /api/ai/chat                SSE 对话流（数据类工具实时查询，提议类工具出提案卡片；
                                   带 conversation_id 时问答自动持久化）
- POST /api/ai/proposals/apply     应用 AI 提案（用户在前端点确认后调用；服务端重新校验）
- POST /api/ai/workout-comment     训练后 AI 点评（完成打卡后生成/重新生成，模型不可用时规则兜底）
- POST /api/ai/plan/from-text      自由文本目标 → 引擎可行性预检（不落库）
- POST /api/ai/plan/confirm        确认后按参数建目标并生成计划（复用 plans 路由的落库逻辑）
"""
from __future__ import annotations

import json
from datetime import date as _date

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..config import settings
from ..data import build_prediction
from ..db import get_db
from ..services import ai_coach, ai_tools, lm_manager, planner
from .deps import require_athlete

router = APIRouter(prefix="/api/ai", tags=["ai"])


@router.get("/status")
def status():
    return ai_coach.probe()


# ---------------------------------------------------------------- 模型切换（本地模型选型）
#
# 两条路径：
#   - 服务在线模型（/v1/models 里挂着的）：改运行时选型即生效，下一个请求就用新模型；
#   - 本地 GGUF（ai_model_path 同目录扫描）：仅限平台托管端口（1234）且进程归平台管，
#     停旧进程 → 按新路径重启 llama-server，前端轮询 /api/ai/status 等待就绪。
# 选型同时持久化到 AppSetting，启动时 apply_overrides 回放，跨重启生效。

@router.get("/models")
def model_catalog():
    """可选模型目录：服务在线模型（立即生效）+ 本地 GGUF（切换需重载，约 1-2 分钟）。"""
    probe = ai_coach.probe()
    endpoint_ids = [m for m in (probe.get("models") or []) if m]
    selectable, seen = [], set(endpoint_ids)
    for mid in endpoint_ids:
        selectable.append({"id": mid, "origin": "endpoint",
                           "ready": True, "reload_required": False})
    for g in lm_manager.local_gguf_models():
        # 当前已加载文件用运行别名作 id（正是 /v1/models 里那个名字），避免同一模型出两条
        is_current = g["path"] == (settings.ai_model_path or "")
        gid = settings.ai_model if is_current else g["id"]
        if gid in seen:
            continue
        seen.add(gid)
        selectable.append({"id": gid, "origin": "gguf", "path": g["path"],
                           "ready": is_current and probe.get("reachable", False),
                           "reload_required": True})
    allowed, allow_reason = lm_manager.gguf_switch_allowed()
    return {
        "current": settings.ai_model,
        "base_url": probe.get("base_url"),
        "reachable": probe.get("reachable", False),
        "selectable": selectable,
        "gguf_switch_allowed": allowed,
        "gguf_switch_block_reason": allow_reason,
        "gguf_dir": str(lm_manager.gguf_dir()),
    }


def _set_app_setting(db: Session, key: str, value: str) -> None:
    row = db.get(models.AppSetting, key)
    if row:
        row.value = value
    else:
        db.add(models.AppSetting(key=key, value=value))
    db.commit()


@router.post("/models/select")
def select_model(data: schemas.AiModelSelectIn, db: Session = Depends(get_db)):
    target = data.model.strip()
    probe = ai_coach.probe()
    endpoint_ids = {m for m in (probe.get("models") or []) if m}

    # 在线模型：改运行时选型，立即生效
    if target in endpoint_ids:
        if target == settings.ai_model:
            return {"changed": False, "reloading": False, "model": target,
                    "message": "该模型正在使用中"}
        settings.ai_model = target
        _set_app_setting(db, lm_manager.OVERRIDE_MODEL_KEY, target)
        return {"changed": True, "reloading": False, "model": target}

    # 本地 GGUF：校验托管前提后停旧起新
    entry = next((g for g in lm_manager.local_gguf_models() if g["id"] == target), None)
    if entry is None:
        raise HTTPException(404, "模型目录里没有这个模型，请刷新后重试")
    allowed, reason = lm_manager.gguf_switch_allowed()
    if not allowed:
        raise HTTPException(400, reason)
    if entry["path"] == (settings.ai_model_path or "") and target == settings.ai_model \
            and probe.get("reachable", False):
        # 服务在线且挂的就是这个文件才是「正在使用」；服务已宕掉时允许借重选拉起
        return {"changed": False, "reloading": False, "model": target,
                "message": "该模型正在使用中"}
    settings.ai_model = target
    settings.ai_model_path = entry["path"]
    _set_app_setting(db, lm_manager.OVERRIDE_MODEL_KEY, target)
    _set_app_setting(db, lm_manager.OVERRIDE_MODEL_PATH_KEY, entry["path"])
    lm_manager.switch_model(entry["path"])
    return {"changed": True, "reloading": True, "model": target,
            "message": "正在重载模型（首次载入约 1-2 分钟）"}


# ---------------------------------------------------------------- 会话管理（对话持久化）

def _conversation_dict(c: models.AiConversation) -> dict:
    return {"id": c.id, "title": c.title,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
            "message_count": len(c.messages)}


def _own_conversation(db: Session, athlete: models.Athlete, cid: int) -> models.AiConversation:
    conv = db.get(models.AiConversation, cid)
    if not conv or conv.athlete_id != athlete.id:
        raise HTTPException(404, "会话不存在")
    return conv


@router.get("/conversations")
def list_conversations(db: Session = Depends(get_db)):
    """会话列表（最近 50 条，按更新时间倒序）。消息数用聚合查询取，
    避免 len(c.messages) 对每个会话懒加载一次的 N+1。"""
    athlete = require_athlete(db)
    rows = db.scalars(select(models.AiConversation).where(
        models.AiConversation.athlete_id == athlete.id).order_by(
        models.AiConversation.updated_at.desc(), models.AiConversation.id.desc()
    ).limit(50)).all()
    counts = dict(db.execute(
        select(models.AiMessage.conversation_id, func.count(models.AiMessage.id))
        .join(models.AiConversation,
              models.AiMessage.conversation_id == models.AiConversation.id)
        .where(models.AiConversation.athlete_id == athlete.id)
        .group_by(models.AiMessage.conversation_id)).all())
    return {"conversations": [{"id": c.id, "title": c.title,
                               "updated_at": c.updated_at.isoformat() if c.updated_at else None,
                               "message_count": counts.get(c.id, 0)} for c in rows]}


@router.post("/conversations")
def create_conversation(data: schemas.AiConversationCreateIn, db: Session = Depends(get_db)):
    athlete = require_athlete(db)
    conv = models.AiConversation(athlete_id=athlete.id, title=(data.title or "").strip() or "新对话")
    db.add(conv)
    db.commit()
    return _conversation_dict(conv)


@router.get("/conversations/{cid}")
def get_conversation(cid: int, db: Session = Depends(get_db)):
    """拉取某会话的全部消息（tools/proposals 原样回放，前端按直播时的结构渲染）。"""
    athlete = require_athlete(db)
    conv = _own_conversation(db, athlete, cid)
    return {"id": conv.id, "title": conv.title,
            "messages": [{"role": m.role, "content": m.content,
                          "tools": m.tools or [], "proposals": m.proposals or []}
                         for m in conv.messages]}


@router.delete("/conversations/{cid}")
def delete_conversation(cid: int, db: Session = Depends(get_db)):
    athlete = require_athlete(db)
    conv = _own_conversation(db, athlete, cid)
    db.delete(conv)
    db.commit()
    return {"ok": True}


@router.post("/chat")
def chat(data: schemas.AiChatIn, db: Session = Depends(get_db)):
    """SSE 流：data: {"type": "delta|tool|proposals|error|done", ...}

    两种调用模式：
    - 会话模式（conversation_id + message）：历史从库内会话加载，问答自动持久化
      （用户消息在流开始前落库，助手消息在流走完后落库；客户端中途断开则不保存助手消息）；
    - 无状态模式（仅 messages，旧契约）：行为与历史版本一致，不落库。
    """
    athlete = require_athlete(db)

    conv = None
    history = data.messages
    if data.conversation_id is not None:
        conv = _own_conversation(db, athlete, data.conversation_id)
        user_text = (data.message or "").strip()
        if not user_text:
            raise HTTPException(400, "缺少消息内容")
        rows = db.scalars(select(models.AiMessage).where(
            models.AiMessage.conversation_id == conv.id).order_by(models.AiMessage.id)).all()
        history = [{"role": m.role, "content": m.content} for m in rows]
        # 本轮用户消息必须进历史：它是在下面才落库的，若 history 只取「库内已有消息」，
        # 模型这一轮看到的就是上一轮的问题；新会话首条更糟——history 为空，模型只拿到
        # 系统提示词（而提示词要求「涉及个人数据必须先调用工具」），于是它只能按 schema
        # 顺序扫零参数工具，最后撞满轮数只吐一句报错。这正是 2026-09-13 复盘的那次失败。
        history.append({"role": "user", "content": user_text})
        db.add(models.AiMessage(conversation_id=conv.id, role="user", content=user_text))
        if not any(m.role == "user" for m in rows):
            conv.title = user_text[:24] or conv.title
        db.commit()

    def gen():
        content_acc: list[str] = []
        tools_acc: list[dict] = []
        proposals_acc: list[dict] = []
        for event in ai_coach.chat_stream(db, history):
            if event["type"] == "delta":
                content_acc.append(event.get("text") or "")
            elif event["type"] == "tool":
                tools_acc.append({"label": event.get("label") or event.get("name") or "查询数据",
                                  "ok": event.get("ok", True)})
            elif event["type"] == "proposals":
                proposals_acc.extend(event.get("items") or [])
            yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
        # 生成器被客户端中断时不会执行到这里，因此只持久化完整回答
        if conv is not None and (content_acc or tools_acc or proposals_acc):
            db.add(models.AiMessage(conversation_id=conv.id, role="assistant",
                                    content="".join(content_acc), tools=tools_acc,
                                    proposals=proposals_acc))
            conv.updated_at = models.utcnow_naive()
            db.commit()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/workout-comment")
def workout_comment(data: schemas.AiWorkoutCommentIn, db: Session = Depends(get_db)):
    """生成（或 force 重新生成）一节已完成训练课的 AI 教练点评。

    数字全部由引擎装配，本地模型只负责组织成短评；模型不可用时服务端
    自动退化为规则点评，端点始终有可用输出。
    """
    athlete = require_athlete(db)
    from ..services import coach_comment
    try:
        return coach_comment.generate_for_workout(db, athlete, data.workout_id, force=data.force)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/assessment-interpretation")
def assessment_interpretation(db: Session = Depends(get_db)):
    """评分页 AI 解读：五维得分/短板/目标差距/趋势全部由引擎装配进 prompt，
    本地模型只组织成解读文案；模型不可用时自动退化为规则解读，端点始终有输出。
    解读不落库（评估随时可重算，重新点按钮即重新生成）。"""
    athlete = require_athlete(db)
    from ..services import assessment_review
    return assessment_review.generate_for_athlete(db, athlete)


@router.post("/weekly-recap-review")
def weekly_recap_review(db: Session = Depends(get_db)):
    """总览页「本周复盘」卡的 AI 点评：weekly_recap 引擎事实 → LLM 组织 → 规则兜底。
    点评不落库（复盘每周滚动刷新，每次点按钮重新生成）。"""
    require_athlete(db)
    from ..services import recap_review
    return recap_review.generate_for_recap(db)


@router.post("/diet-review")
def diet_review(db: Session = Depends(get_db)):
    """饮食页 AI 分析：diet_analysis_payload 引擎事实 → LLM 组织 → 规则兜底。不落库。"""
    athlete = require_athlete(db)
    from ..services import diet_review
    return diet_review.generate_for_diet(db, athlete)


@router.post("/prediction-review")
def prediction_review(db: Session = Depends(get_db)):
    """预测页 AI 解读：build_prediction/riegel_calibration 引擎事实 → LLM 组织 → 规则兜底。不落库。"""
    athlete = require_athlete(db)
    from ..services import prediction_review
    return prediction_review.generate_for_prediction(db, athlete)


@router.post("/plan-review")
def plan_review(db: Session = Depends(get_db)):
    """计划页 AI 课表分析：计划结构 + 前瞻负荷推演（load_project 同源）+ 当前水平
    评估 → LLM 组织 → 规则兜底。事实随 active 计划（高驰镜像或本地）即时读取，
    分析不落库，点按钮即按最新课表重新生成。"""
    require_athlete(db)
    from ..services import plan_review
    return plan_review.generate_for_plan(db)


def _checked_or_400(checked: dict) -> dict:
    """工具层重检不通过 → 直接 400（服务端闸门，LLM 无法绕过）。"""
    if not checked["ok"]:
        raise HTTPException(400, "；".join(checked["reasons"]))
    return checked


def _require(value, msg: str):
    if value is None:
        raise HTTPException(400, msg)
    return value


def _apply_profile_update(db: Session, athlete: models.Athlete, data) -> dict:
    if not data.changes:
        raise HTTPException(400, "缺少变更字段")
    # 与工具层同闸门：confirm_anomaly=True 只跳过异常追问，范围/交叉校验照常重检
    checked = ai_tools.check_profile_update(athlete, data.changes, confirm_anomaly=True)
    _checked_or_400(checked)
    for field, val in (checked.get("_internal", {}).get("applied") or {}).items():
        setattr(athlete, field, val)
    # 标记为用户确认过的值：画像解析层不该再用估计值把它覆盖回去
    from ..services import profile
    profile.mark_user_fields(athlete, list(
        (checked.get("_internal", {}).get("applied") or {}).keys()))
    db.commit()
    return {"ok": True, "changes": checked["proposal"]["changes"]}


def _apply_goal_update(db: Session, athlete: models.Athlete, data) -> dict:
    if not data.changes:
        raise HTTPException(400, "缺少变更字段")
    # 先以当前活动目标为基线重检，再整体替换（旧目标归档，与 AI 建计划流程口径一致）
    checked = ai_tools.check_goal_update(db, athlete, data.changes)
    _checked_or_400(checked)
    for old in db.scalars(select(models.Goal).where(
            models.Goal.athlete_id == athlete.id, models.Goal.status == "active")).all():
        old.status = "paused"
    vals = checked["_internal"]["new_values"]
    goal = models.Goal(athlete_id=athlete.id, status="active", priority="primary",
                       race_type=vals["race_type"], target_time_sec=vals["target_time_sec"],
                       target_date=vals["target_date"], target_label=vals["target_label"])
    db.add(goal)
    db.commit()
    return {"ok": True, "goal_id": goal.id, "target_label": goal.target_label}


def _apply_move_workout(db: Session, athlete: models.Athlete, data) -> dict:
    _require(data.workout_id, "缺少 workout_id")
    _require(data.target_weekday, "缺少 target_weekday")
    checked = ai_tools.check_move(db, athlete, data.workout_id, data.target_weekday)
    _checked_or_400(checked)
    wo = db.get(models.PlanWorkout, data.workout_id)
    proposal = checked["proposal"]
    wo.date = _date.fromisoformat(proposal["to"]["date"])
    wo.start_time = proposal["to"]["start_time"]
    wo.description = (wo.description + "\n" if wo.description else "") + \
        f"[AI 提案·用户确认] 由 {proposal['from']['date']} 挪至 {proposal['to']['date']}"
    db.commit()
    return {"ok": True, "workout_id": wo.id, "date": wo.date.isoformat()}


def _apply_quality_adjustment(db: Session, athlete: models.Athlete, data) -> dict:
    _require(data.workout_id, "缺少 workout_id")
    _require(data.target_reps, "缺少 target_reps")
    checked = ai_tools.check_quality_adjustment(db, athlete, data.workout_id, data.target_reps)
    _checked_or_400(checked)
    internal = checked.get("_internal") or {}
    wo = db.get(models.PlanWorkout, data.workout_id)
    wo.structured = internal["new_steps"]
    wo.distance_km = internal["new_km"]
    wo.duration_min = internal["new_dur"]
    title_reps = f"{data.target_reps} 组"
    wo.description = (wo.description + "\n" if wo.description else "") + \
        f"[AI 提案·用户确认] 主课组数调整为 {title_reps}"
    db.commit()
    return {"ok": True, "workout_id": wo.id, "distance_km": wo.distance_km,
            "duration_min": wo.duration_min}


def _apply_easy_replacement(db: Session, athlete: models.Athlete, data) -> dict:
    _require(data.workout_id, "缺少 workout_id")
    checked = ai_tools.check_easy_replacement(db, athlete, data.workout_id)
    _checked_or_400(checked)
    internal = checked.get("_internal") or {}
    wo = db.get(models.PlanWorkout, data.workout_id)
    wo.structured = internal["new_steps"]
    wo.distance_km = internal["new_km"]
    wo.duration_min = internal["new_dur"]
    wo.session_type = "easy"
    wo.title = internal["new_title"]
    wo.description = (wo.description + "\n" if wo.description else "") + \
        "[AI 提案·用户确认] 强度课替换为轻松跑"
    db.commit()
    return {"ok": True, "workout_id": wo.id, "title": wo.title,
            "distance_km": wo.distance_km, "duration_min": wo.duration_min}


def _apply_add_workout(db: Session, athlete: models.Athlete, data) -> dict:
    _require(data.target_date, "缺少加课日期")
    checked = ai_tools.check_add_workout(db, athlete, data.target_date.isoformat())
    _checked_or_400(checked)
    internal = checked.get("_internal") or {}
    wo = models.PlanWorkout(
        week_id=internal["week_id"], athlete_id=athlete.id, date=internal["date"],
        start_time=internal["start_time"], session_type="easy", title=internal["title"],
        description="[AI 提案·用户确认] 追加轻松跑",
        distance_km=internal["km"], duration_min=internal["dur"],
        structured=internal["steps"])
    db.add(wo)
    db.commit()
    return {"ok": True, "workout_id": wo.id, "date": wo.date.isoformat()}


def _apply_skip_workout(db: Session, athlete: models.Athlete, data) -> dict:
    _require(data.workout_id, "缺少 workout_id")
    checked = ai_tools.check_skip_workout(db, athlete, data.workout_id)
    _checked_or_400(checked)
    wo = db.get(models.PlanWorkout, data.workout_id)
    wo.status = "skipped"
    wo.description = (wo.description + "\n" if wo.description else "") + \
        "[AI 提案·用户确认] 跳过本节课"
    db.commit()
    return {"ok": True, "workout_id": wo.id, "status": wo.status}


# 提案类型 → 应用函数。新增提案类型时在这里登记一个处理函数即可，
# 校验统一走各 handler 里的 ai_tools.check_*（两段式的「apply 端点重检」）。
_PROPOSAL_APPLIERS = {
    "profile_update": _apply_profile_update,
    "goal_update": _apply_goal_update,
    "move_workout": _apply_move_workout,
    "quality_adjustment": _apply_quality_adjustment,
    "easy_replacement": _apply_easy_replacement,
    "add_workout": _apply_add_workout,
    "skip_workout": _apply_skip_workout,
}


@router.post("/proposals/apply")
def apply_proposal(data: schemas.AiProposalApplyIn, db: Session = Depends(get_db)):
    """应用 AI 提案。带 LLM 无法绕过的闸门：服务端按当前库内状态重新校验后写入。"""
    athlete = require_athlete(db)
    handler = _PROPOSAL_APPLIERS.get(data.kind)
    if handler is None:
        raise HTTPException(400, "未知提案类型")
    return handler(db, athlete, data)


@router.post("/plan/from-text")
def plan_from_text(data: schemas.AiPlanFromTextIn, db: Session = Depends(get_db)):
    """自由文本 → 结构化目标参数 + 引擎可行性预检（不落库，等用户确认）。"""
    require_athlete(db)
    text = (data.text or "").strip()
    if not text:
        raise HTTPException(400, "请描述你的目标")
    try:
        params = ai_coach.extract_goal_params(text)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    athlete = require_athlete(db)
    pred = build_prediction(db, athlete.id)
    if not pred.current_vdot:
        raise HTTPException(400, "暂无足够训练数据估算当前 VDOT，请先同步/录入近期的跑步记录")
    from ..data import activities_dicts
    from ..services import evaluator
    acts = activities_dicts(db, athlete.id)
    talent = evaluator.eval_talent(pred, acts, _date.today().year - athlete.birth_year,
                                   athlete.sex, athlete.training_age_years,
                                   athlete.weight_kg, athlete.height_cm).get("score")
    if not talent:
        raise HTTPException(400, "训练数据不足以评估天赋响应速度，请先积累更多训练记录")
    slots = ai_tools.active_slots(db, athlete.id)
    if not slots:
        raise HTTPException(400, "日程管理中还没有可训练时段，请先到「日程管理」添加")
    from datetime import date as _d2
    from datetime import timedelta as _td
    start = _d2.today()
    end = (_d2.fromisoformat(params["target_date"]) if params.get("target_date")
           else start + _td(weeks=16))
    weeks = max(4, min(30, (end - start).days // 7))
    feasibility = planner.assess_feasibility(
        params["race_type"], params.get("target_time_sec"), pred.current_vdot, talent, weeks, slots)
    return {"params": params, "feasibility": feasibility,
            "current_vdot": pred.current_vdot, "note":
            "确认后将创建目标并生成完整周期化计划（旧计划自动归档）"}


@router.post("/plan/confirm")
def plan_confirm(data: schemas.AiPlanConfirmIn, db: Session = Depends(get_db)):
    """用户确认自由文本解析结果：建目标（旧活动目标暂停）并生成计划。"""
    from .plans import _create_plan_for_goal

    athlete = require_athlete(db)
    for old in db.scalars(select(models.Goal).where(
            models.Goal.athlete_id == athlete.id, models.Goal.status == "active")).all():
        old.status = "paused"
    goal = models.Goal(
        athlete_id=athlete.id, race_type=data.race_type,
        target_time_sec=data.target_time_sec, target_label=data.target_label,
        target_date=data.target_date, status="active",
    )
    db.add(goal)
    try:
        # 目标与计划同会话落库：计划生成失败（如日程为空）时整体回滚，
        # 不残留一个没有计划的活动目标（反复确认会累积 paused 目标）
        result = _create_plan_for_goal(db, athlete, goal, data.start_date, data.weekly_km_peak, "")
    except Exception:
        db.rollback()
        raise
    result["goal_id"] = goal.id
    return result
