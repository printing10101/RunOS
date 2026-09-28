"""比赛目标项目多元化：800m/1k/1500m/3k 等中短距离全链路可用。

词汇单源在 services/vdot.py（RACE_DISTANCES / GOAL_RACE_TYPES）。
本文件锁定三件事：
1. 目标 API 接受新项目，默认标签用全称（「1000米 3:20」），垃圾项目 422；
2. 规划器对短距离项目不再 KeyError（曾只排 5k/10k/hm/marathon 四种），
   且 planner 的跑量/长距离字典与项目词汇表对齐（防漂移）；
3. AI 目标提案校验接受新项目并按项目适用成绩区间。
"""
from __future__ import annotations

import pytest
from app.db import Base, get_db
from app.main import app
from app.services import ai_tools, planner, predictor, vdot
from factories import make_athlete
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.session import Session
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

SHORT_RACES = [("800m", 110), ("1k", 170), ("1500m", 250), ("3k", 600)]


@pytest.fixture()
def mem_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture()
def api(mem_db):
    mem_db.add(make_athlete(name="测试跑者", birth_year=1995,
                            height_cm=175.0, weight_kg=65.0,
                            resting_hr=55, max_hr=190))
    mem_db.commit()

    def _ov():
        yield mem_db

    app.dependency_overrides[get_db] = _ov
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------- 词汇单源一致性
def test_label_tables_cover_all_race_types():
    """标签表（简称/全称）必须覆盖全部项目，新增距离漏标签 = 前端裸显 key。"""
    assert set(vdot.RACE_LABELS) == set(vdot.RACE_DISTANCES)
    assert set(vdot.RACE_LABELS_FULL) == set(vdot.RACE_DISTANCES)
    assert set(predictor.TARGET_DISTANCES) == set(vdot.RACE_DISTANCES)


def test_planner_tables_cover_all_race_types():
    """规划器跑量字典必须覆盖全部项目——漏一项，该项目的目标一生成计划就 KeyError。"""
    assert set(planner.RACE_PEAK_KM) == set(vdot.GOAL_RACE_TYPES)
    assert set(planner.LONG_RUN_CAP_KM) == set(vdot.GOAL_RACE_TYPES)
    assert set(ai_tools._GOAL_TIME_RANGE) == set(vdot.GOAL_RACE_TYPES)


# ---------------------------------------------------------------- 目标 API
@pytest.mark.parametrize("race_type,sec", SHORT_RACES)
def test_goal_api_accepts_short_races(api, mem_db, race_type, sec):
    r = api.post("/api/athlete/goals", json={"race_type": race_type, "target_time_sec": sec})
    assert r.status_code == 200, r.text
    from app.models import Goal
    g = mem_db.query(Goal).order_by(Goal.id.desc()).first()
    assert g is not None and g.race_type == race_type and g.target_time_sec == sec


@pytest.mark.parametrize("race_type,sec,label", [
    ("1k", 200, "1000米 03:20"),
    ("800m", 110, "800米 01:50"),
    ("marathon", 10800, "全程马拉松 3:00:00"),
])
def test_goal_default_label_uses_full_name(api, mem_db, race_type, sec, label):
    """没填标签时自动生成全称标签，短距离不该显示成裸 key「1k」。"""
    r = api.post("/api/athlete/goals", json={"race_type": race_type, "target_time_sec": sec})
    assert r.status_code == 200, r.text
    from app.models import Goal
    g = mem_db.query(Goal).order_by(Goal.id.desc()).first()
    assert g.target_label == label


def test_goal_api_rejects_unknown_race_type(api):
    r = api.post("/api/athlete/goals", json={"race_type": "2k", "target_time_sec": 300})
    assert r.status_code == 422


# ---------------------------------------------------------------- 规划器
@pytest.mark.parametrize("race_type,sec", SHORT_RACES)
def test_generate_plan_supports_short_races(race_type, sec):
    """短距离目标能生成完整周期计划（曾直接 KeyError: race_type）。"""
    athlete = {"id": 1, "sex": "male", "age": 30, "max_hr": 190, "resting_hr": 55,
               "weight_kg": 65.0, "training_age_years": 3}
    slots = [{"weekday": 1, "start_time": "19:00", "duration_minutes": 60, "kind": "available"},
             {"weekday": 3, "start_time": "19:00", "duration_minutes": 60, "kind": "available"},
             {"weekday": 6, "start_time": "09:00", "duration_minutes": 120, "kind": "available"}]
    plan = planner.generate_plan(athlete, {"race_type": race_type, "target_time_sec": sec,
                                           "target_label": "", "target_date": None},
                                 current_vdot=52.0, talent_score=70, weekly_km_now=30,
                                 available_slots=slots)
    assert plan["race_type"] == race_type
    assert plan["weeks"] and all(w["workouts"] for w in plan["weeks"])
    # 短距离峰值周跑量不应按马拉松档（75km）拉满
    assert plan["weekly_km_peak"] <= planner.RACE_PEAK_KM[race_type] * 1.2


def test_short_race_long_run_feasibility_has_sane_floor():
    """800m 的「峰值长距离需时」不得按比例缩水到 4 分钟——中短距离同样要长距离课。"""
    slots = [{"weekday": 6, "start_time": "09:00", "duration_minutes": 30, "kind": "available"}]
    out = planner.assess_feasibility("800m", 110, 52.0, 70, 16, slots)
    assert out["long_run_time"]["peak_long_run_needs_minutes"] >= 40
    assert out["long_run_time"]["ok"] is False


# ---------------------------------------------------------------- AI 目标提案校验
def test_check_goal_update_accepts_1k(mem_db):
    a = make_athlete(name="测试跑者", birth_year=1995, height_cm=175.0, weight_kg=65.0)
    mem_db.add(a)
    mem_db.commit()
    out = ai_tools.check_goal_update(mem_db, a, {"race_type": "1k", "target_time_sec": 200})
    assert out["ok"] is True, out
    assert out["_internal"]["new_values"]["race_type"] == "1k"


def test_check_goal_update_rejects_out_of_range_time(mem_db):
    """800m 目标 110 秒被「固定下限 600 秒」的年代会误拒，区间必须按项目走。"""
    a = make_athlete(name="测试跑者", birth_year=1995, height_cm=175.0, weight_kg=65.0)
    mem_db.add(a)
    mem_db.commit()
    out = ai_tools.check_goal_update(mem_db, a, {"race_type": "800m", "target_time_sec": 110})
    assert out["ok"] is True, out
    bad = ai_tools.check_goal_update(mem_db, a, {"race_type": "800m", "target_time_sec": 10})
    assert bad["ok"] is False and any("超出合理范围" in r for r in bad["reasons"])


def test_precheck_goal_rejects_unknown_race_type(mem_db: Session):
    a = make_athlete(name="测试跑者", birth_year=1995, height_cm=175.0, weight_kg=65.0)
    mem_db.add(a)
    mem_db.commit()
    out = ai_tools.precheck_goal_impl(mem_db, a, {"race_type": "2k"})
    assert out["ok"] is False and "不支持的比赛项目" in out["reasons"][0]
