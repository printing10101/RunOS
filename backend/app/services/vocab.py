"""课型词汇表：课型分类与中文标签的唯一事实源。

历史教训（AGENTS.md 记录）：method_library / ics / 打卡建议曾各留一份内联课型
元组，方法库二代课型（fartlek/hill 等）被多处漏判。现在所有「是不是质量课」
「是不是硬课」「中文叫什么」都必须走本模块，新增课型只改这里。
"""
from __future__ import annotations

# 质量课全家族：一代词汇（tempo/interval/race）+ 方法库二代词汇（fartlek/hill）。
# "quality" 是方法库模板的通用占位课型，同样按质量课对待。
QUALITY_SESSION_TYPES = ("quality", "tempo", "interval", "fartlek", "hill", "race")

# 硬课 = 质量课 + 长距离：排课时与打卡建议里「需要恢复」的那一类。
HARD_TYPES = QUALITY_SESSION_TYPES + ("long",)

# 中文标签与 web/src/api.js 的 SESSION_STYLE 保持同源口径
_LABELS = {
    "easy": "轻松跑",
    "recovery": "恢复跑",
    "long": "长距离",
    "quality": "质量课",
    "tempo": "节奏跑",
    "interval": "间歇跑",
    "fartlek": "变速跑",
    "hill": "坡地跑",
    "race": "比赛",
    "strength": "力量",
    "core": "核心",
    "cross": "交叉训练",
    "rest": "休息",
}


def is_quality_session(session_type: str | None) -> bool:
    """质量课判定（含二代词汇），None/未知课型一律 False。"""
    return bool(session_type) and session_type in QUALITY_SESSION_TYPES


def is_hard_or_long(session_type: str | None) -> bool:
    """硬课判定：质量课或长距离。力量/核心不算硬课（调用方需要单独叠加判断）。"""
    return is_quality_session(session_type) or session_type == "long"


def session_label(session_type: str | None) -> str:
    """课型中文标签；未知课型原样返回，不编造名字。"""
    if not session_type:
        return "训练"
    return _LABELS.get(session_type, session_type)
