"""个性化身体基线：滚动中位数 + MAD 波动带 + 异常/漂移判定（确定性引擎）。

为什么用中位数 + MAD 而不是均值 ± SD：HRV/睡眠常有离档天（饮酒、熬夜、
测量失败），均值会被单日坏值拖走；中位数对离档值稳健，MAD×1.4826 在正态
假设下等价于 SD，是稳健统计的标准组合。样本不足时宁可缺省（None）也不猜。

方向语义按指标：HRV 偏低是负面信号（恢复不足/疾病前兆），偏高属中性；
静息心率偏高负面；睡眠偏低负面；体重双向中性（只报事实）。偏离不等于
「练废了」——疾病、时差、测试条件都会造成偏离，引擎只标注事实与严重度，
解释交给 AI 教练和用户。

不落库：原料就是 body_metrics 按天行，28 天窗口现算足够快。与档案里的
hrv_baseline / resting_hr（profile.py 解析链路）并存：展示与打卡联动优先
用本引擎的客观数据基线，档案值仍是用户显式声明的口径。
"""
from __future__ import annotations

from datetime import date, timedelta

# 指标 → 展示名与偏离方向语义（low_bad/high_bad：该方向的偏离是否负面）
_METRICS = {
    "hrv_rmssd": {"label": "HRV", "low_bad": True, "high_bad": False},
    "resting_hr": {"label": "静息心率", "low_bad": False, "high_bad": True},
    "sleep_hours": {"label": "睡眠", "low_bad": True, "high_bad": False},
    "weight_kg": {"label": "体重", "low_bad": False, "high_bad": False},
}

WINDOW_DAYS = 28
MIN_BASELINE_N = 7    # 中位数基线最少样本
MIN_SPREAD_N = 10     # 波动带最少样本（少了 z 分数不可信）
RECENT_DAYS = 7       # 漂移检测：近 7 天 vs 前 21 天
MIN_LATEST_AGE_DAYS = 3   # 最新值超过 3 天前的基线没有当下意义
DRIFT_THRESHOLD_PCT = 5.0

# 档位严重度从低到高（与 checkin_advice._LADDER 同口径，只降不升用）
CAP_EASY = "easy"


def _median(vals: list[float]) -> float:
    s = sorted(vals)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def _mad_spread(vals: list[float]) -> float | None:
    """MAD×1.4826（正态等价 SD）。全等值（MAD=0）或样本少时不给波动带。"""
    if len(vals) < MIN_SPREAD_N:
        return None
    med = _median(vals)
    mad = _median([abs(v - med) for v in vals])
    if mad <= 0:
        return None
    return mad * 1.4826


def _series(metrics: list[dict], key: str, today: date) -> list[tuple[date, float]]:
    out: list[tuple[date, float]] = []
    for m in metrics:
        v = m.get(key)
        if v is None:
            continue
        d = m.get("date")
        d = d if isinstance(d, date) else date.fromisoformat(str(d))
        out.append((d, float(v)))
    return [(d, v) for d, v in out if today - timedelta(days=WINDOW_DAYS - 1) <= d <= today]


def _drift_pct(recent: list[float], prior: list[float]) -> float | None:
    """近段均值相对前段均值的变化百分比；任一段样本不足则缺省。"""
    if len(recent) < 3 or len(prior) < 7:
        return None
    base = sum(prior) / len(prior)
    if base == 0:
        return None
    return (sum(recent) / len(recent) - base) / base * 100


def _metric_status(metrics: list[dict], key: str, today: date) -> dict:
    cfg = _METRICS[key]
    pts = _series(metrics, key, today)
    n = len(pts)
    out = {"label": cfg["label"], "baseline": None, "spread": None, "latest": None,
           "latest_z": None, "drift_pct": None, "status": "数据不足",
           "adverse": False, "n": n}

    if n < MIN_BASELINE_N:
        return out

    vals = [v for _, v in pts]
    baseline = _median(vals)
    spread = _mad_spread(vals)
    latest_d, latest = pts[-1]

    out.update(baseline=round(baseline, 1), spread=None if spread is None else round(spread, 1),
               latest=latest)

    # 最新值太旧：基线仍有效，但不给当下判定
    if (today - latest_d).days > MIN_LATEST_AGE_DAYS:
        out["status"] = "数据不足"
        out["note"] = f"最新数据停在 {latest_d.isoformat()}"
        return out

    if spread is not None:
        z = (latest - baseline) / spread
        out["latest_z"] = round(z, 2)
        if abs(z) >= 2:
            out["status"] = "明显偏低" if z < 0 else "明显偏高"
            out["adverse"] = (z < 0 and cfg["low_bad"]) or (z > 0 and cfg["high_bad"])
            return out

    split = today - timedelta(days=RECENT_DAYS - 1)
    drift = _drift_pct([v for d, v in pts if d >= split], [v for d, v in pts if d < split])
    if drift is not None:
        out["drift_pct"] = round(drift, 1)
        drift_bad = (drift <= -DRIFT_THRESHOLD_PCT and cfg["low_bad"]) or \
                    (drift >= DRIFT_THRESHOLD_PCT and cfg["high_bad"])
        if drift_bad:
            out["status"] = "基线漂移"
            out["adverse"] = True
            return out

    out["status"] = "均衡"
    return out


def build_baseline(metrics: list[dict], today: date | None = None) -> dict:
    """metrics: body_metrics_dicts 输出（date 为 ISO 字符串或 date）。

    返回 {指标: {label, baseline, spread, latest, latest_z, drift_pct, status,
    adverse, n}}；as_of 标注判定基准日。窗口函数内部自行截取近 28 天。
    """
    today = today or date.today()
    out = {key: _metric_status(metrics, key, today) for key in _METRICS}
    return {"as_of": today.isoformat(), **out}


def objective_cap(metrics: list[dict], today: date | None = None) -> dict:
    """客观身体信号 → 今日建议档位封顶（fueling cap 同构，只降不升）。

    规则从严到宽（取最严者）：
    - 近 3 个记录日 HRV 中位数 z≤-2（持续抑制）→ 封顶 easy；
    - 最新 HRV z≤-2（单日大幅偏低）→ 封顶 reduce；
    - 昨夜睡眠 <6h（急性不足）→ 封顶 reduce。
    基线或波动带数据不足时不出 cap——客观信号宁可缺席也不猜。
    """
    today = today or date.today()
    hrv = _metric_status(metrics, "hrv_rmssd", today)
    sleep = _metric_status(metrics, "sleep_hours", today)

    reasons: list[str] = []
    cap = None

    if hrv["spread"] is not None and hrv["latest_z"] is not None:
        pts = _series(metrics, "hrv_rmssd", today)
        recent3 = [v for _, v in pts[-3:]]
        med3 = _median(recent3)
        z3 = (med3 - hrv["baseline"]) / hrv["spread"]
        if z3 <= -2:
            cap = CAP_EASY
            reasons.append(
                f"HRV 近 {len(recent3)} 个记录日中位数 {med3:.0f} 明显低于基线 "
                f"{hrv['baseline']:.0f}（z={z3:.1f}），恢复受抑制")
        elif hrv["latest_z"] <= -2:
            cap = "reduce"
            reasons.append(
                f"最新 HRV {hrv['latest']:.0f} 明显低于基线 {hrv['baseline']:.0f}"
                f"（z={hrv['latest_z']}），先观察一天")
    elif hrv["baseline"] is not None and hrv["latest"] is not None \
            and hrv["latest"] < 0.85 * hrv["baseline"]:
        # 样本不足以估波动带（7-9 天）时的退化口径：按固定比例保守处理
        cap = "reduce"
        reasons.append(
            f"HRV 最新值 {hrv['latest']:.0f} 低于基线 {hrv['baseline']:.0f} 的 85%（样本尚少，保守处理）")

    # 最新值是否新鲜已由 _metric_status 判过（数据不足 = 停更或样本少）
    if sleep["latest"] is not None and sleep["status"] != "数据不足" and sleep["latest"] < 6:
        if cap is None:
            cap = "reduce"
        reasons.append(f"昨夜睡眠仅 {sleep['latest']:.1f}h，急性不足")

    return {"cap_level": cap, "reasons": reasons}
