"""计划漂移引擎（services/plan_drift.py）：偏差检测与窄决策的场景回归。

用固定「今天」（周三）做日期冻结，覆盖四类 findings 与三类建议：
- 过载场景（ACWR 1.68）→ 下一节强度课换轻松跑（high）；
- 漏课 1 天 + 未来空档 → 挪课（medium）；
- 漏课 5 天 → 跳过（low）；
- 健康场景 → 无 findings 无建议；
- 无数据场景 → 不报跑量缺口（噪声抑制）；
- 建议总数封顶（MAX_SUGGESTIONS）。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from app import models
from app.services import ai_tools, plan_drift
from factories import make_athlete
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# 日期冻结点：选一个周三，保证「昨天漏课」与「未来空档」都在同一周框架内
FREEZE = date(2026, 9, 30)      # 周三


class FakeDate(date):
    @classmethod
    def today(cls):
        return cls(FREEZE.year, FREEZE.month, FREEZE.day)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch):
    """把漂移引擎与校验器的「今天」一起冻住，测试与星期几解耦。"""
    monkeypatch.setattr(plan_drift, "date", FakeDate)
    monkeypatch.setattr(ai_tools, "date", FakeDate)


@pytest.fixture()
def athlete(db):
    a = make_athlete(name="测试跑者", birth_year=1996, max_hr=190, resting_hr=55)
    db.add(a)
    db.commit()
    return a


def _mk_plan(db, athlete_id: int, workouts: list[dict]) -> models.TrainingPlan:
    """建计划：周框架从 FREEZE 的本周周一开始，workouts 给定相对 FREEZE 的天数偏移。"""
    monday = FREEZE - timedelta(days=FREEZE.weekday())
    plan = models.TrainingPlan(athlete_id=athlete_id, name="测试计划", race_type="5k",
                               start_date=monday, race_date=monday + timedelta(weeks=10))
    db.add(plan)
    db.flush()
    week = models.PlanWeek(plan_id=plan.id, week_index=1, start_date=monday,
                           phase="build", target_km=0)
    db.add(week)
    db.flush()
    for spec in workouts:
        db.add(models.PlanWorkout(
            week_id=week.id, athlete_id=athlete_id, date=FREEZE + timedelta(days=spec["offset"]),
            session_type=spec.get("session_type", "easy"),
            title=spec.get("title", "轻松跑"),
            distance_km=spec.get("distance_km", 6.0),
            duration_min=spec.get("duration_min", 36)))
    db.commit()
    return plan


def _add_slots_all_days(db, athlete_id: int, minutes: int = 90) -> None:
    for wd in range(7):
        db.add(models.WeeklySlot(athlete_id=athlete_id, weekday=wd, start_time="19:00",
                                 duration_minutes=minutes, kind="available"))
    db.commit()


def _add_load_history(db, athlete_id: int, *, base_days: int, base_load: float,
                      spike_days: int, spike_load: float) -> None:
    """构造 ACWR 过载：前段每日低负荷，近段每日高负荷（活动自带距离/时长供 VDOT 估算）。"""
    now = datetime.now()
    rows = []
    for i in range(base_days):
        start = now - timedelta(days=spike_days + (base_days - i))
        rows.append(models.Activity(athlete_id=athlete_id, platform="coros", sport="run",
                                    title="轻松跑", external_id=f"base-{i}-{start.isoformat()}",
                                    start_time=start, duration_sec=2880, distance_m=5000,
                                    training_load=base_load))
    for i in range(spike_days):
        start = now - timedelta(days=spike_days - 1 - i)
        rows.append(models.Activity(athlete_id=athlete_id, platform="coros", sport="run",
                                    title="加量跑", external_id=f"spike-{i}-{start.isoformat()}",
                                    start_time=start, duration_sec=3300, distance_m=5000,
                                    training_load=spike_load))
    db.add_all(rows)
    db.commit()


# ---------------------------------------------------------------- 建议决策
def test_overload_suggests_easy_replacement(db, athlete):
    """ACWR 1.68（>1.4 高危线）→ 明天的强度课建议整节换轻松跑，severity=high。"""
    _add_load_history(db, athlete.id, base_days=21, base_load=120, spike_days=7, spike_load=260)
    _mk_plan(db, athlete.id, [{"offset": 1, "session_type": "quality",
                               "title": "1km 间歇×6", "distance_km": 10, "duration_min": 60}])
    _add_slots_all_days(db, athlete.id)
    out = plan_drift.build_adjustments(db, athlete)
    assert out["signals"]["acwr"] is not None and out["signals"]["acwr"] > plan_drift.ACWR_CRITICAL
    kinds = [s["kind"] for s in out["suggestions"]]
    assert kinds == ["easy_replacement"]
    assert out["suggestions"][0]["severity"] == "high"
    assert out["suggestions"][0]["payload"] == {
        "kind": "easy_replacement", "workout_id": db.query(models.PlanWorkout).first().id}
    assert any("负荷比" in r for r in out["suggestions"][0]["reasons"])


def test_missed_recent_key_workout_suggests_move(db, athlete):
    """昨天漏掉的长距离 + 明天空档 → 挪课建议（payload 带目标 weekday）。"""
    _mk_plan(db, athlete.id, [
        {"offset": -1, "session_type": "long", "title": "长距离 14km",
         "distance_km": 14, "duration_min": 90},
        {"offset": 5, "session_type": "easy", "title": "周六轻松跑",
         "distance_km": 6, "duration_min": 36},
    ])
    _add_slots_all_days(db, athlete.id)
    out = plan_drift.build_adjustments(db, athlete)
    moves = [s for s in out["suggestions"] if s["kind"] == "move_workout"]
    assert len(moves) == 1
    s = moves[0]
    assert s["severity"] == "medium"
    assert s["payload"]["kind"] == "move_workout" and 0 <= s["payload"]["target_weekday"] <= 6
    missed_wo = [w for w in db.query(models.PlanWorkout).all()
                 if w.date == FREEZE - timedelta(days=1)][0]
    assert s["payload"]["workout_id"] == missed_wo.id


def test_stale_missed_workout_suggests_skip(db, athlete):
    """5 天前漏的课：超出可挪窗口 → 建议正式跳过。"""
    _mk_plan(db, athlete.id, [
        {"offset": -5, "session_type": "quality", "title": "节奏跑",
         "distance_km": 8, "duration_min": 45},
    ])
    _add_slots_all_days(db, athlete.id)
    out = plan_drift.build_adjustments(db, athlete)
    kinds = [s["kind"] for s in out["suggestions"]]
    assert "skip_workout" in kinds
    skip = next(s for s in out["suggestions"] if s["kind"] == "skip_workout")
    assert skip["severity"] == "low"
    assert any("5 天" in r for r in skip["reasons"])


def test_suggestions_capped_and_sorted(db, athlete):
    """过载 + 新鲜漏课 + 陈旧漏课并存：只留 severity 最高的 2 条。"""
    _add_load_history(db, athlete.id, base_days=21, base_load=120, spike_days=7, spike_load=260)
    _mk_plan(db, athlete.id, [
        {"offset": -5, "session_type": "quality", "title": "节奏跑",
         "distance_km": 8, "duration_min": 45},
        {"offset": -1, "session_type": "long", "title": "长距离 14km",
         "distance_km": 14, "duration_min": 90},
        {"offset": 1, "session_type": "quality", "title": "1km 间歇×6",
         "distance_km": 10, "duration_min": 60},
    ])
    _add_slots_all_days(db, athlete.id)
    out = plan_drift.build_adjustments(db, athlete)
    assert len(out["suggestions"]) <= plan_drift.MAX_SUGGESTIONS
    severities = [s["severity"] for s in out["suggestions"]]
    assert severities == sorted(severities, key=plan_drift.SEVERITY_ORDER.get)


def test_healthy_state_no_suggestions(db, athlete):
    """均衡负荷 + 没有漏课：引擎闭嘴，不打扰。"""
    _add_load_history(db, athlete.id, base_days=28, base_load=120, spike_days=0, spike_load=0)
    _mk_plan(db, athlete.id, [
        {"offset": 1, "session_type": "easy", "title": "轻松跑", "distance_km": 6,
         "duration_min": 36},
    ])
    out = plan_drift.build_adjustments(db, athlete)
    assert out["suggestions"] == []
    assert [f for f in out["findings"] if f["severity"] in ("high", "medium")] == []


def test_no_activities_no_gap_noise(db, athlete):
    """刚建计划还没有同步任何数据：不报跑量缺口。"""
    _mk_plan(db, athlete.id, [{"offset": 1, "session_type": "easy"}])
    out = plan_drift.build_adjustments(db, athlete)
    assert all(f["type"] != "load_gap" for f in out["findings"])


def test_load_gap_detected(db, athlete):
    """本周计划 40km 实际只有 8km：报 load_gap。"""
    monday = FREEZE - timedelta(days=FREEZE.weekday())
    plan = models.TrainingPlan(athlete_id=athlete.id, name="测试计划", race_type="5k",
                               start_date=monday, race_date=monday + timedelta(weeks=10))
    db.add(plan)
    db.flush()
    week = models.PlanWeek(plan_id=plan.id, week_index=1, start_date=monday,
                           phase="build", target_km=40)
    db.add(week)
    db.flush()
    db.add(models.PlanWorkout(week_id=week.id, athlete_id=athlete.id, date=FREEZE + timedelta(days=2),
                              session_type="easy", title="轻松跑", distance_km=6, duration_min=36))
    # 本周实际只有一次 8km
    start = datetime.combine(FREEZE - timedelta(days=1), datetime.min.time()) + timedelta(hours=8)
    db.add(models.Activity(athlete_id=athlete.id, platform="coros", sport="run",
                           title="周中跑", external_id=f"gap-{start.isoformat()}",
                           start_time=start, duration_sec=2880, distance_m=8000,
                           training_load=110))
    db.commit()
    out = plan_drift.build_adjustments(db, athlete)
    gaps = [f for f in out["findings"] if f["type"] == "load_gap"]
    assert len(gaps) == 1
    assert "落后于计划" in gaps[0]["reasons"][0]
