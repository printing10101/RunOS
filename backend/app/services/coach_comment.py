"""训练后 AI 点评：训练课标记完成后，对照「计划课表 vs 实际执行」生成一段教练短评；
未关联课表课的活动（纯同步/手动录入）也能生成活动级综合分析（训练状态 + 近期强度）。

数字纪律（与 ai_tools 同一口径）：所有事实数字由引擎从库内数据装配进 prompt，
LLM 只负责把给定事实组织成教练口吻的中文短评，禁止引入 prompt 之外的数据；
本地模型不可用/超时时退化为确定性规则点评，功能不缺席（-weekly_recap 的
「不指责」基调在这里同样适用：只做客观归因 + 正面收尾）。

自动触发：plans.complete_workout 打卡完成后拉起后台线程调用 regenerate_async；
平台同步新活动 / 手动录入活动后调用 regenerate_activity_async；
手动触发：POST /api/ai/workout-comment（关联课）与 /api/ai/activity-comment（任意活动，
服务端自动路由：关联已完成课走课表点评，其余落 activities.coach_comment）。
"""
from __future__ import annotations

import logging
import threading
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..data import active_plan, activities_dicts, build_training_status_for
from . import ai_coach, review_guard, vdot, vocab, weather
from .coach_persona import persona_for_athlete
from .vocab import WEEKDAY_CN

logger = logging.getLogger(__name__)

# 点评是短文本，给小模型的输出上限压住时长（也避免跑题长文）
MAX_COMMENT_TOKENS = 320

# 首次同步可能一次导入几十条历史活动，后台点评线程全部涌向本地模型会把
# 单槽推理栈打满：所有点评 LLM 调用在此串行排队（手动/自动共用一道闸门）
_LLM_GATE = threading.Semaphore(1)

COMMENT_PROMPT = """你是跑者的专属 AI 教练。刚结束的一节训练课信息如下（今天是 {today}）。

【教练风格】{persona}
【计划课】{plan_line}
【课表结构】{steps}
【实际执行】{actual_line}
【近期背景】{context_line}

请写一段 2-4 句的中文教练点评，规则：
1. 只能使用上面给出的事实与数字，禁止编造、换算或补充任何其他数字；
2. 先给完成质量的结论，最多点出一个最值得注意的观察（计划与实际的对比/心率/体感），
   最后给一条面向下一节课的具体建议；高温/高湿环境下的配速偏慢可如实归因于
   条件，不算执行不力；
3. 专业、正面、不指责：没有实际数据时就点评这节课在周期中的作用与执行意义，不批评用户；
4. 直接输出点评正文，不要标题、不要列点、不要任何前后缀。"""

ACTIVITY_PROMPT = """你是跑者的专属 AI 教练。一条训练记录的信息如下（今天是 {today}）。

【教练风格】{persona}
【本次训练】{activity_line}
【训练背景】{context_line}

请写一段 2-4 句的中文教练点评，规则：
1. 只能使用上面给出的事实与数字，禁止编造、换算或补充任何其他数字；
2. 结合训练背景给综合判断：先给本次完成质量的结论，最多点出一个最值得注意的
   对比或观察（和上次同类训练比/和当前负荷状态比），最后给一条具体的下一步建议；
   高温/高湿环境下的配速偏慢可如实归因于条件，不算退步；
3. 专业、正面、不指责：数据稀少时就点评这节课在周期中的作用与执行意义，不批评用户；
4. 直接输出点评正文，不要标题、不要列点、不要任何前后缀。"""


# ---------------------------------------------------------------- 事实装配（全部引擎数据）

def _planned_line(wo: models.PlanWorkout, phase: str | None) -> str:
    typ = vocab.session_label(wo.session_type)
    parts = [f"{wo.date.isoformat()}（{WEEKDAY_CN[wo.date.weekday()]}）{typ}「{wo.title}」"]
    if wo.distance_km:
        parts.append(f"计划 {wo.distance_km}km")
    if wo.duration_min:
        parts.append(f"约 {round(wo.duration_min)} 分钟")
    if phase:
        parts.append(f"所处周期阶段：{phase}")
    return "，".join(parts)


def _activity_data_line(act: models.Activity) -> str:
    """一次活动的全部数据事实（计划路径与活动路径共用）。"""
    parts = []
    if act.distance_m:
        parts.append(f"实际 {round(act.distance_m / 1000, 1)}km")
    if act.duration_sec:
        parts.append(f"{round(act.duration_sec / 60)} 分钟")
        if act.distance_m:
            sec_per_km = act.duration_sec / (act.distance_m / 1000)
            parts.append(f"平均配速 {vdot.pace_label(sec_per_km)}")
    if act.avg_hr:
        parts.append(f"平均心率 {act.avg_hr}")
    if act.max_hr:
        parts.append(f"最高心率 {act.max_hr}")
    if act.avg_cadence:
        parts.append(f"步频 {round(act.avg_cadence)}")
    if act.elevation_m:
        parts.append(f"爬升 {round(act.elevation_m)}m")
    if act.temp_c is not None:
        parts.append(f"气温 {round(act.temp_c)}℃")
    if act.weather:
        parts.append(f"天气 {act.weather}")
    if act.training_load:
        parts.append(f"训练负荷 {round(act.training_load)}")
    if act.rpe:
        parts.append(f"主观强度 RPE {act.rpe}/10")
    if not parts:
        return "该记录缺少距离/时长等关键字段"
    return "，".join(parts)


def _actual_line(act: models.Activity | None) -> str:
    if act is None:
        return "未关联实际运动数据（用户手动标记完成）"
    return _activity_data_line(act)


def _context_line(db: Session, athlete: models.Athlete, wo: models.PlanWorkout) -> str:
    """近期负荷背景，全部尽力而为：任何一项算不出来就跳过，不影响点评生成。"""
    parts = []
    try:
        # 一次拉全窗口（训练状态模型需要 200 天），近 7 天跑量从同一份结果切片；
        # 不能把 7 天数据喂给状态引擎，ACWR 的急性/慢性基数会被截断
        acts = activities_dicts(db, athlete.id, days=200)
        since7 = date.today() - timedelta(days=7)
        run_km_7d = sum(a["distance_m"] for a in acts
                        if a["sport"] == "run" and a["start_time"].date() >= since7) / 1000
        if run_km_7d:
            parts.append(f"近 7 天跑量 {round(run_km_7d, 1)}km")
        st, _ = build_training_status_for(db, athlete, acts)
        if st.get("acwr") is not None:
            parts.append(f"ACWR {round(st['acwr'], 2)}")
        score = (st.get("readiness") or {}).get("score")
        if score is not None:
            parts.append(f"训练准备度 {score}")
    except Exception as e:   # 背景数据缺失不该挡住点评
        logger.debug("点评背景数据装配失败（忽略）: %s", e)
    plan = active_plan(db)
    if plan and plan.race_date and plan.race_date >= wo.date:
        parts.append(f"距比赛日（{plan.race_date.isoformat()}）{(plan.race_date - wo.date).days} 天")
    return "；".join(parts) if parts else "暂无足够的近期数据"


def _steps_line(wo: models.PlanWorkout) -> str:
    from .ai_tools import _steps_summary
    return _steps_summary(wo.structured) if wo.structured else "（无结构化步骤）"


def _env_note(db: Session, athlete: models.Athlete, day, act_temp_c: float | None) -> str:
    """当日环境注记：活动自带气温已在数据行里时不再重复（避免两个温度打架）；
    手动天气日志补缺。没有环境数据返回空串——点评与环境修正都不编造。"""
    if act_temp_c is not None:
        return ""
    w = weather.get_day_weather(db, athlete.id, day)
    if not w or w.get("temp_c") is None:
        return ""
    parts = [f"气温 {round(w['temp_c'])}℃"]
    if w.get("condition"):
        parts.append(str(w["condition"]))
    return "当日环境：" + "，".join(parts)


def gather_facts(db: Session, athlete: models.Athlete, wo: models.PlanWorkout) -> dict:
    week = wo.week
    act = db.get(models.Activity, wo.completed_activity_id) if wo.completed_activity_id else None
    context = _context_line(db, athlete, wo)
    env = _env_note(db, athlete, wo.date, act.temp_c if act else None)
    if env:
        context = f"{context}；{env}"
    return {
        "date": wo.date.isoformat(),
        "persona": persona_for_athlete(db, athlete.id),
        "plan_line": _planned_line(wo, week.phase if week else None),
        "steps": _steps_line(wo),
        "actual_line": _actual_line(act),
        "context_line": context,
        "env_line": env,
    }


# ---------------------------------------------------------------- 活动级（未关联课表课）

def _activity_line(act: models.Activity) -> str:
    day = act.start_time.date()
    head = f"{day.isoformat()}（{WEEKDAY_CN[day.weekday()]}）"
    if act.title:
        head += f"「{act.title}」"
    return f"{head}，{_activity_data_line(act)}"


def _last_same_sport_line(db: Session, athlete: models.Athlete, act: models.Activity) -> str | None:
    """上一次同类运动（趋势对照的锚点）：日期/距离/配速/心率，全部尽力而为。"""
    prev = db.scalars(select(models.Activity).where(
        models.Activity.athlete_id == athlete.id,
        models.Activity.sport == act.sport,
        models.Activity.distance_m > 0,
        models.Activity.start_time < act.start_time,
        models.Activity.id != act.id,
    ).order_by(models.Activity.start_time.desc()).limit(1)).first()
    if not prev:
        return None
    seg = [prev.start_time.strftime("%m-%d")]
    if prev.distance_m:
        seg.append(f"{round(prev.distance_m / 1000, 1)}km")
    if prev.duration_sec and prev.distance_m:
        sec_per_km = prev.duration_sec / (prev.distance_m / 1000)
        # pace_label 吃 sec_per_km；pace_str 吃的是 m/min，喂错单位会差一个数量级
        seg.append(f"配速 {vdot.pace_label(sec_per_km)}" if act.sport == "run"
                   else f"均速 {round(3600 / sec_per_km)}km/h")
    if prev.avg_hr:
        seg.append(f"心率 {prev.avg_hr}")
    return "上一次同类训练：" + "，".join(seg)


def _activity_context_line(db: Session, athlete: models.Athlete, act: models.Activity) -> str:
    """「之前的训练强度 + 当前训练状态」，全部尽力而为：任何一项算不出来就跳过。"""
    parts = []
    try:
        # 训练状态引擎需要 200 天窗口；本次之前的 7 天跑量从同一份结果切片
        acts = activities_dicts(db, athlete.id, days=200)
        act_day = act.start_time.date()
        win_start = act_day - timedelta(days=7)
        prev = [a for a in acts if a["sport"] == "run"
                and win_start <= a["start_time"].date() < act_day]
        km7 = sum(a["distance_m"] for a in prev) / 1000
        if km7:
            parts.append(f"本次之前 7 天跑量 {round(km7, 1)}km（{len(prev)} 次）")
        st, _ = build_training_status_for(db, athlete, acts)
        if st.get("acwr") is not None:
            parts.append(f"当前 ACWR {round(st['acwr'], 2)}")
        score = (st.get("readiness") or {}).get("score")
        if score is not None:
            parts.append(f"训练准备度 {score}")
    except Exception as e:   # 背景数据缺失不该挡住点评
        logger.debug("活动点评背景装配失败（忽略）: %s", e)
    last = _last_same_sport_line(db, athlete, act)
    if last:
        parts.append(last)
    try:
        plan = active_plan(db)
        if plan and plan.race_date and plan.race_date >= act.start_time.date():
            days = (plan.race_date - act.start_time.date()).days
            parts.append(f"距比赛日（{plan.race_date.isoformat()}）{days} 天")
    except Exception as e:
        logger.debug("活动点评比赛日装配失败（忽略）: %s", e)
    return "；".join(parts) if parts else "暂无足够的近期数据"


def gather_activity_facts(db: Session, athlete: models.Athlete, act: models.Activity) -> dict:
    context = _activity_context_line(db, athlete, act)
    env = _env_note(db, athlete, act.start_time.date(), act.temp_c)
    if env:
        context = f"{context}；{env}"
    return {
        "date": act.start_time.date().isoformat(),
        "persona": persona_for_athlete(db, athlete.id),
        "activity_line": _activity_line(act),
        "context_line": context,
        "env_line": env,
    }


# ---------------------------------------------------------------- 文案生成

def generate_text(facts: dict, prompt_template: str = COMMENT_PROMPT,
                  max_tokens: int = MAX_COMMENT_TOKENS) -> str:
    """调用本地模型写点评。失败抛 ValueError（调用方转规则兜底或报错）。

    max_tokens 供不同长度的产物覆写（晨报 300、点评 320）；走 _LLM_GATE 串行排队，
    首次同步的批量点评不会打满单槽推理栈。
    """
    prompt = prompt_template.format(today=date.today().isoformat(), **facts)
    return ai_coach.complete_text(prompt, max_tokens=max_tokens, gate=_LLM_GATE)


def fallback_comment(facts: dict) -> str:
    """规则点评（模型不可用时的兜底）：只做客观对比 + 正面收尾，不编任何引擎没给的数字。"""
    body = f"「{facts['plan_line']}」已完成。"
    actual = facts["actual_line"]
    if actual.startswith("未关联"):
        body += "这节课在当前周期里的作用已经拿到，记得同步或关联运动记录，下次我可以对比计划和实际完成情况。"
    else:
        body += f"实际执行：{actual}。"
    if facts.get("env_line"):
        body += f"{facts['env_line']}。环境条件会影响配速表现，对比时把它算进去。"
    body += "按计划执行就是最好的积累，注意休息恢复，准备好下一节课。"
    return body


def generate_for_workout(db: Session, athlete: models.Athlete, workout_id: int,
                         force: bool = False) -> dict:
    """为已完成的训练课生成点评并落库。同步调用（本地模型短输出，秒级）。"""
    wo = db.get(models.PlanWorkout, workout_id)
    if not wo or wo.athlete_id != athlete.id:
        raise ValueError("训练课不存在")
    if wo.status != "completed":
        raise ValueError("该课尚未标记完成，完成打卡后才会生成点评")
    if wo.coach_comment and not force:
        return {"ok": True, "cached": True, "comment": wo.coach_comment,
                "at": wo.coach_comment_at.isoformat() if wo.coach_comment_at else None}

    facts = gather_facts(db, athlete, wo)
    used_llm = True
    try:
        comment = generate_text(facts)
        review_guard.check(comment, facts)   # 数字核验：引用事实之外的数字就退规则版
    except ValueError as e:
        logger.info("AI 点评退化为规则文案：%s", e)
        comment, used_llm = fallback_comment(facts), False

    wo.coach_comment = comment
    wo.coach_comment_at = models.utcnow_naive()
    db.commit()
    return {"ok": True, "cached": False, "source": "llm" if used_llm else "rule",
            "comment": comment,
            "at": wo.coach_comment_at.isoformat() if wo.coach_comment_at else None}


def regenerate_async(workout_id: int) -> None:
    """打卡完成后自动生成（后台线程，尽力而为：失败只落日志，不影响打卡请求）。"""
    def _job():
        from ..data import get_default_athlete
        from ..db import SessionLocal

        db = SessionLocal()
        try:
            wo = db.get(models.PlanWorkout, workout_id)
            if not wo or wo.status != "completed" or wo.coach_comment:
                return   # 已被手动生成过/已取消完成：不覆盖
            athlete = get_default_athlete(db)
            if not athlete:
                return
            generate_for_workout(db, athlete, workout_id, force=False)
        except Exception as e:
            logger.warning("自动生成训练点评失败（workout %s）: %s", workout_id, e)
        finally:
            db.close()

    threading.Thread(target=_job, name=f"coach-comment-{workout_id}", daemon=True).start()


# ---------------------------------------------------------------- 活动级点评（每条记录都有分析）

def _linked_workout(db: Session, activity_id: int) -> models.PlanWorkout | None:
    return db.scalars(select(models.PlanWorkout).where(
        models.PlanWorkout.completed_activity_id == activity_id)).first()


def fallback_activity_comment(facts: dict) -> str:
    """活动级规则点评（模型不可用时的兜底）：只复述引擎事实 + 正面收尾，不编数字。"""
    body = f"「{facts['activity_line']}」已完成。"
    if facts.get("env_line"):
        body += f"{facts['env_line']}。环境条件会影响配速表现，对比时把它算进去。"
    elif facts["context_line"] != "暂无足够的近期数据":
        body += f"训练背景：{facts['context_line']}。"
    body += "把这节课记进积累里，注意休息恢复，准备好下一节课。"
    return body


def generate_for_activity(db: Session, athlete: models.Athlete, activity_id: int,
                          force: bool = False) -> dict:
    """任意活动记录的 AI 教练点评（同步调用，秒级）。

    路由：关联了已完成的课表课 → 走「计划 vs 实际」的课表点评（处方语境更足，
    落 plan_workouts.coach_comment）；其余 → 活动级综合分析（训练状态 + 近期
    强度对照），落 activities.coach_comment。
    """
    act = db.get(models.Activity, activity_id)
    if not act or act.athlete_id != athlete.id:
        raise ValueError("活动不存在")
    linked = _linked_workout(db, activity_id)
    if linked and linked.status == "completed":
        r = generate_for_workout(db, athlete, linked.id, force=force)
        r["target"] = "workout"
        return r
    if act.coach_comment and not force:
        return {"ok": True, "cached": True, "target": "activity",
                "source": "cached", "comment": act.coach_comment,
                "at": act.coach_comment_at.isoformat() if act.coach_comment_at else None}

    facts = gather_activity_facts(db, athlete, act)
    used_llm = True
    try:
        comment = generate_text(facts, prompt_template=ACTIVITY_PROMPT)
        review_guard.check(comment, facts)   # 引用事实之外的数字就退规则版
    except ValueError as e:
        logger.info("活动点评退化为规则文案：%s", e)
        comment, used_llm = fallback_activity_comment(facts), False

    act.coach_comment = comment
    act.coach_comment_at = models.utcnow_naive()
    db.commit()
    return {"ok": True, "cached": False, "target": "activity",
            "source": "llm" if used_llm else "rule", "comment": comment,
            "at": act.coach_comment_at.isoformat()}


def regenerate_activity_async(activity_id: int) -> None:
    """同步/手动录入新活动后自动生成（后台线程，尽力而为，失败只落日志）。"""
    def _job():
        from ..data import get_default_athlete
        from ..db import SessionLocal

        db = SessionLocal()
        try:
            act = db.get(models.Activity, activity_id)
            # 已有点评不覆盖；空壳记录（无距离无时长的手填占位）没有可分析的数据
            if not act or act.coach_comment:
                return
            if not act.duration_sec and not act.distance_m:
                return
            athlete = get_default_athlete(db)
            if not athlete:
                return
            generate_for_activity(db, athlete, activity_id, force=False)
        except Exception as e:
            logger.warning("自动生成活动点评失败（activity %s）: %s", activity_id, e)
        finally:
            db.close()

    threading.Thread(target=_job, name=f"activity-comment-{activity_id}", daemon=True).start()
