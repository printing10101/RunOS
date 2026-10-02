"""预测演变曲线（services/prediction_trend.py）的口径锁定。

锚定三条：
- 本地快照按 (race_date, race_type, distance_m) 分组，组内按快照日有序；
- 官方当期预测按项目键对照（half_marathon → hm 归一），不挂比赛日；
- 赛后实测回填只带第一条，不构造凭空对比。
"""
from __future__ import annotations

from datetime import date

import pytest
from app import models
from app.db import get_db
from app.main import app
from app.services.prediction_trend import (
    _calibration_note,
    build_prediction_trend,
    calibration_summary,
)
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
    s.add(make_athlete())
    s.commit()
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


def _seed_10k_race(db, with_actual=False):
    race_date = date(2026, 10, 25)
    for i, (on, sec) in enumerate([(date(2026, 9, 25), 3300), (date(2026, 9, 26), 3290),
                                   (date(2026, 9, 28), 3280)]):
        db.add(models.RacePrediction(
            athlete_id=1, race_date=race_date, race_type="10k", race_name="秋季十公里",
            distance_m=10000, predicted_sec=sec, predicted_on=on,
            **({"actual_sec": 3310, "delta_pct": 0.9} if (with_actual and i == 2) else {})))
    return race_date


def test_grouping_series_order_and_official_join(mem_db):
    _seed_10k_race(mem_db)
    mem_db.add(models.FitnessSnapshot(
        athlete_id=1, date=date(2026, 9, 27), load_ratio=1.1,
        race_predictions={"5k": 1500, "10k": 3250, "half_marathon": 6600}))
    mem_db.commit()
    body = build_prediction_trend(mem_db, 1)
    assert body["empty"] is False
    assert len(body["races"]) == 1
    g = body["races"][0]
    assert g["race_type"] == "10k" and g["race_name"] == "秋季十公里"
    assert [p["predicted_sec"] for p in g["local_series"]] == [3300, 3290, 3280]
    # 官方对照线按 race_type 挂接；half_marathon 键不会错挂到 10k 组
    assert len(g["official_series"]) == 1
    assert g["official_series"][0]["predicted_sec"] == 3250


def test_official_half_marathon_key_maps_to_hm(mem_db):
    db = mem_db
    db.add(models.RacePrediction(
        athlete_id=1, race_date=date(2026, 12, 6), race_type="hm", race_name="城市半马",
        distance_m=21097.5, predicted_sec=6300, predicted_on=date(2026, 9, 29)))
    db.add(models.FitnessSnapshot(
        athlete_id=1, date=date(2026, 9, 29),
        race_predictions={"half_marathon": 6450}))
    db.commit()
    g = build_prediction_trend(db, 1)["races"][0]
    assert g["race_type"] == "hm" and len(g["official_series"]) == 1
    assert g["official_series"][0]["predicted_sec"] == 6450


def test_actual_backfill_carried_once(mem_db):
    _seed_10k_race(mem_db, with_actual=True)
    mem_db.commit()
    g = build_prediction_trend(mem_db, 1)["races"][0]
    assert g["actual"]["actual_sec"] == 3310 and g["actual"]["delta_pct"] == 0.9


def test_race_type_filter_and_empty(mem_db):
    _seed_10k_race(mem_db)
    mem_db.commit()
    body = build_prediction_trend(mem_db, 1, race_type="hm")
    assert body["empty"] is True and body["races"] == []
    assert build_prediction_trend(mem_db, 2)["empty"] is True   # 别的档案无数据


def test_prediction_trend_endpoint(mem_db, client):
    r = client.get("/api/stats/prediction-trend")
    assert r.status_code == 200
    assert r.json() == {"empty": True, "races": []}

    _seed_10k_race(mem_db)
    mem_db.commit()
    body = client.get("/api/stats/prediction-trend").json()
    assert body["empty"] is False and body["races"][0]["race_type"] == "10k"


# ---------------------------------------------------------------- 校准报告

def test_calibration_empty_honest(mem_db):
    cal = calibration_summary(mem_db, 1)
    assert cal["empty"] is True and cal["n"] == 0
    assert cal["bias_pct"] is None and "校准" in cal["note"]


def test_calibration_bias_and_note(mem_db):
    # 两次实测：+3%（偏慢）与 +1% → 平均 +2.0，压线算可信
    for race_date, delta in ((date(2026, 3, 1), 3.0), (date(2026, 5, 1), 1.0)):
        mem_db.add(models.RacePrediction(
            athlete_id=1, race_date=race_date, race_type="10k", race_name="测试",
            distance_m=10000, predicted_sec=3000, predicted_on=race_date,
            actual_sec=round(3000 * (1 + delta / 100)), delta_pct=delta))
    mem_db.commit()
    cal = calibration_summary(mem_db, 1)
    assert cal["n"] == 2 and cal["bias_pct"] == 2.0
    assert cal["mean_abs_pct"] == 2.0 and "可信" in cal["note"]
    assert cal["items"][0]["actual_str"]

    # 偏乐观/偏保守场景的文案口径单独锁定
    assert "偏乐观" in _calibration_note(1, 4.0)
    assert "偏保守" in _calibration_note(1, -4.0)


def test_calibration_endpoint(mem_db, client):
    r = client.get("/api/stats/prediction-calibration")
    assert r.status_code == 200 and r.json()["empty"] is True
