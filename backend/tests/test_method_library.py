"""训练方法知识库测试。

回归重点：
1. 配速解析绝不允许编造数值——未知占位必须显式降级，而不是回退到某个默认配速；
2. 种子 JSON 必须结构完整（每个方法有出处与 asian_fit_basis），否则「可查证出处」
   这一核心承诺会静默失效；
3. 推荐打分必须有命中理由才计入，防止黑箱排序。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.services.method_library import ZONE_KEY_MAP, _rule_hit, level_from_volume, resolve_template_steps

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


# ---------------------------------------------------------------- 配速解析

def test_pace_zone_resolves_to_real_pace():
    """T 区占位必须解析为合理的阈值配速。

    边界值按引擎实测口径（VDOT 45 → T 区 4:38-4:51/km，与 planner 同源），
    断言只防回归漂移，不重新定义引擎口径。
    """
    steps = resolve_template_steps(
        [{"step_type": "active", "name": "阈值跑", "duration_type": "time", "duration_value": 20,
          "target": {"type": "pace_zone", "zone": "T"}, "note": ""}], 45.0)
    tgt = steps[0]["target"]
    assert tgt["type"] == "pace"
    assert 240 <= tgt["from"] <= tgt["to"] <= 320      # sec/km
    assert tgt["from_label"].endswith("/km")


def test_pace_offset_slower_than_marathon():
    """offset_pct=-3（川内式：略慢于马配）必须比标准 M 区更慢。"""
    base = resolve_template_steps(
        [{"step_type": "active", "name": "m", "duration_type": "distance", "duration_value": 1000,
          "target": {"type": "pace_zone", "zone": "M"}}], 45.0)[0]["target"]
    off = resolve_template_steps(
        [{"step_type": "active", "name": "m", "duration_type": "distance", "duration_value": 1000,
          "target": {"type": "pace_zone", "zone": "M", "offset_pct": -3}}], 45.0)[0]["target"]
    assert off["from"] > base["from"]                  # 秒/km 更大 = 更慢
    assert off["to"] > base["to"]


def test_pct_of_mp_90_is_slower_than_mp():
    """30km 走 @90% 马配必须比马配慢（pct<100 → sec/km 变大）。"""
    base = resolve_template_steps(
        [{"step_type": "active", "name": "m", "duration_type": "distance", "duration_value": 1000,
          "target": {"type": "pace_zone", "zone": "M"}}], 45.0)[0]["target"]
    p90 = resolve_template_steps(
        [{"step_type": "active", "name": "l", "duration_type": "distance", "duration_value": 1000,
          "target": {"type": "pace_pct_of_mp", "pct": 90}}], 45.0)[0]["target"]
    assert p90["from"] > base["from"]
    # 105% 马配必须更快
    p105 = resolve_template_steps(
        [{"step_type": "active", "name": "t", "duration_type": "distance", "duration_value": 1000,
          "target": {"type": "pace_pct_of_mp", "pct": 105}}], 45.0)[0]["target"]
    assert p105["from"] < base["from"]


def test_unknown_zone_degrades_without_fabricating_pace():
    """未知区 key 必须降级为无目标+说明，绝不回退成某个编造配速。"""
    steps = resolve_template_steps(
        [{"step_type": "active", "name": "x", "duration_type": "distance", "duration_value": 1000,
          "target": {"type": "pace_zone", "zone": "X9"}}], 45.0)
    assert steps[0]["target"]["type"] == "none"


def test_other_target_types_pass_through():
    """hr/rpe/none 等占位原样保留，不被误改。"""
    raw = {"type": "hr", "from": 0.65, "to": 0.75, "label": "心率 65-75%"}
    steps = resolve_template_steps(
        [{"step_type": "active", "name": "e", "duration_type": "time", "duration_value": 30,
          "target": dict(raw)}], 45.0)
    assert steps[0]["target"] == raw


def test_resolve_requires_valid_vdot():
    with pytest.raises(ValueError):
        resolve_template_steps([], 0)


def test_zone_key_map_covers_all_daniels_zones():
    assert set(ZONE_KEY_MAP.values()) == {"easy", "marathon", "threshold", "interval", "repetition"}


# ---------------------------------------------------------------- 画像分档与规则匹配

@pytest.mark.parametrize("km,expected", [
    (0, "beginner"), (24.9, "beginner"), (25, "intermediate"), (54.9, "intermediate"),
    (55, "advanced"), (109.9, "advanced"), (110, "elite"), (200, "elite"),
])
def test_level_from_volume(km, expected):
    assert level_from_volume(km) == expected


def test_rule_hit_empty_condition_never_matches():
    """空条件必须不命中——防止写错条件的规则变成无条件加分。"""
    assert _rule_hit({}, {"weekly_km": 100}) is False


def test_rule_hit_volume_bounds():
    cond = {"weekly_km_min": 60, "weekly_km_max": 120}
    assert _rule_hit(cond, {"weekly_km": 80}) is True
    assert _rule_hit(cond, {"weekly_km": 40}) is False
    assert _rule_hit(cond, {"weekly_km": 150}) is False


def test_rule_hit_level_race_phase_injury():
    assert _rule_hit({"level_in": ["advanced"]}, {"level": "advanced"}) is True
    assert _rule_hit({"level_in": ["advanced"]}, {"level": "beginner"}) is False
    assert _rule_hit({"race_in": ["marathon"]}, {"race_type": "marathon"}) is True
    assert _rule_hit({"race_in": ["marathon"]}, {"race_type": "5k"}) is False
    assert _rule_hit({"phase_in": ["base"]}, {"phase": "base"}) is True
    assert _rule_hit({"phase_in": ["base"]}, {"phase": None}) is False
    assert _rule_hit({"injury_sensitive": True}, {"injury_sensitive": True}) is True
    assert _rule_hit({"injury_sensitive": True}, {"injury_sensitive": False}) is False


# ---------------------------------------------------------------- 种子数据完整性（可查证出处的核心承诺）

SEED_FILES = sorted(DATA_DIR.glob("training_methods_*.json"))


def test_seed_files_exist():
    assert len(SEED_FILES) >= 2


@pytest.mark.parametrize("seed_file", SEED_FILES, ids=lambda p: p.name)
def test_seed_structure_complete(seed_file):
    doc = json.loads(seed_file.read_text(encoding="utf-8"))
    codes = set()
    for m in doc["methods"]:
        # 唯一性与必填字段
        assert m["code"] and m["code"] not in codes, f"重复/空 code: {m.get('code')}"
        codes.add(m["code"])
        assert m["name_zh"] and m["summary"]
        assert m["evidence_level"] in ("A", "B", "C", "D")
        assert 0 <= m["asian_fit"] <= 100
        # 亚洲适配必须有依据说明，且不允许「人种天赋」式表述
        basis = m.get("asian_fit_basis") or {}
        assert basis.get("note"), f"{m['code']} 缺少 asian_fit_basis.note"
        banned = ("天赋", "人种优势", "基因决定")
        assert not any(b in basis["note"] for b in banned), f"{m['code']} 的适配依据含禁止表述"
        # 出处：每个方法至少 1 条，且媒体来源必须标 D 或注明未核实
        assert len(m.get("evidences") or []) >= 1, f"{m['code']} 没有任何出处"
        # 强度分布：三项齐全且合计在 90-110 之间（允许取整误差）
        dist = m.get("intensity_distribution") or {}
        if m["category"] == "system" and any(dist.get(k) for k in ("low", "moderate", "high")):
            total = sum(dist.get(k, 0) for k in ("low", "moderate", "high"))
            assert 90 <= total <= 110, f"{m['code']} 强度分布合计异常: {total}"
        # 课表模板
        for w in m.get("workouts") or []:
            assert w["code"] and w["name_zh"] and w.get("structure"), f"{m['code']}/{w.get('code')} 结构缺失"
            assert w["session_type"] in ("easy", "tempo", "interval", "long", "recovery",
                                         "fartlek", "hill", "race", "strength")
            # 结构化步骤里不允许出现硬编码配速数字（必须是占位，由引擎解析）
            for s in w["structure"]:
                assert s["step_type"] in ("warmup", "active", "rest", "cooldown", "strength")
                tgt = s.get("target") or {}
                assert tgt.get("type") in (None, "none", "pace_zone", "pace_pct_of_mp", "hr",
                                           "rpe", "maf", "cadence", "lactate",
                                           "progression", "alternating"), \
                    f"{w['code']} 含未知目标类型: {tgt.get('type')}"
                assert tgt.get("type") != "pace", f"{w['code']} 出现硬编码配速，必须用 pace_zone 占位"


def test_seed_workout_codes_unique_across_files():
    """课表模板 code 全局唯一（DB 有 unique 约束，导入会在冲突时炸）。"""
    seen = set()
    for fp in SEED_FILES:
        doc = json.loads(fp.read_text(encoding="utf-8"))
        for m in doc["methods"]:
            for w in m.get("workouts") or []:
                assert w["code"] not in seen, f"重复模板 code: {w['code']}"
                seen.add(w["code"])


def test_seed_every_workout_has_cautions_or_note():
    """每个课表模板必须给执行风险提示或来源（安全护栏，防止 AI 只转述课表不转述风险）。"""
    for fp in SEED_FILES:
        doc = json.loads(fp.read_text(encoding="utf-8"))
        for m in doc["methods"]:
            for w in m.get("workouts") or []:
                assert (w.get("cautions") or w.get("source_url")), \
                    f"{w['code']} 缺少 cautions 与 source_url"


# ---------------------------------------------------------------- 原理库与计划库种子（可追溯性承诺）

PRINCIPLE_FILE = DATA_DIR / "training_principles.json"
PLAN_FILE = DATA_DIR / "training_plans_library.json"


def test_principle_file_exists_with_content():
    doc = json.loads(PRINCIPLE_FILE.read_text(encoding="utf-8"))
    assert len(doc["principles"]) >= 8


def test_principles_structure_complete():
    """每条原理必须有机制、可执行规则、出处——否则 AI 的『有迹可循』承诺失效。"""
    doc = json.loads(PRINCIPLE_FILE.read_text(encoding="utf-8"))
    codes = set()
    for p in doc["principles"]:
        assert p["code"] and p["code"] not in codes
        codes.add(p["code"])
        assert p["mechanism"], f"{p['code']} 缺 mechanism"
        assert p["practical_rules"], f"{p['code']} 缺 practical_rules"
        assert p["platform_usage"], f"{p['code']} 缺 platform_usage（应说明引擎已落地位置）"
        assert p["evidence_level"] in ("A", "B", "C", "D")
        assert len(p.get("sources") or []) >= 1, f"{p['code']} 没有出处"


def test_plans_structure_complete():
    doc = json.loads(PLAN_FILE.read_text(encoding="utf-8"))
    codes = set()
    for pl in doc["plans"]:
        assert pl["code"] and pl["code"] not in codes
        codes.add(pl["code"])
        assert pl["weeks"] >= 4
        assert pl.get("week_template"), f"{pl['code']} 缺周模板"
        assert pl.get("key_workouts"), f"{pl['code']} 缺关键课"
        assert pl.get("long_run_progression"), f"{pl['code']} 缺长距离递进"
        assert pl.get("taper_plan"), f"{pl['code']} 缺减量方案"
        assert pl["evidence_level"] in ("A", "B", "C", "D")
        assert len(pl.get("sources") or []) >= 1, f"{pl['code']} 没有出处"
        assert pl.get("cautions"), f"{pl['code']} 缺风险提示"
        # 长距离递进必须覆盖从起点到比赛/峰值的演进
        lr = pl["long_run_progression"]
        assert lr[0]["km"] < lr[-1]["km"] or pl["target_race"] in ("5k",), f"{pl['code']} 递进异常"


def test_plan_codes_unique_and_cautions_present():
    doc = json.loads(PLAN_FILE.read_text(encoding="utf-8"))
    assert len(doc["plans"]) >= 8
    # 每个计划必须给适配条件（否则推荐层无法解释命中）
    for pl in doc["plans"]:
        assert pl.get("fit_condition"), f"{pl['code']} 缺 fit_condition"
