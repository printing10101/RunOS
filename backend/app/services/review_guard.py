"""AI 解读的数字纪律守卫。

规则：LLM 只把引擎装配的 facts 组织成文案。数字必须「可溯源到 facts」：
- 直接引用：数字原样出现在 facts 里；
- 合法推导：数字是 facts 中数字的算术结果（差值/和/积/商/同比百分比）——
  「比上周多 11.7km」是 51.3−39.6 的差值，属于合法推理而非编造；
- 结构性数字：列表编号与 1-9 的约数区间（"2-3 件事"）不算事实引用。
其余一律视为编造，调用方捕获 ValueError 后退回规则版文案。
六个解读链路（评估/预测/计划/周报/饮食/训练点评）共用这一个实现。
"""
from __future__ import annotations

import json
import re

# 数字 token：整数或小数
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
# 行首列表编号：1. / 1、/ 1)
_LIST_ENUM_RE = re.compile(r"^\s*\d{1,2}\s*[.、)]", re.M)
# 小数字区间（2-3、1~2 之类结构性约数），两端都是 1-9
_SMALL_RANGE_RE = re.compile(r"(?<!\d)[1-9]\s*[-~–]\s*[1-9](?!\d)")

_DERIVED_TOL_ABS = 0.15      # 推导数判定容差（绝对）
_DERIVED_TOL_REL = 0.01      # 推导数判定容差（相对）
_MAX_OPERAND = 1_000_000     # 参与算术的量级上限（排除爆搜）


def _numbers_in(text: str) -> set[float]:
    return {float(t) for t in _NUM_RE.findall(text)}


def _is_derived(target: float, facts_nums: list[float]) -> bool:
    """target 是否可由 facts 中两个数字的四则/同比运算得到（含容差）。"""
    tol = max(_DERIVED_TOL_ABS, abs(target) * _DERIVED_TOL_REL)
    for a in facts_nums:
        if abs(a) > _MAX_OPERAND:
            continue
        for b in facts_nums:
            if abs(b) > _MAX_OPERAND:
                continue
            if abs(a + b - target) <= tol:
                return True
            if abs(abs(a - b) - target) <= tol:
                return True
            if b != 0 and abs(a / b - target) <= tol:
                return True
            if abs(a * b - target) <= tol and abs(b) >= 1:
                return True
            if a != 0 and abs((b - a) / a * 100 - target) <= tol:
                return True
    return False


def check(review: str, facts: dict) -> None:
    """核验 review 中引用的数字都能溯源到 facts。违规抛 ValueError。"""
    if not review:
        return
    haystack = json.dumps(facts, ensure_ascii=False, default=str)
    facts_nums = list(_numbers_in(haystack))

    cleaned = _LIST_ENUM_RE.sub(" ", review)
    cleaned = _SMALL_RANGE_RE.sub(" ", cleaned)

    offenders = []
    for m in _NUM_RE.finditer(cleaned):
        token = m.group(0)
        if token in haystack:
            continue
        value = float(token)
        if _is_derived(value, facts_nums):
            continue
        offenders.append(token)
    if offenders:
        # 去重保序，最多报 3 个就够定位问题
        uniq = list(dict.fromkeys(offenders))[:3]
        raise ValueError(f"解读引用了事实之外的数字：{', '.join(uniq)}")
