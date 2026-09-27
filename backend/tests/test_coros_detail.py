"""高驰 MCP 文本解析测试。

重点覆盖 getActivityDetail 的详情文本解析：步频/功率/训练负荷等字段
只存在于这段文本里，解析失效会让这些列长期为空且不报错（静默失效）。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.integrations.coros import (
    CorosAdapter,
    best_lap_group,
    parse_activity_detail,
    parse_fitness_assessment,
    parse_laps,
    parse_recovery_status,
    parse_sport_records,
    parse_training_load,
)

RUN_DETAIL = """🏃 Outdoor Run Activity Details
========================================

Workout Time: 40:00
Distance: 6.00 km
Total Time: 45:00
Average Pace: 6:40 /km
Moving Average Pace: 6:30 /km
Best Kilometer: 6:00 /km
Average Heart Rate: 150 bpm
Average Cadence: 170 spm
Average Stride Length: 1.00 m
Average Power: 200 W
Elevation Gain / Loss: 10 m / 12 m
Calories: 500 kcal
Training Load: 50
Aerobic TE: 2.0
Anaerobic TE: 1.0
Training Focus: Base
Performance: Best
"""

STRENGTH_DETAIL = """🏋️ Strength Activity Details
========================================

Workout Time: 1:30
Total Time: 1:30
Sets: 3
Average Heart Rate: 130 bpm
Calories: 20 kcal
Training Load: 2
Aerobic TE: 0.1
Anaerobic TE: 0.0
Perceived Effort: Moderate
"""


def test_run_detail_full_extraction():
    m = parse_activity_detail(RUN_DETAIL)
    assert m["cadence"] == 170.0
    assert m["power"] == 200.0
    assert m["te_aerobic"] == 2.0
    assert m["te_anaerobic"] == 1.0
    assert m["training_load"] == 50.0
    assert m["avg_hr"] == 150
    assert m["elevation_gain"] == 10.0
    assert m["elevation_loss"] == 12.0
    assert m["stride_m"] == 1.00
    assert m["best_km_pace_sec"] == 6 * 60
    assert m["workout_time_sec"] == 40 * 60
    assert m["focus"] == "Base"
    assert m["performance"] == "Best"


def test_strength_detail_extraction():
    m = parse_activity_detail(STRENGTH_DETAIL)
    assert m["sets"] == 3
    assert m["training_load"] == 2.0
    assert m["te_anaerobic"] == 0.0
    assert m["perceived_effort"] == "Moderate"
    assert "cadence" not in m  # 力量训练没有步频，不应臆造


def test_detail_parser_never_raises():
    assert parse_activity_detail("") == {}
    assert parse_activity_detail("hello world") == {}
    assert parse_activity_detail("Training Load: abc") == {}


def test_partial_detail_only_returns_present_fields():
    m = parse_activity_detail("Workout Time: 10:00\nCalories: 100 kcal\n")
    assert m == {"workout_time_sec": 600}


# 合成数据（隐私守卫白名单值，勿替换为真实接口响应，见 tools/privacy_guard.py 头注释）
def test_sport_records_list_parsing():
    text = """1. Outdoor Run — 2026-01-01
   Location: 示例市
   Start Coordinates: 12.345678, 98.765432
   Time Window: startTimestamp=1000000000 | endTimestamp=1000001800
   Duration: 30:00 | Distance: 5.00 km
   Average Pace: 6:00 /km | Avg HR: 150 bpm | Calories: 400 kcal
   LabelId: 123456789012345678 | SportType: 100
"""
    rows = parse_sport_records(text)
    assert len(rows) == 1
    row = rows[0]
    assert row["label_id"] == "123456789012345678"
    assert row["sport_type"] == 100
    assert row["distance_m"] == 5000.0
    assert row["duration_sec"] == 30 * 60
    assert row["avg_hr"] == 150
    assert row["latitude"] == 12.345678
    assert row["longitude"] == 98.765432
    assert row["record_date"] == "2026-01-01"


# ---------------------------------------------------------------- 高阶数据（2026-09 扩展）

_APPLY_LAPS = CorosAdapter({}).apply_laps

FITNESS_TEXT = """Fitness Assessment Overview
========================

VO2max: 51
Running Level: 75
Threshold Pace: 4:57 /km
5 km Prediction: 23:50
10 km Prediction: 49:52
Half Marathon Prediction: 1:52:36
Marathon Prediction: 4:01:09
"""

RECOVERY_TEXT = """Recovery Status
========================

Recovery: 93%
Level: Heavy training allowed
Estimated Full Recovery: 14h
"""

LOAD_TEXT = """2026-09-17
Comment: Optimized
Short-Term Load: 62
Long-Term Load: 51
Load Ratio: 1.21

2026-09-16
Comment: Excessive
Short-Term Load: 86
Long-Term Load: 53
"""


def test_fitness_assessment_parsing():
    f = parse_fitness_assessment(FITNESS_TEXT)
    assert f["vo2max"] == 51
    assert f["running_level"] == 75
    assert f["threshold_pace_sec"] == 4 * 60 + 57
    pred = f["race_predictions"]
    assert pred["5k"] == 23 * 60 + 50
    assert pred["10k"] == 49 * 60 + 52
    assert pred["half_marathon"] == 3600 + 52 * 60 + 36
    assert pred["marathon"] == 4 * 3600 + 60 + 9
    assert parse_fitness_assessment("") == {}
    assert parse_fitness_assessment("nothing useful") == {}


def test_recovery_status_parsing():
    r = parse_recovery_status(RECOVERY_TEXT)
    assert r == {"recovery_pct": 93, "level": "Heavy training allowed",
                 "full_recovery_hours": 14}
    assert parse_recovery_status("") == {}


def test_training_load_parsing():
    days = parse_training_load(LOAD_TEXT)
    assert set(days) == {"2026-09-17", "2026-09-16"}
    assert days["2026-09-17"] == {"date": "2026-09-17", "comment": "Optimized",
                                  "short_load": 62, "long_load": 51, "load_ratio": 1.21}
    assert days["2026-09-16"]["short_load"] == 86
    assert "load_ratio" not in days["2026-09-16"]   # 缺字段不编造
    assert parse_training_load("") == {}


# 圈数据载荷按真机结构构造（数值为隐私守卫白名单量级的合成值）
LAPS_PAYLOAD = {
    "columns": [{"name": "lapIndex"}, {"name": "distance"}, {"name": "time"}],
    "lapGroups": [
        {"type": 10, "lapDistance": 100000, "fastLapIndexList": [1], "laps": [
            {"lapIndex": 1, "distance": 100000, "time": 300.0, "avgPace": 300.0,
             "avgHr": 150, "maxHr": 158, "avgPower": 200, "avgCadence": 170,
             "elevGain": 5.0, "totalDescent": 0.0, "adjustedPace": 295,
             "avgStrideLength": 95, "strideHeight": 75, "groundTime": 260,
             "strideRatio": 95, "formPower": 0, "legStiffness": 0},
            {"lapIndex": 2, "distance": 100000, "time": 280.0, "avgPace": 280.0,
             "avgHr": 162, "maxHr": 168, "avgPower": 210, "avgCadence": 174,
             "elevGain": 0.0, "totalDescent": 5.0},
        ]},
        {"type": -1, "lapDistance": 200000, "laps": [
            {"lapIndex": 1, "distance": 200000, "time": 580.0, "avgHr": 156},
        ]},
    ],
}


def test_laps_normalization_units():
    laps = parse_laps(LAPS_PAYLOAD)
    assert laps and len(laps["groups"]) == 2
    auto = laps["groups"][0]
    assert auto["type"] == 10 and auto["lap_distance_m"] == 1000.0
    l1, l2 = auto["laps"]
    assert l1["distance_m"] == 1000.0            # 0.01km → m
    assert l1["duration_sec"] == 300.0
    assert l1["pace_sec_per_km"] == 300          # 由 distance/time 推得
    assert l1["gap_sec_per_km"] == 295           # 官方坡度调整配速保留
    assert l1["stride_m"] == 0.95                # cm → m
    assert l1["vosc_mm"] == 75 and l1["gct_ms"] == 260 and l1["vr_pct"] == 9.5
    assert "elev_loss_m" not in l1               # 0 值剔除（无数据）
    assert l2["elev_loss_m"] == 5.0
    assert "formPower" not in l1                 # 噪声字段不透传
    assert best_lap_group(laps["groups"])["type"] == 10   # 自动 1km 组优先
    assert parse_laps({"lapGroups": []}) is None
    assert parse_laps("not a dict") is None


def test_apply_laps_projects_canonical_splits():
    act = SimpleNamespace(raw={"source": "coros_mcp"}, dynamics={})
    assert _APPLY_LAPS(act, parse_laps(LAPS_PAYLOAD)) is True
    splits = act.raw["splits"]
    assert len(splits) == 2 and splits[0]["index"] == 1
    assert splits[0]["avg_hr"] == 150 and splits[0]["gap_sec_per_km"] == 295
    assert act.raw["splits_source"] == "coros_laps"
    assert act.raw["coros_laps"]["groups"][1]["type"] == -1   # 全组保留
    # 动态按圈时长加权：两圈 vosc 均 75 → 75；gct 只有一圈有值 → 260
    assert act.dynamics["vosc_cm"] == 7.5
    assert act.dynamics["gct_ms"] == 260
    # 已有设备分段（佳明/Strava 或已投影过）时不再覆盖 splits，但全组圈数据仍留存
    act2 = SimpleNamespace(raw={"splits": [{"distance": 1000, "duration": 300}]}, dynamics={})
    assert _APPLY_LAPS(act2, parse_laps(LAPS_PAYLOAD)) is False
    assert act2.raw["splits"][0]["distance"] == 1000
    assert "coros_laps" in act2.raw
    assert _APPLY_LAPS(act, None) is False
