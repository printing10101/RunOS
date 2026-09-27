"""跑鞋防伤联动测试。

规则：旧鞋（磨损 ≥0.8 且 active）遇上高负荷（ACWR > 1.3）才触发防伤提醒；
ACWR > 1.5 升级为 high 等级。新鞋/低负荷即使 ACWR 高也给不吃提醒。
"""
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from app.routers.gear import _gear_dict


def _gear(**over):
    """构造一个 Gear 替身（仅依赖序列化所需字段）。"""
    base = {
        "id": 1, "name": "缓震鞋", "kind": "shoe", "brand": "X",
        "start_date": date(2026, 1, 1), "initial_km": 0.0,
        "retire_km": 600.0, "status": "active", "notes": "",
        "created_at": datetime(2026, 1, 1),
    }
    base.update(over)
    return SimpleNamespace(**base)


def _gear_for_wear(retire_km, used_km, initial_km=0.0):
    return _gear(initial_km=initial_km, retire_km=retire_km), used_km


# ---------------------------------------------------------------- 防伤联动核心

def test_old_shoe_high_acwr_warns_high():
    """旧鞋(磨损≥0.8) + ACWR 1.6 → high 级提醒。"""
    g, used = _gear_for_wear(600, 500)  # 500/600 ≈ 0.83 ≥ 0.8
    out = _gear_dict(g, used, 3, acwr=1.6)
    assert out["injury"] is not None
    assert out["injury"]["level"] == "high"
    assert "换鞋" in out["injury"]["msg"]


def test_old_shoe_medium_acwr_warns_medium():
    """ACWR 在 1.3~1.5 之间 → medium 级提醒。"""
    g, used = _gear_for_wear(600, 500)
    out = _gear_dict(g, used, 3, acwr=1.4)
    assert out["injury"]["level"] == "medium"


def test_old_acwr_threshold_boundary():
    """ACWR 恰好 1.3 → 不触发（规则是 >1.3）。"""
    g, used = _gear_for_wear(600, 500)
    out = _gear_dict(g, used, 3, acwr=1.3)
    assert out["injury"] is None


def test_new_shoe_no_warning_even_high_acwr():
    """新鞋（磨损低）即使 ACWR 高也不触发防伤提醒。"""
    g, used = _gear(initial_km=50, retire_km=600), 100  # (50+100)/600=0.25
    out = _gear_dict(g, used, 1, acwr=1.9)
    assert out["injury"] is None


def test_old_shoe_low_acwr_no_warning():
    """旧鞋但 ACWR ≤1.3（负荷正常）→ 不触发。"""
    g, used = _gear_for_wear(600, 500)
    out = _gear_dict(g, used, 3, acwr=1.0)
    assert out["injury"] is None


def test_retired_shoe_no_warning():
    """已退役的鞋不触发防伤提醒。"""
    g, used = _gear_for_wear(600, 500)
    g.status = "retired"
    out = _gear_dict(g, used, 3, acwr=1.8)
    assert out["injury"] is None


def test_non_shoe_kind_no_warning():
    """非跑鞋（watch）即使磨损高也不触发。"""
    g = _gear(kind="watch", retire_km=600)
    out = _gear_dict(g, used_km=500, run_count=0, acwr=1.8)
    assert out["injury"] is None


# ---------------------------------------------------------------- 磨损 flag 联动


def test_wear_flag_ok_below_80():
    g, used = _gear_for_wear(600, 300)
    out = _gear_dict(g, used, 0)
    assert out["flag"] == "ok"
    assert out["flag_note"] == ""
    assert out["wear_pct"] == 50


def test_wear_flag_warning_at_80():
    g, used = _gear_for_wear(600, 480)
    out = _gear_dict(g, used, 0)
    assert out["flag"] == "warning"
    assert "接近退役里程" in out["flag_note"]


def test_wear_flag_overdue_at_100():
    g, used = _gear_for_wear(600, 600)
    out = _gear_dict(g, used, 0)
    assert out["flag"] == "overdue"
    assert "超退役里程" in out["flag_note"]


def test_wear_pct_capped_at_150():
    """wear_pct 封顶 150，避免超里程后台数显示爆表。"""
    g, used = _gear_for_wear(600, 1200)
    out = _gear_dict(g, used, 0)
    assert out["wear_pct"] == 150


def test_remaining_km_not_negative():
    g, used = _gear_for_wear(600, 700)
    out = _gear_dict(g, used, 0)
    assert out["remaining_km"] == 0.0
    assert out["total_km"] == pytest.approx(700)


# ---------------------------------------------------------------- 集成：ACWR 实时计算联动

def _fresh_db():
    from app.db import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_current_acwr_from_recent_loads():
    """集成：_current_acwr 依据近 28 天真实负荷算 ACWR。"""
    from datetime import timedelta

    from app.models import Activity
    from app.routers.gear import _current_acwr
    from factories import make_athlete

    db = _fresh_db()
    a = make_athlete(name="测跑者", max_hr=190, resting_hr=52)
    db.add(a)
    db.commit()
    now = datetime.now()
    # 恒定 100/天，共 28 天 → ACWR=1.0
    for i in range(28):
        db.add(Activity(athlete_id=a.id, sport="run", start_time=now - timedelta(days=i),
                        duration_sec=3600, distance_m=10000, avg_hr=145, training_load=100.0))
    db.commit()
    assert _current_acwr(db) == pytest.approx(1.0, abs=0.02)


def test_current_acwr_none_without_athlete():
    from app.routers.gear import _current_acwr
    db = _fresh_db()
    assert _current_acwr(db) is None


def test_current_acwr_none_without_activity():
    from app.routers.gear import _current_acwr
    from factories import make_athlete
    db = _fresh_db()
    db.add(make_athlete(name="无人训练"))
    db.commit()
    assert _current_acwr(db) is None