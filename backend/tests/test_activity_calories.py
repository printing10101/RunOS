"""手动录入活动卡路里估算的回归测试。

锁定估算语义：有心率+年龄档案走 Keytel 公式，否则 MET 法；
体重取档案真实值，未建档时回退 65 kg。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from app import models, schemas
from app.routers.activities import _estimate_calories, add_activity, update_activity
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    # 模型已改为「可解析字段无默认值」：任何创建路径都必须显式给值
    s.add(models.Athlete(name="测试跑者", sex="male", birth_year=1996,
                         height_cm=175, weight_kg=70,
                         resting_hr=52, max_hr=190, hrv_baseline=60,
                         training_age_years=2))
    s.commit()
    yield s
    s.close()


# ---------------------------------------------------------------- 纯函数
def test_met_fallback_uses_given_weight():
    """MET 法必须用传入体重而非写死 65：70kg 骑行 1h @8MET = 560 kcal。"""
    assert _estimate_calories("ride", 3600, None, 0, weight_kg=70) == 560
    # 未传体重（如同步导入的旧路径）回退 65
    assert _estimate_calories("ride", 3600, None, 0) == 520


def test_keytel_formula_with_hr_and_age():
    """有心率+年龄时走 Keytel 心率公式（ male 150bpm/70kg/30岁 ≈ 11.4 kcal/min）。"""
    kcal = _estimate_calories("run", 3600, 150, 10000, weight_kg=70, age=30, sex="male")
    expected = (-55.0969 + 0.6309 * 150 + 0.1988 * 70 + 0.2017 * 30) / 4.184 * 60
    assert kcal == int(expected)
    assert kcal > 500  # 明显高于纯 MET 法对同配速的估算，确认公式确实生效


def test_run_met_by_speed():
    """6km/h 以下按步行 1.0 MET，快跑按 ACSM 速度近似。"""
    slow = _estimate_calories("run", 3600, None, 5000, weight_kg=65)   # 5km/h
    fast = _estimate_calories("run", 3600, None, 12000, weight_kg=65)  # 12km/h
    assert slow == 65
    assert fast == int(12 * 1.036 * 65)


# ---------------------------------------------------------------- 路由层
def test_add_activity_with_distance_estimates_calories(db):
    """录入带距离/时长、未填卡路里的活动正常返回，卡路里落库。"""
    d = add_activity(
        schemas.ActivityIn(sport="run", start_time=datetime.now() - timedelta(hours=2),
                           duration_sec=3600, distance_m=10000, avg_hr=150),
        db)
    assert d["calories"] and d["calories"] > 0


def test_update_activity_recomputes_calories(db):
    """更新时长/距离重算卡路里：athlete 取值必须在计算之前。"""
    d = add_activity(
        schemas.ActivityIn(sport="run", start_time=datetime.now() - timedelta(hours=2),
                           duration_sec=3600, distance_m=10000),
        db)
    d2 = update_activity(d["id"],
                         schemas.ActivityUpdate(duration_sec=3000, distance_m=10000), db)
    assert d2["activity"]["calories"] and d2["activity"]["calories"] > 0


def test_explicit_calories_not_overwritten(db):
    """用户自带卡路里时不得被估算值覆盖。"""
    d = add_activity(
        schemas.ActivityIn(sport="run", start_time=datetime.now() - timedelta(hours=2),
                           duration_sec=3600, distance_m=10000, calories=999),
        db)
    assert d["calories"] == 999
