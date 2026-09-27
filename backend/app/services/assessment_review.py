"""综合评估 AI 解读：评分页「AI 解读」按钮的后端。

数字纪律与 coach_comment 同口径：五维得分/短板/目标差距/趋势全部由引擎
（build_evaluation + VDOT 趋势 + 计划执行率）装配进 prompt，LLM 只把给定事实
组织成教练解读，禁止引入 prompt 之外的数据；本地模型不可用时退化为确定性规则
解读，端点不缺席。评估随时可重算，解读不落库（区别于挂在训练课行上的
coach_comment——那边有行可挂、且要求「完成只点评一次」）。
"""
from __future__ import annotations

import logging
from datetime import date

import httpx
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from ..data import activities_dicts, build_evaluation, plan_adherence_dict
from . import ai_coach, evaluator, review_guard

logger = logging.getLogger(__name__)

# 解读是短文案：压住输出上限（时长）也避免小模型跑题长文
MAX_REVIEW_TOKENS = 550

REVIEW_PROMPT = """你是跑者的专属 AI 教练。以下是平台引擎基于用户全部数据算出的综合评估事实（今天是 {today}），请写一段解读。

【总体】总分 {total}/100，等级 {grade}，超过同龄同性别 {percentile}% 的跑者{runner_line}
【能力底数】当前 VDOT {vdot}，近 4 周周跑量均值 {weekly_km}km{trend_line}
【五维得分】{dimensions}
【目标差距】{goal_gap}
【短板清单】{findings}
【强度分布（近 6 个月）】{intensity}
【执行背景】{context}

写作规则：
1. 只能使用上面给出的事实与数字，禁止编造、换算或补充任何其他数字；「缺数据」就如实说缺数据；
2. 结构：先用 1-2 句总评点出一强一弱；再挑 2-3 个最值得处理的短板逐条点评——可训练的给
   具体做法（结合分数与证据），先天特质的给扬长策略；最后给「未来两周该做的 2-3 件事」；
3. 简体中文，专业但口语化，总长不超过 300 字；直接输出正文，不要标题和 markdown 符号，
   分点时用 1. 2. 3. 编号。"""


# ---------------------------------------------------------------- 事实装配（全部引擎数据）

def gather_facts(db: Session, athlete: models.Athlete) -> dict:
    ev = build_evaluation(db, athlete)
    dims, labels = ev["dimensions"], ev["dimension_labels"]
    scored = {k: d["score"] for k, d in dims.items() if d.get("score") is not None}
    weakest = min(scored, key=scored.get) if scored else None

    dim_lines = []
    for k, d in dims.items():
        line = f"{labels[k]} {d['score'] if d.get('score') is not None else '缺数据'}"
        # 最弱维多给证据（解读的主材料），其余维只报缺口
        if k == weakest and d.get("evidence"):
            line += "（证据：" + "；".join(d["evidence"][:3]) + "）"
        elif d.get("gaps"):
            line += "（缺数据：" + "；".join(d["gaps"][:2]) + "）"
        dim_lines.append(line)

    wk = ev.get("weakness") or {}
    finding_lines = []
    for f in (wk.get("findings") or [])[:4]:
        tag = "可训练" if f.get("category") == "trainable" else "先天"
        sev = "·高优先" if f.get("severity") == "high" else ""
        parts = [f"[{tag}{sev}] {f.get('title')}"]
        if f.get("what"):
            parts.append(str(f["what"])[:60])
        actions = "；".join((f.get("actions") or [])[:2])
        if actions:
            parts.append("改进：" + actions)
        finding_lines.append("：".join(parts) if len(parts) > 1 else parts[0])

    gap = wk.get("goal_gap")
    goal_line = ("未设活动目标" if not gap else
                 f"{gap.get('goal')}：需要 VDOT {gap.get('required_vdot')}，"
                 f"当前 {gap.get('current_vdot')}，{gap.get('verdict')}")

    dist = ev.get("intensity_distribution") or {}
    if dist.get("easy_pct") is not None:
        intensity = (f"轻松 {dist.get('easy_pct')}% / 中等 {dist.get('moderate_pct')}% / "
                     f"高强度 {dist.get('hard_pct')}%，{dist.get('verdict')}")
    else:
        intensity = dist.get("verdict") or "数据不足"

    trend = evaluator.vdot_trend(activities_dicts(db, athlete.id),
                                 date.today().year - athlete.birth_year, athlete.sex)
    trend_line = f"，近 8 周 VDOT 变化 {trend:+.1f}" if trend is not None else ""

    rt = ev.get("runner_type") or {}
    runner_line = f"；跑者类型：{rt.get('headline')}" if rt.get("headline") else ""

    adh = plan_adherence_dict(db, athlete.id)
    context_parts = []
    if adh and adh.get("total"):
        pct = round(adh["completed"] / adh["total"] * 100)
        context_parts.append(f"近 8 周计划课执行率 {pct}%（{adh['completed']}/{adh['total']}）")
    if ev.get("career_ceiling"):
        ceiling = ev["career_ceiling"]
        if isinstance(ceiling, dict) and ceiling.get("career_best"):
            context_parts.append(
                f"生涯上限参考：预计可到 {ceiling['career_best']}"
                f"（当前 {ceiling.get('current', '?')}，还有 {ceiling.get('improvement_pct', '?')}% 空间）")
    if not context_parts:
        context_parts.append("暂无计划执行记录")

    return {
        "total": ev.get("total_score"), "grade": ev.get("grade"),
        "percentile": ev.get("percentile"), "runner_line": runner_line,
        "vdot": ev.get("current_vdot") if ev.get("current_vdot") else "缺数据",
        "weekly_km": ev.get("weekly_km_avg"), "trend_line": trend_line,
        "dimensions": "；".join(dim_lines), "goal_gap": goal_line,
        "findings": "；".join(finding_lines) if finding_lines else "未检出明确短板",
        "intensity": intensity, "context": "；".join(context_parts),
        # 规则兜底要用的结构化字段
        "strongest": max(scored.items(), key=lambda kv: kv[1]) if scored else None,
        "weakest": min(scored.items(), key=lambda kv: kv[1]) if scored else None,
        "labels": labels, "weakness_summary": wk.get("summary"),
        "top_findings": (wk.get("findings") or [])[:2],
    }


# ---------------------------------------------------------------- 文案生成

def generate_text(facts: dict) -> str:
    """调用本地模型写解读。失败抛 ValueError（调用方转规则兜底或报错）。"""
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
    """规则解读（模型不可用时的兜底）：只复述引擎给过的事实，不编任何新数字。"""
    lines = [f"综合得分 {facts['total']}/100（{facts['grade']}），"
             f"超过同龄同性别 {facts['percentile']}% 的跑者。"]
    strongest, weakest = facts["strongest"], facts["weakest"]
    if strongest and weakest:
        s_label, w_label = facts["labels"][strongest[0]], facts["labels"][weakest[0]]
        lines.append(f"你的长板是{s_label}（{strongest[1]} 分），"
                     f"当前最值得投入的是{w_label}（{weakest[1]} 分）。")
    for f in facts["top_findings"]:
        actions = f.get("actions") or []
        if actions:
            lines.append(f"{f.get('title')}：{actions[0]}")
    if facts.get("weakness_summary"):
        lines.append(str(facts["weakness_summary"]))
    lines.append("（本地模型暂不可用，以上为规则生成的简要解读；可在模型可用后重新生成。）")
    return "\n".join(lines)


def generate_for_athlete(db: Session, athlete: models.Athlete) -> dict:
    """生成评估解读。同步调用（本地模型短输出），不落库：评估可随时重算。"""
    facts = gather_facts(db, athlete)
    try:
        review = generate_text(facts)
        review_guard.check(review, facts)   # 数字核验：引用事实之外的数字就退规则版
        source = "llm"
    except ValueError as e:
        logger.info("评估解读退化为规则文案：%s", e)
        review, source = fallback_review(facts), "rule"
    return {"ok": True, "source": source, "review": review}
