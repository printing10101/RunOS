"""个人响应曲线的口径锁定：课型次日 HRV 反应、硬课恢复时长、样本量守卫。

锚定三条（改 response_curves.py 先跑这里）：
- 样本只算「关联了课表课的硬课」，未关联的不猜课型，easy 不进；
- 抑制线 = 基线 90%，恢复线 = 基线 95%；没有抑制的硬课不进恢复时长样本；
- 样本 <3 如实标 insufficient，不硬凑结论。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from app import models
from app.db import get_db
from app.main import app
from app.services import response_curves
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
    s.add(make_athlete(hrv_baseline=50))
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


def _seed_hard_links(db, athlete, entries: list[tuple[date, str]]) -> None:
    """entries: [(date, session_type)]，每条建 已完成课 + 关联活动。"""
    plan = models.TrainingPlan(athlete_id=athlete.id, name="计划", race_type="10k",
                               start_date=min(d for d, _ in entries),
                               race_date=max(d for d, _ in entries) + timedelta(days=30))
    db.add(plan)
    db.flush()
    week = models.PlanWeek(plan_id=plan.id, week_index=1,
                           start_date=min(d for d, _ in entries), phase="build")
    db.add(week)
    db.flush()
    for d, st in entries:
        act = models.Activity(athlete_id=athlete.id, sport="run", title=st,
                              start_time=datetime(d.year, d.month, d.day, 8, 0),
                              duration_sec=3600, distance_m=8000)
        db.add(act)
        db.flush()
        db.add(models.PlanWorkout(week_id=week.id, athlete_id=athlete.id, date=d,
                                  session_type=st, title=st, status="completed",
                                  completed_source="linked", completed_activity_id=act.id))
    db.commit()


def _seed_hrv(db, athlete, points: list[tuple[date, float]]) -> None:
    for d, v in points:
        db.add(models.BodyMetric(athlete_id=athlete.id, date=d, hrv_rmssd=v))
    db.commit()


def test_type_response_deltas_and_suppression(db):
    """三次间歇：次日 HRV 40/42/46 vs 基线 50 → 均值 -7.3，抑制 2/3（<45 算）。"""
    a = _athlete(db)
    d1, d2, d3 = date(2026, 6, 1), date(2026, 6, 8), date(2026, 6, 15)
    _seed_hard_links(db, a, [(d1, "interval"), (d2, "interval"), (d3, "interval")])
    _seed_hrv(db, a, [(d1 + timedelta(days=1), 40), (d2 + timedelta(days=1), 42),
                      (d3 + timedelta(days=1), 46)])
    types = response_curves.workout_type_response(db, a)
    assert len(types) == 1
    t = types[0]
    assert t["session_type"] == "interval" and t["label"] == "间歇跑"
    assert t["measured"] == 3 and t["insufficient"] is False
    assert t["next_day_hrv_delta"] == -7.3
    assert t["suppression_rate"] == round(2 / 3, 3)


def test_easy_and_unlinked_are_excluded(db):
    a = _athlete(db)
    _seed_hard_links(db, a, [(date(2026, 6, 1), "interval")])
    week = db.query(models.PlanWeek).first()
    d = date(2026, 6, 2)
    act = models.Activity(athlete_id=a.id, sport="run", title="轻松跑",
                          start_time=datetime(2026, 6, 2, 8, 0), duration_sec=1800,
                          distance_m=4000)
    db.add(act)
    db.flush()
    db.add(models.PlanWorkout(week_id=week.id, athlete_id=a.id, date=d,
                              session_type="easy", title="轻松跑", status="completed",
                              completed_activity_id=act.id))
    # 未关联任何课的活动也不进样本
    db.add(models.Activity(athlete_id=a.id, sport="run", title="自由跑",
                           start_time=datetime(2026, 6, 3, 8, 0), duration_sec=2400,
                           distance_m=6000))
    db.commit()
    types = response_curves.workout_type_response(db, a)
    assert [t["session_type"] for t in types] == ["interval"]
    assert types[0]["sessions"] == 1 and types[0]["measured"] == 0
    assert types[0]["insufficient"] is True and types[0]["next_day_hrv_delta"] is None


def test_sleep_score_response_channel(db):
    """次日睡眠分数相对前 28 天中位数的偏移：80 稳定基线下次日 75/76/77 → -4.0。"""
    a = _athlete(db)
    d1, d2, d3 = date(2026, 6, 1), date(2026, 6, 8), date(2026, 6, 15)
    _seed_hard_links(db, a, [(d1, "interval"), (d2, "interval"), (d3, "interval")])
    points = [(date(2026, 5, 4) + timedelta(days=i), 80.0) for i in range(28)]
    points += [(d1 + timedelta(days=1), 75), (d2 + timedelta(days=1), 76),
               (d3 + timedelta(days=1), 77)]
    for d, v in points:
        db.add(models.BodyMetric(athlete_id=a.id, date=d, sleep_score=v))
    db.commit()
    t = response_curves.workout_type_response(db, a)[0]
    assert t["sleep_measured"] == 3 and t["sleep_insufficient"] is False
    assert t["next_day_sleep_delta"] == -4.0


def test_sleep_channel_absent_is_insufficient(db):
    """没有睡眠分数数据：HRV 通道照常，睡眠通道如实 insufficient，互不拖累。"""
    a = _athlete(db)
    d1, d2, d3 = date(2026, 6, 1), date(2026, 6, 8), date(2026, 6, 15)
    _seed_hard_links(db, a, [(d1, "interval"), (d2, "interval"), (d3, "interval")])
    _seed_hrv(db, a, [(d1 + timedelta(days=1), 40), (d2 + timedelta(days=1), 42),
                      (d3 + timedelta(days=1), 46)])
    t = response_curves.workout_type_response(db, a)[0]
    assert t["next_day_hrv_delta"] == -7.3
    assert t["sleep_measured"] == 0
    assert t["next_day_sleep_delta"] is None and t["sleep_insufficient"] is True


def test_recovery_span_median_and_guards(db):
    """三次抑制（次日 40/42/44，均 <45）→ 回基线用时 [2,3,2]（从硬课日算）→ 中位 2 天。"""
    a = _athlete(db)
    d1, d2, d3 = date(2026, 6, 1), date(2026, 6, 8), date(2026, 6, 15)
    _seed_hard_links(db, a, [(d1, "interval"), (d2, "long"), (d3, "interval")])
    _seed_hrv(db, a, [
        (d1 + timedelta(days=1), 40), (d1 + timedelta(days=2), 48),   # 1 天恢复
        (d2 + timedelta(days=1), 42), (d2 + timedelta(days=2), 43),
        (d2 + timedelta(days=3), 49),                                  # 3 天恢复
        (d3 + timedelta(days=1), 44), (d3 + timedelta(days=2), 48),   # 1 天恢复
    ])
    # 手表恢复时间对照（硬课当日行）
    for d in (d1, d2, d3):
        db.add(models.BodyMetric(athlete_id=a.id, date=d, recovery_hours=24))
    db.commit()
    rec = response_curves.recovery_profile(db, a)
    assert rec["sample"] == 3 and rec["insufficient"] is False
    assert rec["median_days_to_baseline"] == 2.0
    assert rec["watch_recovery_hours_median"] == 24.0


def test_no_suppression_no_recovery_sample(db):
    """次日 HRV 没被抑制的硬课：没有「恢复时长」可言，样本为 0。"""
    a = _athlete(db)
    d1 = date(2026, 6, 1)
    _seed_hard_links(db, a, [(d1, "tempo")])
    _seed_hrv(db, a, [(d1 + timedelta(days=1), 49)])
    rec = response_curves.recovery_profile(db, a)
    assert rec["sample"] == 0 and rec["insufficient"] is True
    assert rec["median_days_to_baseline"] is None


def test_response_curves_endpoint(mem_db, client):
    mem_db.add(make_athlete(hrv_baseline=50))
    mem_db.commit()
    r = client.get("/api/stats/response-curves")
    assert r.status_code == 200
    body = r.json()
    assert body["empty"] is False
    assert body["types"] == []
    assert body["recovery"]["insufficient"] is True
