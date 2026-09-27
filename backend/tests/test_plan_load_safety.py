"""训练负荷安全边界回归测试（防过度训练）。

本文件锁定的是「计划生成不得把跑者推入过载」这条底线，对应三类真实缺陷：

1. 起始跑量不得用绝对硬底把低跑量跑者成倍放大
   （原实现 `max(15.0, weekly_km_now * 0.8)` 会把周跑量 5km 的初跑者一次拉到 15km）；
2. 周跑量爬升必须受单周增幅封顶约束
   （纯插值会在前几周出现远超 10% 的跳增）；
3. 基础期不得安排质量课、初跑者的力量课必须降档
   （基础期堆强度的定位矛盾，6 动作约 20 组的力量课对零基础跑者是单次过载）。

这些断言一旦被改回去必须立刻失败，避免过载逻辑被静默恢复。
"""
from __future__ import annotations

from app.services import planner

SLOTS = [
    {"weekday": 1, "start_time": "19:00", "duration_minutes": 75},
    {"weekday": 3, "start_time": "12:30", "duration_minutes": 60},
    {"weekday": 5, "start_time": "19:00", "duration_minutes": 60},
    {"weekday": 6, "start_time": "07:00", "duration_minutes": 180},
    {"weekday": 0, "start_time": "07:00", "duration_minutes": 45},
]

ATHLETE = {"id": 1, "sex": "male", "age": 19, "max_hr": 192, "resting_hr": 52,
           "weight_kg": 85, "training_age_years": 0.0}
GOAL = {"race_type": "5k", "target_time_sec": None, "target_label": None, "target_date": None}


def _plan(weekly_km_now: float) -> dict:
    return planner.generate_plan(ATHLETE, GOAL, current_vdot=29.8, talent_score=50.0,
                                 weekly_km_now=weekly_km_now, available_slots=SLOTS)


def test_start_volume_anchored_to_real_volume():
    """周跑量 5km 的初跑者，起始周跑量必须锚在真实跑量附近，不得被硬底放大。"""
    plan = _plan(5.0)
    first = plan["weeks"][0]["target_km"]
    assert first <= 5.0 + 0.5, f"起始周跑量 {first}km 相对真实 5km/周 明显放大（硬底回归？）"
    # 峰值同样必须受控：`start_km * 2.2` 的上限在真实锚点下约为 11km
    assert plan["weekly_km_peak"] <= 11.5, f"峰值周跑量 {plan['weekly_km_peak']}km 过高"


def test_peak_volume_scales_with_baseline():
    """峰值周跑量随基础跑量单调增长，且不脱离基础跑量一个数量级。"""
    low = _plan(5.0)["weekly_km_peak"]
    high = _plan(40.0)["weekly_km_peak"]
    assert low < high, "峰值跑量没有随基础跑量增长"
    assert low <= 5.0 * 2.2 + 0.5, "低基础跑者的峰值未被安全上限约束"


def test_weekly_km_increase_capped_at_ten_percent():
    """周跑量增长必须受 10% 单周增幅约束（含减量周之后的恢复周）。

    判定口径：基准只沿「非减量周」推进——减量周是刻意下调，不参与比较；
    恢复周必须落在「减量前基准 ×1.10」以内，防止减量后回跳补量。
    容差 1% 覆盖 round(km, 1) 的量化误差（最坏 +0.05km）。
    """
    for weekly_km_now in (5.0, 15.2, 40.0):
        kms = [w["target_km"] for w in _plan(weekly_km_now)["weeks"]]
        baseline: list[float] = []
        for km in kms:
            if baseline and km < baseline[-1]:
                continue                     # 减量周：跳过且不推进基准
            baseline.append(km)
        for i in range(1, len(baseline)):
            growth = (baseline[i] - baseline[i - 1]) / baseline[i - 1]
            assert growth <= 0.11, (
                f"基础跑量 {weekly_km_now}km 时 {baseline[i - 1]} → {baseline[i]} "
                f"增幅 {growth:.1%} 超过 10% 上限（含量化容差）")


def test_base_phase_has_no_quality_session():
    """基础期是打有氧底子阶段，不得安排质量课。"""
    for weekly_km_now in (5.0, 15.2):
        for w in _plan(weekly_km_now)["weeks"]:
            if w["phase"] != "base":
                continue
            kinds = {wo["session_type"] for wo in w["workouts"]}
            assert "quality" not in kinds, f"第 {w['week_index']} 周（基础期）被排了质量课：{kinds}"


def test_beginner_strength_session_downgraded():
    """初跑者力量课降档；非初跑者保持完整方案。"""
    easy = planner.build_strength_session("build", beginner=True)
    full = planner.build_strength_session("build", beginner=False)
    assert len(easy[2]) < len(full[2]), "初跑者力量课动作数未减少"
    assert easy[4] < full[4], "初跑者力量课时长未缩短"
    # 初跑者不得出现大重量低次数方案
    names = " ".join(s["name"] for s in easy[2])
    assert "1RM" not in names, f"初跑者力量课仍含 1RM 大重量方案：{names}"


def test_beginner_flag_reaches_strength_session():
    """generate_plan 必须把 beginner 判定传到力量课（训练年龄 0 年的跑者）。"""
    plan = _plan(15.2)
    st = next((wo for w in plan["weeks"] for wo in w["workouts"]
               if wo["session_type"] == "strength"), None)
    assert st is not None, "计划里没有力量课，测试前提失效"
    names = " ".join(s["name"] for s in st["structured"])
    assert "1RM" not in names, f"训练年龄 0 年的跑者仍拿到大重量力量方案：{names}"
