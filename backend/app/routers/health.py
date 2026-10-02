"""身体数据：每日指标的查询 / 手动录入修正 / 状态速览。

数据来源两条线：平台同步（佳明/高驰，services 侧 upsert）与手动录入
（按日期覆盖，字段可缺省）。/api/health 本身是 main.py 的服务心跳，
所以本路由只注册 /metrics 与 /overview 子路径，不与心跳冲突。
"""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..data import get_default_athlete
from ..db import get_db

router = APIRouter(prefix="/api/health", tags=["health"])

_METRIC_FIELDS = ("weight_kg", "resting_hr", "hrv_rmssd", "sleep_hours", "body_fat_pct",
                  "sleep_score", "spo2", "resp_rate", "stress", "body_battery",
                  "recovery_pct", "recovery_level", "recovery_hours")


def _metric_dict(r: models.BodyMetric) -> dict:
    out = {"date": r.date.isoformat()}
    for k in _METRIC_FIELDS:
        out[k] = getattr(r, k, None)
    return out


@router.get("/metrics")
def list_metrics(days: int = 30, db: Session = Depends(get_db)):
    athlete = get_default_athlete(db)
    if not athlete:
        return {"empty": True, "metrics": [], "baseline": None}
    since = date.today() - timedelta(days=max(1, min(days, 365)))
    rows = db.scalars(select(models.BodyMetric).where(
        models.BodyMetric.athlete_id == athlete.id,
        models.BodyMetric.date >= since).order_by(models.BodyMetric.date)).all()
    return {
        "empty": not rows,
        "metrics": [_metric_dict(r) for r in rows],
        "baseline": {"hrv_baseline": athlete.hrv_baseline,
                     "resting_hr": athlete.resting_hr,
                     "weight_kg": athlete.weight_kg},
    }


@router.post("/metrics")
def upsert_metric(data: schemas.BodyMetricIn, db: Session = Depends(get_db)):
    """按日期 upsert：只覆盖传入的字段，同步链路写入的其余字段不动。"""
    athlete = get_default_athlete(db)
    if not athlete:
        return {"empty": True, "ok": False}
    row = db.scalar(select(models.BodyMetric).where(
        models.BodyMetric.athlete_id == athlete.id,
        models.BodyMetric.date == data.date))
    if not row:
        row = models.BodyMetric(athlete_id=athlete.id, date=data.date)
        db.add(row)
    for k, v in data.model_dump().items():
        if v is not None:
            setattr(row, k, v)
    db.commit()
    return {"ok": True}


@router.get("/weather")
def weather_brief(day: date | None = None, days: int = 14, db: Session = Depends(get_db)):
    """环境简报：某天的天气 + 配速放慢百分比 + 今日质量课的配速放宽提示，
    以及近 N 天已录入的环境记录（前端补录入口用）。无环境数据返回 empty。"""
    from ..services import weather
    athlete = get_default_athlete(db)
    if not athlete:
        return {"empty": True, "brief": None, "recent": []}
    since = date.today() - timedelta(days=max(1, min(days, 365)))
    rows = db.scalars(select(models.WeatherLog).where(
        models.WeatherLog.athlete_id == athlete.id,
        models.WeatherLog.date >= since).order_by(models.WeatherLog.date.desc())).all()
    return {
        "empty": False,
        "brief": weather.daily_brief(db, athlete, day),
        "recent": [{"date": r.date.isoformat(), "temp_c": r.temp_c,
                    "humidity_pct": r.humidity_pct, "wind_kph": r.wind_kph,
                    "condition": r.condition, "source": r.source} for r in rows],
    }


@router.post("/weather")
def upsert_weather(data: schemas.WeatherIn, db: Session = Depends(get_db)):
    """按日期 upsert 当日环境（幂等，重录即覆盖）。缺日期默认今天。"""
    from ..services import weather
    athlete = get_default_athlete(db)
    if not athlete:
        return {"empty": True, "ok": False}
    row = weather.upsert_day_weather(db, athlete, data.day or date.today(),
                                     temp_c=data.temp_c, humidity_pct=data.humidity_pct,
                                     wind_kph=data.wind_kph, condition=data.condition)
    return {"ok": True, "brief": weather.daily_brief(db, athlete, row.date)}


# ---------------------------------------------------------------- 伤病追踪

def _injury_or_404(db: Session, athlete: models.Athlete, injury_id: int) -> models.InjuryLog | None:
    return db.scalar(select(models.InjuryLog).where(
        models.InjuryLog.athlete_id == athlete.id, models.InjuryLog.id == injury_id))


@router.get("/injuries")
def injury_panel(db: Session = Depends(get_db)):
    """伤病面板：全部记录 + 恢复中伤病的渐进建议 + 负荷关联分析。"""
    from ..services import injury
    athlete = get_default_athlete(db)
    if not athlete:
        return {"empty": True, "items": [], "advice": None, "correlation": None}
    actives = injury.active_injuries(db, athlete.id)
    return {
        "empty": False,
        "items": injury.injuries_dicts(db, athlete.id),
        "active_count": len(actives),
        "advice": injury.return_to_run_advice(actives[0]) if actives else None,
        "correlation": injury.load_correlation(db, athlete.id),
    }


@router.post("/injuries")
def create_injury(data: schemas.InjuryIn, db: Session = Depends(get_db)):
    athlete = get_default_athlete(db)
    if not athlete:
        return {"empty": True, "ok": False}
    if data.end_date and data.end_date < data.start_date:
        raise HTTPException(status_code=400, detail="痊愈日期不能早于起始日期")
    row = models.InjuryLog(athlete_id=athlete.id, **data.model_dump())
    db.add(row)
    db.commit()
    return {"ok": True, "id": row.id}


@router.put("/injuries/{injury_id}")
def update_injury(injury_id: int, data: schemas.InjuryIn, db: Session = Depends(get_db)):
    """整条修正：前端「标记痊愈」走 end_date=today 的 PUT。"""
    athlete = get_default_athlete(db)
    if not athlete:
        return {"empty": True, "ok": False}
    row = _injury_or_404(db, athlete, injury_id)
    if not row:
        raise HTTPException(status_code=404, detail="伤病记录不存在")
    if data.end_date and data.end_date < data.start_date:
        raise HTTPException(status_code=400, detail="痊愈日期不能早于起始日期")
    for k, v in data.model_dump().items():
        setattr(row, k, v)
    db.commit()
    return {"ok": True}


@router.delete("/injuries/{injury_id}")
def delete_injury(injury_id: int, db: Session = Depends(get_db)):
    athlete = get_default_athlete(db)
    if not athlete:
        return {"empty": True, "ok": False}
    row = _injury_or_404(db, athlete, injury_id)
    if not row:
        raise HTTPException(status_code=404, detail="伤病记录不存在")
    db.delete(row)
    db.commit()
    return {"ok": True}


def _last7(vals: list[float | None]) -> list[float]:
    return [v for v in vals if v is not None][-7:]


@router.get("/overview")
def overview(db: Session = Depends(get_db)):
    """佳明式状态速览：7 日均值对比基线，比单日最新值更能反映恢复趋势。

    baseline 块来自基线引擎（28 天中位数 + MAD 波动带），比单一档案值多出
    波动带/漂移/异常判定；原 hrv/sleep/resting_hr 键保留（前端既有消费）。
    """
    athlete = get_default_athlete(db)
    if not athlete:
        return {"has_data": False}
    since = date.today() - timedelta(days=14)
    rows = db.scalars(select(models.BodyMetric).where(
        models.BodyMetric.athlete_id == athlete.id,
        models.BodyMetric.date >= since).order_by(models.BodyMetric.date)).all()
    if not rows:
        return {"has_data": False}

    hrv7 = _last7([r.hrv_rmssd for r in rows])
    sleep7 = _last7([r.sleep_hours for r in rows])
    score7 = _last7([float(r.sleep_score) for r in rows if r.sleep_score is not None])
    rhr7 = _last7([float(r.resting_hr) for r in rows if r.resting_hr is not None])

    hrv_avg = round(sum(hrv7) / len(hrv7), 1) if hrv7 else None
    if hrv_avg is None or not athlete.hrv_baseline:
        hrv_status = "数据不足"
    else:
        # 基线上方视为均衡；低于基线 10% 以内轻度偏低，更多则明显偏低
        drop = (athlete.hrv_baseline - hrv_avg) / athlete.hrv_baseline
        hrv_status = "均衡" if drop <= 0 else ("轻度偏低" if drop <= 0.1 else "明显偏低")

    from ..data import body_metrics_dicts
    from ..services.baseline import build_baseline
    baselines = build_baseline(body_metrics_dicts(db, athlete.id, days=35))

    return {
        "has_data": True,
        "days_recorded": len(rows),
        "hrv": {"status": hrv_status, "last7_avg": hrv_avg,
                "baseline": athlete.hrv_baseline},
        "sleep": {"last7_hours": round(sum(sleep7) / len(sleep7), 1) if sleep7 else None,
                  "last7_score": round(sum(score7) / len(score7)) if score7 else None},
        "resting_hr": {"last7_avg": round(sum(rhr7) / len(rhr7), 1) if rhr7 else None},
        "baseline": {"resting_hr": athlete.resting_hr,
                     "weight_kg": athlete.weight_kg},
        "baselines": baselines,
    }
