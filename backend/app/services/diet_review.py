"""饮食 AI 分析：饮食页「近 14 天习惯分析」卡的人工智能叙事层。

diet_analysis_payload 产出的全是确定性事实（当日目标/达成度/能量平衡/补给状态）；
本模块把事实装配进 prompt，本地模型只负责组织成饮食教练点评，禁止引入 prompt
之外的数据；模型不可用时退化为规则点评，端点不缺席（与 assessment_review /
recap_review 同一套三段式纪律）。点评不落库，每次点按钮重新生成。
"""
from __future__ import annotations

import logging
from datetime import date

import httpx
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from ..data import activities_dicts, diet_analysis_payload, weekly_km
from . import ai_coach, review_guard

logger = logging.getLogger(__name__)

MAX_REVIEW_TOKENS = 480

REVIEW_PROMPT = """你是跑者的专属 AI 饮食教练。以下是平台引擎算出的饮食事实（今天是 {today}），请写一段饮食点评。

【今日目标】{today_type}日型：{targets_line}；今日已摄入 {intake_kcal} kcal（目标 {target_kcal}），运动消耗 {burned_kcal} kcal
【近 14 天达成度】习惯得分 {score}；热量达标 {kcal_rate}%，蛋白质达标 {protein_rate}%
【达成亮点】{highlights}
【存在问题】{issues}
【能量平衡（近 7 天均值）】摄入 {avg_intake} / 目标 {avg_target} / 运动消耗 {avg_burned} kcal，日均平衡 {avg_balance}
【补给状态】{fueling_line}
【近期课表背景】{training_line}

写作规则：
1. 只能使用上面给出的事实与数字，禁止编造、换算或补充任何其他数字；没有记录就如实说没有；
2. 结构：先用 1-2 句给饮食现状定调（达成度与能量平衡）；再点出与训练的联动关系
   （补给不足 + 有质量课/长距离时必须明确警告）；最后给 2-3 条具体的饮食改进动作；
3. 不指责、不说教：问题只做客观归因，改进动作要具体到「吃什么/吃多少/什么时候吃」的粒度；
4. 简体中文，总长 250 字以内；直接输出正文，不要标题和 markdown 符号。"""


def gather_facts(db: Session, athlete: models.Athlete) -> dict:
    p = diet_analysis_payload(db, athlete)
    targets = p.get("targets") or {}
    analysis = p.get("analysis") or {}
    balance7 = (p.get("balance") or {}).get("avg7") or {}
    fueling = p.get("fueling") or {}
    today = p.get("today") or {}

    # 今日摄入从能量平衡序列的当天条目取（与饮食页图表同口径）
    series = (p.get("balance") or {}).get("series") or []
    today_entry = next((s for s in reversed(series) if s.get("date") == today.get("date")), {})

    fueling_line = "暂无判定（近几天没有饮食记录）"
    # fueling_status 无记录时返回 level="unknown"，与 low/deficit/ok 一并用字符串表达
    if fueling.get("level") and fueling["level"] != "unknown":
        fueling_line = f"{fueling['level']}"
        for r in (fueling.get("reasons") or [])[:2]:
            fueling_line += f"；{r}"

    logs = p.get("logs") or []
    days = len({log.get("date") for log in logs})
    wk = weekly_km(activities_dicts(db, athlete.id))
    training_line = (f"今天课型「{today.get('day_type')}」"
                     + (f"（{today.get('workout_title')}）" if today.get("workout_title") else "")
                     + f"，近 4 周周跑量均值 {round(wk, 1) if wk else '—'}km")

    return {
        "today_type": today.get("day_type") or "常规",
        "targets_line": (f"{targets.get('kcal', '—')} kcal / 蛋白 {targets.get('protein_g', '—')}g / "
                         f"碳水 {targets.get('carb_g', '—')}g / 脂肪 {targets.get('fat_g', '—')}g"),
        "intake_kcal": today_entry.get("intake_kcal", 0), "target_kcal": today_entry.get("target_kcal", 0),
        "burned_kcal": today_entry.get("burned_kcal", 0),
        "score": analysis.get("score"), "kcal_rate": analysis.get("kcal_rate"),
        "protein_rate": analysis.get("protein_rate"),
        "highlights": "；".join(analysis.get("highlights") or []) or "无",
        "issues": "；".join(analysis.get("issues") or []) or "无",
        "avg_intake": balance7.get("intake_kcal", "—"), "avg_target": balance7.get("target_kcal", "—"),
        "avg_burned": balance7.get("burned_kcal", "—"), "avg_balance": balance7.get("balance", "—"),
        "fueling_line": fueling_line,
        "training_line": training_line,
        "logged_days": days,
        # 规则兜底要用的结构化字段
        "fueling_level": fueling.get("level"),
    }


def generate_text(facts: dict) -> str:
    """调用本地模型写饮食点评。失败抛 ValueError（调用方转规则兜底或报错）。"""
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
    """规则点评（模型不可用时的兜底）：只复述引擎事实 + 与训练的联动提醒。"""
    lines = []
    if facts["logged_days"] == 0:
        lines.append("近 14 天还没有饮食记录，先在「记录一餐」里补几天，才能给出有依据的分析。")
    else:
        if facts.get("kcal_rate") is not None:
            lines.append(f"近 14 天热量达标 {facts['kcal_rate']}%、蛋白质达标 {facts['protein_rate']}%。")
        bal = facts.get("avg_balance")
        if isinstance(bal, (int, float)) and bal < -300:
            lines.append(f"近 7 天日均能量缺口 {abs(bal)} kcal，长期缺口会拖垮质量课并增加伤病风险。")
        elif isinstance(bal, (int, float)) and bal > 300:
            lines.append(f"近 7 天日均盈余 {bal} kcal，若体重目标是维持/下降，需适当收紧。")
    if facts.get("fueling_level") in ("low", "deficit"):
        lines.append(f"补给状态 {facts['fueling_level']}：在能量缺口下安排质量课/长距离会明显掉表现，优先补足碳水与总热量。")
    lines.append("（本地模型暂不可用，以上为规则生成的简要点评。）")
    return "\n".join(lines)


def generate_for_diet(db: Session, athlete: models.Athlete) -> dict:
    """生成饮食点评。同步调用（本地模型短输出），不落库。"""
    facts = gather_facts(db, athlete)
    try:
        review = generate_text(facts)
        review_guard.check(review, facts)   # 数字核验：引用事实之外的数字就退规则版
        source = "llm"
    except ValueError as e:
        logger.info("饮食点评退化为规则文案：%s", e)
        review, source = fallback_review(facts), "rule"
    return {"ok": True, "source": source, "review": review}
