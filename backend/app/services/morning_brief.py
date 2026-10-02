"""主动式教练晨报：每天第一次打开总览页时自动生成「今天该注意什么」。

AI 教练此前完全被动（用户问才答），数据管道（今日课/打卡建议/天气/伤病/
负荷状态/待确认提案）其实早已齐备——晨报把这些事实装配好，由本地模型按
教练人设组织成 2-4 句话，让教练每天主动说第一句话。

工程纪律（与 coach_comment 同一套）：
- 事实数字全部由引擎装配进 prompt，LLM 只组织文案，禁止引入事实之外的数据；
- 模型不可用/超时退化为规则版（source=rule），功能不缺席；
- 走 coach_comment 的 _LLM_GATE 串行排队，不打满单槽推理栈；
- 每天幂等一条（coach_briefs 表），由总览页首次请求触发后台线程生成，
  生成中的请求拿到 generating=True，前端稍后刷新即可。
"""
from __future__ import annotations

import logging
import threading
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..data import get_default_athlete
from . import vocab, weather
from .coach_persona import persona_for_athlete
from .vocab import WEEKDAY_CN

logger = logging.getLogger(__name__)

# 晨报是短文本：小模型的输出上限压住时长与跑题空间
MAX_BRIEF_TOKENS = 300

# 同一档案的晨报线程在飞时，后续总览页请求不再重复拉起
_in_flight: set[int] = set()
_in_flight_lock = threading.Lock()

MORNING_PROMPT = """你是跑者的专属 AI 教练。今天 {today}（{weekday}），以下是装配好的当日事实。

【教练风格】{persona}
【今日课】{workout_line}
【晨间打卡】{checkin_line}
【环境】{weather_line}
【伤病】{injury_line}
【负荷状态】{status_line}
【待确认提案】{proposal_line}

请写一段 2-4 句的中文教练晨报，规则：
1. 只能使用上面给出的事实与数字，禁止编造、换算或补充任何其他数字；
2. 第一句说今天练什么，中间点出今天最需要注意的一件事（打卡建议/伤病/环境/负荷
   里最有信号的那一项），最后一句收尾鼓励或具体提醒；
3. 没打卡就说「先打卡」而不是替用户假设状态；事实哪项缺失就跳过，不提；
4. 直接输出晨报正文，不要标题、不要列点、不要任何前后缀。"""


def get_today_brief(db: Session, athlete_id: int) -> models.CoachBrief | None:
    return db.scalar(select(models.CoachBrief).where(
        models.CoachBrief.athlete_id == athlete_id,
        models.CoachBrief.date == date.today()))


def ensure_today_brief_async(athlete_id: int) -> bool:
    """当天还没有晨报且没有线程在生成时，拉起后台生成线程。返回是否新拉起。"""
    with _in_flight_lock:
        if athlete_id in _in_flight:
            return False
        _in_flight.add(athlete_id)
    threading.Thread(target=_job, args=(athlete_id,),
                     name=f"coach-brief-{athlete_id}", daemon=True).start()
    return True


def _job(athlete_id: int) -> None:
    from ..db import SessionLocal

    db = SessionLocal()
    try:
        athlete = get_default_athlete(db)
        if not athlete:
            return
        generate_brief(db, athlete)
    except Exception as e:
        logger.warning("生成教练晨报失败（athlete %s）: %s", athlete_id, e)
    finally:
        with _in_flight_lock:
            _in_flight.discard(athlete_id)
        db.close()


def gather_facts(db: Session, athlete: models.Athlete) -> dict:
    """当日事实装配：全部走既有引擎，任何一项失败就跳过，不挡晨报。"""
    today = date.today()
    workout_line = "休息日或暂无排课"
    wo = db.scalars(select(models.PlanWorkout).where(
        models.PlanWorkout.athlete_id == athlete.id,
        models.PlanWorkout.date == today,
        models.PlanWorkout.status.in_(["planned", "synced"]))).all()
    if wo:
        first = sorted(wo, key=lambda w: (w.start_time, w.id))[0]
        parts = [f"{vocab.session_label(first.session_type)}「{first.title}」"]
        if first.distance_km:
            parts.append(f"{first.distance_km}km")
        if first.duration_min:
            parts.append(f"约 {round(first.duration_min)} 分钟")
        workout_line = "，".join(parts) + (f"（共 {len(wo)} 节）" if len(wo) > 1 else "")

    from .checkin_advice import build_checkin_advice
    from .baseline import objective_cap
    from ..data import body_metrics_dicts
    row = db.scalar(select(models.DailyCheckin).where(
        models.DailyCheckin.athlete_id == athlete.id,
        models.DailyCheckin.date == today))
    checkin = None
    if row:
        checkin = {"muscle_soreness": row.muscle_soreness, "energy_level": row.energy_level,
                   "sleep_quality": row.sleep_quality, "motivation": row.motivation,
                   "pain_area": row.pain_area}
    advice = build_checkin_advice(checkin, None, None,
                                  objective_cap(body_metrics_dicts(db, athlete.id, days=35)))
    checkin_line = (f"建议档位 {advice.get('level')}：{advice.get('verdict')}"
                    if advice.get("level") else "用户还没打卡，不要假设状态，提醒先打卡")

    w = weather.get_day_weather(db, athlete.id, today)
    weather_line = "暂无环境记录"
    if w and w.get("temp_c") is not None:
        weather_line = f"气温 {round(w['temp_c'])}℃"
        if w.get("humidity_pct") is not None:
            weather_line += f"、湿度 {w['humidity_pct']}%"
        if w.get("condition"):
            weather_line += f"、{w['condition']}"

    from .injury import active_injuries, return_to_run_advice
    actives = active_injuries(db, athlete.id)
    if actives:
        inj = actives[0]
        adv = return_to_run_advice(inj)
        injury_line = f"{inj['body_part']}（{inj['severity_label']}，第 {inj['days_since_start']} 天），建议档位 {adv['level'] if adv else '-'}"
    else:
        injury_line = "无恢复中的伤病"

    status_line = "暂无负荷数据"
    try:
        from ..data import build_training_status_for
        st, _ = build_training_status_for(db, athlete)
        label = (st.get("status") or {}).get("label")
        if label:
            status_line = f"训练状态「{label}」"
            if st.get("acwr") is not None:
                status_line += f"，ACWR {round(st['acwr'], 2)}"
    except Exception as e:   # 状态装配失败不挡晨报
        logger.debug("晨报状态装配失败（忽略）: %s", e)

    pending = db.scalar(select(models.AiProposal.id).where(
        models.AiProposal.athlete_id == athlete.id,
        models.AiProposal.status == "pending").order_by(models.AiProposal.id.desc()).limit(1))
    proposal_line = "有提案待你确认，看完晨报可以顺手处理" if pending else "无待确认提案"

    return {
        "weekday": WEEKDAY_CN[today.weekday()],
        "persona": persona_for_athlete(db, athlete.id),
        "workout_line": workout_line,
        "checkin_line": checkin_line,
        "weather_line": weather_line,
        "injury_line": injury_line,
        "status_line": status_line,
        "proposal_line": proposal_line,
    }


def fallback_brief(facts: dict) -> str:
    """规则版晨报（模型不可用时的兜底）：只复述引擎事实，不编任何数字。"""
    parts = [f"今天的安排：{facts['workout_line']}。"]
    if facts["checkin_line"].startswith("建议档位"):
        parts.append(f"打卡信号：{facts['checkin_line']}。")
    else:
        parts.append("先完成晨间打卡，我再按你的状态给准话。")
    if facts["injury_line"] != "无恢复中的伤病":
        parts.append(f"注意伤病：{facts['injury_line']}。")
    if facts["weather_line"] != "暂无环境记录":
        parts.append(f"环境：{facts['weather_line']}。")
    parts.append("按计划执行就是最好的积累，训练顺利。")
    return "".join(parts)


def generate_brief(db: Session, athlete: models.Athlete, force: bool = False) -> dict:
    """生成当天晨报并落库（同步调用）。已存在且不 force 时直接返回缓存。"""
    row = get_today_brief(db, athlete.id)
    if row and not force:
        return {"date": row.date.isoformat(), "content": row.content,
                "source": row.source, "cached": True}

    facts = gather_facts(db, athlete)
    used_llm = True
    try:
        from .coach_comment import generate_text
        content = generate_text(facts, prompt_template=MORNING_PROMPT,
                                max_tokens=MAX_BRIEF_TOKENS)
    except ValueError as e:
        logger.info("晨报退化为规则文案：%s", e)
        content, used_llm = fallback_brief(facts), False

    if row is None:
        row = models.CoachBrief(athlete_id=athlete.id, date=date.today())
        db.add(row)
    row.content = content
    row.source = "llm" if used_llm else "rule"
    db.commit()
    return {"date": row.date.isoformat(), "content": content,
            "source": row.source, "cached": False}


def dashboard_brief_payload(db: Session, athlete: models.Athlete) -> dict:
    """总览页用的晨报块：有就带出来，没有就触发后台生成（不阻塞请求）。

    generating 的语义是「还没有晨报可看」：线程已在飞（ensure 返回 False）时
    也要报 True，前端才会定时补拉，而不是干等 2 分钟兜底轮询。
    """
    row = get_today_brief(db, athlete.id)
    if row:
        return {"generating": False, "brief": {"content": row.content, "source": row.source}}
    ensure_today_brief_async(athlete.id)
    return {"generating": True, "brief": None}
