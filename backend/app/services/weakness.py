"""短板分析与目标差距：综合评估页「哪里最值得练」的确定性引擎。

输入全部来自已算好的引擎事实（评估五维/力量测评/饮食分析/目标与预测），
这里只做「找短板、定优先级、给可执行动作」，不重新发明任何数字。
findings 直接落库到 assessments.weaknesses 并被 AI 解读引用
（assessment_review.gather_facts），所以字段口径（category/severity/title/
what/actions）不能随意改。
"""
from __future__ import annotations

from .vdot import RACE_LABELS, required_vdot

_DIM_LABELS = {
    "aerobic": "有氧能力", "strength": "力量", "willpower": "意志品质",
    "recovery": "恢复管理", "talent": "天赋潜质",
}

# 各短板维度对应的可执行动作（与平台已有功能对应，能直接照做）
_DIM_ACTIONS = {
    "aerobic": ["把轻松跑占比拉回 80% 左右，打好有氧底盘",
                "每周安排 1 次乳酸阈值课（T 心率 20 分钟起步）"],
    "strength": ["完成一次五大项（深蹲/卧推/硬拉/推举/划船）力量测试",
                 "每周 2 次力量课，优先补相对力量（kg/kg 体重）"],
    "willpower": ["从「近 8 周计划课完成率」入手，先保完成率再保强度",
                  "把最常被跳过的那档课挪到每周最空闲的时段"],
    "recovery": ["连续 7 天记录晨间打卡与睡眠，找出恢复短板",
                 "高强度课后安排次日恢复跑或完全休息"],
    "talent": ["天赋是先天分布，重点在扬长：选择与能力类型匹配的比赛距离",
               "把训练资源投给可训练维度（有氧/力量/恢复）"],
}

_SEVERITY_ORDER = {"high": 0, "mid": 1, "low": 2}


def _severity_of(score: float | None) -> str | None:
    if score is None:
        return None
    if score < 45:
        return "high"
    if score < 60:
        return "mid"
    return "low"


def _dimension_findings(evaluation: dict) -> list[dict]:
    dims = evaluation.get("dimensions") or {}
    out = []
    for key, dim in dims.items():
        score = dim.get("score")
        sev = _severity_of(score)
        if not sev or sev == "low":
            continue
        # 天赋是先天分布，其余维度可训练
        category = "innate" if key == "talent" else "trainable"
        evidence = (dim.get("evidence") or [])
        out.append({
            "category": category,
            "severity": sev,
            "title": f"{_DIM_LABELS.get(key, key)}偏弱（{score} 分）",
            "what": (evidence[0] if evidence else
                     f"该维度得分 {score}，低于同人群平均水平"),
            "actions": _DIM_ACTIONS.get(key, []),
        })
    out.sort(key=lambda f: _SEVERITY_ORDER[f["severity"]])
    return out


def _goal_gap(evaluation: dict, goal: dict | None) -> dict | None:
    if not goal or not goal.get("target_time_sec"):
        return None
    race_type = goal.get("race_type") or "marathon"
    if race_type not in RACE_LABELS:
        return None
    current = evaluation.get("current_vdot") or 0
    try:
        req = required_vdot(race_type, goal["target_time_sec"])
    except ValueError:
        return None
    label = goal.get("target_label") or f"{RACE_LABELS[race_type]} {goal['target_time_sec'] // 60} 分"
    if not current:
        verdict = "当前还没有有效成绩，先积累训练数据再评估可行性"
    elif current >= req:
        verdict = "当前水平已达标，重心放在比赛执行与状态管理"
    elif current >= req - 1.5:
        verdict = "差距很小，赛前 6-8 周针对性强化即可达标"
    elif current >= req - 4:
        verdict = "有差距但可追赶，需要 1-2 个训练周期稳步提升"
    else:
        verdict = "差距较大，建议先设定阶梯目标，避免盲目冲线"
    return {"goal": label, "race_type": race_type,
            "required_vdot": round(req, 1), "current_vdot": round(current, 1),
            "verdict": verdict}


def _goal_gap_finding(gap: dict | None) -> dict | None:
    if not gap or not gap.get("current_vdot"):
        return None
    delta = gap["required_vdot"] - gap["current_vdot"]
    if delta <= 1.5:
        return None
    return {
        "category": "trainable",
        "severity": "high" if delta > 4 else "mid",
        "title": f"距离目标还有 {round(delta, 1)} 个 VDOT 的差距",
        "what": f"{gap['goal']}：需要 VDOT {gap['required_vdot']}，当前 {gap['current_vdot']}",
        "actions": ["把目标拆成分段节点（每 6-8 周一个中间目标）",
                    "优先补有氧底盘与阈值能力，这是 VDOT 提升的主引擎"],
    }


def _diet_finding(diet_analysis: dict | None) -> dict | None:
    if not diet_analysis or diet_analysis.get("score") is None:
        return None
    if diet_analysis.get("score", 100) >= 60:
        return None
    issues = diet_analysis.get("issues") or []
    return {
        "category": "trainable",
        "severity": "mid",
        "title": f"饮食习惯待改善（{diet_analysis['score']} 分）",
        "what": issues[0] if issues else "近期热量/蛋白质摄入不达标",
        "actions": ["至少记录每日主餐，让平台能持续评估",
                    "训练量大时优先补足碳水与蛋白质（1.6-2.0g/kg）"],
    }


def analyze(ad: dict, evaluation: dict, pred, strength_analysis: dict | None,
            diet_analysis: dict | None, goal: dict | None, weekly_km: float) -> dict:
    """装配短板清单 + 目标差距 + 一句话总结。全部输入尽力而为，缺数据时如实说。"""
    findings = _dimension_findings(evaluation)
    gap = _goal_gap(evaluation, goal)
    for extra in (_goal_gap_finding(gap), _diet_finding(diet_analysis)):
        if extra:
            findings.append(extra)
    findings.sort(key=lambda f: _SEVERITY_ORDER.get(f["severity"], 3))

    strength_analysis = strength_analysis or {}
    if strength_analysis.get("score") is None:
        findings.append({
            "category": "trainable", "severity": "low",
            "title": "力量数据缺失",
            "what": "暂无力量测试数据，力量维度无法评估",
            "actions": ["完成一次五大项测试，解锁力量评估与伤病风险对照"],
        })

    weakest = next((f for f in findings if f["severity"] in ("high", "mid")), None)
    if weakest:
        summary = f"当前最值得投入的是：{weakest['title']}"
    elif findings:
        summary = "各维度无明显短板，保持当前训练结构即可"
    else:
        summary = "数据尚不足以判断短板，先积累 2-4 周训练记录"

    return {"findings": findings, "goal_gap": gap, "summary": summary}
