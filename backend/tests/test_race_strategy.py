"""赛时计划的口径锁定：分段配速、温度修正、补给表、倒计时、环境预案。

锚定三条（改 race_strategy.py 先跑这里）：
- 配速只能来自目标成绩或预测引擎，没有两者就如实 empty，不硬编；
- 温度修正复用 weather.heat_slowdown_pct 的同一口径；
- 补给/倒计时是通用静态规则，与用户数据解耦，可独立断言。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from app import models
from app.db import get_db
from app.main import app
from app.services import race_strategy, weather
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


def _goal(db, athlete, race_type="marathon", target_sec=10800, days=30, label="全马破三"):
    g = models.Goal(athlete_id=athlete.id, race_type=race_type,
                    target_time_sec=target_sec, target_label=label,
                    target_date=date.today() + timedelta(days=days), priority="primary",
                    status="active")
    db.add(g)
    db.commit()
    return g


# ---------------------------------------------------------------- 目标与配速
def test_empty_without_goal(db):
    r = race_strategy.build_race_plan(db, _athlete(db))
    assert r["empty"] is True and "目标" in r["reason"]


def test_goal_pace_splits_come_from_target(db):
    """全马 3 小时目标 → 256 s/km 均速；分段累计收口在目标成绩上。"""
    a = _athlete(db)
    _goal(db, a)
    r = race_strategy.build_race_plan(db, a)
    assert r["empty"] is False
    assert r["race"]["label"] == "全马" and r["race"]["days_to_go"] == 30
    pace = r["pace_plan"]
    assert pace["pace_sec_per_km"] == round(10800 / 42.195)
    assert pace["weather_slowdown_pct"] == 0
    assert pace["adjusted_pace_sec_per_km"] == pace["pace_sec_per_km"]   # 无天气不加修正
    last = pace["splits"][-1]
    assert last["km"] == 42.2   # round(42.195, 1)
    assert abs(last["cumulative_sec"] - 10800) <= 2   # 分段累计收敛到目标成绩


def test_prediction_used_when_no_target_time(db):
    """没填目标成绩：用预测引擎的同距离预测当目标，不硬编。"""
    a = _athlete(db)
    db.add(models.Activity(athlete_id=a.id, sport="run", title="10k 测试",
                           start_time=datetime.now() - timedelta(days=10),
                           duration_sec=3000, distance_m=10000))
    _goal(db, a, race_type="10k", target_sec=None, label="")
    r = race_strategy.build_race_plan(db, a)
    assert r["empty"] is False
    assert r["race"]["target_time_sec"] is None
    target = r["pace_plan"]["target_sec"]
    assert abs(target - 3000) < 60   # 预测 ≈ 10k 实测（Riegel 同距离回归）
    assert r["prediction"]["predicted_sec"] is not None


def test_weather_adjusts_goal_pace(db):
    a = _athlete(db)
    g = _goal(db, a)
    weather.upsert_day_weather(db, a, g.target_date, temp_c=30.0, humidity_pct=80)
    r = race_strategy.build_race_plan(db, a)
    pace = r["pace_plan"]
    assert pace["weather_slowdown_pct"] == 7
    assert pace["adjusted_pace_sec_per_km"] > pace["pace_sec_per_km"]
    assert r["contingencies"][0]["applies"] is True   # 高温预案命中


# ---------------------------------------------------------------- 补给与倒计时
def test_fueling_rules_by_duration():
    long = race_strategy._fueling(3 * 3600)
    assert long["needed"] is True
    assert long["gel_count"] == int(3 * 75 / 22)
    assert len(long["schedule"]) == long["gel_count"]
    short = race_strategy._fueling(25 * 60)
    assert short["needed"] is False


def test_countdown_respects_days_to_go(db):
    a = _athlete(db)
    _goal(db, a, days=3)
    r = race_strategy.build_race_plan(db, a)
    offsets = [c["day_offset"] for c in r["countdown"]]
    assert offsets == [-3, -2, -1, 0]   # D-7 已过不再出现，但近 3 天全保留


def test_rain_contingency_applies(db):
    a = _athlete(db)
    g = _goal(db, a)
    weather.upsert_day_weather(db, a, g.target_date, temp_c=15.0, condition="中雨")
    r = race_strategy.build_race_plan(db, a)
    rain = next(c for c in r["contingencies"] if c["condition"] == "降雨")
    assert rain["applies"] is True
    assert r["pace_plan"]["weather_slowdown_pct"] == 0   # 15℃ 不触发高温修正


# ---------------------------------------------------------------- 预测可信度标注
def test_calibration_attached_uncalibrated(db):
    """没有实测校准点：如实说未校准，不改配速数字。"""
    a = _athlete(db)
    _goal(db, a)
    r = race_strategy.build_race_plan(db, a)
    cal = r["calibration"]
    assert cal["n"] == 0 and "校准" in cal["note"]


def test_calibration_bias_annotated(db):
    """两次实测都偏慢（+3%/+5%）：标注偏乐观，提示留余量。"""
    a = _athlete(db)
    _goal(db, a)
    for race_date, predicted in ((date(2026, 3, 1), 3000), (date(2026, 5, 1), 3000)):
        db.add(models.RacePrediction(
            athlete_id=a.id, race_date=race_date, race_type="10k", race_name="测试",
            distance_m=10000, predicted_sec=predicted, predicted_on=race_date,
            actual_sec=round(predicted * 1.04), delta_pct=4.0))
    db.commit()
    r = race_strategy.build_race_plan(db, a)
    cal = r["calibration"]
    assert cal["n"] == 2 and cal["bias_pct"] == 4.0
    assert "偏乐观" in cal["note"]


# ---------------------------------------------------------------- 端点
def test_strategy_endpoint_empty_and_full(mem_db, client):
    mem_db.add(make_athlete())
    mem_db.commit()
    r = client.get("/api/races/strategy")
    assert r.status_code == 200 and r.json()["empty"] is True

    a = mem_db.query(models.Athlete).first()
    mem_db.add(models.Goal(athlete_id=a.id, race_type="hm", target_time_sec=5400,
                           target_label="半马 130", priority="primary", status="active",
                           target_date=date.today() + timedelta(days=60)))
    mem_db.commit()
    r = client.get("/api/races/strategy")
    assert r.status_code == 200
    body = r.json()
    assert body["empty"] is False and body["race"]["race_type"] == "hm"
    assert body["pace_plan"]["splits"]


def test_strategy_endpoint_without_athlete(mem_db, client):
    r = client.get("/api/races/strategy")
    assert r.status_code == 200 and r.json()["empty"] is True
