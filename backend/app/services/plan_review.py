"""课表前瞻 AI 分析：计划页「AI 课表分析」卡的人工智能叙事层。

回答两个问题：**照这份课表练会不会过度训练**、**课表符不符合当前水平**。
事实全部由既有引擎装配，本模块不做新口径：计划结构来自 plan_weeks/plan_workouts
（高驰镜像或本地计划，随 active 计划自动切换），当前水平来自 evaluator.build_evaluation
与 weekly_recap 引擎，未来负荷推演复用 load_project.project_load（前瞻 PMC，与
训练状态页 forecast 完全同源）。LLM 只组织文案，模型不可用退化为规则点评，
端点永远有输出；分析不落库，课表每次随同步更新，点按钮即按最新状态重新生成
（assessment_review / recap_review 同一套纪律）。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from ..data import active_plan, build_evaluation, get_default_athlete, planned_workouts
from . import ai_coach, review_guard
from .vocab import is_hard_or_long

logger = logging.getLogger(__name__)

MAX_REVIEW_TOKENS = 500

REVIEW_PROMPT = """你是跑者的专属 AI 教练。以下是平台引擎算出的课表前瞻事实（今天是 {today}），请判断这份课表会不会练过头、和跑者当前水平是否匹配。

【课表】{plan_name}，共 {weeks} 周（至 {plan_end}）
【周安排】{week_lines}
【单课之最】最长 {longest_km}km；单周质量/长距离课最多 {hard_max} 节
【当前水平】VDOT {vdot}，近四周周均 {weekly_km}km；体能 {fitness} / 疲劳 {fatigue} / 形态 {form}；ACWR {acwr}；准备度 {readiness}
【未来 12 周负荷推演】{forecast_verdict}；引擎风险提示：{forecast_warnings}

写作规则：
1. 只能使用上面给出的事实与数字，禁止编造、换算或补充任何其他数字；
2. 第一句必须给明确总判：这份课表对当前水平是「合理 / 偏激进 / 偏保守」，并直接回答照它练有没有过度训练风险；
3. 点名具体证据：周跑量环比超过 +10% 的周、过深的形态谷、ACWR 异常、连续高强度周，有哪个说哪个；
4. 最后给 1-2 条具体执行建议（哪一周该注意什么、要不要加恢复周）；
5. 简体中文，总长 250 字以内；直接输出正文，不要标题和 markdown 符号。"""


def gather_facts(db: Session) -> dict:
    """装配课表结构 + 当前水平 + 前瞻推演三块事实（全部引擎口径）。"""
    athlete = get_default_athlete(db)
    if not athlete:
        return {"empty": True, "reasons": ["还没有跑者档案"]}
    plan = active_plan(db)
    if not plan:
        return {"empty": True, "reasons": ["暂无有效训练计划"]}

    today = date.today()
    weeks = db.scalars(select(models.PlanWeek).where(
        models.PlanWeek.plan_id == plan.id).order_by(models.PlanWeek.week_index)).all()
    week_lines, ramps = [], []
    hard_max, longest_km, prev_km, plan_end = 0, 0.0, None, plan.race_date
    for w in weeks:
        if w.start_date + timedelta(days=6) < today:
            continue  # 已结束的周不参与爬升判断
        hard = sum(1 for wo in w.workouts if is_hard_or_long(wo.session_type))
        hard_max = max(hard_max, hard)
        km = round(sum(wo.distance_km or 0 for wo in w.workouts), 1)
        line = f"第{w.week_index}周{km or w.target_km}km/{len(w.workouts)}课/强度{hard}节"
        if prev_km:
            pct = round((km - prev_km) / prev_km * 100)
            ramps.append(pct)
            line += f"(环比{pct:+d}%)"
        week_lines.append(line)
        prev_km = km
        plan_end = max(plan_end, w.start_date + timedelta(days=6))
        longest_km = max(longest_km, max((wo.distance_km or 0 for wo in w.workouts), default=0))
    if not week_lines:
        return {"empty": True, "reasons": ["课表里没有未来的课次"]}

    ev = build_evaluation(db, athlete)
    forecast_verdict, forecast_warnings = "引擎数据不足，无法推演", "无"
    min_form = None
    st_proj, st_full = _training_status(db, athlete)
    if st_proj is not None:
        planned, planned_race = planned_workouts(db, plan.id)
        from .load_project import project_load
        ad = {"max_hr": athlete.max_hr, "resting_hr": athlete.resting_hr, "sex": athlete.sex}
        proj = project_load(st_proj["daily"], planned, ad, fitness=st_proj["fitness"],
                            fatigue=st_proj["fatigue"], race_date=planned_race or plan.race_date)
        forecast_verdict = proj["verdict"]
        forecast_warnings = "；".join(proj["warnings"]) if proj["warnings"] else "无"
        future_tsb = [s["tsb"] for s in proj["series"] if s.get("planned")]
        min_form = min(future_tsb) if future_tsb else None

    return {
        "empty": False,
        "plan_name": plan.name,
        "weeks": len(week_lines),
        "plan_end": plan_end.isoformat(),
        "week_lines": "；".join(week_lines),
        "longest_km": round(longest_km, 1),
        "hard_max": hard_max,
        "ramp_over10": sum(1 for p in ramps if p > 10),
        "ramp_max": max(ramps) if ramps else 0,
        "vdot": ev.get("current_vdot") or "-",
        "weekly_km": round(ev.get("weekly_km_avg") or 0, 1),
        "fitness": (st_full or {}).get("fitness"), "fatigue": (st_full or {}).get("fatigue"),
        "form": (st_full or {}).get("form"), "acwr": (st_full or {}).get("acwr"),
        "readiness": ((st_full or {}).get("readiness") or {}).get("score"),
        "forecast_verdict": forecast_verdict,
        "forecast_warnings": forecast_warnings,
        "min_form": min_form,
    }


def _training_status(db: Session, athlete):
    """训练状态（无历史数据时返回 (None, None)，前瞻推演跳过而不是报错）。

    返回 (投影输入, 完整 payload)：投影输入只含 daily/fitness/fatigue（load_project
    需要的三件），完整 payload 供 facts 取 acwr/readiness 等展示字段。
    """
    try:
        from ..data import build_training_status_for
        st, _ = build_training_status_for(db, athlete)
        if st.get("empty"):
            return None, None
        return {"daily": st["daily"], "fitness": st["fitness"], "fatigue": st["fatigue"]}, st
    except Exception as e:
        logger.info("课表分析取训练状态失败：%s", e)
        return None, None


def generate_text(facts: dict) -> str:
    """调用本地模型写课表分析。失败抛 ValueError（调用方转规则兜底）。"""
    base = settings.ai_base_url.rstrip("/")
    bad = ai_coach.validate_base_url(base)
    if bad:
        raise ValueError(bad)
    prompt = REVIEW_PROMPT.format(today=date.today().isoformat(), **facts)
    try:
        r = httpx.post(
            f"{base}/chat/completions",
            json={"model": ai_coach.review_model_id(ai_coach.probe_cached()["models"]),
                  "stream": False, "temperature": 0.5,
                  **ai_coach.review_gen_kwargs(MAX_REVIEW_TOKENS),
                  "messages": [{"role": "user", "content": prompt}]},
            headers=ai_coach._auth_headers(),
            timeout=httpx.Timeout(ai_coach.review_timeout(), connect=5.0),
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"] or ""
    except Exception as e:
        raise ValueError(f"本地模型不可用：{e}") from e
    text = ai_coach._strip_think(content).strip()
    if not text:
        raise ValueError("模型没有返回内容")
    return text


def fallback_review(facts: dict) -> str:
    """规则分析（模型不可用时的兜底）：复述引擎事实与风险，不编任何新数字。"""
    lines = [f"「{facts['plan_name']}」共 {facts['weeks']} 周，最长单课 {facts['longest_km']}km，"
             f"单周强度课最多 {facts['hard_max']} 节；你当前 VDOT {facts['vdot']}、"
             f"周均 {facts['weekly_km']}km。"]
    risks = []
    if facts["ramp_over10"]:
        risks.append(f"有 {facts['ramp_over10']} 周跑量环比超过 +10%（最大 +{facts['ramp_max']}%），"
                     "加量幅度超出安全线，到那几周可主动压一档")
    if facts["hard_max"] > 2:
        risks.append(f"单周强度课多达 {facts['hard_max']} 节，质量课之间至少留两天轻松日")
    min_form = facts.get("min_form")
    if min_form is not None and min_form < -30:
        risks.append(f"推演中最深形态到 {min_form:.0f}，疲劳谷偏深，峰值周前建议插一个减量周")
    acwr = facts.get("acwr")
    if acwr is not None and acwr > 1.5:
        risks.append(f"当前 ACWR {acwr} 已在伤病风险窗口，先降负荷再谈执行课表")
    if risks:
        lines.append("总判：偏激进。主要风险：" + "；".join(risks) + "。")
    else:
        lines.append("总判：整体合理。周跑量爬升在安全线内，强度分布有节制，"
                     "按课表执行并保证睡眠营养即可。")
    lines.append(f"前瞻推演：{facts['forecast_verdict']}。")
    return "\n".join(lines)


def generate_for_plan(db: Session) -> dict:
    """生成课表前瞻 AI 分析。同步调用（本地模型短输出），不落库。"""
    facts = gather_facts(db)
    if facts.get("empty"):
        return {"ok": False, "reasons": facts.get("reasons", ["暂无可分析的数据"])}
    try:
        review = generate_text(facts)
        review_guard.check(review, facts)   # 数字核验：引用事实之外的数字就退规则版
        source = "llm"
    except ValueError as e:
        logger.info("课表分析退化为规则文案：%s", e)
        review, source = fallback_review(facts), "rule"
    return {"ok": True, "source": source, "review": review}
