"""力量评估测试。

回归重点：曾经因 standards 用英文键、seed 数据用中文动作名，
`if ex not in standards: continue` 把所有条目静默跳过 → 力量维度 100% 失效且不报错。
"""

import pytest
from app.services.strength import assess_strength, canonical_exercise, estimate_1rm


# ---------------------------------------------------------------- 动作名归一
@pytest.mark.parametrize("raw,expected", [
    ("深蹲", "squat"), ("卧推", "bench"), ("硬拉", "deadlift"),
    ("站姿推举", "ohp"), ("划船", "row"), ("杠铃划船", "row"),
    ("引体向上", "pullup"), ("臀推", "hip_thrust"), ("核心", "core"),
])
def test_chinese_names_normalized(raw, expected):
    """中文动作名必须能归一到标准键——否则评估会静默全跳过。"""
    assert canonical_exercise(raw) == expected


@pytest.mark.parametrize("raw", ["squat", "Squat", "SQUAT", " squat "])
def test_english_names_are_case_space_insensitive(raw):
    assert canonical_exercise(raw) == "squat"


@pytest.mark.parametrize("raw", ["bench", "deadlift", "ohp", "row", "pullup", "hip_thrust", "core"])
def test_english_names_unchanged(raw):
    assert canonical_exercise(raw) == raw


# ---------------------------------------------------------------- 1RM 估算
def test_estimate_1rm_single_rep_is_itself():
    assert estimate_1rm(100, 1) == pytest.approx(100.0)


def test_estimate_1rm_increases_with_reps_at_same_weight():
    assert estimate_1rm(100, 10) > estimate_1rm(100, 5) > estimate_1rm(100, 1)


def test_estimate_1rm_known_value():
    assert estimate_1rm(100, 5) == pytest.approx(114.6, abs=0.5)


# ---------------------------------------------------------------- 评估主流程
def _test(exercise, weight, day="2026-08-31"):
    return {
        "exercise": exercise,
        "best_weight_kg": weight,
        "reps": 1,
        "date": day,
        "bodyweight_kg": 63.5,
    }


CHINESE_SEED = [
    _test("深蹲", 110), _test("硬拉", 140), _test("卧推", 65),
    _test("站姿推举", 45), _test("杠铃划船", 70), _test("臀推", 120),
    _test("引体向上", 8),
]


def test_chinese_seed_data_is_not_silently_dropped():
    """核心回归：中文种子数据必须被全部识别，score 不得为 None。"""
    r = assess_strength(CHINESE_SEED, "male", 63.5)
    assert r["score"] is not None, "力量维度静默失效（score=None）"
    assert len(r["items"]) == 7, f"应识别 7 个动作，实际 {len(r['items'])}"


def test_english_input_equivalent_to_chinese():
    """同一份数据用中英文录入，评估结果必须一致（键名契约双向对齐）。"""
    en = [
        _test("squat", 110), _test("deadlift", 140), _test("bench", 65),
        _test("ohp", 45), _test("row", 70), _test("hip_thrust", 120),
        _test("pullup", 8),
    ]
    r_cn = assess_strength(CHINESE_SEED, "male", 63.5)
    r_en = assess_strength(en, "male", 63.5)
    assert r_cn["score"] == r_en["score"]
    assert len(r_cn["items"]) == len(r_en["items"])


def test_mixed_language_dedupes_by_latest():
    """同一动作中英文混录不得重复计数，且取最新一次。"""
    mixed = [
        _test("深蹲", 100, "2026-08-01"),
        _test("squat", 120, "2026-08-20"),
    ]
    r = assess_strength(mixed, "male", 63.5)
    squat_items = [i for i in r["items"] if i["label"] == "深蹲"]
    assert len(squat_items) == 1
    assert squat_items[0]["orm_kg"] == pytest.approx(120.0)


def test_unknown_exercise_is_skipped_not_crashing():
    r = assess_strength([_test("未知动作", 100), _test("深蹲", 110)], "male", 63.5)
    assert len(r["items"]) == 1


def test_balance_diagnosis_present():
    r = assess_strength(CHINESE_SEED, "male", 63.5)
    assert r["balance"], "肌群平衡诊断不应为空"
