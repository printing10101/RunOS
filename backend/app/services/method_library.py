"""训练方法知识库服务层：检索、画像匹配与推荐。

设计原则（与 ai_tools 一致）：
- 数据全部来自 method_library 表（backend/data/training_methods_*.json 导入），
  每个方法都能查到出处与证据等级，AI 不得凭记忆编造训练法；
- 推荐是「规则命中打分」，每一条得分都能解释（返回命中理由），不是黑箱排序；
- 课表模板里的配速用「区 key」占位（E/M/T/I/R），resolve_template_steps 按当前
  VDOT 解析成真实数值，能力变化后模板不失效。
"""
from __future__ import annotations

import datetime as _dt
import re

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .. import models
from ..data import active_plan, archive_plan
from . import planner, vdot
from .vocab import QUALITY_SESSION_TYPES

ZONE_KEY_MAP = {"E": "easy", "M": "marathon", "T": "threshold", "I": "interval", "R": "repetition"}
WEEKDAY_CN = ["一", "二", "三", "四", "五", "六", "日"]

EVIDENCE_SCORE = {"A": 8.0, "B": 5.0, "C": 2.0, "D": 0.0}
LEVEL_ORDER = ["beginner", "intermediate", "advanced", "elite"]

# 水平判定（按近 28 天周均跑量，km）：与评估口径保持宽松一致
LEVEL_VOLUME_BOUNDS = [(25, "beginner"), (55, "intermediate"), (110, "advanced")]  # 以上逐级，否则 elite

# 体验周每周质量课上限（按跑者水平分级）。
# 依据：极化训练要求高强度课占比 ≤20%，一周 5 练即 ≤1 次；初跑者肌腱/骨适应慢于心肺，
# 每周 2 次质量课会把强度课占比推到 40%，是体验周最常见的过载来源。
MAX_QUALITY_BY_LEVEL = {"beginner": 1, "intermediate": 2, "advanced": 2, "elite": 2}


def level_from_volume(weekly_km: float) -> str:
    for bound, level in LEVEL_VOLUME_BOUNDS:
        if weekly_km < bound:
            return level
    return "elite"


# ---------------------------------------------------------------- 查询

def list_methods(db: Session, *, origin: str | None = None, category: str | None = None,
                 level: str | None = None, race: str | None = None, phase: str | None = None,
                 min_evidence: str | None = None, q: str | None = None) -> list[models.TrainingMethod]:
    """按条件过滤训练方法（全部为等值/包含过滤，无动态 SQL 拼接）。"""
    stmt = select(models.TrainingMethod).options(selectinload(models.TrainingMethod.fit_rules))
    rows = db.scalars(stmt).all()
    q_low = (q or "").strip().lower()
    ev_rank = {"D": 0, "C": 1, "B": 2, "A": 3}
    out = []
    for m in rows:
        if origin and m.origin != origin:
            continue
        if category and m.category != category:
            continue
        if level and m.level != level:
            continue
        if race and race not in (m.target_races or []):
            continue
        if phase and phase not in (m.phases or []):
            continue
        if min_evidence and ev_rank.get(m.evidence_level, 0) < ev_rank.get(min_evidence, 0):
            continue
        if q_low:
            haystack = " ".join([m.name_zh, m.name_ja, m.name_en, m.code, m.summary,
                                 " ".join(m.tags or [])]).lower()
            if q_low not in haystack:
                continue
        out.append(m)
    # 默认排序：亚洲适配度优先、证据等级其次（前端可再按 origin 过滤）
    out.sort(key=lambda m: (-m.asian_fit, -ev_rank.get(m.evidence_level, 0), m.id))
    return out


def get_method(db: Session, code: str) -> models.TrainingMethod | None:
    return db.scalar(select(models.TrainingMethod).where(models.TrainingMethod.code == code))


def method_summary(m: models.TrainingMethod) -> dict:
    return {
        "id": m.id, "code": m.code, "name_zh": m.name_zh, "name_ja": m.name_ja,
        "name_en": m.name_en, "origin": m.origin, "category": m.category,
        "summary": m.summary, "level": m.level, "evidence_level": m.evidence_level,
        "asian_fit": m.asian_fit,
        "intensity_distribution": m.intensity_distribution,
        "weekly_km_range": m.weekly_km_range,
        "quality_days_per_week": m.quality_days_per_week,
        "target_races": m.target_races, "phases": m.phases, "tags": m.tags,
        "workout_count": len(m.workouts) if m.workouts else 0,
    }


# ---------------------------------------------------------------- 跑者画像（全部来自真实数据，不用默认值）

def build_athlete_profile(db: Session, athlete: models.Athlete) -> dict:
    from datetime import date, timedelta

    from ..data import activities_dicts, weekly_km

    acts = activities_dicts(db, athlete.id, days=28)
    week_km = round(weekly_km(acts), 1) if acts else 0.0
    slots = db.scalars(select(models.WeeklySlot).where(
        models.WeeklySlot.athlete_id == athlete.id,
        models.WeeklySlot.kind == "available")).all()
    goal = db.scalar(select(models.Goal).where(
        models.Goal.athlete_id == athlete.id, models.Goal.status == "active")
        .order_by(models.Goal.id))
    plan = active_plan(db)
    phase = None
    if plan:
        today = date.today()
        cur = next((w for w in plan.weeks if w.start_date <= today < w.start_date + timedelta(days=7)), None)
        phase = cur.phase if cur else None
    # 伤病史信号：近 14 天打卡的酸痛与疼痛部位（主观通道，无数据则为 False）
    since = date.today() - timedelta(days=14)
    checks = db.scalars(select(models.DailyCheckin).where(
        models.DailyCheckin.athlete_id == athlete.id,
        models.DailyCheckin.date >= since)).all()
    soreness = [c.muscle_soreness for c in checks if c.muscle_soreness]
    pains = [c.pain_area for c in checks if c.pain_area]
    injury_sensitive = bool(pains) or (bool(soreness) and sum(soreness) / len(soreness) >= 2.5)
    return {
        "athlete_id": athlete.id, "name": athlete.name,
        "weekly_km": week_km, "level": level_from_volume(week_km),
        "days_per_week": len(slots),
        "race_type": goal.race_type if goal else None,
        "phase": phase,
        "injury_sensitive": injury_sensitive,
        "has_data": bool(acts),
    }


# ---------------------------------------------------------------- 推荐（规则命中打分，结果可解释）

def _rule_hit(cond: dict, p: dict) -> bool:
    """适配规则的条件匹配。空条件视为不命中（防止写错条件导致无条件加分）。"""
    if not cond:
        return False
    wk, days = p.get("weekly_km") or 0, p.get("days_per_week") or 0
    if "weekly_km_min" in cond and wk < cond["weekly_km_min"]:
        return False
    if "weekly_km_max" in cond and wk > cond["weekly_km_max"]:
        return False
    if "days_min" in cond and days < cond["days_min"]:
        return False
    if "days_max" in cond and days > cond["days_max"]:
        return False
    if "level_in" in cond and p.get("level") not in cond["level_in"]:
        return False
    if "race_in" in cond and p.get("race_type") not in cond["race_in"]:
        return False
    if "phase_in" in cond and p.get("phase") not in cond["phase_in"]:
        return False
    if cond.get("injury_sensitive") and not p.get("injury_sensitive"):
        return False
    return True


def recommend_methods(db: Session, profile: dict, top_n: int = 5) -> list[dict]:
    rows = db.scalars(select(models.TrainingMethod)
                      .options(selectinload(models.TrainingMethod.fit_rules))).all()
    ranked = []
    for m in rows:
        score, reasons = 0.0, []
        for rule in m.fit_rules or []:
            if _rule_hit(rule.condition or {}, profile):
                score += float(rule.weight or 0)
                if rule.reason:
                    reasons.append(rule.reason)
        if not reasons:
            continue  # 没有任何命中理由的方法不进入推荐（保证可解释性）
        # 亚洲适配度：以 50 为基线做有界加成（-20 ~ +16）
        asian_bonus = max(-20.0, min(16.0, (m.asian_fit - 50) * 0.4))
        score += asian_bonus
        if m.asian_fit >= 80:
            reasons.append(f"亚洲/东亚适配度高（{m.asian_fit}/100：{m.asian_fit_basis.get('note', '生活方式与训练文化适配')}")
        score += EVIDENCE_SCORE.get(m.evidence_level, 0.0)
        if m.evidence_level in ("A", "B"):
            reasons.append(f"证据等级 {m.evidence_level}（有研究或精英群体数据支撑）")
        # 水平错配惩罚：elite 体系不推给 beginner，反之亦然
        gap = abs(LEVEL_ORDER.index(m.level) - LEVEL_ORDER.index(profile.get("level") or "intermediate"))
        if gap >= 2:
            score -= 10
            reasons.append(f"注意：该方法面向 {m.level} 水平，与你当前水平差距较大")
        # 跑量区间匹配
        km = profile.get("weekly_km") or 0
        wk_rng = m.weekly_km_range or {}
        if wk_rng.get("min") and km and km < wk_rng["min"]:
            score -= 8
            reasons.append(f"注意：该方法建议周跑量 ≥{wk_rng['min']}km，你当前约 {km}km")
        ranked.append({"method": m, "score": round(score, 1), "reasons": reasons})
    ranked.sort(key=lambda x: -x["score"])
    return ranked[:max(1, min(10, top_n))]


def recommend_payload(db: Session, athlete: models.Athlete, top_n: int = 5) -> dict:
    profile = build_athlete_profile(db, athlete)
    if not profile["has_data"]:
        return {"ok": False, "profile": profile,
                "reasons": ["暂无足够训练数据（近 28 天无跑步记录），无法按画像推荐；可直接浏览方法库"]}
    ranked = recommend_methods(db, profile, top_n)
    return {
        "ok": True, "profile": profile,
        "items": [{"score": r["score"], "reasons": r["reasons"],
                   "method": method_full(r["method"])} for r in ranked],
        "note": "得分为规则命中的加和（含亚洲适配与证据加成），每条推荐都附命中理由；asian_fit 描述的是生活方式/训练文化/气候适配，不是人种生理差异。",
    }


# ---------------------------------------------------------------- 课表模板与配速解析

def method_full(m: models.TrainingMethod) -> dict:
    return {**method_summary(m),
            "principles": m.principles, "sessions_per_week": m.sessions_per_week,
            "pros": m.pros, "cons": m.cons, "cautions": m.cautions,
            "key_figures": m.key_figures, "evidence_note": m.evidence_note,
            "asian_fit_basis": m.asian_fit_basis,
            "workouts": [workout_dict(w) for w in (m.workouts or [])],
            "evidences": [{"title": e.title, "url": e.url, "source_type": e.source_type,
                           "year": e.year, "level": e.level, "note": e.note}
                          for e in (m.evidences or [])]}


def workout_dict(w: models.WorkoutTemplate, *, with_structure: bool = True) -> dict:
    return {"id": w.id, "code": w.code, "method_code": w.method.code if w.method else None,
            "name_zh": w.name_zh, "name_ja": w.name_ja, "session_type": w.session_type,
            "purpose": w.purpose, "intensity_anchor": w.intensity_anchor,
            "distance_km_range": w.distance_km_range, "duration_min_range": w.duration_min_range,
            "weekly_km_min": w.weekly_km_min, "phases": w.phases,
            "frequency_hint": w.frequency_hint, "progression": w.progression,
            "cautions": w.cautions, "source_url": w.source_url,
            **({"structure": w.structure} if with_structure else {})}


def _pace_label(sec_per_km: float) -> str:
    m, s = divmod(int(round(sec_per_km)), 60)
    return f"{m}:{s:02d}/km"


def _zone_targets(vdot_value: float, zone_key: str) -> tuple[int, int] | None:
    zones = {z["key"]: z for z in vdot.daniels_paces(vdot_value)}
    z = zones.get(zone_key)
    if not z:
        return None
    to_sec = lambda v: round(1000 / v)          # noqa: E731  m/s → sec/km
    lo, hi = sorted((to_sec(z["velocity_from"]), to_sec(z["velocity_to"])))
    return lo, hi


def resolve_template_steps(structure: list[dict], vdot_value: float) -> list[dict]:
    """把模板里的 pace_zone / pace_pct_of_mp / maf 占位解析为真实配速目标。

    输出格式与 planner 的结构化步骤完全一致（可下发手表）。
    未识别的占位一律降级为「无目标 + 说明」，不编造配速。
    """
    if not vdot_value or vdot_value <= 0:
        raise ValueError("需要有效的 VDOT 才能解析模板配速")
    steps: list[dict] = []
    for s in structure or []:
        s = dict(s)
        tgt = dict(s.get("target") or {})
        ttype = tgt.get("type")
        if ttype == "pace_zone":
            zk = ZONE_KEY_MAP.get(str(tgt.get("zone", "")).upper())
            rng = _zone_targets(vdot_value, zk) if zk else None
            if rng:
                lo, hi = rng
                off = tgt.get("offset_pct")
                if off:
                    if abs(100.0 + float(off)) < 1:
                        raise ValueError(f"offset_pct 非法（{off}）：会使配速倍率趋近除零")
                    # offset_pct 的语义是「配速为标准区的 (100+off)%」：
                    # off=-3 → 97% MP（更慢，sec/km 变大）；off=+3 → 103%（更快）
                    factor = 100.0 / (100.0 + float(off))
                    lo, hi = round(lo * factor), round(hi * factor)
                    lo, hi = min(lo, hi), max(lo, hi)
                s["target"] = {"type": "pace", "from": lo, "to": hi,
                               "from_label": _pace_label(lo), "to_label": _pace_label(hi)}
            else:
                s["target"] = {"type": "none", "label": tgt.get("label") or "按体系配速（缺 VDOT 区定义）"}
        elif ttype == "pace_pct_of_mp":
            rng = _zone_targets(vdot_value, "marathon")
            if rng:
                mp_mid = (rng[0] + rng[1]) / 2
                pct = float(tgt.get("pct") or 100)
                center = mp_mid * 100.0 / pct        # pct>100 更快，pct<100 更慢
                lo, hi = round(center * 0.98), round(center * 1.02)
                s["target"] = {"type": "pace", "from": lo, "to": hi,
                               "from_label": _pace_label(lo), "to_label": _pace_label(hi)}
            else:
                s["target"] = {"type": "none", "label": tgt.get("label") or "按目标马配百分比"}
        elif ttype == "maf":
            s["target"] = {"type": "hr", "from": 0.65, "to": 0.75,
                           "label": tgt.get("label") or "MAF 心率上限（180-年龄）"}
        # 其余类型（hr/rpe/none/cadence/lactate/progression/alternating）原样保留
        steps.append(s)
    return steps


# ---------------------------------------------------------------- 训练原理层（AI 定制计划的「为什么」）

def list_principles(db: Session, *, category: str | None = None,
                    min_evidence: str | None = None, q: str | None = None) -> list[models.TrainingPrinciple]:
    rows = db.scalars(select(models.TrainingPrinciple)).all()
    q_low = (q or "").strip().lower()
    ev_rank = {"D": 0, "C": 1, "B": 2, "A": 3}
    out = []
    for p in rows:
        if category and p.category != category:
            continue
        if min_evidence and ev_rank.get(p.evidence_level, 0) < ev_rank.get(min_evidence, 0):
            continue
        if q_low:
            hay = " ".join([p.name_zh, p.name_en, p.summary, p.mechanism,
                            " ".join(p.practical_rules or [])]).lower()
            if q_low not in hay:
                continue
        out.append(p)
    out.sort(key=lambda p: (-ev_rank.get(p.evidence_level, 0), p.id))
    return out


def principle_dict(db: Session, p: models.TrainingPrinciple, *, with_sources: bool = True) -> dict:
    d = {"id": p.id, "code": p.code, "name_zh": p.name_zh, "name_en": p.name_en,
         "category": p.category, "summary": p.summary, "mechanism": p.mechanism,
         "practical_rules": p.practical_rules, "platform_usage": p.platform_usage,
         "evidence_level": p.evidence_level, "evidence_note": p.evidence_note}
    if with_sources:
        d["sources"] = _sources_of(db, "principle", p.code)
    return d


def _sources_of(db: Session, ref_type: str, ref_code: str) -> list[dict]:
    rows = db.scalars(select(models.KnowledgeSource).where(
        models.KnowledgeSource.ref_type == ref_type,
        models.KnowledgeSource.ref_code == ref_code)).all()
    return [{"title": s.title, "url": s.url, "source_type": s.source_type,
             "year": s.year, "level": s.level, "note": s.note} for s in rows]


# ---------------------------------------------------------------- 参考计划层（骨架参照 + 画像匹配）

def list_plan_templates(db: Session, *, race: str | None = None, level: str | None = None,
                        q: str | None = None) -> list[models.PlanTemplate]:
    rows = db.scalars(select(models.PlanTemplate)).all()
    q_low = (q or "").strip().lower()
    out = []
    for pl in rows:
        if race and race != "any" and pl.target_race not in (race, "any"):
            continue
        if level and pl.level != level:
            continue
        if q_low:
            hay = " ".join([pl.name_zh, pl.name_en, pl.author, pl.code,
                            pl.fit_notes or "", pl.pros or ""]).lower()
            if q_low not in hay:
                continue
        out.append(pl)
    out.sort(key=lambda pl: (LEVEL_ORDER.index(pl.level) if pl.level in LEVEL_ORDER else 9, pl.id))
    return out


def plan_dict(db: Session, pl: models.PlanTemplate, *, with_sources: bool = True) -> dict:
    d = {"id": pl.id, "code": pl.code, "name_zh": pl.name_zh, "name_en": pl.name_en,
         "author": pl.author, "target_race": pl.target_race, "level": pl.level,
         "weeks": pl.weeks, "weekly_km_range": pl.weekly_km_range,
         "days_per_week": pl.days_per_week, "phases": pl.phases,
         "week_template": pl.week_template, "long_run_progression": pl.long_run_progression,
         "key_workouts": pl.key_workouts, "taper_plan": pl.taper_plan,
         "fit_notes": pl.fit_notes, "pros": pl.pros, "cons": pl.cons,
         "cautions": pl.cautions, "evidence_level": pl.evidence_level,
         "evidence_note": pl.evidence_note}
    if with_sources:
        d["sources"] = _sources_of(db, "plan", pl.code)
    return d


def recommend_plans(db: Session, profile: dict, top_n: int = 4) -> list[dict]:
    """按画像匹配参考计划：命中 fit_condition 计入并给出理由，无命中不入选。"""
    rows = db.scalars(select(models.PlanTemplate)).all()
    ranked = []
    for pl in rows:
        score, reasons = 0.0, []
        cond = pl.fit_condition or {}
        hit = _rule_hit(cond, profile) if cond else False
        if hit:
            score += 15.0
            if pl.fit_notes:
                reasons.append(pl.fit_notes)
            # 画像与计划口径的贴合度
            km = profile.get("weekly_km") or 0
            rng = pl.weekly_km_range or {}
            if rng.get("min") and km and km >= rng["min"] * 0.6:
                score += 8
                reasons.append(f"你的周跑量（{km}km）已接近该计划起点（峰值 {rng.get('min')}-{rng.get('max')}km）")
            days = profile.get("days_per_week") or 0
            d_rng = pl.days_per_week or {}
            if d_rng.get("min") and days and days >= d_rng["min"]:
                score += 8
                reasons.append(f"你的可练天数（每周 {days} 天）满足该计划要求的 {d_rng['min']} 天以上")
            if profile.get("race_type") and profile["race_type"] == pl.target_race:
                score += 10
                reasons.append(f"目标项目与该计划一致（{pl.target_race}）")
            if profile.get("injury_sensitive") and "16 英里" in (pl.name_en or ""):
                score += 5
                reasons.append("有伤病信号：该计划长距离封顶设计（Hanson 16 英里）更保守")
        else:
            continue
        ranked.append({"plan": pl, "score": round(score, 1), "reasons": reasons})
    ranked.sort(key=lambda x: -x["score"])
    return ranked[:max(1, min(8, top_n))]


def recommend_plans_payload(db: Session, athlete: models.Athlete, top_n: int = 4) -> dict:
    profile = build_athlete_profile(db, athlete)
    if not profile["has_data"]:
        return {"ok": False, "profile": profile,
                "reasons": ["暂无足够训练数据，无法按画像匹配计划；可直接浏览 plan_templates"]}
    ranked = recommend_plans(db, profile, top_n)
    return {
        "ok": True, "profile": profile,
        "items": [{"score": r["score"], "reasons": r["reasons"],
                   "plan": plan_dict(db, r["plan"])} for r in ranked],
        "traceability_note": "每个计划都附 sources（出处与证据等级）；定制时请引用原理层（practical_rules）说明每个决策的依据，不得凭空编造周跑量或配速。",
    }


# ---------------------------------------------------------------- 体验周：把方法体系按用户真实数据落成一周课表

# 判断/估算口径统一引用唯一来源：质量课集合见 vocab，时段配速见 planner
QUALITY_TYPES = QUALITY_SESSION_TYPES
MIN_PER_KM = planner.SLOT_CAP_MIN_PER_KM


def _estimate_minutes(steps: list[dict]) -> float:
    """按已解析步骤估算课程时长（分钟）：时间步直接累加，距离步用配速中点换算。"""
    total = 0.0
    for s in steps or []:
        if s.get("duration_type") == "time":
            total += float(s.get("duration_value") or 0)
        elif s.get("duration_type") == "distance" and s.get("step_type") == "active":
            tgt = s.get("target") or {}
            pace_mid = None
            if tgt.get("type") == "pace" and tgt.get("from"):
                pace_mid = (float(tgt["from"]) + float(tgt.get("to") or tgt["from"])) / 2
            total += (float(s.get("duration_value") or 0) / 1000.0) * (pace_mid or MIN_PER_KM * 60) / 60.0
        elif s.get("step_type") == "strength":
            total += float(s.get("duration_value") or 0)
    return total


def _steps_km(steps: list[dict]) -> float:
    return round(sum(float(s.get("duration_value") or 0) for s in steps or []
                     if s.get("step_type") == "active" and s.get("duration_type") == "distance") / 1000.0, 1)


def _halve_repeats(steps: list[dict]) -> list[dict] | None:
    """把课表里的重复段组数减半（时段不够时的降档手段）。

    找最长重复 cycle（周期 1-3，至少重复 4 次才有减半意义），保留头部/尾部，
    重建中间段并重排 x/y 组号。无重复结构时返回 None。
    """
    import re

    def tpl(s: dict) -> tuple:
        name = re.sub(r"\d+", "N", s.get("name", ""))
        tgt = s.get("target") or {}
        return (s.get("step_type"), name, s.get("duration_type"), s.get("duration_value"),
                tgt.get("type"), tgt.get("from"), tgt.get("to"))

    n = len(steps)
    best = None  # (reps, period, start)
    for p in (1, 2, 3):
        for i in range(0, n - p * 2 + 1):
            reps = 1
            while i + (reps + 1) * p <= n and [tpl(x) for x in steps[i + reps * p: i + (reps + 1) * p]] == [tpl(x) for x in steps[i: i + p]]:
                reps += 1
            if reps >= 4 and any(steps[k].get("step_type") == "active" for k in range(i, i + p)):
                if best is None or reps > best[0]:
                    best = (reps, p, i)
    if not best:
        return None
    reps, p, start = best
    keep = reps // 2
    out = steps[:start] + steps[start: start + keep * p] + steps[start + reps * p:]
    # 重排 x/y 组号
    idx = 0
    for s in out[start: start + keep * p]:
        m = re.search(r"(\d+)/(\d+)", s.get("name", ""))
        if m and s.get("step_type") == "active":
            idx += 1
            s["name"] = re.sub(r"\d+/\d+", f"{idx}/{keep}", s["name"])
    return out


def _relax_paces(steps: list[dict], pct: float) -> None:
    """湿热环境下把已解析的配速目标整体放慢 pct%（sec/km 变大 = 变慢）。"""
    f = 1.0 + pct / 100.0
    for s in steps or []:
        t = s.get("target") or {}
        if t.get("type") == "pace" and t.get("from"):
            t["from"] = round(t["from"] * f)
            t["to"] = round((t.get("to") or t["from"]) * f)
            t["from_label"], t["to_label"] = _pace_label(t["from"]), _pace_label(t["to"])


def season_hint(db: Session, athlete_id: int) -> dict:
    """季节/气温因子：优先用近 7 天活动的实测气温，缺数据时按月份估算（贵阳气候带）。

    返回 hot=True 时，体验周会整体放宽配速并附防暑指引，是 heat-acclimation
    原理的反向应用：没做热适应的人在湿热条件下按标准配速跑，实际强度会偏高。
    """
    from ..data import activities_dicts
    acts = activities_dicts(db, athlete_id, days=7)
    temps = [a["temp_c"] for a in acts if a.get("temp_c") is not None]
    if temps:
        avg, src = sum(temps) / len(temps), "近 7 天实测气温"
    else:
        month = _dt.date.today().month
        avg, src = ((30.0, "按月份估算（5-9 月湿热季，无实测气温数据）") if month in (5, 6, 7, 8, 9)
                    else (15.0, "按月份估算（无实测气温数据）"))
    hot = avg >= 27.0
    return {"avg_temp_c": round(avg, 1), "hot": hot, "source": src,
            "relax_pct": 4.0 if hot else 0.0,
            "advice": ("湿热环境：配速已整体放慢 4%，以心率与体感为准；"
                       "质量课安排在清晨或傍晚，跑前 2 小时补水、全程补盐"
                       if hot else "")}


def build_week_plan_from_method(db: Session, athlete: models.Athlete, method_code: str) -> dict:
    """把某个方法体系降档成「一周体验课表」（不落库）。

    降档规则（写死且可解释，返回时随 notes 一起给出）：
    - 质量课次数按跑者水平分级（初跑者 ≤1、其余 ≤2），模板之间不重复，
      放不进时段的模板先减组数、再换下一个；
    - 长距离 ≤ 周跑量 35% 且 ≤ 周跑量 45%（下限随跑量缩放，不再固定 6km），模板距离按此缩放；
    - 排课窗口锚定「下一个完整自然周（周一起）」，已过去的日期不排；
    - race 类型模板一律排除（体验周不安排比赛）；
    - 配速全部由当前 VDOT 解析，距离按周跑量与时段双重约束缩放。
    """
    from ..data import build_prediction
    from . import planner

    m = get_method(db, method_code)
    if not m:
        raise ValueError(f"方法 {method_code} 不存在")
    pred = build_prediction(db, athlete.id)
    if not pred.current_vdot:
        raise ValueError("暂无足够训练数据估算当前 VDOT，无法按你的能力解析配速；请先录入近期跑步记录")
    profile = build_athlete_profile(db, athlete)
    if not profile["weekly_km"]:
        raise ValueError("近 28 天无跑步记录，无法确定跑量基数；请先录入训练数据")
    slots = db.scalars(select(models.WeeklySlot).where(
        models.WeeklySlot.athlete_id == athlete.id,
        models.WeeklySlot.kind == "available")).all()
    if not slots:
        raise ValueError("请先在「日程管理」中添加至少一个可训练时段")
    # 每个 weekday 取最长时段（体验周一天一课）
    by_wd: dict[int, dict] = {}
    for s in slots:
        d = {"weekday": s.weekday, "start_time": s.start_time, "duration_minutes": s.duration_minutes}
        if s.weekday not in by_wd or d["duration_minutes"] > by_wd[s.weekday]["duration_minutes"]:
            by_wd[s.weekday] = d
    # 排课窗口：锚定到「下一个完整自然周」的周一。
    #
    # 为什么不能是「明天起的 7 天」：days_sorted 里 7 个可练时段按各自 weekday 落位后，
    # 从任意一天起算的 7 天窗口会横跨两个自然周，而全系统对「周」的统一约定是
    # 「周一为第一天」（planner._alloc 归一、weekly_recap._week_start、总览页
    # plan_week 查询的 start_date <= today <= start_date+6、AI 挪课的
    # start_date + weekday 推日期，全部建立在这个前提上）。
    # 一旦 start_date 落在周中，上述逻辑就会把「今天属于哪一周」算错，
    # 表现为总览页显示不出本周、起始日期与课次日期对不上。
    # 因此这里把窗口起点对齐到周一：若「明天」就是周一，则明天即开窗（不等待）；
    # 否则顺延到下一个周一，保证 7 个时段严格落在同一自然周内。
    tomorrow = _dt.date.today() + _dt.timedelta(days=1)
    window_start = tomorrow - _dt.timedelta(days=tomorrow.weekday())
    if window_start < tomorrow:
        window_start += _dt.timedelta(days=7)
    # days_sorted 保持「时段时长降序」：长距离取 days_sorted[0]、质量课取次长的语义，
    # 排课依赖这个顺序，不能按日期重排（否则长距离会落在窗口内最早的一天而非最长时段）
    days_sorted = sorted(by_wd.values(), key=lambda d: -d["duration_minutes"])
    for d in days_sorted:
        # window_start 已归一为周一，weekday 语义与 date.weekday() 一致（0=周一）
        d["date"] = window_start + _dt.timedelta(days=d["weekday"])

    tpls = list(m.workouts or [])
    quality_pool = sorted((t for t in tpls if t.session_type in QUALITY_TYPES),
                          key=lambda t: (t.distance_km_range or {}).get("max", 99))
    long_pool = [t for t in tpls if t.session_type == "long"]
    easy_pool = [t for t in tpls if t.session_type == "easy"]
    notes: list[str] = []

    # ---- 季节因子：湿热季整体放宽配速并附防暑指引 ----
    season = season_hint(db, athlete.id)
    if season["hot"]:
        notes.append(f"季节因子：{season['source']}约 {season['avg_temp_c']}℃，已进入湿热适应口径"
                     f"——配速整体放慢 {season['relax_pct']:.0f}%，质量课尽量安排在清晨/傍晚")

    def _season_fit(steps: list[dict]) -> list[dict]:
        if season["hot"]:
            _relax_paces(steps, season["relax_pct"])
        return steps

    days = len(days_sorted)
    # 质量课上限按水平分级（见 MAX_QUALITY_BY_LEVEL）
    quality_cap = MAX_QUALITY_BY_LEVEL.get(profile["level"], 2)
    n_quality = min(quality_cap, m.quality_days_per_week or 0, max(0, days - 1))
    if (m.quality_days_per_week or 0) > quality_cap:
        notes.append(f"降档：原体系每周 {m.quality_days_per_week} 次质量课，"
                     f"按你 {profile['level']} 水平每周上限 {quality_cap} 次（强度课占比 ≤20%，伤病风险）")
    if m.level == "elite" and profile["level"] in ("beginner", "intermediate"):
        notes.append(f"降档：原体系面向 {m.level} 水平，已按你 {profile['level']} 水平收缩跑量与强度")

    # ---- 长距离：周跑量 35% 封顶、45% 绝对上限、受最长时段约束 ----
    long_tpl = long_pool[0] if long_pool else None
    slot_cap_km = days_sorted[0]["duration_minutes"] / MIN_PER_KM * 0.9
    long_km = round(min(profile["weekly_km"] * 0.35, slot_cap_km,
                        (long_tpl.distance_km_range or {}).get("max", 999) if long_tpl else 999), 1)
    # 下限随跑量缩放。原来固定 6km 保底，会让周跑量 8km 的初跑者单次长距离占到 75%，
    # 是典型的单次过载；改为「周跑量 25%」与 3km 取大者，并用「周跑量 45%」与时段封顶。
    long_floor = max(3.0, min(6.0, profile["weekly_km"] * 0.25))
    long_km = round(min(max(long_km, long_floor),
                        max(3.0, profile["weekly_km"] * 0.45),
                        max(3.0, slot_cap_km)), 1)
    if long_tpl and long_km < (long_tpl.distance_km_range or {}).get("min", 0):
        notes.append(f"降档：原模板长距离下限 {long_tpl.distance_km_range['min']}km 已按你的跑量与时段缩放到 {long_km}km")

    workouts: list[dict] = []
    used_tpl = set()

    def _emit(day: dict, stype: str, title: str, steps: list[dict], desc: str, src: str):
        # 模板名常带原体系的固定课日（如「周六法特雷克」「周四渐进长距离」），
        # 体验周按用户日程重排了日期，前缀会产生误导，统一去掉
        title = re.sub(r"^(周[一二三四五六日日]点?试?[：:]?|每周[一二三四五六日][：:]?)", "", title)
        workouts.append({
            "date": day["date"].isoformat(),
            "start_time": day["start_time"],
            "session_type": stype, "title": title, "structured": steps,
            "distance_km": _steps_km(steps), "duration_min": round(_estimate_minutes(steps)),
            "description": f"【{m.name_zh} 体验周】{desc}",
            "source_template": src,
        })

    def _fit_steps(tpl, slot_min: int, *, scale_to_km: float | None = None):
        """解析模板并适配时段：先按目标距离缩放，超时则组数减半（最多两次）。"""
        steps = resolve_template_steps(tpl.structure, pred.current_vdot)
        if scale_to_km is not None:
            km_now = _steps_km(steps)
            if km_now > 0 and abs(km_now - scale_to_km) > 0.5:
                ratio = scale_to_km / km_now
                for s in steps:
                    if s.get("step_type") == "active" and s.get("duration_type") == "distance":
                        s["duration_value"] = round(s["duration_value"] * ratio)
        for _ in range(2):
            if _estimate_minutes(steps) <= slot_min * 1.1:
                return _season_fit(steps), None
            shrunk = _halve_repeats(steps)
            if shrunk is None:
                break
            steps = shrunk
        if _estimate_minutes(steps) <= slot_min * 1.1:
            return _season_fit(steps), None
        return None, "超过当日可用时段"

    # ---- 长距离（最长时段日）----
    long_day = days_sorted[0]
    if long_tpl:
        steps, why = _fit_steps(long_tpl, long_day["duration_minutes"], scale_to_km=long_km)
        if steps:
            _emit(long_day, "long", long_tpl.name_zh, steps, long_tpl.purpose, long_tpl.code)
        else:
            notes.append(f"长距离模板放不进最长时段（{why}），已用引擎通用长距离替代")
            _, title, steps, _, _ = planner.build_long_run("base", pred.current_vdot, long_km, "marathon")
            _emit(long_day, "long", title, steps, "按你当前跑量与时段封顶的通用长距离", "planner.generic_long")
    else:
        _, title, steps, _, _ = planner.build_long_run("base", pred.current_vdot, long_km, "marathon")
        _emit(long_day, "long", title, steps, "按你当前跑量 35% 封顶的通用长距离", "planner.generic_long")

    # ---- 质量课（次长时段日，模板不重复，放不下先减组再换）----
    quality_days = [d for d in days_sorted[1:]][:n_quality]
    qi = 0
    for day in quality_days:
        placed = False
        for t in quality_pool:
            if t.code in used_tpl and len(quality_pool) > len(quality_days):
                continue
            steps, why = _fit_steps(t, day["duration_minutes"])
            if steps:
                used_tpl.add(t.code)
                _emit(day, t.session_type, t.name_zh, steps, t.purpose, t.code)
                placed = True
                break
        if not placed:
            notes.append(f"周{WEEKDAY_CN[day['weekday']]}的可用时段不足，该日质量课改为轻松跑")
            _, title, steps, _, _ = planner.build_easy_run(pred.current_vdot, 4.0)
            _emit(day, "easy", title, steps, "时段不足以完成质量课，降级为轻松跑", "planner.generic_easy")
        qi += 1

    # ---- 其余日期：轻松跑（优先用体系自己的模板；按日期先后补位，输出保持时序）----
    used_days = {w["date"] for w in workouts}
    for day in sorted(days_sorted, key=lambda d: d["date"]):
        if day["date"].isoformat() in used_days:
            continue
        cap_km = round(min(day["duration_minutes"] / MIN_PER_KM,
                           max(3.0, (profile["weekly_km"] - long_km) / max(1, days - 1))), 1)
        tpl = easy_pool[len(workouts) % len(easy_pool)] if easy_pool else None
        if tpl:
            steps, _ = _fit_steps(tpl, day["duration_minutes"])
            if steps:
                _emit(day, "easy", tpl.name_zh, steps, tpl.purpose, tpl.code)
                continue
        _, title, steps, _, _ = planner.build_easy_run(pred.current_vdot, cap_km)
        _emit(day, "easy", title, steps, "体系未提供合适的轻松跑模板，用引擎通用轻松跑补位", "planner.generic_easy")

    total_km = round(sum(w["distance_km"] for w in workouts), 1)
    return {
        "method_code": m.code, "method_name": m.name_zh,
        "vdot": round(pred.current_vdot, 1),
        # 落库为 PlanWeek/TrainingPlan.start_date：必须是这一周的周一（见排课窗口注释），
        # 不能取 workouts[0] 的日期——那是「最长时段」那天，可能是周中任意一天
        "week_start": window_start.isoformat(),
        "weekly_km_base": profile["weekly_km"],
        "planned_km": total_km,
        "n_quality": sum(1 for w in workouts if w["session_type"] in QUALITY_TYPES),
        "workouts": workouts,
        "notes": notes,
        "season": season,
        "traceability": {
            "method_evidence": m.evidence_level,
            "principles_hint": "强度节奏可参考 acwr-load-management / intensity-distribution / progressive-overload 原理",
        },
    }


def apply_method_week(db: Session, athlete: models.Athlete, method_code: str) -> dict:
    """生成并把体验周落库为当前 active 计划（与 /plan/generate 同样的单 active 语义）。"""

    draft = build_week_plan_from_method(db, athlete, method_code)
    old = db.scalars(select(models.TrainingPlan).where(models.TrainingPlan.status == "active")).all()
    for o in old:
        archive_plan(db, o)
    week_start = _dt.date.fromisoformat(draft["week_start"])
    row = models.TrainingPlan(
        athlete_id=athlete.id, goal_id=None,
        name=f"体验周 · {draft['method_name']}（{draft['planned_km']}km）",
        race_type="custom", target_time_sec=None,
        start_date=week_start, race_date=week_start + _dt.timedelta(days=6),
        weekly_km_peak=draft["planned_km"],
        feasibility={"source": "method_library", "method_code": draft["method_code"],
                     "vdot": draft["vdot"], "notes": draft["notes"]},
    )
    db.add(row)
    db.flush()
    pw = models.PlanWeek(plan_id=row.id, week_index=1, start_date=week_start,
                         phase="build", phase_note=f"体验周：{draft['method_name']} 的业余降档执行",
                         focus="方法体验", target_km=draft["planned_km"])
    db.add(pw)
    db.flush()
    for w in draft["workouts"]:
        db.add(models.PlanWorkout(
            week_id=pw.id, athlete_id=athlete.id,
            date=_dt.date.fromisoformat(w["date"]), start_time=w["start_time"],
            session_type=w["session_type"], title=w["title"],
            description=w["description"], distance_km=w["distance_km"],
            duration_min=w["duration_min"], structured=w["structured"],
            diet_tip=(draft["season"]["advice"] if draft["season"]["hot"] and
                      w["session_type"] in QUALITY_TYPES else ""),
        ))
    db.commit()
    return {"ok": True, "plan_id": row.id,
            "message": f"已生成体验周计划（{draft['planned_km']}km，{len(draft['workouts'])} 节课），"
                       f"原计划{'已归档' if old else '（原本无进行中计划）'}；可在「训练计划」页查看并同步手表",
            "planned_km": draft["planned_km"], "n_workouts": len(draft["workouts"])}
