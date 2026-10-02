"""官方↔本地交叉校验（services/crosscheck.py）的口径锁定。

锚定三条：
- 背离是「判断分歧」不是数值差：双方同处正常档时数值再悬殊也不算背离；
- 任一侧数据缺席的日子跳过，不硬凑可比日；
- 官方 VO2max 趋势跨度不足 28 天如实缺省，不出「上升/下降」结论。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from app import models
from app.db import get_db
from app.main import app
from app.services import crosscheck
from factories import make_athlete
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

TODAY = date(2026, 9, 30)


@pytest.fixture()
def mem_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
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


# ---------------------------------------------------------------- 逐日本地 ACWR

def test_acwr_series_golden_and_window_guard():
    """30 天恒定负荷 700：窗口补满后每天 ACWR=1.0；前 27 天窗口不完整跳过。"""
    daily = [{"date": (TODAY - timedelta(days=29 - i)).isoformat(), "load": 700}
             for i in range(30)]
    series = crosscheck.acwr_series(daily)
    first = TODAY - timedelta(days=29) + timedelta(days=27)
    assert all(2 > v > 0 for v in series.values())
    assert min(series) == first
    assert series[TODAY] == 1.0


def test_acwr_series_empty():
    assert crosscheck.acwr_series([]) == {}


# ---------------------------------------------------------------- 负荷背离

def _snaps(entries: list[tuple[date, float]]) -> list[dict]:
    return [{"date": d, "load_ratio": lr, "vo2max": None, "race_predictions": {}}
            for d, lr in entries]


def test_divergence_triggered_when_official_flags_risk():
    """官方连日 high（1.6）而本地 normal（1.0）：4/7 天背离 → 触发并给例子。"""
    entries = [(TODAY - timedelta(days=i), 1.6 if i % 2 == 0 else None)
               for i in range(6, 0, -1)]
    entries += [(TODAY, 1.6)]
    snaps = _snaps([e for e in entries if e[1] is not None])
    local = {s["date"]: 1.0 for s in snaps}
    div = crosscheck.load_divergence(snaps, local)
    assert div["days_compared"] == 4 and div["divergent_days"] == 4
    assert div["triggered"] is True
    assert div["worst_example"]["official"] == 1.6
    assert "背离" in div["detail"]


def test_divergence_not_triggered_when_bands_agree():
    """官方 1.2 vs 本地 1.1：都在正常档，数值差是口径差异不是分歧。"""
    entries = [(TODAY - timedelta(days=i), 1.2) for i in range(6, -1, -1)]
    div = crosscheck.load_divergence(_snaps(entries),
                                     {d: 1.1 for d, _ in entries})
    assert div["days_compared"] == 7 and div["divergent_days"] == 0
    assert div["triggered"] is False and div["detail"] is None


def test_divergence_requires_one_side_abnormal():
    """官方 1.35（high）vs 本地 1.25（normal）：分歧；反向 low 同理各自算一天。"""
    entries = [(TODAY - timedelta(days=1), 1.35), (TODAY, 0.7)]
    local = {TODAY - timedelta(days=1): 1.25, TODAY: 1.0}
    div = crosscheck.load_divergence(_snaps(entries), local)
    assert div["divergent_days"] == 2


def test_divergence_skips_days_missing_either_side():
    """官方没同步（load_ratio None）或本地没负荷的日子：跳过不凑数。"""
    entries = [(TODAY - timedelta(days=2), 1.6), (TODAY - timedelta(days=1), None),
               (TODAY, 1.6)]
    snaps = _snaps([e for e in entries if e[1] is not None])
    local = {TODAY: 1.0}   # 前两天本地无数据
    div = crosscheck.load_divergence(snaps, local)
    assert div["days_compared"] == 1 and div["triggered"] is False


# ---------------------------------------------------------------- VO2max 趋势

def test_vo2max_trend_up():
    """28 天前基线中位 50，最新 53 → +3.0，结论点名上升。"""
    pts = [(TODAY - timedelta(days=d), 50.0) for d in (40, 35, 30)]
    pts += [(TODAY - timedelta(days=5), 52.0), (TODAY, 53.0)]
    out = crosscheck.vo2max_trend([{"date": d, "vo2max": v} for d, v in pts])
    assert out["latest"] == 53.0
    assert out["prior_median"] == 50.0 and out["delta"] == 3.0
    assert "上升" in out["detail"]


def test_vo2max_trend_short_span_honest():
    """记录只有 10 天跨度：不算趋势，delta 保持 None。"""
    pts = [(TODAY - timedelta(days=10 - i), 50.0 + i * 0.5) for i in range(10)]
    out = crosscheck.vo2max_trend([{"date": d, "vo2max": v} for d, v in pts])
    assert out["delta"] is None and "不足" in out["detail"]


def test_vo2max_trend_no_data():
    out = crosscheck.vo2max_trend([])
    assert out["latest"] is None and out["n"] == 0


# ---------------------------------------------------------------- 装配与端点

def test_build_crosscheck_empty_without_snapshots():
    assert crosscheck.build_crosscheck([], []) == {"empty": True}


def test_training_status_endpoint_carries_crosscheck(mem_db, client):
    mem_db.add(make_athlete())
    mem_db.commit()
    r = client.get("/api/training-status")
    assert r.status_code == 200
    assert r.json()["crosscheck"] == {"empty": True}

    db_row = models.FitnessSnapshot(athlete_id=1, date=TODAY, load_ratio=1.7,
                                    vo2max=55)
    mem_db.add(db_row)
    mem_db.commit()
    r2 = client.get("/api/training-status")
    cc = r2.json()["crosscheck"]
    assert cc["empty"] is False
    assert cc["vo2max_trend"]["latest"] == 55.0
