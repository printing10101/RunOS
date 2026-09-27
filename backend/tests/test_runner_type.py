"""跑者类型判定测试。

这个模块此前零覆盖，而它输出的是直接给用户看的结论（"你是速度型跑者"、
"竞技导向"），判错了用户会照着错的侧重去练。三条判定轴各自独立，
逐一钉住分档阈值与不可判定时的回落。
"""
from __future__ import annotations

from app.services import runner_type


class _StubPred:
    """predictor.PredictionResult 的鸭子类型替身：classify_runner 只用这两个字段。"""

    def __init__(self, vdot: float, p5: int | None = None, pm: int | None = None) -> None:
        self.current_vdot = vdot
        self.predictions: dict = {}
        if p5 is not None:
            self.predictions["5k"] = {"time_sec": p5}
        if pm is not None:
            self.predictions["marathon"] = {"time_sec": pm}


ATHLETE = {"sex": "male", "age": 30}
P5 = 1200  # 5km 1200s = 4:00/km，每公里 240s

# 速度储备 = (全马每公里秒 / 5k 每公里秒) - 1，分档线 0.13 / 0.22
# 42.195km × 240s/km × (1 + 储备) 得到对应的全马时间
MARATHON_FOR_SPEED = 13000      # 储备 ≈ 0.284  → speed
MARATHON_FOR_ENDURANCE = 11000  # 储备 ≈ 0.086  → endurance
MARATHON_FOR_BALANCED = 11900   # 储备 ≈ 0.175  → balanced


def test_speed_reserve_is_computed_from_two_races():
    r = runner_type.classify_runner(ATHLETE, _StubPred(45, P5, MARATHON_FOR_BALANCED), 40, True)
    # (11900/42.195) / (1200/5) - 1 ≈ 0.175
    assert r["speed_reserve"] is not None
    assert 0.13 < r["speed_reserve"] < 0.22


def test_event_axis_three_bands():
    speed = runner_type.classify_runner(ATHLETE, _StubPred(45, P5, MARATHON_FOR_SPEED), 40, True)
    assert speed["event"] == "speed"
    assert speed["type_name"] == "速度型"

    endurance = runner_type.classify_runner(ATHLETE, _StubPred(45, P5, MARATHON_FOR_ENDURANCE), 40, True)
    assert endurance["event"] == "endurance"
    assert endurance["type_name"] == "耐力型"

    balanced = runner_type.classify_runner(ATHLETE, _StubPred(45, P5, MARATHON_FOR_BALANCED), 40, True)
    assert balanced["event"] == "balanced"
    assert balanced["type_name"] == "均衡型"


def test_event_unknown_when_a_race_is_missing():
    """缺 5k 或全马都不能判定倾向——不能拿半程或 10k 硬凑出一个结论。"""
    for pred in (
        _StubPred(45, P5, None),
        _StubPred(45, None, MARATHON_FOR_SPEED),
        _StubPred(45, None, None),
    ):
        r = runner_type.classify_runner(ATHLETE, pred, 40, True)
        assert r["event"] == "unknown"
        assert r["speed_reserve"] is None
        assert r["type_name"] == "待判定"
        # 待判定不能给出空的改进建议，否则页面出现空白区块
        assert r["training_focus"]


def test_style_axis_competitive_vs_wellness():
    # 带目标 + 周跑量 ≥30 → 竞技导向
    assert runner_type.classify_runner(ATHLETE, _StubPred(45, P5, P5 * 9), 30, True)["style"] == "competitive"
    # 带目标但跑量不足 30 → 仍是健康导向
    assert runner_type.classify_runner(ATHLETE, _StubPred(45, P5, P5 * 9), 29, True)["style"] == "wellness"
    # 无目标时门槛抬高到 50
    assert runner_type.classify_runner(ATHLETE, _StubPred(45, P5, P5 * 9), 49, False)["style"] == "wellness"
    assert runner_type.classify_runner(ATHLETE, _StubPred(45, P5, P5 * 9), 50, False)["style"] == "competitive"


def test_level_is_data_insufficient_without_vdot():
    """VDOT 为 0（无有效成绩）时不该编造一个等级。"""
    r = runner_type.classify_runner(ATHLETE, _StubPred(0, P5, MARATHON_FOR_SPEED), 40, True)
    assert r["level"] == "数据不足"
    # tags 里不该出现等级名，否则界面上会显示一个凭空来的标签
    assert r["tags"] == ["速度型", "竞技导向"]


def test_level_present_and_tags_ordered_when_vdot_known():
    r = runner_type.classify_runner(ATHLETE, _StubPred(50, P5, MARATHON_FOR_SPEED), 40, False)
    assert r["level"] in [name for _, name in runner_type.LEVELS]
    assert r["tags"] == ["速度型", r["level"], "健康导向"]
    assert len(r["tags"]) == 3
