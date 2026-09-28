"""单课分析器：训练课「处方 vs 实际执行」的结构化对照（纯确定性，无 LLM）。

分析三件事并给出人话理由链：
1. 完成度：实际距离/时长 vs 计划；
2. 强度合规：实际配速（质量课用最快完整公里分段近似主课配速，轻松课用全程
   平均配速）vs 课表结构化步骤的目标配速区间；旧课没有结构化步骤时退化为
   计划平均配速对照；
3. 心率合规：实际平均心率 vs 步骤目标心率区间（max_hr 缺失时整段跳过）。

结论落 plan_workouts.analysis（JSON 列），是 plan_drift 调课建议与前端
「这堂课完成得怎么样」的直接数据源。数字纪律与 coach_comment 相同：
只陈述引擎算出的事实，不做训练学评语。
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from .. import models
from . import vdot, vocab

logger = logging.getLogger(__name__)

ANALYSIS_VERSION = 1

# 完成度判定：距离完成不足 85% 视为「未跑完」（GPS 截尾/中途放弃的常见分界）
SHORT_RATIO = 0.85
# 配速偏离判定：超出目标区间 15% 以上才给「慢于处方」（自动配速换算噪声大，
# 稍慢一点点就报警会把人变成指标的奴隶）
PACE_TOL = 0.15
# 心率超出区间上限 5% 最大心率以上记「高于处方」
HR_TOL_PCT = 0.05

VERDICT_LABELS = {
    "on_target": "达标",
    "too_fast": "快于处方",
    "too_slow": "慢于处方",
    "short": "未跑完",
    "no_data": "无实际数据",
}


def _pace_targets(steps: list[dict]) -> tuple[int, int] | None:
    """主课（active 步骤）的目标配速区间（sec/km，快端,慢端）。无配速目标返回 None。"""
    los, his = [], []
    for s in steps or []:
        if s.get("step_type") != "active":
            continue
        t = s.get("target") or {}
        if t.get("type") == "pace" and t.get("from") and t.get("to"):
            los.append(min(t["from"], t["to"]))
            his.append(max(t["from"], t["to"]))
    if not los:
        return None
    return min(los), max(his)


def _hr_targets(steps: list[dict]) -> tuple[float, float] | None:
    """主课目标心率区间（占 max_hr 的比例 0-1）。无心率目标返回 None。"""
    los, his = [], []
    for s in steps or []:
        if s.get("step_type") != "active":
            continue
        t = s.get("target") or {}
        if t.get("type") == "hr" and t.get("from") is not None and t.get("to"):
            los.append(float(t["from"]))
            his.append(float(t["to"]))
    if not los:
        return None
    return min(los), max(his)


def _splits(act: models.Activity) -> list[dict]:
    """完整 1km 分段（canonical 格式见 integrations：raw["splits"]）。"""
    splits = (act.raw or {}).get("splits") or []
    return [s for s in splits
            if isinstance(s, dict) and s.get("pace_sec_per_km")
            and (s.get("distance_m") or 0) >= 900]


def analyze_workout(wo: models.PlanWorkout, act: models.Activity | None,
                    max_hr: int | None) -> dict:
    """分析一节课。act=None（手动打卡无关联）也能出完成度结论。"""
    out: dict = {
        "version": ANALYSIS_VERSION, "verdict": "on_target",
        "completion": {}, "pace": None, "hr": None, "reasons": [],
        "analyzed_at": datetime.now().isoformat(timespec="seconds"),
    }
    reasons = out["reasons"]
    if act is None or not act.distance_m or not act.duration_sec:
        out["verdict"] = "no_data"
        reasons.append("手动标记完成，未关联运动记录，只记录了课表本身")
        return out

    act_km = act.distance_m / 1000
    act_min = act.duration_sec / 60
    km_ratio = act_km / wo.distance_km if wo.distance_km else 1.0
    dur_ratio = act_min / wo.duration_min if wo.duration_min else 1.0
    out["completion"] = {
        "planned_km": wo.distance_km, "actual_km": round(act_km, 1), "km_ratio": round(km_ratio, 2),
        "planned_min": round(wo.duration_min), "actual_min": round(act_min), "dur_ratio": round(dur_ratio, 2),
    }
    if wo.distance_km and km_ratio < SHORT_RATIO:
        out["verdict"] = "short"
        reasons.append(f"完成 {act_km:.1f}km / 计划 {wo.distance_km}km（{round(km_ratio * 100)}%），未跑满")

    # ---- 配速对照 ----
    act_avg_pace = act.duration_sec / act_km   # sec/km
    targets = _pace_targets(wo.structured)
    quality = vocab.is_hard_or_long(wo.session_type) and wo.session_type != "strength"
    # 质量课的强度在主课段落里，全程平均配速被热身/冷身稀释，用最快完整公里近似主课配速；
    # 轻松跑本该全程均匀，平均配速即处方对照
    best_split = min((_splits(act) or [{"pace_sec_per_km": None}]),
                     key=lambda s: s["pace_sec_per_km"]) if _splits(act) else None
    compare_pace = None
    pace_source = ""
    if quality and best_split and best_split.get("pace_sec_per_km"):
        compare_pace = best_split["pace_sec_per_km"]
        pace_source = "最快公里"
    elif not quality:
        compare_pace = round(act_avg_pace)
        pace_source = "平均配速"
    elif targets is None:
        reasons.append("无结构化步骤，质量课无法对照目标配速，仅核对完成度")

    if compare_pace is not None:
        pace_block = {"actual_sec_per_km": round(compare_pace),
                      "pace_label": vdot.pace_label(compare_pace), "source": pace_source}
        if targets:
            fast, slow = targets
            pace_block["target"] = {"fast_sec_per_km": fast, "slow_sec_per_km": slow,
                                    "label": f"{vdot.pace_label(fast)}~{vdot.pace_label(slow)}"}
            if compare_pace > slow * (1 + PACE_TOL):
                if out["verdict"] == "on_target":
                    out["verdict"] = "too_slow"
                reasons.append(f"{pace_source} {vdot.pace_label(compare_pace)} 慢于处方区间 "
                               f"{vdot.pace_label(fast)}~{vdot.pace_label(slow)}")
            elif compare_pace < fast * (1 - PACE_TOL):
                if out["verdict"] == "on_target":
                    out["verdict"] = "too_fast"
                reasons.append(f"{pace_source} {vdot.pace_label(compare_pace)} 快于处方区间 "
                               f"{vdot.pace_label(fast)}~{vdot.pace_label(slow)}，强度超出了今天的安排")
            elif out["verdict"] == "on_target":
                reasons.append(f"{pace_source} {vdot.pace_label(compare_pace)} 落在处方区间 "
                               f"{vdot.pace_label(fast)}~{vdot.pace_label(slow)} 内")
        else:
            pace_block["planned_avg_label"] = (
                vdot.pace_label(round(wo.distance_km * 3600 / (wo.duration_min * 60)))
                if wo.distance_km and wo.duration_min else None)
        out["pace"] = pace_block

    # ---- 心率对照 ----
    if act.avg_hr and max_hr:
        hr_zone = _hr_targets(wo.structured)
        ratio = act.avg_hr / max_hr
        hr_block: dict = {"avg_hr": act.avg_hr, "ratio": round(ratio, 2)}
        if hr_zone:
            lo, hi = hr_zone
            hr_block["target_ratio"] = [round(lo, 2), round(hi, 2)]
            if ratio > hi + HR_TOL_PCT:
                hr_block["verdict"] = "high"
                if out["verdict"] == "on_target":
                    out["verdict"] = "too_fast"
                reasons.append(f"平均心率 {act.avg_hr}（{round(ratio * 100)}% 最大心率）"
                               f"高于处方区间，身体负担比课表安排的重")
            else:
                hr_block["verdict"] = "ok"
        out["hr"] = hr_block

    if not reasons and out["verdict"] == "on_target":
        reasons.append(f"完成 {act_km:.1f}km / {round(act_min)} 分钟，与课表安排一致")
    return out


def analyze_and_store(db: Session, workout_id: int) -> dict | None:
    """读库分析一节课并写回 plan_workouts.analysis（自带提交）。返回分析结果。"""
    wo = db.get(models.PlanWorkout, workout_id)
    if wo is None:
        return None
    act = db.get(models.Activity, wo.completed_activity_id) if wo.completed_activity_id else None
    athlete = db.get(models.Athlete, wo.athlete_id)
    result = analyze_workout(wo, act, athlete.max_hr if athlete else None)
    wo.analysis = result
    db.commit()
    return result


def verdict_label(verdict: str | None) -> str:
    return VERDICT_LABELS.get(verdict or "", verdict or "")


def session_type_label(wo: models.PlanWorkout) -> str:
    return vocab.session_label(wo.session_type)
