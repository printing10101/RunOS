"""心率区间（Karvonen 储备心率法）+ 训练强度分布（80/20）。"""
from __future__ import annotations

ZONE_DEFS = [
    ("Z1", "恢复/热身", 0.50, 0.60, "放松恢复、热身冷身"),
    ("Z2", "有氧耐力", 0.60, 0.70, "轻松跑主力区间，脂肪供能比例高"),
    ("Z3", "马拉松/强度有氧", 0.70, 0.80, "马拉松配速上沿、中长节奏跑"),
    ("Z4", "乳酸阈值", 0.80, 0.88, "节奏跑、间歇间恢复偏高强度"),
    ("Z5", "最大摄氧/无氧", 0.88, 1.00, "VO2max 间歇、冲刺重复"),
]


def hr_zones(max_hr: int | None, resting_hr: int | None) -> list[dict]:
    """Karvonen: target = resting + pct * (max - resting)。

    缺任一基准时返回空列表（页面应显示「请先补心率档案」），
    不用 190/55 编出一套看起来正常的区间。
    """
    if not max_hr or not resting_hr or max_hr <= resting_hr:
        return []
    reserve = max(1, int(max_hr) - int(resting_hr))
    out = []
    for key, label, lo, hi, purpose in ZONE_DEFS:
        hr_lo = round(resting_hr + lo * reserve)
        hr_hi = round(resting_hr + hi * reserve)
        out.append({
            "key": key, "label": label,
            "hr_range": f"{hr_lo}-{hr_hi}",
            "hr_pct_range": f"{int(lo*100)}%-{int(hi*100)}%",
            "purpose": purpose,
        })
    return out


def intensity_distribution(activities: list[dict], max_hr: int | None = None,
                           resting_hr: int | None = None) -> dict:
    """按 HR 占储备心率比例统计三档强度时长占比，对照 80/20 原则。

    使用运动员整体的最大心率/静息心率（Karvonen），而非单次活动的峰值心率。

    两者缺一时返回「数据不足」而不是退回 190/55 这类与用户无关的常数：
    常数会让没填心率的所有用户都拿到同一套分布判定，等于用假数据给评估分。
    调用方应保证传入 ``services.profile`` 解析出的心率基准。
    """
    insufficient = {"easy_pct": None, "moderate_pct": None, "hard_pct": None,
                    "verdict": "缺心率基准（最大/静息心率），无法分档"}
    if not max_hr or not resting_hr or max_hr <= resting_hr:
        return insufficient
    rest_hr = int(resting_hr)
    reserve = max(1, int(max_hr) - rest_hr)
    buckets = {"easy": 0.0, "moderate": 0.0, "hard": 0.0}
    for a in activities:
        if a.get("avg_hr") is None:
            continue
        pct = (a["avg_hr"] - rest_hr) / reserve
        hours = (a.get("duration_sec") or 0) / 3600.0
        if pct < 0.70:
            buckets["easy"] += hours
        elif pct < 0.80:
            buckets["moderate"] += hours
        else:
            buckets["hard"] += hours
    total = sum(buckets.values())
    if total <= 0:
        return {"easy_pct": None, "moderate_pct": None, "hard_pct": None, "verdict": "数据不足"}
    e = buckets["easy"] / total
    m = buckets["moderate"] / total
    h = buckets["hard"] / total
    verdict = "符合 80/20 极化原则" if (e >= 0.7 and h <= 0.25) else "中等强度偏多（'灰色区间'），建议向 80/20 极化"
    return {
        "easy_pct": round(e * 100), "moderate_pct": round(m * 100),
        "hard_pct": round(h * 100), "verdict": verdict,
        "easy_hours": round(buckets["easy"], 1), "moderate_hours": round(buckets["moderate"], 1),
        "hard_hours": round(buckets["hard"], 1),
    }
