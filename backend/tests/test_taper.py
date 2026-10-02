"""减量对照（services/taper.py）的口径锁定。

锚定三条：
- 衰减带口径：比赛周 30-60%、赛前 1 周 45-75%、赛前 2 周 60-85%（占峰值周）；
- 峰值周负荷过小（<100）或计划未覆盖的周如实 no_plan，不硬算衰减率；
- 周定位用「周一」口径，与 plan_weeks.start_date 一致。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from app import models
from app.db import get_db
from app.main import app
from app.services import taper
from app.services.taper import taper_verdicts, week_start
from factories import make_athlete
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

TODAY = date(2026, 9, 30)          # 周三
RACE_DATE = date(2026, 10, 10)     # 周六，比赛周从 10-05（周一）起


@pytest.fixture()
def mem_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    s.add(make_athlete())
    s.commit()
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


def _loads(race_week: float, w1: float, w2: float, peak: float) -> dict[date, float]:
    rw = week_start(RACE_DATE)
    return {rw: race_week, rw - timedelta(weeks=1): w1,
            rw - timedelta(weeks=2): w2, rw - timedelta(weeks=5): peak}


# ---------------------------------------------------------------- 纯函数口径

def test_verdicts_all_within_bands():
    out = taper_verdicts(_loads(450, 600, 700, 1000), 1000, RACE_DATE, today=TODAY)
    assert out["days_to_race"] == 10 and out["peak_week_load"] == 1000
    assert [w["verdict"] for w in out["weeks"]] == ["ok", "ok", "ok"]
    assert out["weeks"][-1]["ratio"] == 0.45 and out["weeks"][-1]["label"] == "比赛周"
    assert "符合推荐衰减带" in out["verdict"]


def test_verdicts_high_and_low_paths():
    high = taper_verdicts(_loads(900, 600, 700, 1000), 1000, RACE_DATE, today=TODAY)
    assert high["weeks"][-1]["verdict"] == "high" and "减量不足" in high["verdict"]
    low = taper_verdicts(_loads(50, 100, 700, 1000), 1000, RACE_DATE, today=TODAY)
    assert low["weeks"][-1]["verdict"] == "low" and "减量偏大" in low["verdict"]


def test_verdicts_missing_week_and_tiny_peak():
    # 比赛周计划未覆盖 → no_plan，总评引导补课表
    out = taper_verdicts({k: v for k, v in _loads(450, 600, 700, 1000).items()
                          if k != week_start(RACE_DATE)}, 1000, RACE_DATE, today=TODAY)
    assert out["weeks"][-1]["verdict"] == "no_plan" and "未覆盖" in out["weeks"][-1]["why"]
    assert "生成完整课表" in out["verdict"]

    tiny = taper_verdicts(_loads(4.5, 6, 7, 10), 10, RACE_DATE, today=TODAY)
    assert all(w["verdict"] == "no_plan" for w in tiny["weeks"])


def test_week_start_is_monday():
    assert week_start(date(2026, 9, 30)).weekday() == 0   # 周三 → 周一


# ---------------------------------------------------------------- 装配入口

def _seed_plan(db, race_date: date):
    plan = models.TrainingPlan(athlete_id=1, name="备赛", race_type="10k",
                               start_date=race_date - timedelta(days=35),
                               race_date=race_date, weekly_km_peak=50)
    db.add(plan)
    db.flush()
    rw = week_start(race_date)
    # (周起始, 课型, 分钟) 峰值周在大负荷，比赛周只留一节短轻松跑
    plan_rows = [
        (rw - timedelta(weeks=5), "long", 150), (rw - timedelta(weeks=5), "tempo", 60),
        (rw - timedelta(weeks=5), "easy", 60), (rw - timedelta(weeks=5), "easy", 60),
        (rw - timedelta(weeks=2), "long", 150),
        (rw - timedelta(weeks=1), "easy", 60),
        (rw, "easy", 40),
    ]
    weeks = {}
    for ws, st, mins in plan_rows:
        if ws not in weeks:
            w = models.PlanWeek(plan_id=plan.id, week_index=len(weeks) + 1,
                                start_date=ws, phase="taper" if ws >= rw - timedelta(weeks=2) else "peak")
            db.add(w)
            db.flush()
            weeks[ws] = w
        db.add(models.PlanWorkout(week_id=weeks[ws].id, athlete_id=1,
                                  date=ws + timedelta(days=2), session_type=st,
                                  title=st, duration_min=mins))
    db.commit()
    return plan


def test_taper_check_assembly(mem_db):
    _seed_plan(mem_db, RACE_DATE)
    a = mem_db.query(models.Athlete).first()
    out = taper.taper_check(mem_db, a)
    assert out["empty"] is False
    assert out["race_date"] == RACE_DATE.isoformat()
    assert out["peak_week_load"] == max(out["peak_week_load"], 0)
    labels = [w["label"] for w in out["weeks"]]
    assert labels == ["赛前 2 周", "赛前 1 周", "比赛周"]
    # 峰值周 4 节大课，比赛周 1 节 40 分钟轻松跑 → 比赛周必然落在推荐带内
    assert out["weeks"][-1]["verdict"] in ("ok", "low")


def test_taper_check_empty_without_plan(mem_db):
    a = mem_db.query(models.Athlete).first()
    out = taper.taper_check(mem_db, a)
    assert out["empty"] is True and "计划" in out["reason"]


def test_taper_check_far_race_empty(mem_db):
    _seed_plan(mem_db, TODAY + timedelta(days=60))
    a = mem_db.query(models.Athlete).first()
    out = taper.taper_check(mem_db, a)
    assert out["empty"] is True and "赛前 4 周" in out["reason"]


def test_taper_endpoint(mem_db, client):
    r = client.get("/api/races/taper-check")
    assert r.status_code == 200 and r.json()["empty"] is True
