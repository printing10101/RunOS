"""跑者类型判定：项目倾向 × 水平阶段 × 训练风格。

输出直接给用户看（"你是速度型跑者"、"竞技导向"），判错用户会照着错的侧重去练，
所以三条判定轴各自独立、阈值可解释：
- 项目倾向（event）：5k 与全马都有的前提下，用「速度储备」=（全马配速/5k 配速）-1
  衡量长短距离的失衡方向。储备大（>0.22）说明短距离远强于长距离 → 速度型；
  储备小（<0.13）说明长距离掉速少 → 耐力型；中间为均衡。缺任一距离一律
  「待判定」，不拿半程/10k 硬凑。
- 水平（level）：按当前 VDOT 分档；VDOT 为 0（无有效成绩）=「数据不足」，
  且等级不进 tags——不编造等级。
- 风格（style）：有目标且周跑量 ≥30km（无目标时门槛抬高到 50）才算竞技导向。
"""
from __future__ import annotations

from .vdot import RACE_DISTANCES

# (VDOT 下限, 等级名)，升序；判定取最后一个满足下限的档。
# 首档下限 30：VDOT < 30 视为「无有效成绩」（best_efforts 的距离/时长门槛
# 本来就滤掉了散步级数据），按数据不足处理而不是给一个凭空来的等级。
LEVELS = (
    (30, "初级"),
    (35, "中级"),
    (45, "高级"),
    (53, "精英"),
    (60, "顶尖"),
)

# 速度储备分档线（tests/test_runner_type.py 锁定）
_SPEED_RESERVE_SPEED = 0.22      # > 0.22 → speed
_SPEED_RESERVE_ENDURANCE = 0.13  # < 0.13 → endurance

_MARATHON_KM = RACE_DISTANCES["marathon"] / 1000


def _level_of(vdot_value: float) -> str:
    name = "数据不足"
    for floor, level_name in LEVELS:
        if vdot_value >= floor:
            name = level_name
    return name


def classify_runner(athlete: dict, pred, weekly_km: float, has_goal: bool) -> dict:
    """pred 只要求鸭子类型：current_vdot + predictions["5k"/"marathon"]["time_sec"]。"""
    preds = getattr(pred, "predictions", {}) or {}
    p5 = (preds.get("5k") or {}).get("time_sec")
    pm = (preds.get("marathon") or {}).get("time_sec")

    speed_reserve = None
    event = "unknown"
    if p5 and pm:
        pace5 = p5 / 5.0                    # 5k 每公里秒数
        pacem = pm / _MARATHON_KM           # 全马每公里秒数
        speed_reserve = round(pacem / pace5 - 1, 3)
        if speed_reserve > _SPEED_RESERVE_SPEED:
            event = "speed"
        elif speed_reserve < _SPEED_RESERVE_ENDURANCE:
            event = "endurance"
        else:
            event = "balanced"

    type_name = {"speed": "速度型", "endurance": "耐力型", "balanced": "均衡型"}.get(event, "待判定")

    threshold = 30 if has_goal else 50
    style = "competitive" if (weekly_km or 0) >= threshold else "wellness"
    style_name = "竞技导向" if style == "competitive" else "健康导向"

    vdot_value = getattr(pred, "current_vdot", 0) or 0
    level = _level_of(vdot_value)

    tags = [type_name]
    if level != "数据不足":
        tags.append(level)
    tags.append(style_name)

    focus = {
        "speed": "优先发展速度与乳酸阈值能力，同时用长距离慢跑守住有氧底盘，"
                 "把短距离优势逐步转化到更长距离",
        "endurance": "继续巩固有氧与长距离耐力，每周安排 1-2 次阈值/间歇课，"
                     "补足短距离速度短板",
        "balanced": "长短能力较均衡，按目标比赛侧重分配质量课：练短项加间歇，"
                    "练长项加阈值与长距离",
        "unknown": "先积累 5k 与全马两个距离的有效成绩（或正式比赛），"
                   "系统才能判定项目倾向并给出针对性侧重",
    }[event]

    if event == "unknown":
        headline = "跑者类型待判定"
        description = "缺少 5k 或全马的有效成绩，暂时无法判定项目倾向"
    else:
        reserve_pct = f"{round((speed_reserve or 0) * 100)}%"
        headline = f"{type_name} · {level} · {style_name}"
        description = {
            "speed": f"短距离表现明显强于长距离（速度储备 {reserve_pct}），速度是你的长板",
            "endurance": f"长距离掉速少（速度储备 {reserve_pct}），耐力是你的长板",
            "balanced": f"长短距离能力较均衡（速度储备 {reserve_pct}）",
        }[event]

    return {
        "event": event,
        "type_name": type_name,
        "speed_reserve": speed_reserve,
        "style": style,
        "style_name": style_name,
        "level": level,
        "tags": tags,
        "training_focus": focus,
        "headline": headline,
        "description": description,
    }
