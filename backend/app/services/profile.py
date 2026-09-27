"""运动画像解析层（Profile Resolution Engine）。

把跑者档案从「写死的静态默认值」改造为「按本人数据实时解析」。

四级解析链（优先级从高到低）：

  1. ``user``      用户在档案里填写过的值（本人的直接陈述）
  2. ``measured``  本人设备/测试数据实测：体测静息心率、HRV、体重、活动峰值心率
  3. ``derived``   由本人其它属性按生理公式推导：Tanaka 最大心率、训练年限代理…
  4. ``estimated`` 以本人属性（年龄/性别/训练年限）为参数的回归先验

三条不可让渡的规则：

  · **实测值优先于用户手填**。观测到的峰值心率是硬事实，会跟着训练自动上调；
    静息心率取近 4 周晨测中位数（本机有 300+ 条时，中位数比单次手填更可靠）。
  · **估计值不覆盖用户手填**。没有实测依据时保留用户填的值，绝不用群体估计
    去覆盖用户真实输入——否则用户会觉得档案被改坏了。估计只在「从未填过」时兜底。
  · **没有依据就标 ``unknown``**，下游必须显式降级，不得退回与用户无关的常数
    （历史上的 190/55/65/1995/2.0 就是这类常量，会让所有用户拿到同一套画像）。

每个字段同时产出 ``basis``（人类可读依据）与 ``at``（时间戳），
前端可直接展示「这个数字是怎么来的」。

本模块只依赖 ``models``，不反向导入 ``data``（``data`` 会导入本模块）。
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models

# 来源标识
USER = "user"
MEASURED = "measured"
DERIVED = "derived"
ESTIMATED = "estimated"
UNKNOWN = "unknown"

SOURCE_LABELS = {
    USER: "你填写的",
    MEASURED: "实测",
    DERIVED: "按你的数据推导",
    ESTIMATED: "按你的属性估算",
    UNKNOWN: "数据不足",
}

# 哪些字段受「自动推算」管辖（前端开关与回写范围）
AUTO_FIELDS = ("max_hr", "resting_hr", "hrv_baseline", "weight_kg", "training_age_years")

# 刷新节流：同一份数据在窗口内不重复解析（GET 路径每请求都会取档案）
_REFRESH_TTL_SEC = 30


@dataclass(frozen=True)
class Resolved:
    """单个字段的解析结果：值 + 来源 + 依据 + 置信度。"""

    key: str
    value: Any
    source: str
    basis: str
    confidence: float = 0.0

    @property
    def known(self) -> bool:
        return self.value is not None and self.source != UNKNOWN

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "value": self.value,
            "source": self.source,
            "source_label": SOURCE_LABELS.get(self.source, self.source),
            "basis": self.basis,
            "confidence": round(self.confidence, 2),
            "known": self.known,
        }


# ---------------------------------------------------------------- 生理先验
# 全部是「本人属性的函数」，不含与用户无关的绝对常数。


def tanaka_max_hr(age: float) -> float:
    """Tanaka 208 − 0.7×年龄（比 220−年龄 误差更小，男女通用）。"""
    return 208.0 - 0.7 * float(age)


def _sex_label(sex: str | None) -> str:
    return "女" if sex == "female" else "男"


def resting_hr_prior(age: float | None, sex: str | None,
                     training_age: float | None) -> tuple[float, float]:
    """静息心率先验：性别基础值，按训练年限下调（有氧适应，前 3 年最明显），
    30 岁后每满 1 年小幅回升。返回 (值, 置信度)。"""
    base = 64.0 if (sex or "male") == "male" else 68.0
    adapt = -min(12.0, 4.0 * float(training_age or 0) ** 0.6)
    age_adj = max(0.0, float(age if age is not None else 30) - 30) * 0.12
    return round(base + adapt + age_adj), 0.35


def hrv_prior(resting_hr: float, age: float | None, sex: str | None) -> tuple[float, float]:
    """HRV(RMSSD) 先验：个体内与静息心率强负相关——RHR 越低 HRV 越高。
    以静息心率为主轴，性别与年龄做小幅修正。返回 (值, 置信度)。"""
    value = 55.0 + (58.0 - float(resting_hr)) * 1.2
    if (sex or "male") == "female":
        value += 3.0
    value -= max(0.0, (float(age if age is not None else 30) - 30)) * 0.15
    return round(min(120.0, max(15.0, value))), 0.3


def max_hr_from_effort(peak_avg_hr: float) -> float:
    """由「一次持续 ≥20 分钟高强度努力的平均心率」推峰值心率。

    经验换算：20 分钟阈值以上努力的均值约为最大心率的 93%~95%，
    取 1.06 倍作为峰值估计（保守，不虚高）。
    """
    return round(peak_avg_hr * 1.06)


# ---------------------------------------------------------------- 证据采集


def _median(values: list[float]) -> float | None:
    vals = [float(v) for v in values if v is not None]
    return statistics.median(vals) if vals else None


def load_evidence(db: Session, athlete_id: int, days: int = 400,
                  today: date | None = None) -> dict:
    """一次性取齐解析所需证据，避免逐字段查库。

    返回 {"activities": [...], "metrics": [...]}，均为轻量 dict，
    只含解析真正要用的列。
    """
    today = today or date.today()
    since_ts = datetime.now() - timedelta(days=days)
    since_d = today - timedelta(days=days)

    arows = db.execute(
        select(models.Activity.start_time, models.Activity.sport, models.Activity.avg_hr,
               models.Activity.max_hr, models.Activity.distance_m, models.Activity.duration_sec)
        .where(models.Activity.athlete_id == athlete_id,
               models.Activity.start_time >= since_ts)
    ).all()
    acts = [{"date": r[0].date() if r[0] else None, "sport": r[1], "avg_hr": r[2],
             "max_hr": r[3], "distance_m": r[4] or 0, "duration_sec": r[5] or 0}
            for r in arows]

    mrows = db.execute(
        select(models.BodyMetric.date, models.BodyMetric.weight_kg,
               models.BodyMetric.resting_hr, models.BodyMetric.hrv_rmssd)
        .where(models.BodyMetric.athlete_id == athlete_id,
               models.BodyMetric.date >= since_d)
    ).all()
    metrics = [{"date": r[0], "weight_kg": r[1], "resting_hr": r[2], "hrv_rmssd": r[3]}
               for r in mrows]

    return {"activities": acts, "metrics": metrics}


# ---------------------------------------------------------------- 单字段解析


def _age_of(athlete: models.Athlete, today: date | None = None) -> float | None:
    if not athlete.birth_year:
        return None
    return float((today or date.today()).year - athlete.birth_year)


def _user_value(athlete: models.Athlete, key: str) -> Any:
    return getattr(athlete, key, None)


def _locked(athlete: models.Athlete, key: str) -> bool:
    """用户是否把该字段钉死为手动值（前端「自动推算」开关关闭）。"""
    meta = athlete.profile_meta or {}
    entry = meta.get(key) or {}
    return bool(entry.get("locked"))


def _prior_source(athlete: models.Athlete, key: str) -> str | None:
    """该字段上一次解析的来源。用于判断当前值是不是用户自己填的。"""
    return ((athlete.profile_meta or {}).get(key) or {}).get("source")


def _can_derived_override(athlete, key, user_val) -> bool:
    """推导/估计值是否可以覆盖当前值。

    当前值来自用户手填（source == user）时不覆盖——生理公式的近似不该盖掉
    用户的真实输入；只有当值本身就是系统推出来的、或用户从未填过时才跟随数据。
    """
    if _locked(athlete, key):
        return False
    if user_val is None:
        return True
    return _prior_source(athlete, key) != USER


def _resolve_max_hr(athlete, ev, age) -> Resolved:
    key = "max_hr"
    user_val = _user_value(athlete, key)
    locked = _locked(athlete, key)
    # 上界守卫：设备峰值偶有伪影，超过年龄预测值 25% 视为脏数据
    ceiling = tanaka_max_hr(age) * 1.25 if age else None
    obs = [a["max_hr"] for a in ev["activities"] if a.get("max_hr")]
    if ceiling:
        obs = [v for v in obs if v <= ceiling]

    if len(obs) >= 5 and not locked:
        peak = max(obs)
        # 实测峰值是硬事实：高于档案值就自动上调；低于档案值则保留用户值并说明
        if user_val is None or peak > user_val:
            return Resolved(key, int(peak), MEASURED,
                            f"近 400 天 {len(obs)} 次活动的心率峰值最高 {peak}bpm", 0.9)
        return Resolved(key, user_val, USER,
                        f"你填写 {user_val}bpm；实测峰值 {peak}bpm 未超过它，保留你的值", 0.85)

    if age:
        hard = [a for a in ev["activities"]
                if a.get("avg_hr") and a.get("duration_sec", 0) >= 1200
                and a.get("sport") in ("run", "ride")]
        tanaka = round(tanaka_max_hr(age))
        derived, basis = None, ""
        if hard:
            peak_avg = max(a["avg_hr"] for a in hard)
            derived = max_hr_from_effort(peak_avg)
            basis = f"由最长高强度课的平均心率 {peak_avg}bpm 推算（×1.06）"
        if derived is None or derived < tanaka * 0.9:
            derived, basis = tanaka, f"Tanaka 公式 208 − 0.7 × {age:.0f} 岁"
        if _can_derived_override(athlete, key, user_val):
            return Resolved(key, int(round(derived)), DERIVED, basis, 0.6 if hard else 0.5)

    if user_val is not None:
        return Resolved(key, user_val, USER, "你在档案里填写的最大心率", 0.7)
    return Resolved(key, None, UNKNOWN, "缺少年龄与心率记录，无法推算最大心率", 0.0)


def _resolve_resting_hr(athlete, ev, age, sex, training_age) -> Resolved:
    key = "resting_hr"
    user_val = _user_value(athlete, key)
    recent = [m["resting_hr"] for m in ev["metrics"]
              if m.get("resting_hr") and m.get("date")
              and m["date"] >= date.today() - timedelta(days=28)]
    med = _median(recent)
    if med is not None and len(recent) >= 14 and not _locked(athlete, key):
        return Resolved(key, int(round(med)), MEASURED,
                        f"近 28 天 {len(recent)} 次体测静息心率中位数 {round(med)}bpm", 0.9)
    if med is not None and len(recent) >= 3 and user_val is None:
        return Resolved(key, int(round(med)), MEASURED,
                        f"近 28 天 {len(recent)} 次体测静息心率中位数 {round(med)}bpm", 0.6)
    if user_val is not None:
        return Resolved(key, user_val, USER, "你在档案里填写的静息心率", 0.7)
    if age is not None:
        val, conf = resting_hr_prior(age, sex, training_age)
        return Resolved(key, int(val), ESTIMATED,
                        f"按{_sex_label(sex)}性、{age:.0f} 岁、训练 {training_age:.1f} 年的静息心率回归先验",
                        conf)
    return Resolved(key, None, UNKNOWN, "缺少静息心率记录与年龄，无法推算", 0.0)


def _resolve_hrv(athlete, ev, age, sex, resting_hr) -> Resolved:
    key = "hrv_baseline"
    user_val = _user_value(athlete, key)
    recent = [m["hrv_rmssd"] for m in ev["metrics"]
              if m.get("hrv_rmssd") and m.get("date")
              and m["date"] >= date.today() - timedelta(days=28)]
    med = _median(recent)
    if med is not None and len(recent) >= 14 and not _locked(athlete, key):
        return Resolved(key, int(round(med)), MEASURED,
                        f"近 28 天 {len(recent)} 条 HRV(RMSSD) 中位数 {round(med)}ms", 0.9)
    if user_val is not None:
        return Resolved(key, user_val, USER, "你在档案里填写的 HRV 基线", 0.7)
    if resting_hr:
        val, conf = hrv_prior(resting_hr, age, sex)
        return Resolved(key, int(val), ESTIMATED,
                        f"按静息心率 {resting_hr}bpm 推 HRV 先验（个体内 RHR 越低 HRV 越高）", conf)
    return Resolved(key, None, UNKNOWN, "缺少 HRV 记录与静息心率，无法推算", 0.0)


def _resolve_weight(athlete, ev) -> Resolved:
    key = "weight_kg"
    user_val = _user_value(athlete, key)
    recent = [m for m in ev["metrics"] if m.get("weight_kg")]
    if recent and not _locked(athlete, key):
        latest = max(recent, key=lambda m: m["date"] or date.min)
        n = len(recent)
        med = _median([m["weight_kg"] for m in recent[-30:]])
        val = round(float(latest["weight_kg"]), 1)
        basis = f"最近一次体测（{latest['date']}）{val}kg"
        if n >= 5 and med is not None:
            basis += f"；近 {min(30, n)} 条中位数 {round(med, 1)}kg"
        return Resolved(key, val, MEASURED, basis, 0.95)
    if user_val is not None:
        return Resolved(key, user_val, USER, "你在档案里填写的体重（尚无体测记录）", 0.6)
    return Resolved(key, None, UNKNOWN, "尚无体重记录，请在体测或档案里补一次", 0.0)


def _resolve_training_age(athlete, ev, today) -> Resolved:
    """系统训练年限：优先取用户填写值与「训练史代理」中的较大者。

    代理 = 活动记录跨度 × 有训练周占比。取较大者是刻意的保守设计：
    训练年限越高，天赋评估对该成绩的期望越高（分数越低），
    宁可低估天赋，也不虚报可开发空间。
    """
    key = "training_age_years"
    user_val = _user_value(athlete, key)
    dates = sorted({a["date"] for a in ev["activities"] if a.get("date")})
    derived = None
    basis = ""
    if dates and (today - dates[0]).days >= 56:      # 至少 8 周历史才算得上「训练史」
        span_years = (today - dates[0]).days / 365.25
        weeks_total = max(1, int((today - dates[0]).days / 7) + 1)
        weeks_active = len({d.isocalendar()[:2] for d in dates})
        ratio = min(1.0, weeks_active / weeks_total)
        derived = round(span_years * ratio, 1)
        basis = (f"活动记录跨度 {span_years:.1f} 年，其中 {weeks_active}/{weeks_total} 周有训练"
                 f"→ 有效训练年限 {derived} 年")
    if derived is not None and not _locked(athlete, key):
        if user_val is None or derived > user_val:
            return Resolved(key, derived, DERIVED, basis, 0.6)
    if user_val is not None:
        return Resolved(key, user_val, USER, "你在档案里填写的系统训练年限", 0.7)
    if derived is not None:
        return Resolved(key, derived, DERIVED, basis, 0.5)
    # 无记录 = 确实没有系统训练史，按 0 年计（这是事实，不是编造的兜底值）
    return Resolved(key, 0.0, ESTIMATED,
                    "活动记录不足 8 周，判定为尚无系统训练史（0 年）", 0.4)


def _resolve_weekly_km(ev, today) -> Resolved:
    """近 4 周周均跑量（实测）。这是绝大多数派生量的输入。"""
    weeks: dict[tuple, float] = {}
    for a in ev["activities"]:
        if a.get("sport") != "run" or not a.get("date"):
            continue
        if a["date"] < today - timedelta(days=28):
            continue
        weeks.setdefault(a["date"].isocalendar()[:2], 0.0)
        weeks[a["date"].isocalendar()[:2]] += a["distance_m"] / 1000
    if not weeks:
        return Resolved("weekly_km_now", 0.0, UNKNOWN, "近 4 周没有跑步记录", 0.0)
    vals = list(weeks.values())
    return Resolved("weekly_km_now", round(sum(vals) / len(vals), 1), MEASURED,
                    f"近 4 周 {len(vals)} 个有训练周的平均跑量", 0.9)


# ---------------------------------------------------------------- 汇总


def resolve(db: Session, athlete: models.Athlete, *,
            evidence: dict | None = None,
            today: date | None = None) -> dict[str, Resolved]:
    """解析全部画像字段，返回 {field: Resolved}。不改库。"""
    today = today or date.today()
    ev = evidence if evidence is not None else load_evidence(db, athlete.id, today=today)
    age = _age_of(athlete, today)
    sex = athlete.sex
    wk = _resolve_weekly_km(ev, today)

    # 训练年限要先算：静息心率先验依赖它
    ta = _resolve_training_age(athlete, ev, today)
    training_age = ta.value if ta.value is not None else 0.0

    out: dict[str, Resolved] = {}
    out["sex"] = (Resolved("sex", sex, USER, "你在档案里填写的性别", 1.0) if sex
                  else Resolved("sex", None, UNKNOWN, "尚未填写性别", 0.0))
    out["age"] = (Resolved("age", age, USER, f"按出生年 {athlete.birth_year} 计算", 1.0)
                  if age is not None else
                  Resolved("age", None, UNKNOWN, "尚未填写出生年份", 0.0))
    out["height_cm"] = (Resolved("height_cm", athlete.height_cm, USER, "你在档案里填写的身高", 1.0)
                        if athlete.height_cm else
                        Resolved("height_cm", None, UNKNOWN, "尚未填写身高，BMI 相关评估会跳过", 0.0))
    out["training_age_years"] = ta
    out["weight_kg"] = _resolve_weight(athlete, ev)
    out["max_hr"] = _resolve_max_hr(athlete, ev, age)
    out["resting_hr"] = _resolve_resting_hr(athlete, ev, age, sex, training_age)
    out["hrv_baseline"] = _resolve_hrv(
        athlete, ev, age, sex, out["resting_hr"].value)
    out["weekly_km_now"] = wk
    return out


def effective(profile: dict[str, Resolved], key: str, default=None):
    """便捷取值：解析结果为空时返回 default。"""
    r = profile.get(key)
    return r.value if (r and r.value is not None) else default


def hr_context(profile: dict[str, Resolved]) -> tuple[int | None, int | None]:
    """统一心率口径 (max_hr, resting_hr)，供负荷/心率区间消费方使用。

    任一缺失返回 None——调用方必须显式降级，不再偷偷退回 190/55。
    """
    return effective(profile, "max_hr"), effective(profile, "resting_hr")


# ---------------------------------------------------------------- 回写与刷新


def _signature(db: Session, athlete_id: int) -> str:
    """数据指纹：活动/体测的条数与最大 id。变了才需要重新解析。"""
    a = db.execute(select(func.count(models.Activity.id), func.max(models.Activity.id))
                   .where(models.Activity.athlete_id == athlete_id)).one()
    m = db.execute(select(func.count(models.BodyMetric.id), func.max(models.BodyMetric.id))
                   .where(models.BodyMetric.athlete_id == athlete_id)).one()
    return f"{a[0]}:{a[1]}:{m[0]}:{m[1]}"


def _meta_of(athlete: models.Athlete) -> dict:
    return dict(athlete.profile_meta or {})


def refresh(db: Session, athlete: models.Athlete, *, force: bool = False,
            commit: bool = False, today: date | None = None,
            evidence: dict | None = None) -> dict[str, Resolved]:
    """按最新数据刷新档案字段，并把来源写进 ``profile_meta``。

    回写规则：
      · 只回写 ``AUTO_FIELDS``；
      · 已被用户「钉死」（locked）的字段不动；
      · 值没变也仍然刷新 ``meta``（依据/时间会变，前端要展示最新依据）。

    节流：数据指纹与上次一致且未超过 ``_REFRESH_TTL_SEC`` 时直接返回上次解析结果的
    快照（``meta._refresh.snapshot``），不再查证据表——否则 TTL 命中时仍要全量
    ``resolve``，两次 400 天范围查询 + 字段重算，节流形同虚设。快照缺失（老 meta）
    时退回全量 resolve。

    ``evidence`` 供**新建档案**场景使用：此时 athlete 还没入库、拿不到 id，
    直接传入空证据（``{"activities": [], "metrics": []}``）按本人属性推算，
    这样就能在第一次 flush 之前把 NOT NULL 的字段填好，避免以 NULL 落库。
    """
    today = today or date.today()
    meta = _meta_of(athlete)
    is_new = athlete.id is None
    sig = "new" if is_new else _signature(db, athlete.id)
    cached = meta.get("_refresh") or {}
    if (not force and not is_new and cached.get("sig") == sig
            and cached.get("at") and _within_ttl(cached["at"])):
        snap = cached.get("snapshot") or {}
        if snap:  # TTL 命中：直接用上次解析结果，零查询
            return {key: Resolved(key, d.get("value"), d.get("source") or UNKNOWN,
                                  d.get("basis") or "", float(d.get("confidence") or 0.0))
                    for key, d in snap.items()}
        return resolve(db, athlete, evidence=evidence, today=today)

    profile = resolve(db, athlete, evidence=evidence, today=today)
    now = datetime.now().isoformat(timespec="seconds")
    changed: list[str] = []

    for key in AUTO_FIELDS:
        r = profile.get(key)
        if r is None:
            continue
        locked = _locked(athlete, key)
        current = getattr(athlete, key, None)
        # 写回条件：
        #   · 实测/推导值 → 覆盖（客观数据比手填更新、更准）
        #   · 估计值 → 只在列为空时填入（有值就说明是用户填的，估计不该盖掉它）
        #   · locked → 一律不动
        should_write = (
            not locked and r.value is not None and current != r.value
            and (r.source in (MEASURED, DERIVED)
                 or (r.source == ESTIMATED and current is None))
        )
        if should_write:
            setattr(athlete, key, r.value)
            changed.append(key)
        entry = r.as_dict()
        entry.pop("known", None)
        entry.pop("key", None)
        entry["locked"] = locked
        entry["at"] = now
        meta[key] = entry

    meta["_refresh"] = {"sig": sig, "at": now, "changed": changed,
                        "snapshot": {k: r.as_dict() for k, r in profile.items()}}
    athlete.profile_meta = meta        # 整体重新赋值，确保 SQLAlchemy 识别 JSON 变更

    if commit:
        db.commit()
    return profile


def _within_ttl(iso_ts: str) -> bool:
    try:
        return (datetime.now() - datetime.fromisoformat(iso_ts)).total_seconds() < _REFRESH_TTL_SEC
    except (TypeError, ValueError):
        return False


def set_locked(db: Session, athlete: models.Athlete, field: str, locked: bool,
               commit: bool = True) -> dict:
    """开关某字段的「自动推算」。关闭=钉死当前值；打开=下次刷新即跟随数据。"""
    if field not in AUTO_FIELDS:
        raise ValueError(f"{field} 不是可自动推算的字段")
    meta = _meta_of(athlete)
    entry = dict(meta.get(field) or {})
    entry["locked"] = bool(locked)
    entry.setdefault("source", USER)
    entry.setdefault("basis", "你在档案里手动填写的值")
    entry["at"] = datetime.now().isoformat(timespec="seconds")
    meta[field] = entry
    athlete.profile_meta = meta
    if commit:
        db.commit()
    return meta[field]


def mark_user_fields(athlete: models.Athlete, fields: list[str]) -> None:
    """把用户本次显式提交的字段标记为 ``user`` 来源（不自动锁定）。

    这是「谁的输入更可信」的判定依据：用户填过的值，估计值不再覆盖它。
    """
    if not fields:
        return
    meta = _meta_of(athlete)
    now = datetime.now().isoformat(timespec="seconds")
    for f in fields:
        entry = dict(meta.get(f) or {})
        entry.update({"source": USER, "source_label": SOURCE_LABELS[USER],
                      "basis": "你在档案里手动填写的值", "confidence": 0.7,
                      "at": now})
        entry.setdefault("locked", False)
        meta[f] = entry
    athlete.profile_meta = meta


def clear_user_marker(athlete: models.Athlete, field: str) -> None:
    """清掉某字段的「用户手填」标记，交回自动解析。

    用于前端把某个输入框清空 = 「让系统算」的场景。清掉标记后，
    ``_can_derived_override`` 就会放行推导值覆盖它，下一次 refresh 即生效。
    """
    meta = _meta_of(athlete)
    meta.pop(field, None)
    meta["_refresh"] = {}        # 失效缓存，迫使下次 refresh 重新解析
    athlete.profile_meta = meta


def payload(profile: dict[str, Resolved], *, athlete: models.Athlete | None = None,
            changed: list[str] | None = None) -> dict:
    """给 API 的可序列化结构：字段值 + 来源溯源 + 自动/锁定状态。

    ``locked`` 只在 ``refresh`` 写回的 ``profile_meta`` 里，所以传 athlete 时一并并进来，
    前端才能渲染「自动推算」开关的当前状态。
    """
    meta = (athlete.profile_meta or {}) if athlete is not None else {}
    fields = {}
    for k, r in profile.items():
        d = r.as_dict()
        d["locked"] = bool((meta.get(k) or {}).get("locked"))
        fields[k] = d
    return {
        "fields": fields,
        "auto_fields": list(AUTO_FIELDS),
        "pending": [k for k, r in profile.items() if not r.known and k != "weekly_km_now"],
        "changed": changed or [],
    }
