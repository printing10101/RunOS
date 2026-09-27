"""有氧解耦与专业洞察接线的行为锁定。

背景：分半解析（activity_halves）与解耦计算原属 pro_insights 私有，详情页
拿不到单次数值；EF 趋势从 load_status 迁往 pro_insights 时实现丢失，前端
「有氧效率」卡长期吃死键恒空；build_pro_insights 引擎整体没有任何调用点。
本轮把解析下沉到 activity_detail（详情页透出）、重建 EF 趋势（load_status）、
洞察引擎接入 /api/training-status/pro-insights。这里锁住：
  1. 分半黄金值（series 流与 laps 两条路径）与解耦百分比
  2. 护栏：均心率活动返回 None、GPS 坏点段丢弃、半程过短不计
  3. EF 趋势的样本门槛与 delta 符号语义
  4. 洞察端点冒烟（结构可用，无设备数据时明确说不可用而非编造）
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from app.services import load_status
from app.services.activity_detail import activity_halves, decoupling_pct


def _run(days_ago: int, km: float, dur_sec: int, avg_hr: int | None,
         sport: str = "run") -> dict:
    return {"sport": sport, "start_time": datetime.now() - timedelta(days=days_ago),
            "distance_m": km * 1000, "duration_sec": dur_sec, "avg_hr": avg_hr,
            "elevation_m": 0}


def _series_points(half1_hr: int, half2_hr: int, pace_sec: int = 330) -> list[dict]:
    """构造逐点流：匀速 5.5min/km，前半心率 half1_hr、后半 half2_hr。

    首点 km=0/time=0（真实 streams 形态），每 0.5km 一点共 16 段（8km），
    中点处心率切换。"""
    pts = []
    for i in range(0, 17):
        pts.append({"km": i * 0.5, "time_sec": i * pace_sec // 2,
                    "hr": half1_hr if i <= 8 else half2_hr})
    return pts


# ---------------------------------------------------------------- 分半与解耦

def test_halves_from_series_golden():
    """前半 140 心率、后半 154 心率、匀速：后半同配速心率更高 → 正解耦 ≈ +9%。

    decoupling = 1 − ef2/ef1 = 1 − hr1/hr2（匀速时），即「后半心率高出前半的幅度」。"""
    a = {"duration_sec": 8 * 330, "avg_hr": 147, "distance_m": 8000,
         "elevation_m": 0, "raw": {"series": {"points": _series_points(140, 154)}}}
    ef1, ef2, p1, p2 = activity_halves(a)
    assert p1 == pytest.approx(p2)                      # 匀速：前后配速一致
    assert ef1 > ef2                                    # 后半同配速心率更高 → EF 更低
    expected = (1 - 140 / 154) * 100
    assert decoupling_pct(ef1, ef2) == pytest.approx(expected, abs=0.3)


def test_halves_returns_none_for_avg_hr_only():
    """只有活动均心率（无设备逐段数据）：明确返回 None，不合成假漂移。"""
    a = {"duration_sec": 3600, "avg_hr": 150, "distance_m": 10000,
         "elevation_m": 0, "raw": {}}
    assert activity_halves(a) is None


def test_halves_discards_gps_glitch_segments():
    """GPS 漂移段（>50km/h）必须整段丢弃，不得污染切半统计。"""
    pts = _series_points(140, 154)
    pts[12]["km"] += 5.0   # 某点距离突增 5km → 该段速度荒谬
    a = {"duration_sec": 8 * 330, "avg_hr": 147, "distance_m": 8000,
         "elevation_m": 0, "raw": {"series": {"points": pts}}}
    halves = activity_halves(a)
    assert halves is None or abs(decoupling_pct(halves[0], halves[1])) < 15


def test_decoupling_sign_semantics():
    """正值 = 效率下降（后半同配速心率更高）；负值 = 后半更省（负漂移/热身效应）。"""
    assert decoupling_pct(2.0, 1.8) > 0
    assert decoupling_pct(2.0, 2.2) < 0
    assert decoupling_pct(0, 1.8) == 0.0   # 除零护栏


# ---------------------------------------------------------------- EF 趋势

ATHLETE = {"max_hr": 190, "resting_hr": 50}


def test_ef_trend_requires_enough_samples():
    runs = [_run(i * 3, 8, 2400, 140) for i in range(4)]   # 近 8 周仅 4 次
    ef = load_status.aerobic_efficiency(runs, ATHLETE)
    assert ef["available"] is False and "5" in ef["verdict"]


def test_ef_trend_detects_improvement_and_decline():
    def recent_batch(hr: int):
        return [_run(d, 8, 2400, hr) for d in (0, 5, 10, 15, 20, 25)]    # 近 8 周

    def prior_batch(hr: int):
        return [_run(d, 8, 2400, hr) for d in (60, 65, 70, 75, 80, 85)]  # 之前 8 周

    improving = load_status.aerobic_efficiency(recent_batch(140) + prior_batch(150), ATHLETE)
    assert improving["available"] is True
    assert improving["delta_pct"] > 2                      # 近期心率更低 → EF 更高

    declining = load_status.aerobic_efficiency(recent_batch(150) + prior_batch(140), ATHLETE)
    assert declining["delta_pct"] < -2

    flat = load_status.aerobic_efficiency(recent_batch(145) + prior_batch(145), ATHLETE)
    assert abs(flat["delta_pct"]) <= 2


def test_ef_trend_filters_high_intensity():
    """储备心率 >75% 的课不进 EF 样本（高强度课的心率受结构干扰）。

    recent 混入 6 次高强度课：若未过滤，EF 会被拉低出现虚假「下降」。"""
    prior_easy = [_run(d, 8, 2400, 145) for d in (60, 65, 70, 75, 80, 85)]
    recent_easy = [_run(d, 8, 2400, 145) for d in (0, 5, 10, 15, 20, 25)]
    recent_hard = [_run(d, 8, 2400, 180) for d in (0, 5, 10, 15, 20, 25)]
    clean = load_status.aerobic_efficiency(recent_easy + prior_easy, ATHLETE)
    polluted = load_status.aerobic_efficiency(recent_easy + recent_hard + prior_easy, ATHLETE)
    assert clean["available"] is True
    assert polluted["delta_pct"] == pytest.approx(clean["delta_pct"],
                                                  abs=0.5)   # 高强度课被滤掉，结果不受影响


# ---------------------------------------------------------------- 洞察端点接线

def _fresh_db():
    from app import models
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_pro_insights_endpoint_smoke():
    """端点已接线（此前 build_pro_insights 全仓零调用）：无设备数据时
    返回结构并明确声明不可用，而不是报错或编造数值。"""
    from app.routers.training_status import pro_insights
    from factories import make_athlete

    db = _fresh_db()
    assert pro_insights(db).get("empty") is True   # 无档案

    db.add(make_athlete())
    db.commit()
    payload = pro_insights(db)
    assert "insights" in payload and isinstance(payload["data_gaps"], list)
    dec = next(i for i in payload["insights"] if i["key"] == "decoupling")
    assert dec["available"] is False and dec["unavailable_reason"]
