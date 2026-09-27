"""每周复盘 AI 点评：总览页「本周复盘」卡上的人工智能叙事层。

weekly_recap.build_weekly_recap 产出的全是确定性引擎事实（本周/上周对比、强度
分布、负荷/恢复、下周预览）；本模块把这些事实装配进 prompt，本地模型只负责组织
成教练点评，禁止引入 prompt 之外的数据；模型不可用时退化为规则点评，端点不缺席
（与 coach_comment / assessment_review 同一套纪律）。点评不落库：复盘每周滚动
刷新，随时可重新生成。
"""
from __future__ import annotations

import logging
from datetime import date

import httpx
from sqlalchemy.orm import Session

from ..config import settings
from . import ai_coach, review_guard

logger = logging.getLogger(__name__)

MAX_REVIEW_TOKENS = 500

REVIEW_PROMPT = """你是跑者的专属 AI 教练。以下是平台引擎算出的本周复盘事实（今天是 {today}），请写一段本周点评。

【本周 vs 上周】本周 {this_sessions} 次 / {this_km}km / {this_hours} 小时；上周 {prev_sessions} 次 / {prev_km}km / {prev_hours} 小时
【强度分布】本周高强度 {hard_pct}%（{hard_km}km），上周 {prev_hard_pct}%
【负荷】急性负荷 {acute}，慢性周负荷 {chronic}，ACWR {acwr}，单调性 {monotony}；体能 {fitness} / 疲劳 {fatigue} / 形态 {form}
【恢复】训练状态「{status}」，准备度 {readiness}，还需 {recovery_h} 小时恢复
【下周安排】{next_line}
【引擎主线】{lead}

写作规则：
1. 只能使用上面给出的事实与数字，禁止编造、换算或补充任何其他数字；
2. 结构：先用 1-2 句给本周定调（对比上周的变化要提到）；再点评负荷与恢复是否支撑继续推进
   （ACWR/单调性/准备度异常时明确说出来）；最后结合下周课表给 1-2 个具体执行重点；
3. 基调与引擎一致：客观归因、不指责，哪怕执行不理想也向前带一步；
4. 简体中文，总长 250 字以内；直接输出正文，不要标题和 markdown 符号。"""


def gather_facts(db: Session) -> dict:
    from .weekly_recap import build_weekly_recap

    r = build_weekly_recap(db)
    if r.get("empty"):
        return {"empty": True}

    def _stats(tag: str, s: dict) -> dict:
        return {f"{tag}_sessions": s.get("sessions", 0), f"{tag}_km": s.get("km", 0),
                f"{tag}_hours": s.get("hours", 0)}

    load = r.get("load") or {}
    recovery = r.get("recovery") or {}
    nxt = r.get("next")
    if nxt:
        next_line = (f"第 {nxt.get('week_index')} 周（{nxt.get('phase')}），目标 {nxt.get('target_km')}km，"
                     f"共 {nxt.get('count')} 节课"
                     + (f"，重点：{nxt.get('focus')}" if nxt.get("focus") else ""))
    else:
        next_line = "暂无计划中的下周课表"

    facts = {
        "empty": False,
        "lead": r.get("lead") or "",
        "next_line": next_line,
        **_stats("this", r.get("this_week") or {}),
        **_stats("prev", r.get("prev_week") or {}),
        "hard_pct": (r.get("split", {}).get("this") or {}).get("hard_pct", 0),
        "hard_km": (r.get("split", {}).get("this") or {}).get("hard_km", 0),
        "prev_hard_pct": (r.get("split", {}).get("prev") or {}).get("hard_pct", 0),
        "acute": load.get("acute"), "chronic": load.get("chronic"),
        "acwr": load.get("acwr"), "monotony": load.get("monotony"),
        "fitness": load.get("fitness"), "fatigue": load.get("fatigue"), "form": load.get("form"),
        "status": recovery.get("status"), "readiness": recovery.get("readiness"),
        "recovery_h": recovery.get("recovery_h"),
    }
    return facts


def generate_text(facts: dict) -> str:
    """调用本地模型写周点评。失败抛 ValueError（调用方转规则兜底或报错）。"""
    base = settings.ai_base_url.rstrip("/")
    bad = ai_coach.validate_base_url(base)
    if bad:
        raise ValueError(bad)
    prompt = REVIEW_PROMPT.format(today=date.today().isoformat(), **facts)
    try:
        r = httpx.post(
            f"{base}/chat/completions",
            json={"model": ai_coach.review_model_id(ai_coach.probe()["models"]),
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
    """规则点评（模型不可用时的兜底）：复述引擎事实 + 客观归因，不编任何新数字。"""
    delta_km = round(facts["this_km"] - facts["prev_km"], 1)
    trend = (f"比上周多 {delta_km}km" if delta_km > 0
             else f"比上周少 {abs(delta_km)}km" if delta_km < 0 else "与上周持平")
    lines = [f"本周 {facts['this_sessions']} 次训练、{facts['this_km']}km（{trend}），"
             f"高强度占比 {facts['hard_pct']}%。"]
    acwr = facts.get("acwr")
    if acwr is not None:
        if acwr > 1.5:
            lines.append(f"ACWR {acwr} 偏高，处于伤病风险窗口，下周优先把负荷拉回安全区间（0.8-1.3）。")
        elif acwr < 0.8:
            lines.append(f"ACWR {acwr} 偏低，负荷还有推进空间，下周可按计划小幅加量。")
        else:
            lines.append(f"ACWR {acwr} 处于安全区间，负荷节奏健康。")
    readiness = facts.get("readiness")
    if readiness is not None and readiness < 60:
        lines.append(f"准备度只有 {readiness}，恢复还没还清，别急着上强度。")
    lines.append(f"下周：{facts['next_line']}。按课表稳稳执行就是最好的积累。")
    return "\n".join(x for x in lines if x)


def generate_for_recap(db: Session) -> dict:
    """生成本周复盘 AI 点评。同步调用（本地模型短输出），不落库。"""
    facts = gather_facts(db)
    if facts.get("empty"):
        return {"ok": False, "reasons": ["还没有跑者档案，无法生成周复盘"]}
    try:
        review = generate_text(facts)
        review_guard.check(review, facts)   # 数字核验：引用事实之外的数字就退规则版
        source = "llm"
    except ValueError as e:
        logger.info("周报点评退化为规则文案：%s", e)
        review, source = fallback_review(facts), "rule"
    return {"ok": True, "source": source, "review": review}
