"""VDOT 引擎黄金值测试。

参照 Daniels《Running Formula》官方 VDOT 表：
- 5km 20:00  → VDOT 50.1
- 全马 3:00:00 → VDOT 53.5
- VDOT 50 的等效成绩：5k 20:03 / 10k 41:20 / 全马 3:10:40
"""
import pytest
from app.services import vdot


# ---------------------------------------------------------------- 官方表对照
@pytest.mark.parametrize("distance_m,time_sec,expected", [
    (5000, 20 * 60, 50.1),
    (42195, 3 * 3600, 53.5),
])
def test_daniels_table_golden_values(distance_m, time_sec, expected):
    """与 Daniels 官方表逐点核对，容差 ±0.2。"""
    assert vdot.vdot_from_performance(distance_m, time_sec) == pytest.approx(expected, abs=0.2)


def test_equivalent_times_match_daniels_table():
    """VDOT 50 的等效成绩必须与官方表一致。"""
    eq = vdot.equivalent_times(50)
    assert eq["5k"] == pytest.approx(1202.7, abs=1)        # 20:03
    assert eq["10k"] == pytest.approx(2479.8, abs=2)       # 41:20
    assert eq["marathon"] == pytest.approx(11439.7, abs=3)  # 3:10:40


# ---------------------------------------------------------------- 自洽性
@pytest.mark.parametrize("distance_m", [1500, 3000, 5000, 10000, 21097.5, 42195])
@pytest.mark.parametrize("vdot_value", [35.0, 45.0, 55.0, 65.0])
def test_roundtrip_vdot_time_vdot(distance_m, vdot_value):
    """vdot → 成绩 → vdot 必须回到原值（正解与反解互为逆运算）。"""
    t = vdot.time_from_vdot(distance_m, vdot_value)
    assert vdot.vdot_from_performance(distance_m, t) == pytest.approx(vdot_value, abs=0.05)


def test_equivalent_times_monotonic_increasing():
    """距离越长，等效成绩时间越长。"""
    eq = vdot.equivalent_times(50)
    order = ["800m", "1k", "1500m", "3k", "5k", "10k", "hm", "marathon"]
    times = [eq[k] for k in order]
    assert times == sorted(times)


def test_faster_time_yields_higher_vdot():
    v_slow = vdot.vdot_from_performance(5000, 25 * 60)
    v_fast = vdot.vdot_from_performance(5000, 20 * 60)
    assert v_fast > v_slow


# ---------------------------------------------------------------- 配速区间
def _pace_to_sec(pace: str) -> float:
    m, s = pace.replace("/km", "").split(":")
    return int(m) * 60 + float(s)


def test_daniels_paces_ordered_easy_to_fast():
    """E 慢于 M 慢于 T 慢于 I 慢于 R（每公里秒数依次递减）。"""
    zones = {z["key"]: z for z in vdot.daniels_paces(50)}
    keys = ["easy", "marathon", "threshold", "interval", "repetition"]
    secs = [_pace_to_sec(zones[k]["pace_from"]) for k in keys]
    assert secs == sorted(secs, reverse=True), f"配速区间顺序异常: {list(zip(keys, secs, strict=True))}"


def test_daniels_paces_values_for_vdot50():
    zones = {z["key"]: z for z in vdot.daniels_paces(50)}
    assert zones["easy"]["pace_from"] == "4:54/km"
    assert zones["threshold"]["pace_from"] == "4:15/km"


def test_time_str_format():
    assert vdot.time_str(3661) == "1:01:01"
    assert vdot.time_str(1200) == "20:00"
