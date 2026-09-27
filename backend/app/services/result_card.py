"""即时成绩卡：一节训练完成后的确定性反馈卡片（Activities 页与录入返回）。

三件事：这是什么距离的成绩（type_label）、一句话头条（headline）、
有没有刷新 PB（new_pb → 前端换 🏆🎉 庆祝样式）。全部由库内已有数据算，
不调模型、不编数字；非跑步或数据不全时降级为基础卡片，端点不缺席。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from .vdot import RACE_DISTANCES, RACE_LABELS, pace_label, time_str

_EMOJI = {"run": "🏃", "ride": "🚴", "swim": "🏊", "strength": "💪",
          "walk": "🚶", "other": "✨"}

# 标准距离 ±5% 视为同距离比较窗口
_PB_BAND = 0.05


def _match_race(distance_m: float) -> tuple[str, float] | None:
    """匹配标准比赛距离，返回 (key, 距离)；未匹配返回 None。"""
    for key, dist in RACE_DISTANCES.items():
        if dist * (1 - _PB_BAND) <= distance_m <= dist * (1 + _PB_BAND):
            return key, dist
    return None


def build_result_card(act: models.Activity, db: Session) -> dict:
    """任意历史活动（含同步来源）→ 完成反馈卡片。"""
    card = {
        "emoji": _EMOJI.get(act.sport, "✨"),
        "type_label": "",
        "headline": "",
        "new_pb": False,
        "highlight": None,
    }
    if not act.distance_m or not act.duration_sec:
        # 无距离（力量/核心等）给时长头条，保证卡片总有内容
        if act.duration_sec:
            card["type_label"] = "训练"
            card["headline"] = f"{round(act.duration_sec / 60)} 分钟"
        return card

    km = act.distance_m / 1000
    pace = act.duration_sec / km

    matched = _match_race(act.distance_m) if act.sport == "run" else None
    if matched:
        key, _ = matched
        card["type_label"] = RACE_LABELS[key]
        card["headline"] = f"{km:g}km · {time_str(act.duration_sec)}"
        card["new_pb"] = _is_new_pb(db, act, matched[1])
    else:
        card["type_label"] = {"run": "跑步", "ride": "骑行", "swim": "游泳",
                              "walk": "步行"}.get(act.sport, "训练")
        card["headline"] = f"{round(km, 1)}km · {time_str(act.duration_sec)}"

    if pace:
        card["highlight"] = {"label": "平均配速", "value": pace_label(pace)}
    elif act.avg_hr:
        card["highlight"] = {"label": "平均心率", "value": f"{act.avg_hr} bpm"}
    return card


def _is_new_pb(db: Session, act: models.Activity, dist: float) -> bool:
    """该距离是否刷新个人最好成绩（此前最快）。同时段多开等极端并发不设防：
    卡片是即时反馈，晚到的同步活动刷新 PB 属可接受语义。"""
    lo, hi = dist * (1 - _PB_BAND), dist * (1 + _PB_BAND)
    floor = (act.start_time or datetime.now()) - timedelta(days=365 * 3)
    prior = db.scalars(select(models.Activity.duration_sec).where(
        models.Activity.athlete_id == act.athlete_id,
        models.Activity.sport == "run",
        models.Activity.distance_m >= lo,
        models.Activity.distance_m <= hi,
        models.Activity.id != act.id,
        models.Activity.start_time >= floor)).all()
    prior = [t for t in prior if t]
    return bool(prior) and act.duration_sec < min(prior)
