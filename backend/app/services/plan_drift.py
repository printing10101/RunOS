"""计划漂移引擎：计划负荷路径 vs 实际执行的滚动对照 → 调整建议。

纯确定性规则，无 LLM（数字纪律同 checkin 建议：引擎只输出档位与理由，
不直接改课表；落库与确认走提案持久层，复用 /api/ai/proposals/apply 的
同一批 check_* 校验函数——两段式的「建议生成」半边）。

四类偏差检测（findings）：
1. missed_keys   近 7 天漏掉的质量课/长距离（仍处 planned）；
2. load_gap      本周计划跑量 vs 实际跑量的缺口/超额（±20% 以上）；
3. acwr          急慢性负荷比越界（>1.3 偏高 / >1.4 高危 / <0.8 退行）；
4. form          疲劳轨迹：今日形态过深 / 前瞻投影的比赛日形态不佳。

v1 窄决策（suggestions，最多 2 条，且必须通过对应 check_* 重检才输出）：
R1 下一节强度课换轻松跑——ACWR 偏高或形态过深时，健康优先；
R2 三天内漏掉的关键课挪到未来空档——还能补就补；
R3 超过三天的陈旧漏课——建议正式跳过，别让课表挂着幽灵债。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..data import active_plan, activities_dicts, body_metrics_dicts
from . import ai_tools, load_project, vocab
from .ai_tools import active_slots
from .load_status import build_training_status

logger = logging.getLogger(__name__)

# ---- 阈值（集中定义，测试与变异验证对着这里改）----
ACWR_HIGH = 1.3
ACWR_CRITICAL = 1.4
ACWR_LOW = 0.8
FORM_DEEP = -20          # 今日形态（fitness-fatigue）深于此值算疲劳积累
GAP_RATIO = 0.20         # 本周实际跑量偏离计划 ±20% 起报
GAP_MIN_KM = 5.0         # 且绝对差 ≥5km（小跑量周按比例报警没有意义）
MISS_MOVE_DAYS = 3       # 漏课三天内可挪，之后建议跳过
MAX_SUGGESTIONS = 2

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


# ---------------------------------------------------------------- 信号收集
def _week_km_actual(acts: list[dict], today: date) -> float:
    monday = today - timedelta(days=today.weekday())
    return round(sum(a.get("distance_m") or 0 for a in acts
                     if a.get("sport") == "run" and a["start_time"].date() >= monday) / 1000, 1)


def collect_signals(db: Session, athlete: models.Athlete) -> dict:
    """漂移判定的全部输入信号（一次装配，findings/suggestions 共用）。"""
    today = date.today()
    acts = activities_dicts(db, athlete.id, days=200)
    plan = active_plan(db)
    st: dict = {}
    if acts:
        metrics = body_metrics_dicts(db, athlete.id, days=60)
        athlete_dict = {"max_hr": athlete.max_hr, "resting_hr": athlete.resting_hr,
                        "hrv_baseline": athlete.hrv_baseline, "weight_kg": athlete.weight_kg}
        race_date = plan.race_date if plan else None
        st = build_training_status(acts, athlete_dict, metrics, race_date=race_date)

    week_target_km = 0.0
    upcoming: list[models.PlanWorkout] = []
    missed: list[models.PlanWorkout] = []
    if plan:
        week = db.scalar(select(models.PlanWeek).where(
            models.PlanWeek.plan_id == plan.id,
            models.PlanWeek.start_date <= today).order_by(models.PlanWeek.start_date.desc()))
        if week and today <= week.start_date + timedelta(days=6):
            week_target_km = week.target_km
        upcoming = db.scalars(select(models.PlanWorkout).where(
            models.PlanWorkout.athlete_id == athlete.id,
            models.PlanWorkout.status.in_(["planned", "synced"]),
            models.PlanWorkout.date >= today).order_by(models.PlanWorkout.date)).all()
        missed = db.scalars(select(models.PlanWorkout).where(
            models.PlanWorkout.athlete_id == athlete.id,
            models.PlanWorkout.status == "planned",
            models.PlanWorkout.date < today,
            models.PlanWorkout.date >= today - timedelta(days=7))).all()

    return {
        "today": today,
        "plan": plan,
        "acts": acts,
        "acwr": st.get("acwr"),
        "form": st.get("form"),
        "fitness": st.get("fitness"),
        "fatigue": st.get("fatigue"),
        "week_target_km": week_target_km,
        "week_actual_km": _week_km_actual(acts, today),
        "upcoming": upcoming,
        "missed": missed,
    }


# ---------------------------------------------------------------- 偏差检测（只报告）
def detect_findings(sig: dict) -> list[dict]:
    findings: list[dict] = []
    today = sig["today"]

    missed_keys = [wo for wo in sig["missed"] if vocab.is_hard_or_long(wo.session_type)]
    if missed_keys:
        stale = [wo for wo in missed_keys if (today - wo.date).days > MISS_MOVE_DAYS]
        findings.append({
            "type": "missed_keys", "severity": "high" if stale else "medium",
            "reasons": [f"{wo.date.isoformat()} 的「{wo.title}」仍标记未完成"
                        for wo in missed_keys[:3]],
        })

    target, actual = sig["week_target_km"], sig["week_actual_km"]
    if target > 0 and sig["acts"]:   # 刚建计划还没有任何数据时，缺口警告是噪声
        gap = actual - target
        if abs(gap) / target >= GAP_RATIO and abs(gap) >= GAP_MIN_KM:
            findings.append({
                "type": "load_gap", "severity": "medium",
                "reasons": [f"本周实际 {actual}km vs 计划 {target}km"
                            + ("，落后于计划" if gap < 0 else "，超出计划")],
            })

    acwr = sig["acwr"]
    if acwr is not None:
        if acwr > ACWR_CRITICAL:
            findings.append({"type": "acwr", "severity": "high",
                             "reasons": [f"急慢性负荷比 {acwr}，已超高危线（{ACWR_CRITICAL}），"
                                         f"进入伤病风险窗口"]})
        elif acwr > ACWR_HIGH:
            findings.append({"type": "acwr", "severity": "medium",
                             "reasons": [f"急慢性负荷比 {acwr}，超出稳妥区间（0.8-{ACWR_HIGH}）"]})
        elif acwr < ACWR_LOW:
            findings.append({"type": "acwr", "severity": "low",
                             "reasons": [f"急慢性负荷比 {acwr}，近期负荷偏低（退行区间）"]})

    form = sig["form"]
    if form is not None and form < FORM_DEEP:
        findings.append({"type": "form", "severity": "high" if form < FORM_DEEP - 10 else "medium",
                         "reasons": [f"今日形态 {form:.0f}（体能 {sig['fitness']:.0f} - 疲劳 "
                                     f"{sig['fatigue']:.0f}），疲劳积累偏深"]})

    if sig["plan"] and sig["plan"].race_date:
        planned = [{"date": wo.date, "session_type": wo.session_type,
                    "duration_min": wo.duration_min, "distance_km": wo.distance_km,
                    "title": wo.title} for wo in sig["upcoming"]]
        try:
            projection = load_project.project_load(
                daily_actual=[], planned=planned,
                athlete={"max_hr": 200, "resting_hr": 60},
                fitness=sig["fitness"] or 0.0, fatigue=sig["fatigue"] or 0.0,
                race_date=sig["plan"].race_date)
        except Exception as e:   # 投影失败不拖垮整体报告
            logger.debug("前瞻负荷投影失败（忽略）: %s", e)
        else:
            for w in projection["warnings"][:1]:
                findings.append({"type": "form_trajectory", "severity": "medium", "reasons": [w]})
    return findings


# ---------------------------------------------------------------- 窄决策（要动作）
def _next_hard_workout(sig: dict) -> models.PlanWorkout | None:
    return next((wo for wo in sig["upcoming"] if vocab.is_hard_or_long(wo.session_type)), None)


def _free_future_weekdays(sig: dict, days: int = 3) -> list[int]:
    """未来 days 天内、日程放得下且没排课的日期（返回 weekday 序号列表）。"""
    today = sig["today"]
    booked = {wo.date for wo in sig["upcoming"]}
    slots = active_slots(sig["_db"], sig["_athlete_id"])
    out = []
    for i in range(1, days + 1):
        d = today + timedelta(days=i)
        if d in booked:
            continue
        if any(s["weekday"] == d.weekday() and s["duration_minutes"] >= 40 for s in slots):
            out.append(d.weekday())
    return out


def _suggest_easy_replacement(db, athlete, sig) -> dict | None:
    acwr, form = sig["acwr"], sig["form"]
    overload = (acwr is not None and acwr > ACWR_HIGH) or (form is not None and form < FORM_DEEP)
    if not overload:
        return None
    wo = _next_hard_workout(sig)
    if wo is None:
        return None
    checked = ai_tools.check_easy_replacement(db, athlete, wo.id)
    if not checked.get("ok"):
        return None
    severity = "high" if (acwr or 0) > ACWR_CRITICAL or (form or 0) < FORM_DEEP - 10 else "medium"
    reasons = []
    if acwr is not None and acwr > ACWR_HIGH:
        reasons.append(f"急慢性负荷比 {acwr} 超出稳妥区间")
    if form is not None and form < FORM_DEEP:
        reasons.append(f"今日形态 {form:.0f}，疲劳积累偏深")
    return {
        "kind": "easy_replacement", "severity": severity,
        "title": checked["proposal"]["title"], "reasons": reasons,
        "payload": {"kind": "easy_replacement", "workout_id": wo.id},
        "preview": checked["proposal"],
        "dedup_key": f"easy_replacement:{wo.id}",
    }


def _suggest_move_missed(db, athlete, sig) -> dict | None:
    """三天内漏掉的关键课：挪到未来第一个放得下的空档。"""
    today = sig["today"]
    for wo in sorted(sig["missed"], key=lambda w: w.date, reverse=True):
        if not vocab.is_hard_or_long(wo.session_type):
            continue
        if not (1 <= (today - wo.date).days <= MISS_MOVE_DAYS):
            continue
        if wo.status != "planned":   # check_move 只接受 planned
            continue
        for wd in _free_future_weekdays(sig):
            checked = ai_tools.check_move(db, athlete, wo.id, wd)
            if checked.get("ok"):
                return {
                    "kind": "move_workout", "severity": "medium",
                    "title": checked["proposal"]["title"],
                    "reasons": [f"{wo.date.isoformat()} 的「{wo.title}」没有执行记录，"
                                f"三天内补上还能保住这次关键刺激"],
                    "payload": {"kind": "move_workout", "workout_id": wo.id, "target_weekday": wd},
                    "preview": checked["proposal"],
                    "dedup_key": f"move_workout:{wo.id}:{wd}",
                }
    return None


def _suggest_skip_stale(db, athlete, sig) -> dict | None:
    """超过三天的陈旧漏课：建议正式跳过，避免课表挂着永远「未完成」。"""
    today = sig["today"]
    for wo in sorted(sig["missed"], key=lambda w: w.date):
        if not vocab.is_hard_or_long(wo.session_type):
            continue
        if (today - wo.date).days <= MISS_MOVE_DAYS:
            continue
        checked = ai_tools.check_skip_workout(db, athlete, wo.id)
        if checked.get("ok"):
            return {
                "kind": "skip_workout", "severity": "low",
                "title": checked["proposal"]["title"],
                "reasons": [f"{wo.date.isoformat()} 的「{wo.title}」已过去 "
                            f"{(today - wo.date).days} 天，补跑价值有限，建议正式跳过"],
                "payload": {"kind": "skip_workout", "workout_id": wo.id},
                "preview": checked["proposal"],
                "dedup_key": f"skip_workout:{wo.id}",
            }
    return None


def build_adjustments(db: Session, athlete: models.Athlete) -> dict:
    """漂移报告主入口：信号 + findings + suggestions（建议必须过 check_* 重检）。"""
    sig = collect_signals(db, athlete)
    sig["_db"] = db
    sig["_athlete_id"] = athlete.id
    findings = detect_findings(sig)

    suggestions = [s for s in (
        _suggest_easy_replacement(db, athlete, sig),
        _suggest_move_missed(db, athlete, sig),
        _suggest_skip_stale(db, athlete, sig),
    ) if s]
    suggestions.sort(key=lambda s: SEVERITY_ORDER[s["severity"]])
    suggestions = suggestions[:MAX_SUGGESTIONS]

    return {
        "generated_at": date.today().isoformat(),
        "has_plan": sig["plan"] is not None,
        "signals": {k: sig[k] for k in ("acwr", "form", "fitness", "fatigue",
                                        "week_target_km", "week_actual_km")},
        "findings": findings,
        "suggestions": suggestions,
    }
