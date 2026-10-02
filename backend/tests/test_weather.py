"""天气环境修正的口径锁定：放慢百分比、配速修正、当日环境回退链、质量课配速提示。

锚定两条纪律（改 weather.py 先跑这里）：
- 冷天不加成（把目标改快是给比赛日挖坑），修正只放慢、封顶 10%；
- 手动录入 > 手表气温（只读回退，不落库）；无环境数据一切照旧，不编造。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from app import models
from app.db import get_db
from app.main import app
from app.services import weather
from factories import make_athlete
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    s.add(make_athlete())
    s.commit()
    yield s
    s.close()


@pytest.fixture()
def mem_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture()
def client(mem_db):
    def _ov():
        yield mem_db

    app.dependency_overrides[get_db] = _ov
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)


def _athlete(db) -> models.Athlete:
    return db.query(models.Athlete).first()


# ---------------------------------------------------------------- 放慢口径
def test_slowdown_calibration():
    assert weather.heat_slowdown_pct(None) == 0
    assert weather.heat_slowdown_pct(10) == 0          # 冷天不加成
    assert weather.heat_slowdown_pct(15) == 0          # 中性点
    assert weather.heat_slowdown_pct(20) == 2
    assert weather.heat_slowdown_pct(25) == 4
    assert weather.heat_slowdown_pct(30) == 6
    assert weather.heat_slowdown_pct(5) == 0


def test_humidity_bonus_and_cap():
    assert weather.heat_slowdown_pct(25, 60) == 4      # 湿度不足 70 不加
    assert weather.heat_slowdown_pct(25, 75) == 5      # +1
    assert weather.heat_slowdown_pct(25, 90) == 6      # ≥85 再 +1
    assert weather.heat_slowdown_pct(40, 95) == 10     # 封顶，不无限放大
    assert weather.heat_slowdown_pct(22, None) == 3    # 无湿度数据只按温度


def test_adjust_pace_sec():
    assert weather.adjust_pace_sec(300, None) == 300
    assert weather.adjust_pace_sec(300, 15) == 300
    assert weather.adjust_pace_sec(300, 25) == 312     # 4% → +12s
    assert weather.adjust_pace_sec(0, 30) == 0


# ---------------------------------------------------------------- 当日环境
def test_manual_log_wins_over_watch(db):
    a = _athlete(db)
    day = date.today()
    db.add(models.Activity(athlete_id=a.id, sport="run", start_time=datetime(
        day.year, day.month, day.day, 8, 0), temp_c=31.0, weather="晴"))
    db.commit()
    w = weather.get_day_weather(db, a.id, day)
    assert w["source"] == "watch" and w["temp_c"] == 31.0
    weather.upsert_day_weather(db, a, day, temp_c=33.0, humidity_pct=80, condition="闷热")
    w = weather.get_day_weather(db, a.id, day)
    assert w["source"] == "manual" and w["temp_c"] == 33.0 and w["humidity_pct"] == 80


def test_upsert_is_idempotent_per_day(db):
    a = _athlete(db)
    day = date.today()
    weather.upsert_day_weather(db, a, day, temp_c=30.0)
    weather.upsert_day_weather(db, a, day, temp_c=32.0, condition="晴")
    rows = db.query(models.WeatherLog).all()
    assert len(rows) == 1 and rows[0].temp_c == 32.0 and rows[0].condition == "晴"


def test_no_weather_returns_none(db):
    a = _athlete(db)
    db.add(models.Activity(athlete_id=a.id, sport="run",
                           start_time=datetime(2026, 6, 1, 8, 0)))   # 无气温字段
    db.commit()
    assert weather.get_day_weather(db, a.id, date(2026, 6, 1)) is None
    assert weather.daily_brief(db, a, date(2026, 6, 1)) is None


# ---------------------------------------------------------------- 质量课配速提示
def test_daily_brief_quality_pace_hint(db):
    """质量课日给具体修正带（数字来自课表处方，不编造）；非质量课日不给。"""
    a = _athlete(db)
    day = date.today()
    weather.upsert_day_weather(db, a, day, temp_c=30.0, humidity_pct=80)
    plan = models.TrainingPlan(athlete_id=a.id, name="计划", race_type="10k",
                               start_date=day - timedelta(days=7), race_date=day + timedelta(days=30))
    db.add(plan)
    db.flush()
    week = models.PlanWeek(plan_id=plan.id, week_index=1, start_date=day, phase="build")
    db.add(week)
    db.flush()
    db.add(models.PlanWorkout(
        week_id=week.id, athlete_id=a.id, date=day, session_type="tempo",
        title="节奏跑", status="planned",
        structured=[{"step_type": "active",
                     "target": {"type": "pace", "from": 280, "to": 300}}]))
    db.commit()
    brief = weather.daily_brief(db, a, day)
    assert brief["slowdown_pct"] == 7            # 30℃→6%，湿度 80 在 70-85 区间 +1
    hint = brief["pace_hint"]
    assert hint["session_label"] == "节奏跑"
    assert hint["target_pace"]["from_sec"] == 280
    assert hint["target_pace"]["adjusted_from_sec"] > 280


def test_daily_brief_easy_day_has_no_pace_hint(db):
    a = _athlete(db)
    day = date.today()
    weather.upsert_day_weather(db, a, day, temp_c=30.0)
    assert "pace_hint" not in (weather.daily_brief(db, a, day) or {})


def test_mild_day_note_is_neutral(db):
    a = _athlete(db)
    weather.upsert_day_weather(db, a, date.today(), temp_c=12.0)
    brief = weather.daily_brief(db, a, date.today())
    assert brief["slowdown_pct"] == 0
    assert "适宜" in brief["note"]


# ---------------------------------------------------------------- 端点
def test_weather_endpoints(mem_db, client):
    mem_db.add(make_athlete())
    mem_db.commit()
    r = client.post("/api/health/weather", json={"temp_c": 31.0, "humidity_pct": 75,
                                                 "condition": "晴热"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["brief"]["slowdown_pct"] == 7   # 31℃→round(6.4)=6；湿度 75 +1
    r = client.get("/api/health/weather")
    assert r.status_code == 200
    body = r.json()
    assert body["empty"] is False
    assert body["brief"]["temp_c"] == 31.0
    assert len(body["recent"]) == 1


def test_weather_endpoint_without_athlete(mem_db, client):
    r = client.get("/api/health/weather")
    assert r.status_code == 200 and r.json()["empty"] is True
