"""个性化基线引擎（services/baseline.py）的行为锁定。

背景：此前身体数据的对照基线只有档案里的一个手填整数，_readiness 里另有
一段手写的 hist[-28:-7] 均值兜底——离档天（饮酒/熬夜/测量失败）会把这类
均值基线拖偏。引擎改用「28 天中位数 + MAD 波动带」的稳健统计，并给打卡
建议提供客观封顶信号。这里锁住：稳健性黄金值、方向语义、诚实缺省、
打卡联动「只降不升」。
"""
from __future__ import annotations

from datetime import date, timedelta

from app.services.baseline import build_baseline, objective_cap
from app.services.checkin_advice import build_checkin_advice

TODAY = date(2026, 9, 27)


def _metric_rows(hrv_seq: list[float], sleep_seq: list[float] | None = None,
                 start: date | None = None) -> list[dict]:
    """按天生成 body_metrics_dicts 形状的行（date 为 ISO 字符串，与真实链路一致）。

    hrv_seq / sleep_seq 自最早一天起逐日取值；序列比日期短时后面几天缺测。"""
    start = start or (TODAY - timedelta(days=27))
    n_days = max(len(hrv_seq), len(sleep_seq or []))
    rows = []
    for i in range(n_days):
        d = (start + timedelta(days=i)).isoformat()
        rows.append({
            "date": d,
            "hrv_rmssd": hrv_seq[i] if i < len(hrv_seq) else None,
            "sleep_hours": sleep_seq[i] if sleep_seq and i < len(sleep_seq) else None,
        })
    return rows


def _flat(value: float, n: int, jitter: list[float] | None = None) -> list[float]:
    j = jitter or [0.0]
    return [value + j[i % len(j)] for i in range(n)]


# ---------------------------------------------------------------- 基线与波动带

def test_median_robust_to_outlier_and_mad_golden():
    """10 个值里混进一个 300 的坏值：中位数基线纹丝不动，MAD 波动带为手算黄金值。

    值集 [60..76, 300]：中位数 69；|x-69| 排序后中位数 5 → spread = 5×1.4826 ≈ 7.41。
    若用均值（≈95.1）会被坏值拖高 26——这正是换掉旧均值兜底的原因。
    """
    vals = [60, 62, 64, 66, 68, 70, 72, 74, 76, 300]
    rows = _metric_rows(vals)
    hrv = build_baseline(rows, today=TODAY)["hrv_rmssd"]
    assert hrv["baseline"] == 69.0
    assert hrv["spread"] == round(5 * 1.4826, 1)
    assert hrv["n"] == 10


def test_insufficient_data_is_honest():
    rows = _metric_rows([65, 64, 66, 65, 63])
    hrv = build_baseline(rows, today=TODAY)["hrv_rmssd"]
    assert hrv["baseline"] is None and hrv["status"] == "数据不足"
    assert hrv["adverse"] is False


def test_low_hrv_flags_adverse_but_high_hrv_does_not():
    """方向语义：HRV 明显偏低是负面信号，明显偏高只报事实。

    抖动让 MAD>0（波动带可估），z 分支才会先于漂移分支命中。"""
    low = build_baseline(_metric_rows(_flat(65, 27, [0, 1, -1, 0, 2]) + [40]), today=TODAY)["hrv_rmssd"]
    assert low["status"] == "明显偏低" and low["adverse"] is True
    assert low["latest_z"] is not None and low["latest_z"] <= -2

    high = build_baseline(_metric_rows(_flat(65, 27, [0, 1, -1, 0, 2]) + [90]), today=TODAY)["hrv_rmssd"]
    assert high["status"] == "明显偏高" and high["adverse"] is False


def test_resting_hr_high_is_adverse_sleep_low_is_adverse():
    from app.services.baseline import _METRICS
    assert _METRICS["resting_hr"]["high_bad"] is True
    assert _METRICS["sleep_hours"]["low_bad"] is True
    assert _METRICS["weight_kg"]["high_bad"] is False and _METRICS["weight_kg"]["low_bad"] is False


def test_drift_detected_when_spread_collapses():
    """21 天恒定 65 + 7 天恒定 58：MAD=0 给不出 z，但漂移检测必须接住（-10.8%）。"""
    rows = _metric_rows(_flat(65, 21) + _flat(58, 7))
    hrv = build_baseline(rows, today=TODAY)["hrv_rmssd"]
    assert hrv["spread"] is None          # MAD=0 → 诚实缺省波动带
    assert hrv["drift_pct"] == round((58 - 65) / 65 * 100, 1)
    assert hrv["status"] == "基线漂移" and hrv["adverse"] is True


def test_stale_latest_falls_back_to_no_verdict():
    """最新值停在 5 天前：基线仍可用，但当下判定必须缺省而不是拿旧值硬算。"""
    rows = _metric_rows(_flat(65, 23), start=TODAY - timedelta(days=32))
    hrv = build_baseline(rows, today=TODAY)["hrv_rmssd"]
    assert hrv["baseline"] is not None
    assert hrv["status"] == "数据不足" and hrv["latest_z"] is None


# ---------------------------------------------------------------- 打卡客观封顶

def test_cap_persistent_hrv_suppression_is_easy():
    """近 3 天 HRV 持续抑制 → 封顶 easy（最严档）。"""
    rows = _metric_rows(_flat(65, 25, [0, 1, -1, 0, 2]) + [45, 44, 46])
    cap = objective_cap(rows, today=TODAY)
    assert cap["cap_level"] == "easy"
    assert any("持续" in r or "抑制" in r for r in cap["reasons"])


def test_cap_single_day_hrv_drop_is_reduce():
    rows = _metric_rows(_flat(65, 25, [0, 1, -1, 0, 2]) + [65, 65, 45])
    cap = objective_cap(rows, today=TODAY)
    assert cap["cap_level"] == "reduce"


def test_cap_short_sleep_is_reduce():
    rows = [{"date": (TODAY - timedelta(days=27 - i)).isoformat(),
             "hrv_rmssd": 65 + (i % 3), "sleep_hours": 7.5} for i in range(27)]
    rows.append({"date": TODAY.isoformat(), "hrv_rmssd": 65, "sleep_hours": 5.5})
    cap = objective_cap(rows, today=TODAY)
    assert cap["cap_level"] == "reduce"
    assert any("睡眠" in r for r in cap["reasons"])


def test_cap_absent_without_enough_data():
    rows = _metric_rows([65, 64, 66, 65, 63])
    assert objective_cap(rows, today=TODAY)["cap_level"] is None


# ---------------------------------------------------------------- 打卡建议联动

def test_checkin_objective_cap_lowers_normal_but_never_raises():
    """主观满状态 + HRV 崩 → 必须降档；主观已 rest + 客观正常 → 不升档。"""
    perfect = {"sleep_quality": 5, "muscle_soreness": 1, "energy_level": 5,
               "motivation": 5, "pain_area": ""}
    advice = build_checkin_advice(perfect, None, None,
                                  {"cap_level": "easy", "reasons": ["HRV 持续抑制"]})
    assert advice["level"] == "easy"
    assert "客观信号联动降档" in advice["verdict"]

    wrecked = {"sleep_quality": 1, "muscle_soreness": 5, "energy_level": 1,
               "motivation": 1, "pain_area": ""}
    advice = build_checkin_advice(wrecked, None, None, {"cap_level": None, "reasons": []})
    assert advice["level"] == "rest"

    # 客观信号比主观结论松（reduce vs rest）→ 不动
    advice = build_checkin_advice(wrecked, None, None,
                                  {"cap_level": "reduce", "reasons": ["单日 HRV 偏低"]})
    assert advice["level"] == "rest"


def test_checkin_advice_backward_compatible_without_objective():
    """不传 objective 的旧调用（两参签名时代）行为不变。"""
    normal = {"sleep_quality": 5, "muscle_soreness": 1, "energy_level": 5,
              "motivation": 5, "pain_area": ""}
    advice = build_checkin_advice(normal, None, None)
    assert advice["level"] == "normal"


# ---------------------------------------------------------------- readiness 接入

def test_readiness_uses_engine_baseline_when_profile_missing():
    """档案未声明 hrv_baseline 时，_readiness 用引擎 28 天中位数（旧代码是手写均值）。"""
    from app.services import load_status
    metrics = [{"date": (TODAY - timedelta(days=27 - i)).isoformat(),
                "hrv_rmssd": 65 + (i % 3), "sleep_hours": 7.5} for i in range(27)]
    metrics.append({"date": TODAY.isoformat(), "hrv_rmssd": 64, "sleep_hours": 7.5})
    result = load_status._readiness(metrics, latest=metrics[-1], athlete={},
                                    acwr=1.0, recovery_h=12)
    hrv_part = next(p for p in result["components"] if p["name"] == "HRV")
    # 28 天中位数 65，近 7 天均值 ≈65.4 → ratio ≈1.0，处于基线区间
    assert "基线" in hrv_part["detail"]
    assert result["score"] is not None and result["score"] >= 60
