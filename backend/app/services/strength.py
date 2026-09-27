"""力量评估：1RM 折算、相对力量标准、肌群平衡、针对跑步的力量短板与建议。"""
from __future__ import annotations

# ---------------------------------------------------------------- 1RM 折算

def estimate_1rm(weight_kg: float, reps: int) -> float:
    """Epley 与 Brzycki 公式取平均（reps<=10 时较准）。"""
    if reps <= 1:
        return weight_kg
    reps = min(reps, 15)
    epley = weight_kg * (1 + reps / 30)
    brzycki = weight_kg * 36 / (37 - reps)
    return round((epley + brzycki) / 2, 1)


# ---------------------------------------------------------------- 标准（相对力量 = 1RM/体重）
# 等级: 0 未入门 / 1 新手 / 2 初级 / 3 中级 / 4 高级 / 5 精英
STANDARDS_MALE = {
    "squat":      [0.60, 0.90, 1.30, 1.80, 2.20],   # 深蹲
    "bench":      [0.40, 0.65, 0.95, 1.35, 1.70],   # 卧推
    "deadlift":   [0.80, 1.15, 1.60, 2.10, 2.60],   # 硬拉
    "ohp":        [0.30, 0.45, 0.62, 0.85, 1.05],   # 站姿推举
    "row":        [0.40, 0.60, 0.85, 1.15, 1.40],   # 划船
    "pullup":     [0.00, 0.05, 0.15, 0.35, 0.55],   # 引体（附加负重/体重）
    "hip_thrust": [0.80, 1.20, 1.70, 2.20, 2.70],   # 臀桥/臀推
}
LEVEL_NAMES = ["未入门", "新手", "初级", "中级", "高级", "精英"]

EXERCISE_NAMES = {
    "squat": "深蹲", "bench": "卧推", "deadlift": "硬拉", "ohp": "站姿推举",
    "row": "划船", "pullup": "引体向上", "hip_thrust": "臀推", "core": "核心",
}

# 中文别名 → 标准 key。历史数据（如 seed_demo 播种）与手动录入可能使用中文动作名，
# 若不做归一，assess_strength 会因 `ex not in standards` 静默跳过全部条目，导致力量维度失效。
EXERCISE_ALIASES = {
    "深蹲": "squat", "杠铃深蹲": "squat", "后蹲": "squat",
    "卧推": "bench", "杠铃卧推": "bench",
    "硬拉": "deadlift", "传统硬拉": "deadlift", "罗马尼亚硬拉": "deadlift",
    "站姿推举": "ohp", "推举": "ohp", "肩上推举": "ohp", "实力推": "ohp",
    "划船": "row", "杠铃划船": "row", "俯身划船": "row", "坐姿划船": "row",
    "引体向上": "pullup", "引体": "pullup", "pull_up": "pullup",
    "臀推": "hip_thrust", "臀桥": "hip_thrust", "hipthrust": "hip_thrust",
    "核心": "core",
}


def canonical_exercise(name: str) -> str:
    """把动作名（中英文、别名、大小写、空格差异）归一到标准 key。"""
    if not name:
        return ""
    key = str(name).strip().lower().replace(" ", "_")
    if key in EXERCISE_ALIASES:
        return EXERCISE_ALIASES[key]
    return EXERCISE_ALIASES.get(str(name).strip(), key)

# 跑者各动作的建议相对力量区间（针对跑步经济性与损伤预防，非健力标准）
RUNNER_TARGETS = {
    "squat": (1.2, 1.8, "支撑跑步冲击、提升跑步经济性"),
    "deadlift": (1.5, 2.2, "后链力量（臀腿腘绳），防伤与推进力"),
    "bench": (0.6, 1.0, "上肢摆臂稳定，无需过高"),
    "ohp": (0.4, 0.7, "肩带上肢力量"),
    "row": (0.6, 1.0, "背部平衡、跑姿挺拔"),
    "hip_thrust": (1.5, 2.5, "臀肌主导的伸髋驱动"),
}


def level_for(relative: float, standards: list[float]) -> int:
    """返回 0-5 等级：超过第 i 阈值则达 i+1 级。"""
    lv = 0
    for th in standards:
        if relative >= th:
            lv += 1
        else:
            break
    return lv


def assess_strength(tests: list[dict], sex: str, bodyweight_kg: float) -> dict:
    """tests: [{exercise, best_weight_kg, reps, date}]（同一动作取最新日期的最好成绩）。

    返回：各动作 1RM/相对力量/等级、肌群平衡诊断、综合力量得分（0-100）、针对性建议。
    """
    if not tests:
        return {
            "score": None, "summary": "暂无力量测试数据，建议先完成五大项测试",
            "items": [], "balance": [], "recommendations": [], "targets": [],
        }

    standards = STANDARDS_MALE if sex == "male" else {k: [round(v * 0.70, 2) for v in vs]
                                                      for k, vs in STANDARDS_MALE.items()}

    latest: dict[str, dict] = {}
    for t in sorted(tests, key=lambda x: x["date"]):
        # 按归一后的 key 去重，保证中英文混录时同一动作不会被算成两项
        latest[canonical_exercise(t["exercise"])] = t   # 同动作取最新

    items, level_pool = [], []
    for ex, t in latest.items():
        if ex not in standards:
            continue
        orm = estimate_1rm(t["best_weight_kg"], t.get("reps", 1))
        bw = t.get("bodyweight_kg") or bodyweight_kg
        rel = orm / max(30, bw)
        lv = level_for(rel, standards[ex])
        level_pool.append(lv)
        item = {
            "exercise": ex, "label": EXERCISE_NAMES.get(ex, ex),
            "date": t["date"], "orm_kg": orm,
            "relative": round(rel, 2), "level": lv, "level_name": LEVEL_NAMES[lv],
        }
        if ex in RUNNER_TARGETS:
            lo, hi, why = RUNNER_TARGETS[ex]
            item["runner_target"] = f"{lo}~{hi}×体重"
            item["runner_target_reason"] = why
            # 上下界都需判定：下限以下为不足/接近，上限以上为过强（防止只判下限导致"过强也算达标"）
            if rel > hi + 0.3:
                meets, status = False, "过强"
            elif rel >= lo:
                meets, status = True, "达标"
            elif rel >= lo - 0.15:
                meets, status = False, "接近"
            else:
                meets, status = False, "不足"
            item["meets_runner_target"] = meets
            item["relative_status"] = status
        items.append(item)

    score = None
    if level_pool:
        # 综合得分：平均等级映射到 0-100，等级权重最高一项（硬拉/深蹲）略加成
        weights = {"squat": 1.15, "deadlift": 1.15, "hip_thrust": 1.0, "bench": 0.9,
                   "ohp": 0.85, "row": 0.9, "pullup": 0.9}
        num = sum(lv * weights.get(i["exercise"], 1.0) for i, lv in zip(items, level_pool, strict=True))
        den = sum(weights.get(i["exercise"], 1.0) for i in items)
        avg_lv = num / den
        score = round(min(100, avg_lv / 5 * 100))

    # ---- 肌群平衡（以下肢为基准） ----
    balance = []
    rel = {i["exercise"]: i["relative"] for i in items}
    checks = [
        ("deadlift", "squat", 1.15, 1.45, "硬拉/深蹲"),
        ("bench", "squat", 0.50, 0.75, "卧推/深蹲"),
        ("ohp", "bench", 0.60, 0.75, "推举/卧推"),
        ("row", "bench", 0.85, 1.10, "划船/卧推"),
        ("squat", "deadlift", 0.65, 0.87, "深蹲/硬拉"),
    ]
    for a, b, lo, hi, label in checks:
        if a in rel and b in rel and rel[b] > 0.1:
            ratio = rel[a] / rel[b]
            if ratio < lo:
                status, advice = "偏弱", f"{label} 偏低，{EXERCISE_NAMES.get(a, a)}是短板，建议每周 2 次针对性加重"
            elif ratio > hi:
                status, advice = "偏强", f"{label} 偏高，{EXERCISE_NAMES.get(b, b)}相对滞后，注意整体均衡"
            else:
                status, advice = "均衡", ""
            balance.append({"label": label, "ratio": round(ratio, 2),
                            "range": f"{lo}-{hi}", "status": status, "advice": advice})

    # ---- 针对跑者的建议 ----
    recommendations = []
    for i in items:
        if i["exercise"] in RUNNER_TARGETS and i.get("relative_status") == "不足":
            lo, hi, why = RUNNER_TARGETS[i["exercise"]]
            recommendations.append({
                "area": i["label"],
                "issue": f"相对力量 {i['relative']}×体重，低于跑者建议下限 {lo}×体重",
                "why": why,
                "how": _how_to(i["exercise"]),
            })
    for b in balance:
        if b["status"] == "偏弱" and b["label"] == "划船/卧推":
            recommendations.append({
                "area": "背部", "issue": "推拉失衡（推强拉弱）易致圆肩、跑姿塌陷",
                "why": "跑步需要背部与臀部链条维持躯干稳定", "how": "每 2 次推类动作配 3 次划船类；加入面拉、直臂下压",
            })
    weak_lower = [i for i in items if i["exercise"] in ("squat", "deadlift", "hip_thrust")
                  and i.get("relative_status") == "不足"]
    if len(weak_lower) >= 2:
        recommendations.append({
            "area": "下肢整体力量", "issue": "下肢多项力量不足，限制跑步经济性与长距离后程稳定性",
            "why": "跑量之外，力量是长跑后程掉速与伤病的第一预测因子",
            "how": "前 8 周每周 2 次下肢力量（深蹲 5×5、罗马尼亚硬拉 4×8、臀推 4×8、单腿硬拉 3×8/侧），与轻松跑错开",
        })
    if not recommendations:
        recommendations.append({
            "area": "保持", "issue": "力量结构总体健康", "why": "",
            "how": "维持每周 1-2 次力量课，重点做单腿稳定性（保加利亚分腿蹲、单腿提踵）与核心抗旋",
        })

    avg_rel = round(sum(i["relative"] for i in items) / len(items), 2) if items else 0
    summary = (f"共 {len(items)} 项测试，平均相对力量 {avg_rel}×体重；"
               f"综合等级 {LEVEL_NAMES[round(avg_lv)] if level_pool else '-'}。"
               + ("下肢力量是跑者重点，当前存在不足。" if weak_lower else "核心链条满足跑步需求。"))

    return {
        "score": score, "summary": summary, "items": items, "balance": balance,
        "recommendations": recommendations,
        "standards_note": "标准为相对力量（1RM÷体重），已按性别调整；跑者建议列针对跑步表现而非健力比赛",
    }


def _how_to(exercise: str) -> str:
    plans = {
        "squat": "每周 2 次：高杠深蹲 5×5@80%1RM（第 1 次）+ 前蹲/箱式深蹲 4×6（第 2 次），8 周后重测",
        "deadlift": "每周 1-2 次：罗马尼亚硬拉 4×8 + 传统硬拉 5×3@85%，与质量课错开 24h",
        "bench": "卧推 5×5 + 哑铃卧推 3×10，跑者保持 0.7×体重即可",
        "ohp": "站姿推举 5×5，配合面拉 3×15 强化肩袖稳定",
        "row": "杠铃划船 4×8 + 引体向上累计 3×力竭，逐步加负重",
        "hip_thrust": "臀推 4×8 渐进至 2×体重，配合单腿臀桥 3×12/侧",
    }
    return plans.get(exercise, "每周 2 次渐进超负荷训练，8 周后重测")
