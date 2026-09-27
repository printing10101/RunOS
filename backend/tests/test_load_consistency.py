"""负荷指标一致性测试。

回归重点：evaluator 曾把 ACWR 算成「7 天总量 ÷ 28 天日均」，
量纲不一致导致数值虚高约 7 倍（0.98 被算成 6.18），
与训练状态页给出互相矛盾的训练建议——且全程不报错。
"""
import re
from datetime import datetime, timedelta

import pytest
from app.services.evaluator import eval_recovery
from app.services.load_status import build_training_status

ATHLETE = {"max_hr": 192, "resting_hr": 52, "sex": "male", "weight_kg": 63.5, "hrv_baseline": 62}


def _acts(daily_loads: list[float]) -> list[dict]:
    """daily_loads[0] 是 27 天前，最后一个是今天。"""
    n = len(daily_loads)
    out = []
    for i, load in enumerate(daily_loads):
        out.append({
            "start_time": datetime.now() - timedelta(days=n - 1 - i),
            "sport": "run",
            "distance_m": 10000,
            "duration_sec": 3000,
            "avg_hr": 150,
            "training_load": load,
        })
    return out


def _acwr_from_evaluator(acts: list[dict]) -> float | None:
    """从 eval_recovery 的 evidence 文本里解析 ACWR。"""
    r = eval_recovery([], acts)
    for line in r["evidence"]:
        m = re.search(r"ACWR\s+([\d.]+)", line)
        if m:
            return float(m.group(1))
    return None


def _acwr_from_load_status(acts: list[dict]) -> float | None:
    return build_training_status(acts, ATHLETE, [], None, None)["acwr"]


# ---------------------------------------------------------------- 黄金值
def test_uniform_load_gives_acwr_one():
    """28 天负荷恒定 → ACWR 必须 = 1.00。修复前会算成 7.00（虚高 7 倍）。"""
    acts = _acts([100.0] * 28)
    assert _acwr_from_evaluator(acts) == pytest.approx(1.0, abs=0.02)
    assert _acwr_from_load_status(acts) == pytest.approx(1.0, abs=0.02)


def test_double_load_gives_expected_ratio():
    """前 21 天 100/天、后 7 天 200/天：急性 200，慢性 (2100+1400)/28=125 → 1.60。"""
    acts = _acts([100.0] * 21 + [200.0] * 7)
    assert _acwr_from_evaluator(acts) == pytest.approx(1.6, abs=0.02)
    assert _acwr_from_load_status(acts) == pytest.approx(1.6, abs=0.02)


# ---------------------------------------------------------------- 跨模块一致性
@pytest.mark.parametrize("loads", [
    [100.0] * 28,                # 恒定
    [100.0] * 21 + [200.0] * 7,  # 突增
    [100.0] * 21 + [30.0] * 7,   # 骤降
    [60.0] * 14 + [140.0] * 14,  # 阶梯上升
])
def test_acwr_consistent_across_modules(loads):
    """两个模块算出的 ACWR 必须一致——这是上次 Bug 的直接检测点。"""
    acts = _acts(loads)
    a, b = _acwr_from_evaluator(acts), _acwr_from_load_status(acts)
    assert a is not None and b is not None
    assert a == pytest.approx(b, abs=0.05), f"ACWR 不一致: evaluator={a}, load_status={b}"


# ---------------------------------------------------------------- 衍生指标
def test_fitness_fatigue_form_under_uniform_load():
    """恒定负荷下体能 = 疲劳，状态值 form ≈ 0。"""
    st = build_training_status(_acts([100.0] * 28), ATHLETE, [], None, None)
    assert st["fitness"] == pytest.approx(100, abs=1)
    assert st["fatigue"] == pytest.approx(100, abs=1)
    assert st["form"] == pytest.approx(0, abs=2)


def test_no_activity_gives_none_acwr():
    assert _acwr_from_load_status([]) is None
    assert _acwr_from_evaluator([]) is None
