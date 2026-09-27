"""训练后 AI 点评：训练课标记完成后，对照「计划课表 vs 实际执行」生成一段教练短评。

数字纪律（与 ai_tools 同一口径）：所有事实数字由引擎从库内数据装配进 prompt，
LLM 只负责把给定事实组织成教练口吻的中文短评，禁止引入 prompt 之外的数据；
本地模型不可用/超时时退化为确定性规则点评，功能不缺席（-weekly_recap 的
「不指责」基调在这里同样适用：只做客观归因 + 正面收尾）。

自动触发：plans.complete_workout 打卡完成后拉起后台线程调用 regenerate_async；
手动触发：POST /api/ai/workout-comment（可 force 重新生成）。
"""
from __future__ import annotations

import logging
import threading
from datetime import date, timedelta

import httpx
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from ..data import active_plan, activities_dicts, build_training_status_for
from . import ai_coach, review_guard, vdot, vocab

logger = logging.getLogger(__name__)

WEEKDAY_CN = ai_coach.WEEKDAY_CN

# 点评是短文本，给小模型的输出上限压住时长（也避免跑题长文）
MAX_COMMENT_TOKENS = 320

COMMENT_PROMPT = """你是跑者的专属 AI 教练。刚结束的一节训练课信息如下（今天是 {today}）。

【计划课】{plan_line}
【课表结构】{steps}
【实际执行】{actual_line}
【近期背景】{context_line}

请写一段 2-4 句的中文教练点评，规则：
1. 只能使用上面给出的事实与数字，禁止编造、换算或补充任何其他数字；
2. 先给完成质量的结论，最多点出一个最值得注意的观察（计划与实际的对比/心率/体感），
   最后给一条面向下一节课的具体建议；
3. 专业、正面、不指责：没有实际数据时就点评这节课在周期中的作用与执行意义，不批评用户；
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


def _actual_line(act: models.Activity | None) -> str:
    if act is None:
        return "未关联实际运动数据（用户手动标记完成）"
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
    if act.rpe:
        parts.append(f"主观强度 RPE {act.rpe}/10")
    if not parts:
        return "关联了实际活动记录，但该记录缺少距离/时长等关键字段"
    return "，".join(parts)


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


def gather_facts(db: Session, athlete: models.Athlete, wo: models.PlanWorkout) -> dict:
    week = wo.week
    act = db.get(models.Activity, wo.completed_activity_id) if wo.completed_activity_id else None
    return {
        "date": wo.date.isoformat(),
        "plan_line": _planned_line(wo, week.phase if week else None),
        "steps": _steps_line(wo),
        "actual_line": _actual_line(act),
        "context_line": _context_line(db, athlete, wo),
    }


# ---------------------------------------------------------------- 文案生成

def generate_text(facts: dict) -> str:
    """调用本地模型写点评。失败抛 ValueError（调用方转规则兜底或报错）。"""
    base = settings.ai_base_url.rstrip("/")
    bad = ai_coach.validate_base_url(base)
    if bad:
        raise ValueError(bad)
    prompt = COMMENT_PROMPT.format(today=date.today().isoformat(), **facts)
    try:
        r = httpx.post(
            f"{base}/chat/completions",
            json={"model": ai_coach._effective_model(ai_coach.probe_cached()["models"]),
                  "stream": False, "think": False, "temperature": 0.5,
                  "max_tokens": MAX_COMMENT_TOKENS,
                  "messages": [{"role": "user", "content": prompt}]},
            headers=ai_coach._auth_headers(),
            timeout=httpx.Timeout(min(settings.ai_timeout, 60.0), connect=5.0),
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"] or ""
    except Exception as e:
        raise ValueError(f"本地模型不可用：{e}") from e
    text = ai_coach._strip_think(content).strip()
    if not text:
        raise ValueError("模型没有返回内容")
    return text


def fallback_comment(facts: dict) -> str:
    """规则点评（模型不可用时的兜底）：只做客观对比 + 正面收尾，不编任何引擎没给的数字。"""
    body = f"「{facts['plan_line']}」已完成。"
    actual = facts["actual_line"]
    if actual.startswith("未关联"):
        body += "这节课在当前周期里的作用已经拿到，记得同步或关联运动记录，下次我可以对比计划和实际完成情况。"
    else:
        body += f"实际执行：{actual}。"
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
