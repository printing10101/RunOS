"""官方 ↔ 本地负荷/体能交叉校验：两套并行体系互为对照，背离时点名。

高驰同步每天都在 fitness_snapshots 落官方快照（负荷比 load_ratio、官方
VO2max），但本地 ACWR/预测引擎此前完全不读它们——用户面对「信本地的还是
信手表的」只能靠猜。本引擎做两件确定性的事：

1. 负荷背离检测：官方 load_ratio 与本地 ACWR 同日对照。两边的分母窗口不同
   （官方口径未知），所以不比数值比「判断」：各自归入 低/正常/高 三档，
   一方报异常而另一方不认同、或两侧档位相左，即记一天背离；近 7 个可比日
   里背离 ≥3 天提示两套体系对负荷状态的判断分歧。
2. 官方 VO2max 走向：最新值相对 28 天前的中位基线的漂移，给预测与用户一个
   官方口径的能力走向（本地 VDOT 趋势之外的第二意见）。

只读、确定性、缺数据如实降级——官方快照是可选数据源，任何一侧缺席都
不出结论，绝不编造。
"""
from __future__ import annotations

from datetime import date, timedelta
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models

# 官方与本地共用同一组档位线（本地安全区间 0.8-1.3，官方负荷比的解读区间一致）
BAND_LOW = 0.8
BAND_HIGH = 1.3
DIVERGENCE_WINDOW = 7      # 近 N 个可比日
DIVERGENCE_TRIGGER = 3     # 其中 ≥N 天背离视为分歧
VO2MAX_TREND_GAP_DAYS = 28  # 趋势基线：最新值往前至少这么久的中位数


def snapshots_dicts(db: Session, athlete_id: int, days: int = 90) -> list[dict]:
    """官方体能快照的轻量投影（交叉校验与预测趋势共用）。"""
    since = date.today() - timedelta(days=days)
    rows = db.scalars(
        select(models.FitnessSnapshot)
        .where(models.FitnessSnapshot.athlete_id == athlete_id,
               models.FitnessSnapshot.date >= since)
        .order_by(models.FitnessSnapshot.date)
    ).all()
    return [{"date": r.date, "load_ratio": r.load_ratio, "vo2max": r.vo2max,
             "race_predictions": r.race_predictions or {}} for r in rows]


def _as_date(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v))


def _band(v: float) -> str:
    if v <= BAND_LOW:
        return "low"
    if v > BAND_HIGH:
        return "high"
    return "normal"


def acwr_series(daily: list[dict]) -> dict[date, float]:
    """本地逐日 ACWR：对 load_status 的 daily 序列 [{date, load}] 按同口径重放——
    当日近 7 天负荷 ÷ 近 28 天周均。序列头部 28 天窗口不完整，跳过不猜。"""
    pts = sorted((date.fromisoformat(x["date"]) if isinstance(x["date"], str) else x["date"],
                  float(x.get("load") or 0))
                 for x in daily)
    if not pts:
        return {}
    window_start = pts[0][0]
    out: dict[date, float] = {}
    for d, _ in pts:
        if (d - window_start).days < 27:
            continue   # 28 天慢性负荷窗口不完整，算出来的比值会虚高
        acute = sum(v for dt, v in pts if d - timedelta(days=6) <= dt <= d)
        chronic = sum(v for dt, v in pts if d - timedelta(days=27) <= dt <= d) / 4.0
        if chronic > 0:
            out[d] = round(acute / chronic, 2)
    return out


def load_divergence(snapshots: list[dict], local_acwr: dict[date, float]) -> dict:
    """官方 load_ratio 与本地 ACWR 的逐日对照结论（只比两边都有的日子）。"""
    pairs: list[dict] = []
    for s in snapshots:
        lr, la = s.get("load_ratio"), local_acwr.get(_as_date(s["date"]))
        if lr is None or la is None:
            continue
        bo, bl = _band(float(lr)), _band(la)
        # 一方报异常而另一方不认同，或两侧档位相左，才算「判断分歧」；
        # 同处正常档但数值有差，是口径差异不是分歧
        divergent = bo != bl and (bo != "normal" or bl != "normal")
        pairs.append({"date": _as_date(s["date"]).isoformat(), "official": float(lr),
                      "local": la, "divergent": divergent})

    recent = pairs[-DIVERGENCE_WINDOW:]
    n_div = sum(1 for p in recent if p["divergent"])
    triggered = len(recent) >= 2 and n_div >= DIVERGENCE_TRIGGER
    worst = max((p for p in recent if p["divergent"]),
                key=lambda p: abs(p["official"] - p["local"]), default=None)
    detail = None
    if triggered and worst:
        detail = (f"近 {len(recent)} 天里 {n_div} 天官方与本地负荷判断背离"
                  f"（如 {worst['date']} 官方 {worst['official']} vs 本地 {worst['local']}），"
                  "两套体系对当前负荷状态的评估分歧，建议以体感与恢复数据为准复核")
    return {"days_compared": len(recent), "divergent_days": n_div,
            "triggered": triggered, "worst_example": worst, "detail": detail}


def vo2max_trend(snapshots: list[dict]) -> dict:
    """官方 VO2max 走向：最新值 vs 28 天前基线中位数。跨度不足如实缺省。"""
    pts = sorted((_as_date(s["date"]), float(s["vo2max"])) for s in snapshots
                 if s.get("vo2max") is not None)
    if not pts:
        return {"latest": None, "delta": None, "detail": None, "n": 0}
    latest_d, latest = pts[-1]
    prior_vals = [v for d, v in pts if (latest_d - d).days >= VO2MAX_TREND_GAP_DAYS]
    out: dict = {"latest": latest, "latest_date": latest_d.isoformat(),
                 "prior_median": None, "delta": None, "n": len(pts), "detail": None}
    if not prior_vals:
        out["detail"] = f"官方 VO2max 最新 {latest:.0f}；记录跨度不足 {VO2MAX_TREND_GAP_DAYS} 天，暂不算趋势"
        return out
    prior = median(prior_vals)
    out["prior_median"] = round(prior, 1)
    out["delta"] = round(latest - prior, 1)
    if out["delta"] >= 1:
        out["detail"] = f"官方 VO2max 较 28 天前基线上升 {out['delta']:.1f}（{prior:.1f} → {latest:.0f}）"
    elif out["delta"] <= -1:
        out["detail"] = f"官方 VO2max 较 28 天前基线下降 {abs(out['delta']):.1f}（{prior:.1f} → {latest:.0f}），检视近期负荷与睡眠"
    else:
        out["detail"] = f"官方 VO2max 与 28 天前基线持平（{latest:.0f}）"
    return out


def build_crosscheck(snapshots: list[dict], daily: list[dict],
                     today: date | None = None) -> dict:
    """统一装配入口：官方快照 × 本地逐日负荷 → 交叉校验结论。

    snapshots: snapshots_dicts 输出；daily: load_status 的每日负荷序列。
    没有任何官方快照（未连接高驰/未同步）→ empty，不打扰。
    """
    today = today or date.today()
    if not snapshots:
        return {"empty": True}
    local_acwr = acwr_series(daily)
    return {
        "empty": False,
        "as_of": today.isoformat(),
        "load_divergence": load_divergence(snapshots, local_acwr),
        "vo2max_trend": vo2max_trend(snapshots),
    }
