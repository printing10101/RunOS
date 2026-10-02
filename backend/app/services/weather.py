"""天气环境修正：高温/高湿天的配速修正与点评环境注记（确定性口径）。

为什么需要：配速是环境敏感的——15℃ 的 5:30 和 31℃ 的 5:30 不是同一个强度。
不做环境修正，高温天的「未达标」点评就是在冤枉人，目标配速带也会系统性偏紧。

口径（简化自通用的温度-配速经验换算，够用就好）：
- 中性温度取 15℃；每高 5℃ 目标配速慢约 2%；
- 湿度补偿：≥20℃ 且湿度 ≥70% 再 +1%、≥85% 再 +1%（湿热天汗液蒸发效率下降）；
- 总修正封顶 10%：这张表是给「不冤枉人」用的，不是精确生理模型，更热的情况
  该由打卡降档（checkin_advice）兜底，不在这里无限放大。
冷天不加成（低温确实更快，但把目标改快是给比赛日挖坑），下限 0%。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from . import vocab, workout_analysis

_NEUTRAL_TEMP_C = 15.0
_STEP_PER_5C = 2.0
_MAX_SLOWDOWN_PCT = 10.0


def heat_slowdown_pct(temp_c: float | None, humidity_pct: int | None = None) -> int:
    """高温/高湿导致的目标配速放慢百分比（整数 %，0-10）。无温度数据返回 0。"""
    if temp_c is None:
        return 0
    over = max(0.0, temp_c - _NEUTRAL_TEMP_C)
    pct = over / 5.0 * _STEP_PER_5C
    if temp_c >= 20 and humidity_pct is not None:
        if humidity_pct >= 85:
            pct += 2
        elif humidity_pct >= 70:
            pct += 1
    return int(min(round(pct), _MAX_SLOWDOWN_PCT))


def adjust_pace_sec(sec_per_km: float, temp_c: float | None,
                    humidity_pct: int | None = None) -> float:
    """目标配速（秒/km）按环境放慢；无温度数据原样返回。"""
    if temp_c is None or sec_per_km <= 0:
        return sec_per_km
    return sec_per_km * (1 + heat_slowdown_pct(temp_c, humidity_pct) / 100)


# ---------------------------------------------------------------- 当日环境

def get_day_weather(db: Session, athlete_id: int, day: date) -> dict | None:
    """某天的环境：weather_logs 手动/接口录入优先，缺失时只读回退当天活动自带的
    手表气温；都没有返回 None。回退不落库——手动录入的永远盖过手表值。"""
    row = db.scalar(select(models.WeatherLog).where(
        models.WeatherLog.athlete_id == athlete_id,
        models.WeatherLog.date == day))
    if row is not None:
        return {"date": day.isoformat(), "temp_c": row.temp_c,
                "humidity_pct": row.humidity_pct, "wind_kph": row.wind_kph,
                "condition": row.condition or "", "source": row.source}
    lo = datetime.combine(day, time.min)
    act = db.scalars(select(models.Activity).where(
        models.Activity.athlete_id == athlete_id,
        models.Activity.temp_c.is_not(None),
        models.Activity.start_time >= lo,
        models.Activity.start_time < lo + timedelta(days=1),
    ).order_by(models.Activity.start_time.desc()).limit(1)).first()
    if act is None:
        return None
    return {"date": day.isoformat(), "temp_c": act.temp_c, "humidity_pct": None,
            "wind_kph": None, "condition": act.weather or "", "source": "watch"}


def _note(slowdown_pct: int, temp_c: float | None) -> str:
    if slowdown_pct <= 0:
        return "气温适宜，按正常目标配速执行"
    return f"气温较高（{round(temp_c)}℃），配速比凉爽天天然慢，属正常现象"


def _quality_pace_hint(db: Session, athlete: models.Athlete, day: date,
                       w: dict) -> dict | None:
    """今日质量课的配速放宽提示：有课表处方就给具体修正带，没有只给百分比，
    不编造任何课表之外的数字。"""
    wo = db.scalars(select(models.PlanWorkout).where(
        models.PlanWorkout.athlete_id == athlete.id,
        models.PlanWorkout.date == day,
        models.PlanWorkout.status.in_(["planned", "synced"]),
    )).all()
    quality = [x for x in wo if vocab.is_quality_session(x.session_type)]
    if not quality:
        return None
    wo = quality[0]
    slowdown = heat_slowdown_pct(w["temp_c"], w["humidity_pct"])
    hint: dict = {"session_type": wo.session_type,
                  "session_label": vocab.session_label(wo.session_type),
                  "slowdown_pct": slowdown}
    if slowdown <= 0:
        return hint
    band = workout_analysis._pace_targets(wo.structured)
    if band:
        fast, slow = band
        factor = 1 + slowdown / 100
        hint["target_pace"] = {"from_sec": round(fast),
                               "to_sec": round(slow),
                               "adjusted_from_sec": round(fast * factor),
                               "adjusted_to_sec": round(slow * factor)}
    return hint


def daily_brief(db: Session, athlete: models.Athlete, day: date | None = None) -> dict | None:
    """某天环境简报：环境数据 + 放慢百分比 + 今日质量课配速提示；无环境数据返回 None。"""
    day = day or date.today()
    w = get_day_weather(db, athlete.id, day)
    if w is None:
        return None
    slowdown = heat_slowdown_pct(w["temp_c"], w["humidity_pct"])
    w["slowdown_pct"] = slowdown
    w["note"] = _note(slowdown, w["temp_c"])
    hint = _quality_pace_hint(db, athlete, day, w)
    if hint:
        w["pace_hint"] = hint
    return w


def upsert_day_weather(db: Session, athlete: models.Athlete, day: date,
                       temp_c: float | None, humidity_pct: int | None = None,
                       wind_kph: float | None = None, condition: str = "",
                       source: str = "manual") -> models.WeatherLog:
    """录入/更新某天环境（幂等）：同一天一条，重录即覆盖。"""
    row = db.scalar(select(models.WeatherLog).where(
        models.WeatherLog.athlete_id == athlete.id,
        models.WeatherLog.date == day))
    if row is None:
        row = models.WeatherLog(athlete_id=athlete.id, date=day)
        db.add(row)
    row.temp_c = temp_c
    row.humidity_pct = humidity_pct
    row.wind_kph = wind_kph
    row.condition = condition
    row.source = source
    db.commit()
    return row
