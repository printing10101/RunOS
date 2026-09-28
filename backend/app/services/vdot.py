"""VDOT 引擎（Daniels《Running Formula》公式化实现）。

成绩 ↔ VDOT 互为逆运算：
- 正解：vdot_from_performance(距离, 时间) → VDOT
- 反解：time_from_vdot(距离, VDOT) → 等效成绩
黄金值对齐 Daniels 官方表：5km 20:00 → 50.1，全马 3:00:00 → 53.5
（tests/test_vdot.py 锁定，容差 ±0.2）。

配速区间（E/M/T/I/R）按 %VO2max 换算，pace 字符串口径统一「M:SS/km」。
"""
from __future__ import annotations

import math

# 标准比赛距离（米）。key 同时用作 Goal.race_type / 计划 race_type 的词汇
RACE_DISTANCES = {
    "800m": 800,
    "1k": 1000,
    "1500m": 1500,
    "3k": 3000,
    "5k": 5000,
    "10k": 10000,
    "hm": 21097.5,
    "marathon": 42195,
}

_RACE_LABELS = {"800m": "800米", "1k": "1公里", "1500m": "1500米", "3k": "3公里",
                "5k": "5公里", "10k": "10公里", "hm": "半马", "marathon": "全马"}

# 公开别名：报表/短板分析等需要中文距离名的地方统一用这份
RACE_LABELS = _RACE_LABELS

# 目标标签 / AI 文案用的全称（「半马破三」场景才说「半程马拉松」）
RACE_LABELS_FULL = {"800m": "800米", "1k": "1000米", "1500m": "1500米", "3k": "3公里",
                    "5k": "5公里", "10k": "10公里", "hm": "半程马拉松", "marathon": "全程马拉松"}

# 允许设为比赛目标的项目（预测/规划/目标校验共用这一份，新增项目只改 RACE_DISTANCES）
GOAL_RACE_TYPES = tuple(RACE_DISTANCES)


def _vo2(velocity_m_per_min: float) -> float:
    """Daniels 耗氧量回归：跑步经济性公式（v 单位 m/min）。"""
    return -4.60 + 0.182258 * velocity_m_per_min + 0.000104 * velocity_m_per_min ** 2


def _velocity_for_vo2_raw(vo2: float) -> float:
    """_vo2 的反解（v 单位 m/min），vo2 必须 > -4.60。"""
    disc = 0.182258 ** 2 + 4 * 0.000104 * (vo2 + 4.60)
    if disc < 0:
        raise ValueError(f"耗氧量 {vo2} 超出公式定义域")
    return (-0.182258 + math.sqrt(disc)) / (2 * 0.000104)


def _percent_max(t_min: float) -> float:
    """可维持时长 t（分钟）对应的 %VO2max（耐力时间衰减曲线）。"""
    return 0.8 + 0.1894393 * math.exp(-0.012778 * t_min) + 0.2989558 * math.exp(-1.92835 * t_min)


def vdot_from_performance(distance_m: float, time_sec: float) -> float:
    """一段全力表现（距离米 + 秒）→ VDOT。非法输入（<=0）直接 ValueError。"""
    if not distance_m or distance_m <= 0 or not time_sec or time_sec <= 0:
        raise ValueError(f"无效的成绩数据：distance_m={distance_m}, time_sec={time_sec}")
    v = distance_m / (time_sec / 60.0)          # m/min
    return _vo2(v) / _percent_max(time_sec / 60.0)


def time_from_vdot(distance_m: float, vdot_value: float) -> float:
    """VDOT + 距离 → 等效成绩（秒，float）。二分求逆，正解单调所以收敛稳定。"""
    if not distance_m or distance_m <= 0:
        raise ValueError(f"无效的距离：{distance_m}")
    if vdot_value <= 0:
        raise ValueError(f"无效的 VDOT：{vdot_value}")
    lo, hi = 1.0, 8 * 3600.0
    # vdot(t) 随 t 单调「递减」（跑得越慢 VDOT 越低）：vdot(mid) < 目标说明
    # mid 太慢，答案在更快的左侧
    for _ in range(80):
        mid = (lo + hi) / 2
        if vdot_from_performance(distance_m, mid) < vdot_value:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def velocity_for_vo2(pct: float, vdot_value: float) -> float:
    """VDOT 的给定百分比强度对应的跑步速度（m/min），配速区间换算的基础。"""
    if vdot_value <= 0:
        raise ValueError(f"无效的 VDOT：{vdot_value}")
    return _velocity_for_vo2_raw(vdot_value * pct)


def required_vdot(race_type: str, target_sec: float) -> float:
    """达成某目标成绩所需要的 VDOT（目标差距 → 可行性判定的输入）。"""
    dist = RACE_DISTANCES.get(race_type)
    if not dist:
        raise ValueError(f"未知比赛距离：{race_type}")
    return vdot_from_performance(dist, target_sec)


def equivalent_times(vdot_value: float) -> dict[str, float]:
    """当前 VDOT 下各标准距离的等效成绩（秒）。"""
    return {key: round(time_from_vdot(dist, vdot_value), 1)
            for key, dist in RACE_DISTANCES.items()}


# 训练配速区间：%VO2max 上下界 + 用途说明（Daniels 六区中的五跑区）
_PACE_ZONES = (
    ("easy", "轻松跑(E)", 0.59, 0.74, "有氧基础，打造毛细血管密度与线粒体"),
    ("marathon", "马拉松配速(M)", 0.75, 0.84, "目标比赛配速耐力，提升乳酸利用"),
    ("threshold", "乳酸阈值(T)", 0.83, 0.88, "抬升乳酸阈值，约 20 分钟的 T 心率区间跑"),
    ("interval", "间歇(I)", 0.95, 1.00, "最大摄氧量，3-5 分钟的 I 配速反复跑"),
    ("repetition", "重复(R)", 1.05, 1.10, "速度与经济性，200-400m 的 R 配速短反复"),
)


def daniels_paces(vdot_value: float) -> list[dict]:
    """当前 VDOT 的五档训练配速区间。

    pace_from 是区间快端（上限强度），pace_to 是慢端；前端按
    「4:54/km ~ 5:42/km」的形式展示。非法 VDOT 返回空列表，不抛异常
    （总览页在无数据时也要能渲染）。
    """
    if not vdot_value or vdot_value <= 0:
        return []
    zones = []
    for key, label, lo, hi, purpose in _PACE_ZONES:
        try:
            fast = velocity_for_vo2(hi, vdot_value)    # m/min
            slow = velocity_for_vo2(lo, vdot_value)
        except ValueError:
            continue
        zones.append({
            "key": key, "label": label, "purpose": purpose,
            # 数值口径（m/s）：planner / method_library 解析结构化步骤的目标配速用
            "velocity_from": round(fast / 60.0, 3),    # 快端
            "velocity_to": round(slow / 60.0, 3),      # 慢端
            # 展示口径（「M:SS/km」字符串）：仪表盘/报表直接渲染
            "pace_from": pace_str(fast), "pace_to": pace_str(slow),
        })
    return zones


# ---------------------------------------------------------------- 格式化

def time_str(sec: float | None) -> str:
    """秒 → 「H:MM:SS」（≥1h）或「MM:SS」。"""
    if sec is None:
        return "-"
    t = int(round(sec))
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def pace_str(velocity_m_per_min: float) -> str:
    """速度（m/min）→ 配速字符串「M:SS/km」。速度 <=0 返回「-」。"""
    if not velocity_m_per_min or velocity_m_per_min <= 0:
        return "-"
    sec_per_km = 1000.0 / (velocity_m_per_min / 60.0)
    m = int(sec_per_km // 60)
    s = int(round(sec_per_km % 60))
    if s == 60:
        m, s = m + 1, 0
    return f"{m}:{s:02d}/km"


def pace_label(sec_per_km: float | None) -> str:
    """每公里秒数 → 配速展示字符串。None/<=0 返回「-」。

    这是文案装配（AI 点评/报表）用得最多的口径：调用方手里是
    time/distance 算出来的 sec_per_km，不需要再换算回速度。
    """
    if not sec_per_km or sec_per_km <= 0:
        return "-"
    m = int(sec_per_km // 60)
    s = int(round(sec_per_km % 60))
    if s == 60:
        m, s = m + 1, 0
    return f"{m}:{s:02d}/km"
