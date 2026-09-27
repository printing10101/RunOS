"""成绩预测 AI 解读：预测页的人工智能叙事层。

build_prediction / riegel_calibration 产出的全是确定性事实（各距离预测、临界速度、
数据质量、Riegel 校准）；本模块把事实装配进 prompt，本地模型只负责组织成解读，
禁止引入 prompt 之外的数据；模型不可用时退化为规则解读，端点不缺席（与
assessment_review / recap_review / diet_review 同一套三段式纪律）。解读不落库。
"""
from __future__ import annotations

import logging
from datetime import date

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from ..data import activities_dicts, build_prediction, riegel_calibration, weekly_km
from . import ai_coach, review_guard, vdot

logger = logging.getLogger(__name__)

MAX_REVIEW_TOKENS = 480

PREDICT_LABELS = {"800m": "800米", "1500m": "1500米", "3k": "3公里",
                  "5k": "5公里", "10k": "10公里", "hm": "半马", "marathon": "全马"}

REVIEW_PROMPT = """你是跑者的专属 AI 教练。以下是平台引擎算出的成绩预测事实（今天是 {today}），请写一段解读。

【当前水平】VDOT {vdot}；数据质量：{quality}；近 4 周周跑量均值 {weekly_km}km
【各距离预测】{pred_line}
【临界速度】{cs_line}
【Riegel 校准】{calib_line}
【目标差距】{goal_line}

写作规则：
1. 只能使用上面给出的事实与数字，禁止编造、换算或补充任何其他数字；
2. 结构：先用 1-2 句定位当前水平（强项距离 vs 弱项距离要基于预测的 VDOT 差异）；
   再评估这份预测有多可信（数据质量 + 校准状态，缺什么数据就明说怎么补）；
   最后结合目标差距给 1-2 条「接下来怎么让预测变准/变好」的行动；
3. 简体中文，总长 250 字以内；直接输出正文，不要标题和 markdown 符号。"""


def gather_facts(db: Session, athlete: models.Athlete) -> dict:
    pred = build_prediction(db, athlete.id)
    calib = riegel_calibration(db, athlete.id)
    wk = weekly_km(activities_dicts(db, athlete.id))

    pred_items = []
    for key, p in (pred.predictions or {}).items():
        label = PREDICT_LABELS.get(key, key)
        pred_items.append(f"{label} {p.get('time_str')}")
    pred_line = "、".join(pred_items) if pred_items else "暂无可用成绩数据，无法预测"

    calib_line = "暂无校准（少于 2 场不同距离的实测比赛，用标准模型）"
    if calib:
        calib_line = (f"个人指数 {calib.get('exponent')}（{calib.get('pairs')} 对比赛，{calib.get('note')}）")

    cs = pred.critical_speed
    cs_line = f"{round(cs, 2)} m/s" if cs else "数据不足（需多距离成绩拟合）"

    goal = db.scalar(select(models.Goal).where(
        models.Goal.athlete_id == athlete.id, models.Goal.status == "active").order_by(models.Goal.id))
    goal_line = "未设活动目标"
    if goal:
        label = PREDICT_LABELS.get(goal.race_type, goal.race_type)
        p = (pred.predictions or {}).get(goal.race_type)
        if p and goal.target_time_sec:
            delta = p["time_sec"] - goal.target_time_sec
            if delta > 0:
                goal_line = (f"{label}目标 {vdot.time_str(goal.target_time_sec)}，"
                             f"当前预测 {p.get('time_str')}，还差 {vdot.time_str(delta)}")
            else:
                goal_line = (f"{label}目标 {vdot.time_str(goal.target_time_sec)}，"
                             f"当前预测 {p.get('time_str')}，已具备达标水平")
        elif p:
            goal_line = f"{label}目标未设成绩，当前预测 {p.get('time_str')}"
        else:
            goal_line = f"{label}目标暂无对应预测"

    return {
        "vdot": pred.current_vdot if pred.current_vdot else "缺数据",
        "quality": pred.data_quality or "无可用成绩",
        "weekly_km": round(wk, 1) if wk else 0,
        "pred_line": pred_line,
        "cs_line": cs_line, "calib_line": calib_line, "goal_line": goal_line,
        # 规则兜底要用的结构化字段
        "has_prediction": bool(pred.predictions),
        "has_calibration": bool(calib),
    }


def generate_text(facts: dict) -> str:
    """调用本地模型写预测解读。失败抛 ValueError（调用方转规则兜底或报错）。"""
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
    """规则解读（模型不可用时的兜底）：只复述引擎事实，不编任何新数字。"""
    if not facts["has_prediction"]:
        return ("暂无足够的实测成绩做预测：录入或同步几场不同距离的比赛/测试后，"
                "这里会给出各距离预测与可信度解读。")
    lines = [f"当前 VDOT {facts['vdot']}，数据质量：{facts['quality']}。"]
    lines.append(facts["pred_line"] + "。")
    if not facts["has_calibration"]:
        lines.append("这份预测还没经过实测校准：跑一场与目标距离相近的测试赛，预测会明显更准。")
    if facts["goal_line"] != "未设活动目标":
        lines.append(facts["goal_line"] + "。")
    lines.append("（本地模型暂不可用，以上为规则生成的简要解读。）")
    return "\n".join(lines)


def generate_for_prediction(db: Session, athlete: models.Athlete) -> dict:
    """生成预测解读。同步调用（本地模型短输出），不落库。"""
    facts = gather_facts(db, athlete)
    try:
        review = generate_text(facts)
        review_guard.check(review, facts)   # 数字核验：引用事实之外的数字就退规则版
        source = "llm"
    except ValueError as e:
        logger.info("预测解读退化为规则文案：%s", e)
        review, source = fallback_review(facts), "rule"
    return {"ok": True, "source": source, "review": review}
