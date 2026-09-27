"""个性化训练计划生成器：

输入：目标（项目+成绩+日期）、当前 VDOT、天赋分（决定进步速度）、每周可用时间槽（课表）、饮食画像
输出：周期化宏循环（基础/强化/巅峰/减量）→ 每周课表 → 结构化训练步骤（可下发高驰/佳明手表）

结构化步骤通用格式（export 模块会转换为各平台格式与 FIT）：
  {step_type, name, duration_type: time|distance|open, duration_value, target: {type, from, to, label}, note}
  step_type: warmup | active | rest | cooldown | strength
"""
from __future__ import annotations

import math
from datetime import date, timedelta

from . import vdot

RACE_KM = {"5k": 5.0, "10k": 10.0, "hm": 21.1, "marathon": 42.2}

# 各项目「高水平选手」的峰值周跑量参照（km）。这只是**起点**，
# 实际峰值还会被本人当前跑量（start_km）与天赋响应速度共同约束：
#   peak = min(参照值 × 天赋系数, start_km × 2.2)
# 所以不同用户拿到的峰值不同，不存在「所有人都练到 75km」这种写法。
RACE_PEAK_KM = {"5k": 45, "10k": 55, "hm": 65, "marathon": 75}

# 峰值跑量相对起始跑量的安全上限（16-20 周内不该翻倍以上）
PEAK_TO_START_CAP = 2.2

PHASE_INFO = {
    "base": ("基础期", "打有氧底子：大量轻松跑 + 长距离，速度只保留 strides"),
    "build": ("强化期", "引入阈值与间歇：提升乳酸阈值与最大摄氧量"),
    "peak": ("巅峰期", "比赛专项化：马拉松配速长距离、比赛模拟"),
    "taper": ("减量期", "减量保强度，恢复糖原与神经状态迎接比赛"),
}


def _pace_targets(vdot_value: float, kind: str) -> dict:
    zones = {z["key"]: z for z in vdot.daniels_paces(vdot_value)}
    v = zones[kind]["velocity_from"]
    v2 = zones[kind]["velocity_to"]
    # 转换为 sec/km
    def to_sec(vel_ms):
        return round(1000 / vel_ms)
    f_sec, t_sec = to_sec(v), to_sec(v2)
    return {"type": "pace", "from": min(f_sec, t_sec), "to": max(f_sec, t_sec),
            "from_label": _pace_label(min(f_sec, t_sec)), "to_label": _pace_label(max(f_sec, t_sec))}


def _pace_label(sec_per_km: int) -> str:
    m, s = divmod(int(sec_per_km), 60)
    return f"{m}:{s:02d}/km"


def _steps_warmup(minutes: int = 12) -> list[dict]:
    return [{"step_type": "warmup", "name": "热身慢跑", "duration_type": "time", "duration_value": minutes,
             "target": {"type": "hr", "from": 0.65, "to": 0.75, "label": "心率 65%-75%"}, "note": "渐进入状态"}]


def _steps_cooldown(minutes: int = 10) -> list[dict]:
    return [{"step_type": "cooldown", "name": "冷身慢跑+拉伸", "duration_type": "time", "duration_value": minutes,
             "target": {"type": "hr", "from": 0.0, "to": 0.65, "label": "心率 <65%"}, "note": "放松恢复"}]


def _fmt_pace_range(t: dict) -> str:
    return f"{t['from_label']}-{t['to_label']}"


def build_quality_session(phase: str, week_vdot: float, weekly_km: float, race_type: str) -> tuple[str, str, list[dict], float, float]:
    """返回 (session_type, title, structured, distance_km, duration_min)。"""
    p_t = _pace_targets(week_vdot, "threshold")
    p_i = _pace_targets(week_vdot, "interval")
    p_m = _pace_targets(week_vdot, "marathon")
    p_r = _pace_targets(week_vdot, "repetition")

    if phase == "base":
        steps = (_steps_warmup(12)
                 + [{"step_type": "active", "name": "节奏跑", "duration_type": "time", "duration_value": 20,
                     "target": p_t, "note": "阈值配速，能说短句"},
                    {"step_type": "rest", "name": "慢跑恢复", "duration_type": "time", "duration_value": 4,
                     "target": {"type": "hr", "from": 0, "to": 0.7, "label": "心率 <70%"}},
                    {"step_type": "active", "name": "节奏跑", "duration_type": "time", "duration_value": 10,
                     "target": p_t, "note": "保持节奏"}]
                 + [{"step_type": "active", "name": "快步跑 strides", "duration_type": "distance", "duration_value": 100,
                     "target": p_r, "note": "20-25 秒快速跑，专注步频"} for _ in range(6)]
                 + _steps_cooldown(10))
        km, dur = 8 + weekly_km * 0.08, 55
        title = "节奏跑 + 快步跑"
    elif phase == "build":
        # 1km 间歇 ×6 @I 配速，间歇 2.5 分钟
        reps = max(4, min(8, round(weekly_km / 10)))
        rep_steps = []
        for i in range(reps):
            rep_steps.append({"step_type": "active", "name": f"间歇 {i+1}/{reps}", "duration_type": "distance",
                              "duration_value": 1000, "target": p_i, "note": "I 配速：3-5 分钟力竭强度"})
            rep_steps.append({"step_type": "rest", "name": "间歇恢复", "duration_type": "time",
                              "duration_value": 2.5 if i < reps - 1 else 0,
                              "target": {"type": "none", "label": "慢走/慢跑"}})
        steps = _steps_warmup(12) + rep_steps + _steps_cooldown(10)
        km, dur = 6 + reps * 2, 60
        title = f"1km 间歇 ×{reps} @I 配速"
    elif phase == "peak":
        if race_type == "marathon":
            steps = (_steps_warmup(12)
                     + [{"step_type": "active", "name": "马拉松配速段落", "duration_type": "distance",
                         "duration_value": 5000, "target": p_m, "note": "目标比赛配速，模拟比赛感觉"},
                        {"step_type": "rest", "name": "慢跑恢复", "duration_type": "time", "duration_value": 5,
                         "target": {"type": "hr", "from": 0, "to": 0.7, "label": "心率 <70%"}}]
                     + [{"step_type": "active", "name": "马拉松配速段落", "duration_type": "distance",
                         "duration_value": 3000, "target": p_m, "note": "保持稳定"}]
                     + _steps_cooldown(10))
            km, dur = 14, 80
            title = "马拉松配速课 5k+3k"
        else:
            cruise = [{"step_type": "active", "name": "巡航间歇 2km", "duration_type": "distance",
                       "duration_value": 2000, "target": p_t, "note": "T 配速上限"},
                      {"step_type": "rest", "name": "恢复", "duration_type": "time", "duration_value": 2,
                       "target": {"type": "none", "label": "慢跑"}}]
            steps = _steps_warmup(12) + [dict(s) for _ in range(3) for s in cruise] + _steps_cooldown(10)
            km, dur = 12, 65
            title = "巡航间歇 2km ×3"
    else:  # taper
        activation_block = [
            {"step_type": "active", "name": "马配激活", "duration_type": "distance", "duration_value": 2000,
             "target": p_m, "note": "找到比赛配速感觉"},
            {"step_type": "rest", "name": "恢复", "duration_type": "time", "duration_value": 3,
             "target": {"type": "none", "label": "慢跑"}},
            {"step_type": "active", "name": "200m 加速", "duration_type": "distance", "duration_value": 200,
             "target": p_r, "note": "提速但不力竭"},
        ]
        activation = [dict(s) for _ in range(3) for s in activation_block]
        steps = _steps_warmup(10) + activation + _steps_cooldown(8)
        km, dur = 8, 45
        title = "赛前激活课"
    return ("quality", title, steps, round(km, 1), dur)


def build_long_run(phase: str, week_vdot: float, distance_km: float, race_type: str) -> tuple[str, str, list[dict], float, float]:
    p_m = _pace_targets(week_vdot, "marathon")
    p_e = _pace_targets(week_vdot, "easy")
    if phase == "peak" and race_type == "marathon" and distance_km >= 26:
        # 末端马配长距离
        easy_km = round(distance_km * 0.7)
        mp_km = distance_km - easy_km
        steps = [
            {"step_type": "active", "name": "轻松跑", "duration_type": "distance", "duration_value": easy_km * 1000,
             "target": p_e, "note": "轻松完成前段"},
            {"step_type": "active", "name": "马配段落", "duration_type": "distance", "duration_value": mp_km * 1000,
             "target": p_m, "note": "比赛配速收尾，练疲劳下的马配"},
        ]
        title = f"长距离 {distance_km}km（末段马配 {mp_km}km）"
    else:
        steps = [{"step_type": "active", "name": "长距离轻松跑", "duration_type": "distance",
                  "duration_value": distance_km * 1000, "target": p_e,
                  "note": "全程轻松，可中途补水，专注有氧"}]
        title = f"长距离 {distance_km}km"
    dur = round(distance_km * (6.4 - min(1.0, week_vdot / 100)))  # 估算用时（分钟）
    return ("long", title, steps, distance_km, dur)


# 轻松跑时段封顶配速（min/km）："一个时长空档放得下多少公里"的全项目统一口径。
# 排课封顶（本文件 cap_km）、AI 加课/换轻松跑（ai_tools）、方法库体验周估算
# （method_library）都从这里引用。
SLOT_CAP_MIN_PER_KM = 6.5


def build_easy_run(vdot_value: float, distance_km: float, with_strides: bool = False) -> tuple[str, str, list[dict], float, float]:
    p_e = _pace_targets(vdot_value, "easy")
    steps = [{"step_type": "active", "name": "轻松跑", "duration_type": "distance", "duration_value": distance_km * 1000,
              "target": p_e, "note": "Z2 有氧区间，能完整对话"}]
    if with_strides:
        steps += [{"step_type": "active", "name": "快步跑", "duration_type": "distance", "duration_value": 100,
                   "target": _pace_targets(vdot_value, "repetition"), "note": "20 秒快步，放松快频"} for _ in range(4)]
    dur = distance_km * 6.6   # 分钟
    return ("easy", f"轻松跑 {distance_km}km" + (" + strides" if with_strides else ""), steps, distance_km, dur)


def build_strength_session(phase: str, beginner: bool = False) -> tuple[str, str, list[dict], float, float]:
    """下肢+核心力量课。

    beginner=True（跑量 <25km/周或训练年龄 <1 年）时降到 3 个动作 × 3 组：
    零基础跑者应先建立动作模式与肌腱耐受，原 6 动作约 20 组的方案
    （含 4×6 @75-80%1RM）对初跑者是单次过载，也是下肢伤病的常见来源。
    """
    if beginner:
        main = [("深蹲", "3×8 自重"), ("臀桥", "3×12"), ("平板支撑", "3×30s")]
    elif phase != "taper":
        main = [
            ("深蹲", "4×6 @75-80%1RM"), ("罗马尼亚硬拉", "4×8"), ("单腿提踵", "3×15/侧"),
            ("臀桥/臀推", "4×10"), ("平板支撑", "3×45s"), ("侧平板", "3×30s/侧"),
        ]
    else:
        main = [("保加利亚分腿蹲", "3×8/侧 轻负荷"), ("臀桥", "3×12"), ("平板支撑", "3×30s")]
    steps = [{"step_type": "warmup", "name": "动态热身", "duration_type": "time", "duration_value": 8,
              "target": {"type": "none", "label": "-"}, "note": "髋/踝灵活性 + 轻负荷激"}]
    for name, scheme in main:
        steps.append({"step_type": "strength", "name": f"{name} {scheme}", "duration_type": "time", "duration_value": 8,
                      "target": {"type": "none", "label": "-"}, "note": "组间休息 60-90s"})
    steps.append({"step_type": "cooldown", "name": "拉伸放松", "duration_type": "time", "duration_value": 6,
                  "target": {"type": "none", "label": "-"}, "note": "下肢静态拉伸"})
    return ("strength", "力量训练（下肢+核心）", steps, 0, 40 if beginner else 60)


def _alloc_phases(weeks: int) -> list[tuple[str, int]]:
    """按总周数比例分配相位（基础25% / 强化40% / 巅峰20% / 减量15%），保证总数一致。

    短周期（<8 周）时各阶段硬下限（基础≥2/强化≥3/减量≥1）会超发，需按实际周数收缩，
    避免"相位总周数 ≠ 计划总周数"导致切片时丢失减量期或出现负值。
    """
    weeks = max(4, weeks)
    taper = max(1, round(weeks * 0.15))
    remain = weeks - taper
    # 各阶段至少 1 周；剩余按比例分配（基础:强化:巅峰 ≈ 25%:40%:35%）
    base = max(1, round(remain * 0.25))
    build = max(1, round(remain * 0.40))
    peak = remain - base - build
    # 若巅峰期被挤没，从强化期匀出
    if peak < 1:
        build += peak - 1
        peak = 1
    if build < 1:
        base += build - 1
        build = 1
    # 兜底：确保三者总和恰好等于 remain（floor 冲突时削减最长的阶段）
    while base + build + peak > remain:
        if build > base:
            build -= 1
        elif base > peak:
            base -= 1
        else:
            peak -= 1
    return [("base", base), ("build", build), ("peak", peak), ("taper", taper)]


def _weekly_km_path(start_km: float, peak_km: float, weeks: int) -> list[float]:
    """渐进 + 每 4 周减量恢复（0.8×），最后一周减量；单周增幅封顶 10%。

    增幅钳制（progressive-overload 的通用安全上限）是防过度训练的关键：
    纯按 ``progress ** 0.9`` 插值时，若 start_km 与 peak_km 差距大，前几周会出现
    远超 10% 的跳增（例如 12km → 18km），是新手周计划最常见的致伤点。

    钳制基准 ``prev`` 只跟随「非减量周」推进：减量周是刻意下调，不应把
    「已达成负荷」拉低，否则减量周之后的一周会相对上一周实际跑量出现
    30% 级别的回跳（恢复周急着补量同样致伤）。恢复周的跑量因此受
    「减量前基准 ×1.10」约束，仍落在渐进路径上。
    """
    path = []
    prev = start_km
    for w in range(weeks):
        progress = w / max(1, weeks - 1)
        base = start_km + (peak_km - start_km) * (progress ** 0.9)
        base = min(base, prev * 1.10)         # 单周增幅 ≤10%
        is_cutback = (w + 1) % 4 == 0 and w < weeks - 2
        if not is_cutback:
            prev = base                       # 减量周不推进负荷基准
        if is_cutback:
            base *= 0.8                       # 减量周
        if w >= weeks - 2:                    # 赛前减量
            base *= 0.65 if w == weeks - 1 else 0.75
        path.append(round(base, 1))
    return path


def assess_feasibility(
    race_type: str,
    target_sec: float | None,
    current_vdot: float,
    talent_score: float,
    total_weeks: int,
    available_slots: list[dict],
) -> dict:
    """目标可行性预检：VDOT 差距 / 所需周数 / 长距离时段是否足够。供 generate_plan 与 AI 预检工具共用。"""
    feasibility = {"weeks": total_weeks}
    if target_sec:
        req = vdot.required_vdot(race_type, target_sec)
        gap = req - current_vdot
        rate = max(0.6, min(2.0, 1.6 * talent_score / 70))       # vdot/12周
        weeks_needed = math.ceil(gap / rate * 12) if gap > 0 else 0
        feasibility.update({
            "required_vdot": round(req, 1), "current_vdot": current_vdot,
            "gap": round(gap, 1), "estimated_weeks_needed": weeks_needed,
            "feasible": weeks_needed <= total_weeks,
            "note": (f"需要 VDOT {round(req,1)}，当前 {current_vdot}；按你的天赋响应速度预计需 {weeks_needed} 周"
                     + ("，在计划周期内可达成" if weeks_needed <= total_weeks else
                        f"，超出计划周期 {total_weeks} 周 —— 建议把目标日期后移或先设置阶段性目标")),
        })
    # 时间可行性：最长单次可用时长是否够长距离
    max_slot_min = max((s["duration_minutes"] for s in available_slots), default=0)
    need_long_min = round(RACE_KM[race_type] * 0.75 * 6.8)       # 峰值长距离约 75% 比赛距离
    feasibility["long_run_time"] = {
        "longest_available_slot_minutes": max_slot_min,
        "peak_long_run_needs_minutes": need_long_min,
        "ok": max_slot_min >= need_long_min * 0.85,
        "note": "" if max_slot_min >= need_long_min * 0.85
        else f"最长可用时段 {max_slot_min} 分钟不足以完成峰值长距离（约 {need_long_min} 分钟），"
             f"建议周末调整出 ≥{need_long_min} 分钟的连续时间，或采用双倍课（早晚各半）",
    }
    return feasibility


def generate_plan(
    athlete: dict,            # {id, sex, age, max_hr, resting_hr, weight_kg,...}
    goal: dict,               # {race_type, target_time_sec, target_label, target_date}
    current_vdot: float,
    talent_score: float,
    weekly_km_now: float,
    available_slots: list[dict],   # [{weekday, start_time, duration_minutes}]
) -> dict:
    """生成完整计划 dict（weeks + workouts），由路由层写库。"""
    race_type = goal["race_type"]
    target_sec = goal.get("target_time_sec")
    today = date.today()
    # 未指定开始日期时，从本周周一开始（周课表按周一~周日对齐）
    start = goal.get("start_date") or today
    start_date = start - timedelta(days=start.weekday())
    race_date = goal.get("target_date") or (start_date + timedelta(weeks=16))
    total_weeks = max(4, min(30, (race_date - start_date).days // 7))

    # ---------- 可行性 ----------
    if not available_slots:
        raise ValueError("请先在「日程管理」中添加至少一个可训练时段")
    feasibility = assess_feasibility(race_type, target_sec, current_vdot, talent_score,
                                     total_weeks, available_slots)

    # ---------- 跑量路径 ----------
    # 初跑者判定：周跑量 <25km 或训练年龄 <1 年（与 method_library.level_from_volume 同口径），
    # 用于力量课降档等保守化处理
    is_beginner = weekly_km_now < 25 or (athlete.get("training_age_years") or 0) < 1
    # 起始周跑量以「真实周跑量 × 0.8」为锚（留 20% 缓冲作为安全起点），最低 5km 兜底。
    # 关键：不要用 15km 这类绝对硬底——周跑量 5km 的初跑者会被一次放大到 3 倍，
    # 这是本生成器最主要的过载来源（配合下面的 10% 单周增幅钳制一起生效）。
    start_km = max(5.0, weekly_km_now * 0.8)
    peak_km = goal.get("weekly_km_peak") or max(
        start_km * 1.5, RACE_PEAK_KM[race_type] * (0.8 + 0.4 * talent_score / 100))
    peak_km = min(peak_km, start_km * PEAK_TO_START_CAP)          # 安全上限
    km_path = _weekly_km_path(start_km, peak_km, total_weeks)

    # ---------- 时间槽分配 ----------
    slots = sorted(available_slots, key=lambda s: -s["duration_minutes"])
    long_slot = slots[0]                                          # 最长时段 → 长距离
    quality_slots = [s for s in slots[1:] if s.get("kind", "available") == "available"][:2]
    easy_slots = [s for s in slots[1:] if s not in quality_slots]
    n_easy = len(easy_slots)
    # 空档 ≥6 时保留一个完全休息日（取最短的空档），休息优先于堆量
    rest_weekday = None
    if len(slots) >= 6 and easy_slots:
        rest_weekday = min(easy_slots, key=lambda s: s["duration_minutes"])["weekday"]

    phases = _alloc_phases(total_weeks)
    phase_seq = []
    for ph, n in phases:
        phase_seq += [ph] * n
    phase_seq = phase_seq[:total_weeks]

    weeks_out = []
    for w in range(total_weeks):
        phase = phase_seq[w]
        week_start = start_date + timedelta(weeks=w)
        # 目标 VDOT：有完赛目标用其反推，否则回退到当前能力 +3（显式分支，避免 and/or 短路歧义）
        target_vdot = (vdot.required_vdot(race_type, target_sec) if target_sec else current_vdot + 3)
        week_vdot = current_vdot + (min(current_vdot + 6, target_vdot) - current_vdot) * (w / max(1, total_weeks - 1))
        week_km = km_path[w]
        cutback = (w + 1) % 4 == 0 and w < total_weeks - 2

        # 每周课表
        workouts = []
        # 长距离占比
        long_share = 0.30 if race_type == "marathon" else 0.28
        long_km = round(min(week_km * long_share * (1.25 if phase == "peak" else 1.0),
                            {"marathon": 32, "hm": 22, "10k": 18, "5k": 14}[race_type] if phase != "taper" else 16), 1)
        if long_km < 8:
            long_km = min(8.0, week_km * 0.35)
        # 基础期不安排质量课：本阶段目标是打有氧底子，此期插强度是过度训练最常见的成因。
        # 原质量课时段改为「轻松跑 + strides」，既保留神经激活又不额外增加强度负荷。
        has_quality = phase in ("build", "peak")
        quality_km = week_km * 0.14 if has_quality else 0.0

        long_slot_day = long_slot["weekday"]
        quality_slot = quality_slots[0] if quality_slots else easy_slots[0] if easy_slots else long_slot
        # 基础期没有质量课，原质量课时段要计入轻松跑天数来分摊周跑量
        n_easy_eff = n_easy + (1 if (not has_quality and quality_slot) else 0)

        for slot in available_slots:
            wd = slot["weekday"]
            wdate = week_start + timedelta(days=wd)
            start_time = slot["start_time"]
            if slot.get("kind", "available") != "available":
                continue
            if wd == rest_weekday:
                continue
            if wd == long_slot_day:
                _, title, steps, km, dur = build_long_run(phase, week_vdot, long_km, race_type)
                tip = "今晚碳水加载（8g/kg），跑中每 45min 补 30g 碳水" if phase in ("build", "peak") else ""
                workouts.append(_mk(wdate, start_time, "long", title, steps, km, dur, tip))
            elif has_quality and quality_slot and wd == quality_slot["weekday"]:
                _, title, steps, km, dur = build_quality_session(phase, week_vdot, week_km, race_type)
                tip = "课前 2-3h 补碳水 1-1.5g/kg；课后 30min 内 蛋白 20-30g + 碳水"
                workouts.append(_mk(wdate, start_time, "quality", title, steps, km, dur, tip))
            elif w % 2 == 0 and wd == (quality_slots[1]["weekday"] if len(quality_slots) > 1 else -1) and phase in ("build", "peak"):
                _, title, steps, km, dur = build_strength_session(phase, beginner=is_beginner)
                tip = "力量课与质量课至少间隔 24h；课后补蛋白 25-30g"
                workouts.append(_mk(wdate, start_time, "strength", title, steps, 0, dur, tip))
            else:
                # 轻松跑单次距离：按「本周剩余跑量 ÷ 剩余次数」摊，并受可用时段时长封顶。
                # 这里**不能**设绝对下限（历史写的是 max(4.0, ...)）：周跑量 5km 的初跑者
                # 会被排出每节 ≥4km 的轻松跑，整周实际跑量远超计划跑量——这是最隐蔽的过载源。
                # 需要「每次别太短」时用相对下限（本人周跑量的 8%），量级随水平缩放。
                remain = max(0.0, (week_km - long_km - quality_km) / max(1, n_easy_eff + 1))
                remain = max(remain, week_km * 0.08)
                cap_km = min(remain, slot["duration_minutes"] / SLOT_CAP_MIN_PER_KM)   # 按可用时间封顶
                _, title, steps, km, dur = build_easy_run(week_vdot, round(cap_km, 1), with_strides=(phase == "base" and not cutback))
                workouts.append(_mk(wdate, start_time, "easy", title, steps, km, dur, ""))

        # 休息日提示
        phase_label, phase_note = PHASE_INFO[phase]
        note = phase_note + ("（减量恢复周）" if cutback else "")
        focus = {"base": "有氧底子", "build": "阈值/摄氧", "peak": "比赛专项", "taper": "恢复蓄力"}[phase]
        weeks_out.append({
            "week_index": w + 1, "start_date": week_start.isoformat(),
            "phase": phase, "phase_note": note, "focus": focus,
            "target_km": week_km if not cutback else week_km,
            "workouts": workouts,
        })

    name = goal.get("target_label") or f"{race_type.upper()} 目标计划"
    plan = {
        "name": f"{name} · {total_weeks} 周",
        "race_type": race_type,
        "target_time_sec": target_sec,
        "start_date": start_date.isoformat(),
        "race_date": race_date.isoformat(),
        "weekly_km_peak": round(peak_km, 1),
        "feasibility": feasibility,
        "weeks": weeks_out,
    }
    return plan


def _mk(wdate: date, start_time: str, stype: str, title: str, steps: list[dict],
        km: float, dur_min: float, tip: str) -> dict:
    return {
        "date": wdate.isoformat(), "start_time": start_time, "session_type": stype,
        "title": title, "structured": steps,
        "distance_km": round(km, 1), "duration_min": round(dur_min),
        "diet_tip": tip,
    }
