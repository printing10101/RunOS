"""目标进度 + 庆祝（achieved）逻辑测试。

对标 NRC 的「完成即激励」：当当前预测成绩达到目标成绩时，标记为已达成（庆祝）。
进度 = 目标成绩 / 当前预测成绩，封顶 1.0；无预测时 progress 为 None。
"""
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from app.routers.athletes import _goal_dict


def _goal(**over):
    """构造一个 Goal 替身（仅依赖序列化所需字段）。"""
    base = {
        "id": 1, "race_type": "5k", "target_time_sec": 1200,
        "target_label": "5K 破 20", "target_date": date(2026, 12, 31),
        "priority": "primary", "status": "active",
        "created_at": datetime(2026, 1, 1),
    }
    base.update(over)
    return SimpleNamespace(**base)


def _pred(predictions):
    """构造 build_prediction 的替身：predictions = {race_type: {time_sec,...}}。"""
    return SimpleNamespace(predictions=predictions)


# ---------------------------------------------------------------- 达成判定与进度

def test_goal_behind_target_progress_below_one():
    """当前预测慢于目标 → 进度 <1，未达成。目标 1200s / 预测 1500s → 0.8。"""
    g = _goal(target_time_sec=1200)
    pred = _pred({"5k": {"time_sec": 1500}})
    out = _goal_dict(g, pred)
    assert out["progress"] == pytest.approx(0.8, abs=0.0001)
    assert out["achieved"] is False


def test_goal_met_exactly_marks_achieved():
    """预测恰好等于目标 → 达成（庆祝），进度封顶 1.0。"""
    g = _goal(target_time_sec=1200)
    pred = _pred({"5k": {"time_sec": 1200}})
    out = _goal_dict(g, pred)
    assert out["achieved"] is True
    assert out["progress"] == 1.0


def test_goal_exceeded_caps_progress_at_one():
    """预测比目标更快 → 达成，进度封顶 1.0（不超 100%）。"""
    g = _goal(target_time_sec=1200)
    pred = _pred({"5k": {"time_sec": 1100}})
    out = _goal_dict(g, pred)
    assert out["achieved"] is True
    assert out["progress"] == 1.0


def test_progress_rounding_four_decimals():
    """进度保留 4 位小数：目标 1800 / 预测 2400 = 0.75。"""
    g = _goal(target_time_sec=1800)
    pred = _pred({"5k": {"time_sec": 2400}})
    assert _goal_dict(g, pred)["progress"] == 0.75


# ---------------------------------------------------------------- 无预测/缺字段的退化

def test_no_prediction_gives_none_progress():
    """pred 为 None → 不计算进度，也不误报达成。"""
    out = _goal_dict(_goal(), None)
    assert out["progress"] is None
    assert out["achieved"] is False


def test_missing_race_type_prediction_gives_none():
    """预测里没有该目标的 race_type → progress None。"""
    pred = _pred({"10k": {"time_sec": 2400}})  # 没有 5k
    out = _goal_dict(_goal(race_type="5k"), pred)
    assert out["progress"] is None
    assert out["achieved"] is False


def test_prediction_without_time_sec_gives_none():
    """预测记录缺 time_sec → 不计算进度。"""
    pred = _pred({"5k": {}})
    out = _goal_dict(_goal(), pred)
    assert out["progress"] is None


def test_no_target_time_gives_none():
    """目标未设 target_time_sec（如仅目标日期）→ 不计算进度。"""
    pred = _pred({"5k": {"time_sec": 1500}})
    out = _goal_dict(_goal(target_time_sec=None), pred)
    assert out["progress"] is None


def test_inactive_goal_not_scored():
    """active 之外（paused/achieved）不计算进度（不放碾压庆祝）。"""
    pred = _pred({"5k": {"time_sec": 1100}})
    paused = _goal_dict(_goal(status="paused"), pred)
    assert paused["progress"] is None
    assert paused["achieved"] is False
    done = _goal_dict(_goal(status="achieved"), pred)
    assert done["progress"] is None


# ---------------------------------------------------------------- 序列化完整性

def test_serialized_fields_present():
    """序列化必须完整保留目标基础字段。"""
    g = _goal(target_date=date(2026, 12, 31))
    out = _goal_dict(g, None)
    assert out["id"] == 1
    assert out["race_type"] == "5k"
    assert out["target_time_sec"] == 1200
    assert out["target_label"] == "5K 破 20"
    assert out["target_date"] == "2026-12-31"
    assert out["priority"] == "primary"
    assert out["status"] == "active"
    assert isinstance(out["created_at"], str)


def test_none_target_date_serializes_null():
    out = _goal_dict(_goal(target_date=None), None)
    assert out["target_date"] is None


# ---------------------------------------------------------------- 集成：真实预测联动

def _fresh_db():
    from app.db import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_goal_progress_driven_by_real_prediction():
    """集成：用真实成绩跑 build_prediction，再算目标进度。

    5km 20:00 → VDOT≈50 → 10km 预测 ≈ 41:20(2480s)。
    目标 10km 破 40 (2400s) 更激进 → 进度<1、未达成；
    目标 10km 45:00 (2700s) 更保守 → 已达成。
    """
    from app.data import build_prediction
    from app.models import Goal, RaceResult
    from factories import make_athlete

    db = _fresh_db()
    a = make_athlete(name="测跑者", max_hr=190, resting_hr=52)
    db.add(a)
    db.commit()
    db.add(RaceResult(athlete_id=a.id, date=date(2026, 8, 1), race_type="5k",
                      distance_m=5000, time_sec=1200, is_official=True))
    db.commit()

    pred = build_prediction(db, a.id)
    assert "10k" in pred.predictions
    pred_t = pred.predictions["10k"]["time_sec"]
    assert 2300 < pred_t < 2700  # 大致落在 41 分上下

    aggressive = Goal(athlete_id=a.id, race_type="10k", target_time_sec=2400,
                      target_label="10K 破 40", priority="primary", status="active")
    db.add(aggressive)
    conservative = Goal(athlete_id=a.id, race_type="10k", target_time_sec=2700,
                        target_label="10K 45 分", priority="primary", status="active")
    db.add(conservative)
    db.commit()

    agg = _goal_dict(aggressive, pred)
    cons = _goal_dict(conservative, pred)

    # 激进目标还没达成
    assert agg["achieved"] is False
    assert 0 < agg["progress"] < 1.0
    # 保守目标已达成
    assert cons["achieved"] is True
    assert cons["progress"] == 1.0
    # 达成目标的进度必然不低于未达成的
    assert cons["progress"] >= agg["progress"]