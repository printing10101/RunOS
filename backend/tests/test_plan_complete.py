"""课表「完成打勾 / 取消完成」端点回归测试。

覆盖 POST /plan/workouts/{id}/complete（含 completed_activity_id 对账）
与 completed=False 取消语义。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from app import models, schemas
from app.routers.plans import complete_workout
from factories import make_athlete
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def db_with_workout():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    athlete = make_athlete(name="测试跑者", birth_year=1996)
    s.add(athlete)
    s.flush()
    plan = models.TrainingPlan(athlete_id=athlete.id, name="测试计划", race_type="marathon",
                               start_date=date(2026, 8, 1), race_date=date(2026, 11, 1))
    s.add(plan)
    s.flush()
    week = models.PlanWeek(plan_id=plan.id, week_index=1, start_date=date(2026, 8, 3),
                           phase="base", target_km=40)
    s.add(week)
    s.flush()
    wo = models.PlanWorkout(week_id=week.id, athlete_id=athlete.id,
                            date=date(2026, 8, 3), session_type="easy",
                            title="轻松跑 40 分钟")
    s.add(wo)
    s.commit()
    yield s, wo
    s.close()


def test_complete_marks_status_and_activity(db_with_workout):
    s, wo = db_with_workout
    act = models.Activity(athlete_id=wo.athlete_id, platform="manual", sport="run",
                          title="实际完成", start_time=datetime.now() - timedelta(hours=1),
                          duration_sec=2400, distance_m=6000)
    s.add(act)
    s.commit()
    r = complete_workout(wo.id, schemas.PlanWorkoutCompleteIn(activity_id=act.id), s)
    assert r["ok"] and r["status"] == "completed"
    assert r["completed_activity_id"] == act.id


def test_complete_without_activity_link(db_with_workout):
    """只打勾不关联活动也允许（手动课表未必有对应记录）。"""
    s, wo = db_with_workout
    r = complete_workout(wo.id, schemas.PlanWorkoutCompleteIn(), s)
    assert r["status"] == "completed" and r["completed_activity_id"] is None


def test_uncomplete_resets_to_planned(db_with_workout):
    """completed=False 必须退回 planned 并清空对账引用（取消打勾）。"""
    s, wo = db_with_workout
    act = models.Activity(athlete_id=wo.athlete_id, platform="manual", sport="run",
                          title="实际完成", start_time=datetime.now() - timedelta(hours=1),
                          duration_sec=2400, distance_m=6000)
    s.add(act)
    s.commit()
    complete_workout(wo.id, schemas.PlanWorkoutCompleteIn(activity_id=act.id), s)
    r = complete_workout(wo.id, schemas.PlanWorkoutCompleteIn(completed=False), s)
    assert r["status"] == "planned"
    assert r["completed_activity_id"] is None


def test_complete_rejects_bad_activity(db_with_workout):
    s, wo = db_with_workout
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        complete_workout(wo.id, schemas.PlanWorkoutCompleteIn(activity_id=99999), s)
    assert e.value.status_code == 404
