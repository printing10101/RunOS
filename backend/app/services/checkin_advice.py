"""每日打卡建议：主观状态 × 饮食补给 → 今日训练档位（确定性规则，只出建议不改课表）。

档位从松到严：normal（按计划）→ reduce（减量）→ easy（仅轻松跑）→ rest（休息）。
两条纪律（tests/test_diet_fueling.py 锁定）：
- 补给降档「只降不升」，且不覆盖主观状态已有的结论——主观很差时补给再好也不升档；
- 没打卡就不出档位：饮食再差也不单独替用户做决定。
"""
from __future__ import annotations

# 严重度从低到高；用 index 比较「只降不升」
_LADDER = ("rest", "easy", "reduce", "normal")

_SUGGESTED_ACTION = {
    "easy": "replace_easy",      # 触发「轻松跑替换」提案
    "reduce": "reduce_volume",   # 建议手动减量
    "rest": "skip_workout",      # 建议休息/跳课
}


def _rank(level: str) -> int:
    return _LADDER.index(level)


def _subjective_level(checkin: dict) -> tuple[str, list[str]]:
    """主观打卡 → 档位与理由。酸痛/精力是硬信号，睡眠/动力是叠加信号。"""
    soreness = checkin.get("muscle_soreness") or 0
    energy = checkin.get("energy_level") or 3
    sleep = checkin.get("sleep_quality") or 3
    motivation = checkin.get("motivation") or 3
    pain = (checkin.get("pain_area") or "").strip()
    reasons: list[str] = []

    if soreness >= 4 or energy <= 1:
        if soreness >= 4:
            reasons.append(f"酸痛明显（{soreness}/5）")
        if energy <= 1:
            reasons.append("精力很差")
        return "rest", reasons

    red_flags = (soreness >= 3) + (energy <= 2) + (sleep <= 2) + (motivation <= 2)
    if red_flags >= 2:
        if soreness >= 3:
            reasons.append(f"酸痛偏重（{soreness}/5）")
        if energy <= 2:
            reasons.append("精力不足")
        if sleep <= 2:
            reasons.append("睡眠质量差")
        if motivation <= 2:
            reasons.append("训练意愿低")
        return "reduce", reasons

    if red_flags == 1 or pain:
        if soreness >= 1:
            reasons.append(f"轻度酸痛（{soreness}/5）")
        if energy <= 2:
            reasons.append("精力不足")
        if sleep <= 2:
            reasons.append("睡眠质量差")
        if motivation <= 2:
            reasons.append("训练意愿低")
        if pain:
            reasons.append(f"有不适部位（{pain}），避免刺激加重")
        return "easy", reasons

    return "normal", []


def build_checkin_advice(checkin: dict | None, workout: dict | None, diet: dict | None,
                         objective: dict | None = None) -> dict:
    """checkin: DailyCheckin 字段 dict 或 None；workout: 今日计划课 dict 或 None；
    diet: services.diet.fueling_status 的输出（可 None）；
    objective: services.baseline.objective_cap 的输出（客观身体信号封顶，可 None）。"""
    if not checkin:
        # 没打卡：不出档位、不因饮食单独给建议，但补给等级照实透出供展示
        return {
            "level": None, "suggested_action": None,
            "verdict": "今日尚未打卡，暂不给训练档位建议；补给状态仅供参考",
            "fueling_level": (diet or {}).get("level"),
            "base_level": None, "cap_level": (diet or {}).get("cap_level"),
        }

    base, base_reasons = _subjective_level(checkin)
    verdict_parts: list[str] = []
    if base == "normal":
        verdict_parts.append("主观状态良好，可以按计划执行")
    elif base == "rest":
        verdict_parts.append("主观状态较差（" + "、".join(base_reasons) + "），今天以休息恢复为主")
    else:
        verdict_parts.append("主观状态有信号（" + "、".join(base_reasons) + "），不宜上强度")

    level = base
    action = _SUGGESTED_ACTION.get(base)

    cap = (diet or {}).get("cap_level")
    if cap and _rank(cap) < _rank(level):
        level = cap
        action = _SUGGESTED_ACTION.get(cap)
        verdict_parts.append(
            "补给联动降档（" + "；".join((diet or {}).get("reasons") or []) + "）")
    elif base == "normal":
        verdict_parts.append("按计划执行，注意课程中的补给")

    # 客观信号（HRV/睡眠基线）与补给同构：只降不升，主观状态差时它再差也不升档
    obj_cap = (objective or {}).get("cap_level")
    if obj_cap and _rank(obj_cap) < _rank(level):
        level = obj_cap
        action = _SUGGESTED_ACTION.get(obj_cap)
        verdict_parts.append(
            "客观信号联动降档（" + "；".join((objective or {}).get("reasons") or []) + "）")

    for note in (diet or {}).get("notes") or []:
        verdict_parts.append(note)

    return {
        "level": level,
        "suggested_action": action,
        "verdict": "；".join(verdict_parts) + "。",
        "fueling_level": (diet or {}).get("level"),
        "base_level": base,
        "cap_level": cap,
    }
