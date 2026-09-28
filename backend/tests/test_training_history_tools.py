"""get_training_history / get_activity_detail：逐次训练明细查询的回归。

此前 AI 教练只有聚合查询（get_recent_training），答不了「上周三跑了多少」
「我那次间歇每公里多少配速」这类逐次问题——这是「能处理的信息很少」的另一来源。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from app import models
from app.services import ai_tools
from factories import make_athlete
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    a = make_athlete()
    s.add(a)
    s.commit()
    yield s
    s.close()


def _athlete(db) -> models.Athlete:
    return db.query(models.Athlete).first()


def _mk_run(db, athlete_id: int, *, when: datetime, km: float, sec: int,
            title: str = "跑步", raw: dict | None = None) -> models.Activity:
    act = models.Activity(athlete_id=athlete_id, sport="run", title=title,
                          start_time=when, duration_sec=sec, distance_m=km * 1000,
                          avg_hr=150, avg_cadence=180, raw=raw or {})
    db.add(act)
    db.commit()
    return act


def test_training_history_lists_each_run(db):
    a = _athlete(db)
    _mk_run(db, a.id, when=datetime.now() - timedelta(days=1), km=8, sec=2400)   # 5:00/km
    _mk_run(db, a.id, when=datetime.now() - timedelta(days=3), km=5, sec=1500)
    db.add(models.Activity(athlete_id=a.id, sport="ride", title="骑行",
                           start_time=datetime.now() - timedelta(days=2),
                           duration_sec=3600, distance_m=30000))
    db.commit()

    r = ai_tools.execute_tool(db, a, "get_training_history", {"days": 30})
    assert r["ok"] is True
    assert r["run_count"] == 2, "非跑步运动不应混入"
    assert r["total_km"] == 13.0
    assert [x["distance_km"] for x in r["runs"]] == [8.0, 5.0], "应按时间倒序"
    assert "5:00" in r["runs"][0]["pace_str"]


def test_activity_detail_by_id_with_linked_workout(db):
    a = _athlete(db)
    act = _mk_run(db, a.id, when=datetime.now() - timedelta(days=1), km=10, sec=3000,
                  raw={"splits": [{"km": 1, "sec": 300}]})
    plan = models.TrainingPlan(athlete_id=a.id, name="测试计划", race_type="5k",
                               start_date=date.today() - timedelta(days=7),
                               race_date=date.today() + timedelta(weeks=6))
    db.add(plan)
    db.flush()
    week = models.PlanWeek(plan_id=plan.id, week_index=1, start_date=date.today() - timedelta(days=7),
                           phase="base", target_km=30)
    db.add(week)
    db.flush()
    db.add(models.PlanWorkout(week_id=week.id, athlete_id=a.id,
                              date=act.start_time.date(), title="有氧 10km",
                              session_type="easy", status="completed",
                              completed_activity_id=act.id, completed_source="linked",
                              coach_comment="执行到位，配速稳定。"))
    db.commit()

    r = ai_tools.execute_tool(db, a, "get_activity_detail", {"activity_id": act.id})
    assert r["ok"] is True
    item = r["activities"][0]
    assert item["distance_km"] == 10.0
    assert item["raw"]["splits"][0]["sec"] == 300, "设备原始分段应原样透出"
    assert item["linked_workout"]["coach_comment"] == "执行到位，配速稳定。"


def test_activity_detail_by_date_returns_all_runs_that_day(db):
    a = _athlete(db)
    day = datetime.now() - timedelta(days=2)
    _mk_run(db, a.id, when=day.replace(hour=7), km=5, sec=1500, title="晨跑")
    _mk_run(db, a.id, when=day.replace(hour=19), km=3, sec=1000, title="夜跑")

    r = ai_tools.execute_tool(db, a, "get_activity_detail",
                              {"date": day.date().isoformat()})
    assert r["ok"] is True
    assert len(r["activities"]) == 2
    assert {x["title"] for x in r["activities"]} == {"晨跑", "夜跑"}


def test_activity_detail_rejects_bad_input(db):
    a = _athlete(db)
    r1 = ai_tools.execute_tool(db, a, "get_activity_detail", {})
    assert r1["ok"] is False and "activity_id 或 date" in r1["reasons"][0]
    r2 = ai_tools.execute_tool(db, a, "get_activity_detail", {"date": "不是日期"})
    assert r2["ok"] is False
    r3 = ai_tools.execute_tool(db, a, "get_activity_detail", {"activity_id": 99999})
    assert r3["ok"] is False
