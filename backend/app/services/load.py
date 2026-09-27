"""训练负荷引擎：全项目唯一的负荷口径。

负荷来源优先级（诚实优先，缺数据时不臆造）：
  1. 平台真实值：如高驰返回的 training_load（记入 raw.coros_training_load）
  2. Banister TRIMP：有心率时按「心率储备」加权，体现个体差异
  3. sRPE：无心率但有主观强度时 时长(min) x RPE（Foster 法）
  4. unknown：都缺时返回 0，并标注来源，不生成假数据

注意：本模块被 load_status 依赖，不得反向导入 load_status（避免循环导入）。
"""
from __future__ import annotations

import math

# Banister 性别系数：(a, b)，男 0.64/1.92、女 0.86/1.67
_BANISTER = {"male": (0.64, 1.92), "female": (0.86, 1.67)}


def _field(athlete: object, key: str, default=None):
    """兼容 ORM 对象（models.Athlete）与 dict（athlete_dict）两种传参形态。"""
    if athlete is None:
        return default
    if isinstance(athlete, dict):
        value = athlete.get(key)
    else:
        value = getattr(athlete, key, None)
    return default if value is None else value


def hr_reserve(avg_hr: int | None, athlete: object = None) -> float | None:
    """心率储备比 HRr = (HR - 静息) / (最大 - 静息)，裁剪到 [0, 1]。

    心率缺失、或档案缺最大/静息心率时返回 None（不臆造），由调用方降级处理
    ——例如退到 sRPE，或标注 source="unknown"。

    此前这里会用 190/55 这两个与用户无关的常数顶上，导致没填心率的用户也
    拿到一套「看起来正常」的负荷数字，实际全是假的。
    """
    if not avg_hr:
        return None
    max_hr = _field(athlete, "max_hr")
    rest_hr = _field(athlete, "resting_hr")
    if max_hr is None or rest_hr is None:
        return None
    try:
        span = float(max_hr) - float(rest_hr)
    except (TypeError, ValueError):
        return None
    if span <= 0:
        return None
    return min(1.0, max(0.0, (float(avg_hr) - float(rest_hr)) / span))


def banister_trimp(duration_sec: int | None, avg_hr: int | None,
                   athlete: object = None) -> float | None:
    """Banister TRIMP = 时长(min) x HRr x a x e^(b x HRr)。

    时长非正或心率缺失时返回 None（不臆造）。
    """
    if not duration_sec or duration_sec <= 0:
        return None
    hrr = hr_reserve(avg_hr, athlete)
    if hrr is None:
        return None
    sex = str(_field(athlete, "sex", "male") or "male").lower()
    a, b = _BANISTER.get(sex, _BANISTER["male"])
    minutes = duration_sec / 60
    return minutes * hrr * a * math.exp(b * hrr)


def session_rpe(duration_sec: int | None, rpe: int | None) -> float | None:
    """sRPE = 时长(min) x 主观强度(1-10)，Foster 法。数据不足返回 None。"""
    if not duration_sec or duration_sec <= 0 or not rpe or rpe <= 0:
        return None
    return duration_sec / 60 * float(rpe)


def estimate_load(duration_sec: int | None, avg_hr: int | None = None,
                  rpe: int | None = None,
                  athlete: object = None) -> tuple[float, str]:
    """估算训练负荷，返回 (load, source)。

    source ∈ {"trimp", "srpe", "unknown"}；unknown 时 load 为 0.0。
    调用方应把 source 记入 raw，便于事后追溯每条负荷是真实值还是估算值。
    """
    trimp = banister_trimp(duration_sec, avg_hr, athlete)
    if trimp is not None:
        return round(trimp, 1), "trimp"
    srpe = session_rpe(duration_sec, rpe)
    if srpe is not None:
        return round(srpe, 1), "srpe"
    return 0.0, "unknown"


# 平台真实负荷写在 raw 里的键名（高驰适配器写入），取值链优先使用它
PLATFORM_LOAD_KEY = "coros_training_load"
SOURCE_KEY = "training_load_source"


def load_for_activity(activity: object, athlete: object = None) -> tuple[float, str]:
    """给出某条活动的负荷及其来源，并把来源写回 activity.raw。

    全项目唯一的负荷取值路径，线上同步与历史重算都调它。

    返回 (load, source)。source 取值见 estimate_load，另加 "coros"。
    """
    raw = getattr(activity, "raw", None)
    # 复制一份再赋值，确保 SQLAlchemy 能识别 JSON 列变更（原地改可能不被标记）
    raw = dict(raw) if isinstance(raw, dict) else {}

    real = raw.get(PLATFORM_LOAD_KEY)
    if real is not None:
        try:
            value = float(real)
        except (TypeError, ValueError):
            # 平台给了非数值：不静默丢弃，记下来再走估算，事后可追溯
            raw[SOURCE_KEY] = "coros_invalid"
            raw[PLATFORM_LOAD_KEY + "_invalid"] = real
            value = None
        if value is not None:
            raw[SOURCE_KEY] = "coros"
            activity.raw = raw
            return value, "coros"

    load, source = estimate_load(
        getattr(activity, "duration_sec", None),
        getattr(activity, "avg_hr", None),
        getattr(activity, "rpe", None),
        athlete,
    )
    raw[SOURCE_KEY] = source
    activity.raw = raw
    return load, source
