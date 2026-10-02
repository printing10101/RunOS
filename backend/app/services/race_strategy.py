"""赛时计划：目标比赛当天的执行方案（分段配速 / 补给 / 倒计时 / 环境预案）。

训练计划引擎管「练什么」，这里管「比赛那天怎么执行」——平台全部数据在这一天
汇聚成一张可执行的纸。数字纪律同点评引擎：配速来自目标成绩与预测引擎，
温度修正复用 weather.heat_slowdown_pct（不新造换算口径），补给/倒计时是通用
静态规则（不是用户数据）。没有进行中的比赛目标、或既无目标成绩又无可用预测时
如实返回 empty，不硬编一场比赛。
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from . import weather
from .vdot import RACE_DISTANCES, RACE_LABELS, pace_label, time_str

# 分段粒度：长距离每 5km 一段，中距离每 2km，短距离每 1km
_SPLIT_STEP = ((20, 5), (8, 2), (0, 1))
# 补给口径（通用运动营养规则，静态）：>75 分钟按 60-90g/h 碳水取 75；
# 凝胶按每支约 22g 碳水折算，每 40 分钟一支；≤45 分钟的比赛不需要途中补碳水
_FUEL_LONG_MIN = 75
_CARB_G_PER_HOUR = 75
_GEL_CARB_G = 22
_GEL_EVERY_MIN = 40
_FUEL_NOT_NEEDED_MIN = 45

_COUNTDOWN = [
    (-7, "减量周开始：周总量降到峰值的 40-60%，保留短促质量课维持神经募集"),
    (-3, "碳水加载开始（约 8-10g/kg 体重/天），停力量训练、不试任何新装备"),
    (-2, "装备终检：比赛鞋累计至少跑过 50km，号码布/补给/交通全过一遍"),
    (-1, "休息或 15 分钟慢跑激活，补足水分，核对早餐计划，早睡"),
    (0, "赛前 2.5-3 小时吃高碳水低纤维早餐，提前 40 分钟到场留出排队时间"),
]


def _pick_goal(db: Session, athlete: models.Athlete, race_type: str | None):
    query = select(models.Goal).where(
        models.Goal.athlete_id == athlete.id, models.Goal.status == "active")
    if race_type:
        query = query.where(models.Goal.race_type == race_type)
    goals = db.scalars(query).all()
    if not goals:
        return None
    goals.sort(key=lambda g: (g.priority != "primary", g.target_date or date.max))
    return goals[0]


def _splits(km: float, pace_sec: float) -> list[dict]:
    step = next(s for floor, s in _SPLIT_STEP if km >= floor)   # 末档 floor=0 必命中
    marks = list(range(step, int(km) + 1, step))
    if not marks or marks[-1] != round(km):
        marks.append(round(km, 1))
    out, cumulative = [], 0.0
    prev = 0.0
    for m in marks:
        seg = (m - prev) * pace_sec
        cumulative += seg
        out.append({"km": m, "lap_sec": round(seg),
                    "cumulative_sec": round(cumulative),
                    "cumulative_str": time_str(cumulative)})
        prev = m
    return out


def _fueling(target_sec: int) -> dict:
    minutes = target_sec / 60
    if minutes <= _FUEL_NOT_NEEDED_MIN:
        return {"needed": False, "note": f"全程约 {round(minutes)} 分钟，正常进水即可，途中不需要补碳水"}
    hours = minutes / 60
    gels = int(hours * _CARB_G_PER_HOUR / _GEL_CARB_G) if minutes >= _FUEL_LONG_MIN else 1
    schedule = [f"第 {i * _GEL_EVERY_MIN} 分钟" for i in range(1, gels + 1)]
    return {
        "needed": True,
        "carb_total_g": gels * _GEL_CARB_G,
        "gel_count": gels,
        "schedule": schedule,
        "note": f"按 {_CARB_G_PER_HOUR}g 碳水/小时口径，全程约 {gels} 支凝胶，"
                f"每 {_GEL_EVERY_MIN} 分钟一支并配水；比赛中首次补给提前到 30-40 分钟，不要等饿了再补",
    }


def _contingencies(w: dict | None) -> list[dict]:
    heat = weather.heat_slowdown_pct(w.get("temp_c"), w.get("humidity_pct")) if w else 0
    out = [{
        "condition": "高温/高湿", "applies": heat > 0,
        "advice": f"配速整体放慢约 {heat}%（前半程就控制，不要等掉速才降）"
                  if heat > 0 else "当前录入的比赛日天气不触发高温修正",
        "detail": "逢每高 5℃ 放慢约 2% 的口径，见环境修正引擎；补水站每站都进",
    }, {
        "condition": "逆风", "applies": False,
        "advice": "顶风段躲在人群里，风阻代价约 1-2%；顺风段再回来，单独顶风硬顶最亏",
    }, {
        "condition": "降雨", "applies": "雨" in str((w or {}).get("condition") or ""),
        "advice": "转弯处与井盖防滑，配速按体感不按表；鞋内进水起水泡风险高，抹足凡士林",
    }]
    return out


def build_race_plan(db: Session, athlete: models.Athlete,
                    race_type: str | None = None) -> dict:
    from ..data import build_prediction  # 局部导入避免 data.py 循环依赖
    from .prediction_trend import calibration_summary

    goal = _pick_goal(db, athlete, race_type)
    if goal is None or goal.race_type not in RACE_DISTANCES:
        return {"empty": True, "reason": "没有进行中的比赛目标，先在「目标」里设定"}
    dist = RACE_DISTANCES[goal.race_type]
    km = dist / 1000

    pred = build_prediction(db, athlete.id)
    entry = pred.predictions.get(goal.race_type) or {}
    predicted_sec = entry.get("time_sec")
    target_sec = goal.target_time_sec or predicted_sec
    race_info = {
        "race_type": goal.race_type, "label": RACE_LABELS.get(goal.race_type, goal.race_type),
        "target_label": goal.target_label or "",
        "target_time_sec": goal.target_time_sec,
        "target_time_str": time_str(goal.target_time_sec) if goal.target_time_sec else None,
        "race_date": goal.target_date.isoformat() if goal.target_date else None,
        "days_to_go": (goal.target_date - date.today()).days if goal.target_date else None,
    }
    if not target_sec:
        return {"empty": True, "race": race_info,
                "reason": "既没有目标成绩，预测引擎也没有可用的同距离预测，无法生成配速表"}

    # 比赛日环境（提前录入才生效）：温度修正作用于目标配速
    race_day = goal.target_date
    w = weather.get_day_weather(db, athlete.id, race_day) if race_day else None
    slowdown = weather.heat_slowdown_pct(w["temp_c"], w["humidity_pct"]) if w else 0
    pace_sec = target_sec / km
    adjusted_pace = weather.adjust_pace_sec(pace_sec, w["temp_c"], w["humidity_pct"]) if w else pace_sec

    return {
        "empty": False,
        "race": race_info,
        "prediction": {"predicted_sec": predicted_sec,
                       "predicted_str": time_str(predicted_sec) if predicted_sec else None,
                       "delta_vs_target_sec": (predicted_sec - goal.target_time_sec)
                       if predicted_sec and goal.target_time_sec else None},
        "pace_plan": {
            "target_sec": target_sec, "target_str": time_str(target_sec),
            "pace_sec_per_km": round(pace_sec), "pace_label": pace_label(pace_sec),
            "weather_slowdown_pct": slowdown,
            "adjusted_pace_sec_per_km": round(adjusted_pace),
            "adjusted_pace_label": pace_label(adjusted_pace),
            "strategy_note": "前 1/3 刻意压 2-3 秒/km，中段回到目标配速，最后 5km 看感觉提速——"
                             "负分割比冲高崩盘期望值高",
            "splits": _splits(km, adjusted_pace),
        },
        "fueling": _fueling(target_sec),
        # 预测可信度标注（历次实测偏差）：不改配速数字，只告诉用户该给目标留多少余量
        "calibration": calibration_summary(db, athlete.id),
        "countdown": [{"day_offset": d, "item": item}
                      for d, item in _COUNTDOWN if race_day and
                      (race_day + timedelta(days=d)) >= date.today() - timedelta(days=1)],
        "contingencies": _contingencies(w),
        "race_day_weather": w,
    }
