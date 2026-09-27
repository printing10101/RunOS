"""成绩预测增强的行为锁定：CS 拟合质量（R²）、多来源预测区间、赛前快照与偏差复盘。

背景：预测此前只有单点估计——多来源估计被加权混合后丢弃，模型间分歧
（不确定性的直接度量）看不见；临界速度拟合不报告拟合优度，两点拟合与
高质量拟合在展示上无差别；README 承诺的「赛前预测 vs 实际偏差复盘」
没有真值锚点（预测不留档，比赛跑完无从对比）。这里锁住四件事：
  1. R² 对完美/偏离数据的区分，以及低 R² 时 cs_reliable 的降档判定
  2. 区间存在时必包住单点（区间是「单点的可信范围」）
  3. 快照按 (race_date, distance, predicted_on) 幂等，一天一条
  4. 成绩录入回填最近赛前快照；无快照（如比赛早于一切预测）不构造对比
"""
from __future__ import annotations

from datetime import date, timedelta

from app import models
from app.data import backfill_race_prediction, snapshot_race_predictions
from app.services.predictor import BestEffort, fit_critical_speed, predict_performances
from factories import make_athlete
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


def _efforts() -> list[BestEffort]:
    """三场不同距离的实测：真实数据不会严格共线（R²<1），但一致性高。"""
    return [
        BestEffort(distance_m=5000, time_sec=1500, date="2026-09-01", label="5k"),
        BestEffort(distance_m=10000, time_sec=3120, date="2026-08-15", label="10k"),
        BestEffort(distance_m=21097.5, time_sec=6900, date="2026-07-01", label="hm"),
    ]


# ---------------------------------------------------------------- CS 拟合质量

def test_cs_r2_perfect_for_collinear_points():
    """三点严格落在 D = CS·t + D' 上：R² = 1。"""
    cs, dp, r2 = fit_critical_speed([
        BestEffort(distance_m=1500, time_sec=300),
        BestEffort(distance_m=2400, time_sec=600),
        BestEffort(distance_m=3600, time_sec=1000),
    ])
    assert r2 is not None and r2 > 0.9999
    assert abs(cs - 3.0) < 1e-6 and abs(dp - 600.0) < 1e-6


def test_cs_r2_penalizes_off_line_points():
    """中间点大幅偏离直线：R² 显著下降但仍可算（不静默吞掉拟合劣化）。

    注：端点偏离会被回归线「追过去」（杠杆效应），只有中间点的偏离才能
    有效拉低 R²——测试数据必须用这种构型。"""
    _, _, r2_good = fit_critical_speed([
        BestEffort(distance_m=1500, time_sec=300),
        BestEffort(distance_m=2400, time_sec=600),
        BestEffort(distance_m=3600, time_sec=1000),
    ])
    _, _, r2_bad = fit_critical_speed([
        BestEffort(distance_m=1500, time_sec=300),
        BestEffort(distance_m=3000, time_sec=600),   # 直线上应为 2400，偏慢 600m
        BestEffort(distance_m=3600, time_sec=1000),
    ])
    assert r2_good > r2_bad
    assert r2_bad < 0.9


def test_cs_r2_collapses_on_dirty_sample():
    """坏记录混入（10k 跑了 2.5 小时，如误同步的徒步）→ R² 必须崩到 0.5 以下。

    正常跑步数据的 CS 拟合 R² 天然偏高（时间-距离强相关，实测 0.92-0.99），
    0.9 阈值恰好只拦真正的脏样本。"""
    cs, _, r2 = fit_critical_speed([
        BestEffort(distance_m=5000, time_sec=1500),
        BestEffort(distance_m=10000, time_sec=9000),
        BestEffort(distance_m=21097.5, time_sec=6900),
    ])
    assert cs > 0
    assert r2 is not None and r2 < 0.5


def test_cs_reliable_flag_downgrades_on_low_r2():
    """R²<0.9：结构化质量必须把 cs_reliable 标为 False（供降权与展示）。"""
    pred = predict_performances([
        BestEffort(distance_m=5000, time_sec=1500, date="2026-09-01", label="5k"),
        BestEffort(distance_m=10000, time_sec=9000, date="2026-08-15", label="10k"),
        BestEffort(distance_m=21097.5, time_sec=6900, date="2026-07-01", label="hm"),
    ])
    assert pred.cs_r2 is not None and pred.cs_r2 < 0.9
    assert pred.quality["cs_reliable"] is False


def test_quality_struct_fields():
    pred = predict_performances(_efforts())
    assert pred.quality["efforts"] == 3
    assert pred.quality["riegel_pairs"] is None   # pairs 由 build_prediction 查库补充
    assert pred.quality["cs_r2"] == pred.cs_r2


# ---------------------------------------------------------------- 预测区间

def test_interval_contains_point_when_present():
    """有区间的距离：P25 ≤ 单点 ≤ P75（区间是单点的可信范围）。"""
    pred = predict_performances(_efforts())
    with_interval = [(k, p) for k, p in pred.predictions.items() if p.get("interval_sec")]
    assert with_interval, "三场不同距离的多来源预测应至少有一个距离给出区间"
    for key, p in with_interval:
        lo, hi = p["interval_sec"]
        assert lo <= p["time_sec"] <= hi, f"{key}: 区间 [{lo},{hi}] 未包住 {p['time_sec']}"


def test_predictions_still_have_point_estimate():
    """区间是增强不是替换：单点字段（time_sec/time_str/pace）必须照常存在。"""
    pred = predict_performances(_efforts())
    for p in pred.predictions.values():
        assert p["time_sec"] > 0 and p["time_str"] and p["pace"]


# ---------------------------------------------------------------- 快照与偏差复盘

def _fresh_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_snapshot_idempotent_per_day():
    db = _fresh_db()
    db.add(make_athlete())
    db.commit()
    pred = predict_performances(_efforts())
    goal = models.Goal(athlete_id=1, race_type="10k", target_time_sec=3000,
                       target_date=date.today() + timedelta(days=30), status="active")
    db.add(goal)
    db.commit()

    assert snapshot_race_predictions(db, 1, pred) == 1
    assert snapshot_race_predictions(db, 1, pred) == 0   # 同日重放不再新增
    rows = db.query(models.RacePrediction).all()
    assert len(rows) == 1
    assert rows[0].predicted_on == date.today()
    assert rows[0].predicted_sec == pred.predictions["10k"]["time_sec"]


def test_backfill_uses_latest_snapshot_before_race_day():
    db = _fresh_db()
    db.add(make_athlete())
    race_day = date.today() + timedelta(days=30)
    db.add(models.RacePrediction(athlete_id=1, race_date=race_day, race_type="10k",
                                 race_name="测试赛", distance_m=10000,
                                 predicted_sec=3000, predicted_on=date.today()))
    db.add(models.RacePrediction(athlete_id=1, race_date=race_day, race_type="10k",
                                 race_name="测试赛", distance_m=10000,
                                 predicted_sec=2950, predicted_on=race_day - timedelta(days=1)))
    db.commit()

    race = models.RaceResult(athlete_id=1, date=race_day, race_type="10k",
                             distance_m=10000, time_sec=3080)
    assert backfill_race_prediction(db, 1, race) is True
    db.commit()
    rows = db.query(models.RacePrediction).order_by(models.RacePrediction.predicted_on).all()
    assert rows[0].actual_sec is None                     # 旧快照不动
    assert rows[1].actual_sec == 3080                     # 回填到比赛日当天或之前最近一条
    assert rows[1].delta_pct == round((3080 - 2950) / 2950 * 100, 1)


def test_backfill_false_without_snapshot():
    db = _fresh_db()
    race = models.RaceResult(athlete_id=1, date=date.today(), race_type="5k",
                             distance_m=5000, time_sec=1500)
    assert backfill_race_prediction(db, 1, race) is False
