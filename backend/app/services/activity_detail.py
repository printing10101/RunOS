"""活动详情引擎：把单次活动解析成详情页所需的分段/曲线/轨迹/心率区间。

数据来源优先级：
  1. 平台同步时存入 raw 的原始详情（佳明 lapDTOs / 高驰 detail）
  2. raw["series"] / raw["track"]（演示数据与支持的平台直接提供）
  3. 按活动均值确定性合成（random 以活动 id 播种，同一条活动每次合成结果一致），
     并在 notes 中明确标注"模拟"，不与设备数据混淆。
"""
from __future__ import annotations

import random

from ..integrations.base import maybe_float

# Minetti 能耗系数（J/kg/m）：坡度 i 下的跑步能耗，cost(0)=3.6
MINETTI = [155.4, -30.4, -43.3, 46.3, 19.5, 3.6]


def minetti_cost(grade: float) -> float:
    i = max(-0.45, min(0.45, grade))
    return (MINETTI[0] * i**5 + MINETTI[1] * i**4 + MINETTI[2] * i**3
            + MINETTI[3] * i**2 + MINETTI[4] * i + MINETTI[5])


def gap_sec_per_km(pace_sec: float, grade: float) -> float:
    """坡度调整配速（GAP）：把坡度配速换算为等能耗的平路配速。"""
    cost = minetti_cost(grade)
    if cost <= 0 or pace_sec <= 0:
        return pace_sec
    return round(pace_sec * cost / 3.6, 1)


def decode_polyline(encoded: str) -> list[list[float]]:
    """Google Encoded Polyline 解码（佳明 geoPolylineDTO 使用）。"""
    out: list[list[float]] = []
    idx = lat = lng = 0
    while idx < len(encoded):
        for which in range(2):
            shift = result = 0
            while True:
                if idx >= len(encoded):
                    return out
                b = ord(encoded[idx]) - 63
                idx += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            val = ~(result >> 1) if result & 1 else result >> 1
            if which == 0:
                lat += val
            else:
                lng += val
        out.append([lat / 1e5, lng / 1e5])
    return out


# ---------------------------------------------------------------- 主入口

def build_activity_detail(act, athlete=None) -> dict:
    """把 Activity ORM 对象装配为活动详情页 payload。"""
    rng = random.Random(act.id * 7919 + int(act.start_time.timestamp()) % 100000)
    sport = act.sport or "run"
    dist_km = (act.distance_m or 0) / 1000
    avg_pace = act.duration_sec / dist_km if dist_km and act.duration_sec else None

    raw = act.raw or {}
    detail = raw.get("detail") or {}
    notes: list[str] = []

    splits = _build_splits(act, raw, detail, dist_km, avg_pace, rng, notes)
    series = _build_series(act, raw, detail, splits, dist_km, rng, notes)
    track = _build_track(raw, detail, notes)
    zone_times = _build_hr_zone_times(act, raw, athlete, notes)
    dynamics = _build_dynamics(act, detail, notes) if sport == "run" else None
    extras = _build_extras(act, detail, sport, notes)
    decoupling = single_decoupling(act, raw) if sport == "run" else None

    pace_sec_per_km = round(avg_pace) if avg_pace else None
    return {
        "id": act.id, "sport": sport, "title": act.title, "platform": act.platform,
        "start_time": act.start_time.isoformat(),
        "distance_m": act.distance_m, "duration_sec": act.duration_sec,
        "pace_sec_per_km": pace_sec_per_km,
        "avg_hr": act.avg_hr, "max_hr": act.max_hr,
        "avg_cadence": act.avg_cadence, "avg_power": act.avg_power,
        "elevation_m": act.elevation_m or 0, "calories": act.calories,
        "temp_c": act.temp_c, "weather": act.weather,
        "te_aerobic": act.te_aerobic, "te_anaerobic": act.te_anaerobic,
        "rpe": act.rpe, "training_load": act.training_load,
        "dynamics": dynamics, "extras": extras,
        "splits": splits, "series": series,
        "hr_zone_times": zone_times, "track": track,
        "decoupling": decoupling,
        "notes": notes,
    }


# ---------------------------------------------------------------- 分段

def _build_splits(act, raw: dict, detail: dict, dist_km: float,
                  avg_pace: float | None, rng: random.Random, notes: list[str]) -> dict:
    items = None
    source = "synthetic"

    laps = raw.get("splits") or detail.get("splits") or detail.get("lapDTOs")
    if isinstance(laps, dict):
        laps = laps.get("lapDTOs") or laps.get("splits")
    parsed = _parse_device_laps(laps) if laps else None
    if parsed:
        items, source = parsed, "device"

    if items is None and dist_km >= 0.5 and avg_pace:
        items = _synthetic_splits(act, dist_km, avg_pace, rng)
        notes.append("本活动无设备分段数据，分段表按活动均值模拟生成，仅供参考")

    if items is None:
        return {"source": None, "items": [], "gap_available": False}

    _apply_elevation(act, items, rng, notes)
    _apply_gap(items, notes)
    return {"source": source, "items": items, "gap_available": any(s.get("gap_sec_per_km") for s in items)}


def _parse_device_laps(laps) -> list[dict] | None:
    """解析佳明/高驰/Strava lap 列表，字段名做宽容适配。"""
    items = []
    for i, lap in enumerate(laps):
        if not isinstance(lap, dict):
            return None
        d = _num(lap, ("distance", "distance_meter", "dist"))
        t = _num(lap, ("duration", "duration_sec", "elapsed_duration", "elapsedTime",
                       "elapsed_time", "moving_time"))
        if not d or not t:
            return None
        items.append({
            "index": i + 1,
            "distance_m": round(d, 1),
            "duration_sec": round(t),
            "pace_sec_per_km": round(t / (d / 1000)),
            "avg_hr": _num(lap, ("averageHR", "avg_hr", "avgHeartRate", "average_heartrate")),
            "elev_gain_m": round(_num(lap, ("elevationGain", "elev_gain", "elevation",
                                            "total_elevation_gain")) or 0, 1),
        })
    return items or None


def _synthetic_splits(act, dist_km: float, avg_pace: float, rng: random.Random) -> list[dict]:
    """按均值确定性合成整公里分段（波动 ±2.5%，总时长严格对齐）。"""
    n_full = int(dist_km)
    rem_m = round((dist_km - n_full) * 1000)
    segs = [1000.0] * n_full + ([rem_m] if rem_m >= 50 else [])
    noise = [1 + rng.uniform(-0.025, 0.025) for _ in segs]
    k = act.duration_sec / sum(1000 * avg_pace * x for x in noise)
    return [{
        "index": i + 1,
        "distance_m": round(seg, 1),
        "duration_sec": round(1000 * avg_pace * noise[i] * k),
        "pace_sec_per_km": round(1000 * avg_pace * noise[i] * k / (seg / 1000)),
        "avg_hr": None,
        "elev_gain_m": 0.0,
    } for i, seg in enumerate(segs)]


def _apply_elevation(act, items: list[dict], rng: random.Random, notes: list[str]) -> None:
    """爬升按权重分摊到各分段；无心率分段时以配速波动近似心率波动。"""
    total_elev = act.elevation_m or 0
    device_elev = any(s.get("elev_gain_m") for s in items)
    weights = [rng.uniform(0.6, 1.4) for _ in items]
    wsum = sum(weights)
    acc = 0.0
    for i, s in enumerate(items):
        gain = total_elev * weights[i] / wsum if i < len(items) - 1 else max(0.0, total_elev - acc)
        acc += gain
        if not s.get("elev_gain_m"):
            s["elev_gain_m"] = round(gain, 1)
        if s.get("avg_hr") is None and act.avg_hr:
            s["avg_hr"] = int(max(90, act.avg_hr + rng.uniform(-6, 6)))
    if not device_elev and total_elev:
        notes.append("分段爬升为按活动总爬升的分摊估计（设备未提供逐段爬升）")


def _apply_gap(items: list[dict], notes: list[str]) -> None:
    """对有分段爬升的跑步活动计算坡度调整配速。"""
    for s in items:
        if not s.get("elev_gain_m"):
            continue
        grade = s["elev_gain_m"] / s["distance_m"]
        s["grade"] = round(grade * 100, 1)
        s["gap_sec_per_km"] = gap_sec_per_km(s["pace_sec_per_km"], grade)
    if any("gap_sec_per_km" in s for s in items):
        notes.append("GAP（坡度调整配速）按 Minetti 能耗模型由分段爬升换算")


# ---------------------------------------------------------------- 曲线 / 轨迹 / 区间

def _build_series(act, raw: dict, detail: dict, splits: dict, dist_km: float,
                  rng: random.Random, notes: list[str]) -> dict:
    own = raw.get("series") or detail.get("series")
    if isinstance(own, dict) and own.get("points"):
        source = own.get("source") or "device"
        if source == "strava_streams":
            notes.append("曲线来自真实逐点数据流（Strava streams，约每百米一个采样点）")
        return {"source": source, "points": own["points"]}

    if splits.get("source") == "device":
        notes.append("曲线图由设备分段聚合而成（精度：每公里，海拔为分摊估计）")
    elif splits.get("source") == "synthetic":
        notes.append("曲线图由活动均值模拟生成，仅供参考")

    points = []
    t = 0.0
    alt = 40.0 + rng.uniform(-15, 25)   # 起点海拔（城市基线）
    for s in splits.get("items", []):
        t += s["duration_sec"]
        alt += s.get("elev_gain_m", 0) * 0.6 - rng.uniform(0, 4)
        points.append({
            "km": round((s["index"] * s["distance_m"]) / 1000, 2),
            "time_sec": round(t),
            "pace_sec_per_km": s["pace_sec_per_km"],
            "hr": s.get("avg_hr"),
            "altitude_m": round(alt, 1),
        })
    return {"source": "aggregated" if splits.get("source") == "device" else "synthetic", "points": points}


def _build_track(raw: dict, detail: dict, notes: list[str]) -> list | None:
    if isinstance(raw.get("track"), list) and raw["track"]:
        return raw["track"]
    pl = (detail.get("geoPolylineDTO") or {}).get("polyline")
    if pl:
        return decode_polyline(pl)
    return None


def _build_hr_zone_times(act, raw: dict, athlete, notes: list[str]) -> dict | None:
    """心率区间时间分布（秒）：优先设备数据，否则按分段心率落入区间估算。"""
    zones_raw = raw.get("hr_zones")
    if isinstance(zones_raw, dict):
        out = {}
        for key in zones_raw:
            z = "".join(ch for ch in key if ch.isdigit())
            if z and _num(zones_raw, (key,)):
                out[f"Z{min(5, int(z))}"] = round(_num(zones_raw, (key,)))
        if out:
            return out
    if not act.avg_hr or not athlete:
        return None
    # 心率基准缺失就不分区：此前用 190/60 顶上，会把「没填档案」变成一套
    # 看起来正常但与本人类别无关的区间占比
    max_hr = athlete.max_hr
    rest_hr = athlete.resting_hr
    if not max_hr or not rest_hr or max_hr <= rest_hr:
        notes.append("档案缺最大/静息心率，本次活动无法估算心率区间分布")
        return None
    reserve = max(1, max_hr - rest_hr)
    bounds = [rest_hr + p * reserve for p in (0.60, 0.70, 0.80, 0.88)]
    total = act.duration_sec
    buckets = [0.0, 0.0, 0.0, 0.0, 0.0]
    if act.avg_hr <= bounds[0]:
        buckets[0] = total
    elif act.avg_hr >= bounds[3]:
        buckets[4] = total
    else:
        # 峰值心率越高，高区间占比越大：按 (avg-rest)/(max-rest) 线性映射
        pct = (act.avg_hr - rest_hr) / reserve
        if pct < 0.50:
            # 均心率低于 Z1 下沿（恢复慢跑常态）：全记 Z1，否则按公式 Z1 会超过总时长
            buckets[0] = total
        elif pct < 0.70:
            buckets[0], buckets[1] = total * (0.70 - pct) / 0.2, total * (pct - 0.5) / 0.2
        elif pct < 0.80:
            buckets[1], buckets[2] = total * (0.80 - pct) / 0.1, total * (pct - 0.70) / 0.1
        else:
            buckets[2], buckets[3] = total * (0.88 - pct) / 0.08, total * (pct - 0.80) / 0.08
    out = {f"Z{i+1}": round(v) for i, v in enumerate(buckets) if v > 0}
    notes.append("心率区间分布为按活动均值的估算，接入佳明后自动替换为设备实测")
    return out


def _build_dynamics(act, detail: dict, notes: list[str]) -> dict | None:
    dyn = act.dynamics or {}
    if not dyn:
        src = {k: maybe_float(detail.get(v)) for k, v in (
            ("stride_m", "avgStrideLength"), ("vosc_cm", "avgVerticalOscillation"),
            ("gct_ms", "avgGroundContactTime"), ("gct_balance_pct", "avgGctBalance"))}
        dyn = {k: v for k, v in src.items() if v is not None}
    return dyn or None


def _build_extras(act, detail: dict, sport: str, notes: list[str]) -> dict | None:
    """游泳/骑行专项字段（划次/SWOLF/踏频等），有则透出。"""
    if sport == "swim":
        strokes = maybe_float(detail.get("avgStrokes") or detail.get("averageStrokeCount"))
        swolf = maybe_float(detail.get("avgSwolf") or detail.get("averageSWOLF"))
        pool_len = maybe_float(detail.get("poolLength"))
        out = {k: v for k, v in {"strokes_per_length": strokes, "swolf": swolf,
                                 "pool_length_m": pool_len}.items() if v is not None}
        return out or None
    if sport == "ride":
        cad = maybe_float(detail.get("averageBikingCadenceInRevPerMinute"))
        power = act.avg_power
        out = {k: v for k, v in {"avg_rpm": cad, "avg_power_w": power}.items() if v is not None}
        return out or None
    return None


# ---------------------------------------------------------------- 跨活动最佳努力（PB）

def rolling_best(splits: list[dict], dist_m: float) -> int | None:
    """在分段序列上滑窗求「最快 contiguous dist_m」用时（秒），窗口边界线性内插。

    分段护栏：GPS 漂移会产生「1 秒飘出几百米」的假峰分段；人类极限约
    44km/h（博尔特巅峰），分段配速低于 2:00/km（即 >50km/h）一律视为
    坏点，整段丢弃。
    """
    splits = [
        s for s in splits
        if s.get("distance_m") and s.get("duration_sec")
        and s["duration_sec"] * 1000.0 / s["distance_m"] >= 120
    ]
    if not splits or dist_m <= 0:
        return None
    total = sum(s["distance_m"] for s in splits)
    if total < dist_m:
        return None

    best: int | None = None
    n = len(splits)
    for i in range(n):
        d_acc = t_acc = 0.0
        for j in range(i, n):
            seg = splits[j]["distance_m"]
            take = min(seg, dist_m - d_acc)
            t_acc += splits[j]["duration_sec"] * (take / seg if seg else 0)
            d_acc += take
            if d_acc >= dist_m - 1e-6:
                cand = round(t_acc)
                best = cand if best is None else min(best, cand)
                break
    return best


# ---------------------------------------------------------------- 工具

def _num(d: dict, keys: tuple) -> float | None:
    for k in keys:
        v = d.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


# ---------------------------------------------------------------- 前后半程切分（有氧解耦原料）

# 爬升 >12m/km 的课受坡度影响大，EF/解耦不可解读（pro_insights 洞察同口径）
MAX_ELEV_PER_KM = 12.0
# GPS 漂移坏点护栏（同 rolling_best）：>50km/h 的分段丢弃
MAX_SPEED_SEC_PER_KM = 120.0


def activity_halves(a: dict) -> tuple[float, float, float, float] | None:
    """把一次跑步切成前后两半，返回 (前半 EF, 后半 EF, 前半配速 s/km, 后半配速 s/km)。

    EF = 速度(km/min) ÷ 平均心率。只认两类设备数据：
      1. raw["series"].points（Strava streams 逐点）
      2. 设备 laps（raw["splits"] / detail.lapDTOs，复用 _parse_device_laps 的宽容解析）
    均心率只有一个点，切半没有意义——直接返回 None（调用方计为数据缺口）。
    """
    raw = a.get("raw") or {}
    pts = (raw.get("series") or {}).get("points")
    if isinstance(pts, list) and len(pts) >= 8:
        halves = _halves_from_series(pts)
        if halves:
            return halves
    laps_raw = raw.get("splits") or (raw.get("detail") or {}).get("lapDTOs") \
        or (raw.get("detail") or {}).get("splits")
    if isinstance(laps_raw, dict):
        laps_raw = laps_raw.get("lapDTOs") or laps_raw.get("splits")
    if laps_raw:
        laps = _parse_device_laps(laps_raw)
        if laps and all(lap.get("avg_hr") for lap in laps):
            return _halves_from_laps(laps)
    return None


def decoupling_pct(ef_first: float, ef_second: float) -> float:
    """心率漂移百分比：正值 = 同样配速下后半程心率更高（效率下降）。"""
    return (ef_first - ef_second) / ef_first * 100 if ef_first > 0 else 0.0


def _halves_from_series(pts: list[dict]) -> tuple[float, float, float, float] | None:
    """逐点流切半：以总用时中点为界，分别累计距离/用时/心率加权均值。"""
    pts = [p for p in pts
           if p.get("hr") and p.get("km") is not None and p.get("time_sec") is not None]
    if len(pts) < 8:
        return None
    total_t = pts[-1]["time_sec"]
    # 相邻点求差得到段增量（首点没有前驱，从第二个点开始算）
    segs = []
    for prev, cur in zip(pts, pts[1:], strict=False):
        dt = cur["time_sec"] - prev["time_sec"]
        dd = (cur["km"] - prev["km"]) * 1000
        if dt <= 0 or dd <= 0:
            continue
        pace = dt / dd * 1000
        if pace < MAX_SPEED_SEC_PER_KM:
            continue   # GPS 漂移坏点：快于 50km/h 的段丢弃（同 rolling_best 护栏）
        segs.append({"dt": dt, "dd": dd, "hr": cur["hr"]})
    if not segs or total_t <= 0:
        return None
    mid = total_t / 2.0
    # 单次扫描分桶：结束时间 ≤ 中点的段进前半，跨界的进后半
    acc1 = {"t": 0.0, "d": 0.0, "hrw": 0.0}
    acc2 = {"t": 0.0, "d": 0.0, "hrw": 0.0}
    cum = 0.0
    for s in segs:
        acc = acc1 if cum + s["dt"] <= mid + 1e-6 else acc2
        acc["t"] += s["dt"]
        acc["d"] += s["dd"]
        acc["hrw"] += s["hr"] * s["dt"]
        cum += s["dt"]

    def ef_and_pace(a: dict) -> tuple[float, float, float] | None:
        if a["t"] < 300 or a["d"] < 400:   # 半程不足 5min/400m 没有统计意义
            return None
        return a["d"] / 1000 / (a["t"] / 60), a["hrw"] / a["t"], a["t"] / a["d"] * 1000

    first = ef_and_pace(acc1)
    second = ef_and_pace(acc2)
    if not (first and second):
        return None
    return first[0] / first[1], second[0] / second[1], first[2], second[2]


def _halves_from_laps(laps: list[dict]) -> tuple[float, float, float, float] | None:
    """设备 lap 切半：按用时中点分界，心率按 lap 时长加权。"""
    total_t = sum(lap["duration_sec"] for lap in laps)
    if total_t <= 0:
        return None
    mid = total_t / 2.0

    def half(laps_sel: list[dict]) -> tuple[float, float, float] | None:
        t = sum(lap["duration_sec"] for lap in laps_sel)
        d = sum(lap["distance_m"] for lap in laps_sel)
        if t < 300 or d < 400:
            return None
        hr_w = sum(lap["avg_hr"] * lap["duration_sec"] for lap in laps_sel)
        return d / 1000 / (t / 60), hr_w / t, t / d * 1000

    acc_t, first_laps = 0, []
    for i, lap in enumerate(laps):
        if acc_t + lap["duration_sec"] <= mid or i == 0:
            first_laps.append(lap)
            acc_t += lap["duration_sec"]
        else:
            break
    second_laps = laps[len(first_laps):]
    first, second = half(first_laps), half(second_laps)
    if not (first and second):
        return None
    return first[0] / first[1], second[0] / second[1], first[2], second[2]


def single_decoupling(act, raw: dict) -> dict | None:
    """单次活动的分半 EF 与解耦（详情页展示块）。

    算不出（无设备逐段心率）返回 None；reliable 标注数据是否满足洞察级
    护栏（40min 以上、爬升 ≤12m/km）——不满足也给数，但提示慎读。
    """
    a = {"duration_sec": act.duration_sec, "avg_hr": act.avg_hr,
         "distance_m": act.distance_m, "elevation_m": act.elevation_m or 0,
         "raw": raw}
    halves = activity_halves(a)
    if not halves:
        return None
    ef1, ef2, p1, p2 = halves
    dur_ok = bool(2400 <= (act.duration_sec or 0) <= 14400)
    elev_ok = not act.distance_m or (act.elevation_m or 0) / (act.distance_m / 1000) <= MAX_ELEV_PER_KM
    return {
        "ef_first": round(ef1, 3), "ef_second": round(ef2, 3),
        "pace_first_sec_per_km": round(p1), "pace_second_sec_per_km": round(p2),
        "decoupling_pct": round(decoupling_pct(ef1, ef2), 1),
        "reliable": dur_ok and elev_ok,
    }
