"""强度分布卡的均心率估算（zones.estimate_zone_seconds）与端点接线。

背景：pro_insights 的强度分布卡对无设备逐段区间的活动回落调用
zones.estimate_zone_seconds，但该函数曾长期「只被引用、从未实现」——
任何同步了带均心率跑步（COROS 常态）的用户打 /pro-insights 必现 500，
且空态测试探不到（无活动时走不到这条路径）。这里锁两件事：
  1. 估算口径：均心率经 Karvonen 落位唯一一档，整段时长记入该档；
     缺基准/坏样本返回 None 不猜；
  2. 端点接线：带均心率活动打 /pro-insights 必须出可用的强度分布卡。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.services.zones import estimate_zone_seconds

MAX_HR, REST_HR = 190, 55
# Z1-Z5 的储备心率上界（Karvonen）：55 + pct * (190-55)
HRS = {"below_rest": 50, "z1": 62, "z2": 121, "z3": 162, "z4": 171, "z5": 191}


def test_anchor_zone_by_karvonen():
    """均心率 121 = 储备 49%（55+0.49*135）落 Z2 以下界？不——121 = 55+0.49*135 < Z1 上界 60%？

    0.494 落 Z1（0.50 下界不满足则取更低档前的判定见实现）；这里用精确档位中点验证。
    """
    # HRR 55%：Z1 与 Z2 边界偏 Z1（0.50 ≤ 0.55 < 0.60）
    out = estimate_zone_seconds(3600, 55 + 0.55 * (MAX_HR - REST_HR), MAX_HR, REST_HR)
    assert out is not None
    assert out["Z1"] == 3600 and sum(out.values()) == 3600
    # HRR 65%：Z2
    out = estimate_zone_seconds(3600, 55 + 0.65 * (MAX_HR - REST_HR), MAX_HR, REST_HR)
    assert out["Z2"] == 3600 and out["Z1"] == 0
    # HRR 75%：Z3；85%：Z4
    assert estimate_zone_seconds(3600, 55 + 0.75 * 135, MAX_HR, REST_HR)["Z3"] == 3600
    assert estimate_zone_seconds(3600, 55 + 0.85 * 135, MAX_HR, REST_HR)["Z4"] == 3600


def test_above_top_and_bad_samples():
    """均心率高于 Z5 上沿记满 Z5；缺基准/无效输入/低于静息一律 None。"""
    out = estimate_zone_seconds(1800, HRS["z5"], MAX_HR, REST_HR)
    assert out is not None and out["Z5"] == 1800

    assert estimate_zone_seconds(1800, 150, None, REST_HR) is None
    assert estimate_zone_seconds(1800, 150, MAX_HR, None) is None
    assert estimate_zone_seconds(1800, None, MAX_HR, REST_HR) is None
    assert estimate_zone_seconds(None, 150, MAX_HR, REST_HR) is None
    assert estimate_zone_seconds(0, 150, MAX_HR, REST_HR) is None
    assert estimate_zone_seconds(1800, HRS["below_rest"], MAX_HR, REST_HR) is None
    # 心率档案倒挂（max ≤ rest）：无法算储备心率
    assert estimate_zone_seconds(1800, 150, 180, 185) is None


def test_pro_insights_endpoint_with_avg_hr_run():
    """带均心率、无设备区间的跑步打 /pro-insights：必须出可用的强度分布卡。

    这是曾经的线上 500（estimate_zone_seconds 未实现）的回归锁定。"""
    from app import models
    from app.routers.training_status import pro_insights
    from factories import make_athlete
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(make_athlete())
    db.add(models.Activity(
        athlete_id=1, sport="run", title="轻松跑", platform="coros",
        start_time=datetime.now() - timedelta(days=1),
        distance_m=8000, duration_sec=2700, avg_hr=140))
    db.commit()

    payload = pro_insights(db)
    assert "insights" in payload
    zone = next(i for i in payload["insights"] if i["key"] == "zones")
    assert zone["available"] is True
    assert zone["samples"], "强度分布卡应给出五区间明细"
