"""活动↔计划课自动关联（services/activity_link.py）与同步后置钩子。

覆盖：日期窗口/距离时长容差的匹配与拒绝、同日优先、幂等（二次跑不重复配对、
不覆盖手动完成）、非跑步活动跳过、on_activities_changed 只触发一次点评，
以及经完整 execute_sync 管道的端到端自动关联。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from app import models
from app.integrations.base import NormalizedActivity
from app.routers.connections import execute_sync
from app.services import activity_link
from factories import make_athlete
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


def _mk_plan(db, athlete_id: int, workouts: list[dict]) -> None:
    """建一个以本周为窗口的计划，workouts 按相对今天的天数偏移给定。"""
    today = date.today()
    plan = models.TrainingPlan(athlete_id=athlete_id, name="测试计划", race_type="5k",
                               start_date=today - timedelta(days=today.weekday()),
                               race_date=today + timedelta(weeks=8))
    db.add(plan)
    db.flush()
    week = models.PlanWeek(plan_id=plan.id, week_index=1,
                           start_date=today - timedelta(days=today.weekday()),
                           phase="build", target_km=40)
    db.add(week)
    db.flush()
    for spec in workouts:
        db.add(models.PlanWorkout(
            week_id=week.id, athlete_id=athlete_id,
            date=today + timedelta(days=spec.get("offset", 0)),
            session_type=spec.get("session_type", "easy"),
            title=spec.get("title", "轻松跑"),
            distance_km=spec.get("distance_km", 6.0),
            duration_min=spec.get("duration_min", 36)))
    db.commit()


def _activity(db, athlete_id: int, *, days_offset: float = 0, km: float = 6.0,
              minutes: float = 36.0, sport: str = "run") -> models.Activity:
    start = datetime.now() + timedelta(days=days_offset)
    act = models.Activity(athlete_id=athlete_id, platform="coros", sport=sport,
                          title="Outdoor Run", external_id=f"act-{start.isoformat()}",
                          start_time=start, duration_sec=int(minutes * 60),
                          distance_m=int(km * 1000))
    db.add(act)
    db.commit()
    return act


@pytest.fixture()
def athlete(db):
    a = make_athlete(name="测试跑者", birth_year=1996)
    db.add(a)
    db.commit()
    return a


# ---------------------------------------------------------------- 匹配与容差
def test_link_same_date_within_tolerance(db, athlete):
    _mk_plan(db, athlete.id, [{"offset": 0, "distance_km": 6.0, "duration_min": 36}])
    act = _activity(db, athlete.id, km=6.2, minutes=35)
    wo = activity_link.auto_link_activity(db, act)
    assert wo is not None
    assert wo.status == "completed" and wo.completed_source == "linked"
    assert wo.completed_activity_id == act.id


def test_link_next_morning_matches_yesterday_workout(db, athlete):
    """晚上计划没跑、次日早上补上：±1 天窗口内仍应配对。"""
    _mk_plan(db, athlete.id, [{"offset": -1, "distance_km": 10.0, "duration_min": 60}])
    act = _activity(db, athlete.id, km=10.4, minutes=62)
    wo = activity_link.auto_link_activity(db, act)
    assert wo is not None and wo.date == date.today() - timedelta(days=1)


def test_distance_out_of_window_rejected(db, athlete):
    """8km 的随意慢跑不应挂到 16km 长距离课上。"""
    _mk_plan(db, athlete.id, [{"offset": 0, "distance_km": 16.0, "duration_min": 100}])
    act = _activity(db, athlete.id, km=8.0, minutes=50)
    assert activity_link.auto_link_activity(db, act) is None


def test_duration_out_of_window_rejected(db, athlete):
    _mk_plan(db, athlete.id, [{"offset": 0, "distance_km": 6.0, "duration_min": 36}])
    # 距离接近但用时只有一半（骑行感/记录异常）：不配
    act = _activity(db, athlete.id, km=6.1, minutes=15)
    assert activity_link.auto_link_activity(db, act) is None


def test_same_date_preferred_over_neighbor(db, athlete):
    """昨天和今天各有一节课时，今天的活动优先配今天的课。"""
    _mk_plan(db, athlete.id, [
        {"offset": -1, "distance_km": 6.0, "duration_min": 36},
        {"offset": 0, "distance_km": 8.0, "duration_min": 48},
    ])
    act = _activity(db, athlete.id, km=8.2, minutes=47)
    wo = activity_link.auto_link_activity(db, act)
    assert wo is not None and wo.date == date.today()


def test_non_run_activity_skipped(db, athlete):
    _mk_plan(db, athlete.id, [{"offset": 0, "distance_km": 6.0}])
    act = _activity(db, athlete.id, sport="strength", km=0)
    assert activity_link.auto_link_activity(db, act) is None


# ---------------------------------------------------------------- 幂等与安全
def test_idempotent_second_pass(db, athlete):
    """同一活动第二次过钩子：不再产生新关联、不改状态。"""
    _mk_plan(db, athlete.id, [{"offset": 0, "distance_km": 6.0}])
    act = _activity(db, athlete.id, km=6.0, minutes=36)
    first = activity_link.on_activities_changed(db, athlete.id, [act.id])
    assert len(first["linked_workouts"]) == 1
    second = activity_link.on_activities_changed(db, athlete.id, [act.id])
    assert second["linked_workouts"] == []


def test_manual_completion_not_overwritten(db, athlete):
    """手动打卡完成的课不在候选集里，新的同步活动不会抢走它的关联。"""
    _mk_plan(db, athlete.id, [{"offset": 0, "distance_km": 6.0}])
    manual = models.Activity(athlete_id=athlete.id, platform="manual", sport="run",
                             title="手动记录", start_time=datetime.now(),
                             duration_sec=2160, distance_m=6000)
    db.add(manual)
    db.flush()
    wo = db.query(models.PlanWorkout).first()
    wo.status = "completed"
    wo.completed_source = "manual"
    wo.completed_activity_id = manual.id
    db.commit()

    synced = _activity(db, athlete.id, km=6.1, minutes=36)
    assert activity_link.auto_link_activity(db, synced) is None
    db.refresh(wo)
    assert wo.completed_activity_id == manual.id and wo.completed_source == "manual"


# ---------------------------------------------------------------- 后置钩子
def test_hook_triggers_comment_once(db, athlete, monkeypatch):
    """配对成功的课触发一次训练后点评；未配对的活动不触发。"""
    _mk_plan(db, athlete.id, [{"offset": 0, "distance_km": 6.0}])
    calls: list[int] = []

    def _fake_async(workout_id):
        calls.append(workout_id)

    from app.services import coach_comment
    monkeypatch.setattr(coach_comment, "regenerate_async", _fake_async)

    act = _activity(db, athlete.id, km=6.0, minutes=36)
    other = _activity(db, athlete.id, km=21.0, minutes=130)   # 不匹配任何课
    out = activity_link.on_activities_changed(db, athlete.id, [act.id, other.id])
    assert out["linked_workouts"] and calls == out["linked_workouts"]


# ---------------------------------------------------------------- 端到端：走完整同步管道
def test_sync_end_to_end_autolink(db, athlete, monkeypatch):
    """execute_sync 新活动落库 → 自动关联今天的计划课 → 触发点评（全管道）。"""
    from app.routers import connections

    _mk_plan(db, athlete.id, [{"offset": 0, "distance_km": 5.0, "duration_min": 30}])
    calls: list[int] = []
    from app.services import coach_comment
    monkeypatch.setattr(coach_comment, "regenerate_async",
                        lambda workout_id: calls.append(workout_id))

    start = datetime.now()
    norm = NormalizedActivity(external_id="123456789012345678", sport="run",
                              title="Outdoor Run", start_time=start,
                              duration_sec=1800, distance_m=4800)
    row = models.PlatformConnection(athlete_id=athlete.id, platform="coros",
                                    credentials={}, status="connected")
    db.add(row)
    db.commit()

    class FakeAdapter:
        def __init__(self, credentials):
            self.credentials = dict(credentials or {})

        def check_config(self):
            pass

        def fetch_activities(self, since, until=None):
            return [norm]

    monkeypatch.setattr(connections, "ADAPTERS", {"coros": FakeAdapter})

    result = execute_sync(db, row, since_days=7, detail_limit=0, backfill_detail=False)
    assert result["ok"] and result["added"] == 1
    wo = db.query(models.PlanWorkout).first()
    assert wo.status == "completed" and wo.completed_source == "linked"
    assert wo.completed_activity_id is not None
    assert calls == [wo.id]
