"""单课分析器（services/workout_analysis.py）：处方 vs 实际执行的判定回归。

覆盖：距离/时长完成度、质量课最快公里 vs 目标区间（达标/过快/过慢）、
轻松课平均配速对照、心率区间与缺 max_hr 的降级、旧课无结构化步骤兼容、
手动打卡无关联活动的 no_data、以及 analyze_and_store 的落库路径。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from app import models
from app.services.workout_analysis import analyze_and_store, analyze_workout
from factories import make_athlete
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    a = make_athlete(name="测试跑者", birth_year=1996, max_hr=190)
    s.add(a)
    s.commit()
    yield s
    s.close()


def _pace_target(fast_sec: int, slow_sec: int) -> dict:
    return {"type": "pace", "from": fast_sec, "to": slow_sec, "label": "目标区间"}


def _interval_steps(fast_sec: int = 240, slow_sec: int = 258) -> list[dict]:
    """间歇课结构：热身 + 1km×4 @目标区间 + 冷身（与 planner 输出同构）。"""
    def rep(i):
        return [{"step_type": "active", "name": f"间歇 {i}/4", "duration_type": "distance",
                 "duration_value": 1000, "target": _pace_target(fast_sec, slow_sec), "note": "I 配速"},
                {"step_type": "rest", "name": "间歇恢复", "duration_type": "time",
                 "duration_value": 90, "target": {"type": "none", "label": "慢跑"}}]
    return ([{"step_type": "warmup", "name": "热身慢跑", "duration_type": "time",
              "duration_value": 12, "target": {"type": "hr", "from": 0.65, "to": 0.75, "label": "E"}}]
            + [s for i in (1, 2, 3, 4) for s in rep(i)]
            + [{"step_type": "cooldown", "name": "冷身", "duration_type": "time",
                "duration_value": 10, "target": {"type": "hr", "from": 0, "to": 0.65, "label": "E"}}])


def _mk_workout(db, *, structured=None, session_type="quality",
                distance_km=10.0, duration_min=55) -> models.PlanWorkout:
    plan = models.TrainingPlan(athlete_id=db.query(models.Athlete).first().id,
                               name="测试计划", race_type="5k",
                               start_date=date.today(), race_date=date.today() + timedelta(weeks=8))
    db.add(plan)
    db.flush()
    week = models.PlanWeek(plan_id=plan.id, week_index=1, start_date=date.today(),
                           phase="build", target_km=40)
    db.add(week)
    db.flush()
    wo = models.PlanWorkout(week_id=week.id, athlete_id=plan.athlete_id, date=date.today(),
                            session_type=session_type, title="间歇课",
                            distance_km=distance_km, duration_min=duration_min,
                            structured=structured or [])
    db.add(wo)
    db.commit()
    return wo


def _mk_activity(db, athlete_id, *, km=10.0, minutes=55.0, avg_hr=None,
                 splits=None) -> models.Activity:
    act = models.Activity(athlete_id=athlete_id, platform="coros", sport="run",
                          title="间歇课", start_time=datetime.now(),
                          duration_sec=int(minutes * 60), distance_m=int(km * 1000),
                          avg_hr=avg_hr, raw={"splits": splits or []})
    db.add(act)
    db.commit()
    return act


def _splits_at(sec_per_km: list[int]) -> list[dict]:
    return [{"index": i + 1, "distance_m": 1000, "duration_sec": sec,
             "pace_sec_per_km": sec} for i, sec in enumerate(sec_per_km)]


# ---------------------------------------------------------------- 完成度
def test_short_completion(db):
    wo = _mk_workout(db, structured=_interval_steps(), distance_km=10.0)
    act = _mk_activity(db, wo.athlete_id, km=7.0, minutes=40)
    out = analyze_workout(wo, act, max_hr=190)
    assert out["verdict"] == "short"
    assert out["completion"]["km_ratio"] == 0.7
    assert any("未跑满" in r for r in out["reasons"])


def test_no_activity_gives_no_data(db):
    wo = _mk_workout(db)
    out = analyze_workout(wo, None, max_hr=190)
    assert out["verdict"] == "no_data"


# ---------------------------------------------------------------- 配速判定（质量课用最快公里）
def test_quality_on_target_via_best_split(db):
    """间歇课：全程平均被热身冷身稀释，达标与否看最快完整公里。"""
    wo = _mk_workout(db, structured=_interval_steps(fast_sec=240, slow_sec=258))
    splits = _splits_at([300, 250, 252, 248, 305])
    act = _mk_activity(db, wo.athlete_id, km=10.0, minutes=55, splits=splits)
    out = analyze_workout(wo, act, max_hr=190)
    assert out["verdict"] == "on_target"
    assert out["pace"]["source"] == "最快公里"
    assert 240 <= out["pace"]["actual_sec_per_km"] <= 258


def test_quality_too_fast_flagged(db):
    """主段明显快于处方上限：超强度要被点名，不能因为『跑得快』就算达标。"""
    wo = _mk_workout(db, structured=_interval_steps(fast_sec=240, slow_sec=258))
    splits = _splits_at([300, 200, 202, 198, 305])
    act = _mk_activity(db, wo.athlete_id, km=10.0, minutes=52, splits=splits)
    out = analyze_workout(wo, act, max_hr=190)
    assert out["verdict"] == "too_fast"


def test_quality_too_slow_flagged(db):
    wo = _mk_workout(db, structured=_interval_steps(fast_sec=240, slow_sec=258))
    splits = _splits_at([330, 310, 315, 320, 335])
    act = _mk_activity(db, wo.athlete_id, km=10.0, minutes=60, splits=splits)
    out = analyze_workout(wo, act, max_hr=190)
    assert out["verdict"] == "too_slow"


# ---------------------------------------------------------------- 轻松课用平均配速
def test_easy_uses_average_pace(db):
    """轻松跑本该全程均匀：平均配速快出处方区间 15% 以上 → 快于处方。"""
    steps = [{"step_type": "active", "name": "轻松跑", "duration_type": "distance",
              "duration_value": 6000, "target": _pace_target(300, 348), "note": "E 配速"}]
    wo = _mk_workout(db, structured=steps, session_type="easy", distance_km=6.0, duration_min=36)
    act = _mk_activity(db, wo.athlete_id, km=6.0, minutes=24)   # 平均 4:00/km = 240s，快出下限 300×0.85=255
    out = analyze_workout(wo, act, max_hr=190)
    assert out["verdict"] == "too_fast"
    assert out["pace"]["source"] == "平均配速"


# ---------------------------------------------------------------- 心率
def test_hr_overrun_downgrades_verdict(db):
    """配速达标但心率明显超出区间：整体判快于处方并给出理由。"""
    steps = [{"step_type": "active", "name": "轻松跑", "duration_type": "distance",
              "duration_value": 6000,
              "target": {"type": "hr", "from": 0.59, "to": 0.74, "label": "E"}, "note": "E"}]
    wo = _mk_workout(db, structured=steps, session_type="easy", distance_km=6.0, duration_min=36)
    act = _mk_activity(db, wo.athlete_id, km=6.0, minutes=36, avg_hr=172)   # 172/190=0.91 > 0.74+0.05
    out = analyze_workout(wo, act, max_hr=190)
    assert out["verdict"] == "too_fast"
    assert out["hr"]["verdict"] == "high"


def test_missing_max_hr_skips_hr_block(db):
    steps = [{"step_type": "active", "name": "轻松跑", "duration_type": "distance",
              "duration_value": 6000,
              "target": {"type": "hr", "from": 0.59, "to": 0.74, "label": "E"}, "note": "E"}]
    wo = _mk_workout(db, structured=steps, session_type="easy", distance_km=6.0, duration_min=36)
    act = _mk_activity(db, wo.athlete_id, km=6.0, minutes=36, avg_hr=172)
    out = analyze_workout(wo, act, max_hr=None)   # 档案没有最大心率：跳过心率判定
    assert out["hr"] is None
    assert out["verdict"] == "on_target"


# ---------------------------------------------------------------- 旧课兼容
def test_legacy_workout_without_structured(db):
    """旧课表没有结构化步骤：只核对完成度，不装懂配速。"""
    wo = _mk_workout(db, structured=None, session_type="quality", distance_km=10.0, duration_min=55)
    act = _mk_activity(db, wo.athlete_id, km=10.2, minutes=55)
    out = analyze_workout(wo, act, max_hr=190)
    assert out["verdict"] == "on_target"
    assert out["pace"] is None
    assert any("无结构化步骤" in r for r in out["reasons"])


# ---------------------------------------------------------------- 落库
def test_analyze_and_store_persists(db):
    wo = _mk_workout(db, structured=_interval_steps())
    act = _mk_activity(db, wo.athlete_id, km=10.0, minutes=55,
                       splits=_splits_at([300, 250, 252, 248, 305]))
    wo.status = "completed"
    wo.completed_activity_id = act.id
    db.commit()
    out = analyze_and_store(db, wo.id)
    db.refresh(wo)
    assert wo.analysis["version"] == 1
    assert wo.analysis["verdict"] == out["verdict"] == "on_target"
