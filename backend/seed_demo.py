"""一键灌入演示数据：档案 + 27 周训练史（含分段/曲线/轨迹/动态/训练效果）+ 90 天身体数据 + 力量/饮食 + 计划 + 评估。

用法：cd backend && python seed_demo.py [--force]
默认在数据库已有训练数据时拒绝执行（防止覆盖真实数据），--force 强制重灌。
"""
from __future__ import annotations

import math
import random
import sys
from datetime import date, datetime, timedelta

from app import models
from app.data import build_evaluation
from app.db import SessionLocal, init_db
from sqlalchemy import select

rng = random.Random(42)
ATHLETE_ID = 1
START = datetime.now() - timedelta(days=27 * 7 + 2)

# 北京奥森公园一带（演示轨迹环绕点）
TRACK_CENTER = (40.012, 116.390)


def main(force: bool = False) -> None:
    init_db()
    db = SessionLocal()
    try:
        n_act = len(db.scalars(select(models.Activity.id)).all())
        if n_act and not force:
            print(f"数据库已有 {n_act} 条活动，跳过演示数据灌入（--force 可强制重灌）")
            return
        if n_act:
            _wipe(db)

        athlete = _seed_athlete(db)
        acts = _seed_activities(db)
        _seed_body_metrics(db)
        _seed_strength(db)
        _seed_diet(db)
        _seed_plan(db, athlete)
        ev = build_evaluation(db, athlete, with_persist=True)
        db.commit()
        print(f"✅ 演示数据就绪：{len(acts)} 条活动（分段/曲线/轨迹/动态/训练效果齐备）、"
              f"{len(db.scalars(select(models.BodyMetric.id)).all())} 天身体数据、"
              f"综合评估 {ev['total_score']} 分（{ev['grade']} 级）")
    finally:
        db.close()


def _wipe(db) -> None:
    for t in (models.Assessment, models.PlanWorkout, models.PlanWeek, models.TrainingPlan,
              models.DietLog, models.StrengthTest, models.BodyMetric, models.Activity,
              models.WeeklySlot, models.Goal, models.PlatformConnection, models.Athlete):
        for row in db.scalars(select(t)).all():
            db.delete(row)
        db.commit()


# ---------------------------------------------------------------- 档案

def _seed_athlete(db) -> models.Athlete:
    a = models.Athlete(
        name="林川", sex="male", birth_year=1996, height_cm=176, weight_kg=63.5,
        resting_hr=52, max_hr=192, hrv_baseline=62, training_age_years=3.0,
    )
    db.add(a)
    db.commit()
    g = models.Goal(athlete_id=ATHLETE_ID, race_type="marathon", target_time_sec=10740,
                    target_label="全马破三", target_date=date.today() + timedelta(days=190),
                    priority=1, status="active")
    db.add(g)
    for wd, st, mins, kind, label in [
        (1, "19:00", 75, "available", "周二 · 质量课时段"),
        (3, "12:30", 60, "available", "周三午间 · 轻松跑"),
        (5, "19:00", 60, "available", "周五 · 有氧"),
        (5, "19:00", 60, "busy", "周五晚 · 团建"),
        (6, "07:00", 180, "available", "周六晨 · 长距离"),
        (0, "07:00", 45, "available", "周一晨 · 力量"),
    ]:
        db.add(models.WeeklySlot(athlete_id=ATHLETE_ID, weekday=wd, start_time=st,
                                 duration_minutes=mins, kind=kind, label=label))
    # 平台连接不在此预置：真实连接只能由 OAuth 授权流程创建
    # （见 app/routers/connections.py）。
    db.commit()
    return a


# ---------------------------------------------------------------- 训练史
# 会话模板见 _seed_activities；马拉松基准配速 4:15/km，随周期线性改善


def _seed_activities(db) -> list[models.Activity]:
    weeks = 27
    out: list[models.Activity] = []
    race_date_10k = datetime.now() - timedelta(days=42)
    race_date_hm = datetime.now() - timedelta(days=84)
    trial_5k_date = datetime.now() - timedelta(days=21)

    for w in range(weeks):
        week_start = START + timedelta(weeks=w)
        # 周跑量爬坡 + 每 4 周减量
        base = 40 + (85 - 40) * (w / (weeks - 1)) ** 0.9
        if (w + 1) % 4 == 0:
            base *= 0.62
        target_km = base * rng.uniform(0.92, 1.08)
        mpace = 255 - 8 * w / (weeks - 1)   # 马拉松配速随周期改善（4:15 → 4:07）

        # (周几, 类型, 标题池, 距离占比, 相对马拉松配速因子, 平均心率范围)
        plan_sessions = [
            (1, "quality", ["间歇 800m×8", "节奏跑 6km", "亚索 800", "1000m×6 间歇", "阈值节奏 8km"], 0.16, 0.99, (166, 176)),
            (3, "easy", ["有氧跑", "轻松跑", "恢复跑", "午间慢跑"], 0.13, 1.22, (136, 146)),
            (4, "easy", ["轻松跑", "有氧跑"], 0.12, 1.20, (138, 148)),
            (5, "easy", ["周末放松跑", "短距离放松"], 0.10, 1.25, (134, 144)),
            (6, "long", ["长距离拉练", "长距离（含 3km 冲刺）", "长距离耐力"], 0.30, 1.26, (143, 153)),
        ]
        sessions = []
        for wd, kind, titles, share, pace_factor, hr_rng in plan_sessions:
            km = target_km * share
            if km < 5:
                continue
            sessions.append((wd, kind, rng.choice(titles), km, pace_factor, hr_rng))

        # 跳过部分课（模拟真实执行率）
        sessions = [s for s in sessions if rng.random() > 0.06]
        # 附加交叉训练
        extra = rng.random()
        if extra > 0.55:
            sessions.append((0, "strength", rng.choice(["核心力量", "下肢力量", "全身力量"]), 0, 0, (112, 128)))
        if extra > 0.72:
            sessions.append((2, "ride", rng.choice(["骑行通勤", "骑行有氧"]), 0, 0, (128, 145)))
        if extra > 0.82:
            sessions.append((2, "swim", rng.choice(["游泳 1.5km", "游泳有氧"]), 0, 0, (125, 140)))
        if rng.random() > 0.5:
            sessions.append((4, "strength", "跑者力量维护", 0, 0, (112, 128)))

        for wd, kind, title, km, pace_factor, hr_rng in sessions:
            start = (week_start + timedelta(days=wd)).replace(
                hour=7 if wd in (0, 5, 6) else 19,
                minute=rng.choice([0, 15, 30]), second=rng.randint(0, 59))
            act = _make_activity(kind, title, start, km, pace_factor, hr_rng, mpace, w)
            out.append(act)
            db.add(act)

    # 三场关键比赛/测试（供 VDOT/成绩预测；距离在 best_efforts 的 ±5% 带内）
    for start, title, km, vdot_ev in [
        (trial_5k_date.replace(hour=9, minute=0), "5km 测试赛", 5.0, 53.9),
        (race_date_10k.replace(hour=9, minute=0), "10k 城市联赛", 10.0, 53.8),
        (race_date_hm.replace(hour=8, minute=30), "半程马拉松测试", 21.0975, 53.5),
    ]:
        event = {"5km 测试赛": "5k", "10k 城市联赛": "10k", "半程马拉松测试": "hm"}[title]
        act = _make_activity("run", title, start, km, 1.0, (172, 180), mpace_now(vdot_ev, event),
                             -1, race=True)
        out.append(act)
        db.add(act)

    db.commit()
    return out


def mpace_now(vdot: float, event: str) -> float:
    """按 VDOT 给出对应事件的比赛配速（sec/km，对照 Daniels 表的线性近似）。"""
    anchors = {"5k": (52.0, 246, 55.0, 226), "10k": (52.0, 252, 55.0, 231),
               "hm": (52.0, 261, 55.0, 241)}
    v0, p0, v1, p1 = anchors[event]
    return p0 + (p1 - p0) * (vdot - v0) / (v1 - v0)


def _make_activity(kind, title, start, km, pace_factor, hr_rng, mpace, week_idx,
                   race: bool = False) -> models.Activity:
    # quality/easy/long 属跑步课，sport 统一为 run，保留 kind 供 TE/RPE 取值
    is_run = kind in ("run", "quality", "easy", "long")
    if is_run:
        pace = mpace * pace_factor * rng.uniform(0.99, 1.01)
        if race:
            pace = mpace  # 比赛按事件配速
        dist_m = km * 1000
        dur = int(dist_m / 1000 * pace)
        avg_hr = rng.randint(*hr_rng)
        max_hr = min(192, avg_hr + rng.randint(9, 16))
        elev = int(km * rng.uniform(1.2, 4.5))
        if "长距离" in title:
            elev = int(km * rng.uniform(2.5, 6.5))
        act = models.Activity(
            athlete_id=ATHLETE_ID, platform="manual", sport="run",
            title=f"{title}{'（比赛）' if race else ''}", start_time=start,
            duration_sec=dur, distance_m=round(dist_m), avg_hr=avg_hr, max_hr=max_hr,
            elevation_m=elev, avg_cadence=round(rng.uniform(166, 180) + (4 if pace_factor <= 1 else 0), 1),
            calories=int(km * rng.uniform(63, 68)), temp_c=_temp_of(start), weather=_weather_of(start),
            te_aerobic=round(_te(kind, race, aer=True), 1), te_anaerobic=round(_te(kind, race, aer=False), 1),
            rpe={"quality": 8, "long": 6}.get(kind, 9 if race else 4),
        )
        act.training_load = round(dur / 60 * avg_hr / 140)
        act.effort_score = round(dist_m / dur * 3.6, 2)
        act.dynamics = {
            "stride_m": round(rng.uniform(0.92, 1.12) + (0.08 if pace_factor <= 1.0 else 0), 2),
            "vosc_cm": round(rng.uniform(7.4, 9.6), 1),
            "gct_ms": round(rng.uniform(252, 294) - (18 if pace_factor <= 1.0 else 0)),
            "gct_balance_pct": round(rng.uniform(48.6, 51.4), 1),
        }
        act.raw = _run_raw(act, elev, race)
        return act

    if kind == "ride":
        hours = rng.uniform(0.9, 2.2)
        dist = hours * rng.uniform(24, 31)
        act = models.Activity(
            athlete_id=ATHLETE_ID, platform="manual", sport="ride", title=title,
            start_time=start.replace(hour=19), duration_sec=int(hours * 3600),
            distance_m=round(dist * 1000), avg_hr=rng.randint(128, 145), max_hr=rng.randint(150, 168),
            elevation_m=int(dist * rng.uniform(3, 7)), avg_cadence=round(rng.uniform(78, 90), 1),
            avg_power=round(rng.uniform(140, 205)), calories=int(hours * rng.uniform(480, 620)),
            temp_c=_temp_of(start), weather=_weather_of(start),
            te_aerobic=round(rng.uniform(2.2, 3.1), 1), te_anaerobic=round(rng.uniform(0.2, 0.6), 1), rpe=5,
        )
        act.training_load = round(act.duration_sec / 60 * act.avg_hr / 140)
        act.raw = {"detail": {"averageBikingCadenceInRevPerMinute": act.avg_cadence}}
        return act

    if kind == "swim":
        meters = rng.choice([1000, 1500, 1500, 2000])
        pace_100m = rng.uniform(115, 132)
        dur = int(meters / 100 * pace_100m)
        act = models.Activity(
            athlete_id=ATHLETE_ID, platform="manual", sport="swim", title=title,
            start_time=start.replace(hour=19), duration_sec=dur, distance_m=meters,
            avg_hr=rng.randint(125, 140), max_hr=rng.randint(142, 156),
            calories=int(meters / 100 * rng.uniform(22, 28)),
            te_aerobic=round(rng.uniform(2.0, 2.8), 1), te_anaerobic=round(rng.uniform(0.3, 0.8), 1), rpe=5,
        )
        act.training_load = round(dur / 60 * act.avg_hr / 140)
        act.raw = {"detail": {"avgStrokes": round(rng.uniform(14.5, 18.5), 1),
                              "avgSwolf": round(rng.uniform(33, 44), 1), "poolLength": 50}}
        return act

    # strength
    mins = rng.randint(40, 60)
    act = models.Activity(
        athlete_id=ATHLETE_ID, platform="manual", sport="strength", title=title,
        start_time=start.replace(hour=7, minute=30), duration_sec=mins * 60,
        avg_hr=rng.randint(112, 128), max_hr=rng.randint(130, 146),
        calories=int(mins * rng.uniform(4.4, 6.2)), te_aerobic=round(rng.uniform(1.2, 2.0), 1),
        te_anaerobic=round(rng.uniform(1.6, 2.6), 1), rpe=6,
    )
    act.training_load = round(mins * act.avg_hr / 140)
    return act


def _te(kind: str, race: bool, aer: bool) -> float:
    if race:
        return rng.uniform(3.8, 4.6) if aer else rng.uniform(1.8, 2.6)
    base = {"quality": (3.4, 1.9), "long": (3.0, 1.0), "easy": (2.4, 0.35)}.get(kind, (2.4, 0.35))
    lo, hi = (base[0], base[0] + 0.9) if aer else (base[1], base[1] + 0.5)
    return rng.uniform(lo, hi)


def _run_raw(act: models.Activity, elev_total: int, race: bool) -> dict:
    """生成 raw：整公里分段（设备格式）+ 每公里曲线点 + 环形轨迹。"""
    dist_m, dur = act.distance_m, act.duration_sec
    pace_avg = dur / (dist_m / 1000)
    n_full = int(dist_m // 1000)
    rem = dist_m - n_full * 1000
    segs = [1000.0] * n_full + ([rem] if rem > 50 else [])

    noise = [1 + rng.uniform(-0.028, 0.028) for _ in segs]
    if race:  # 比赛前后半程略加速
        noise = [1 - 0.02 * (i / max(1, len(noise) - 1) - 0.5) for i in range(len(noise))]
    k = dur / sum(1000 * pace_avg * x for x in noise)

    laps, points = [], []
    t, alt, hr = 0, 38.0 + rng.uniform(-8, 10), float(act.avg_hr or 145)
    for i, seg in enumerate(segs):
        seg_t = 1000 * pace_avg * noise[i] * k if seg == 1000 else dur - t
        t += seg_t
        gain = elev_total / len(segs) * rng.uniform(0.3, 1.7)
        alt += gain - rng.uniform(0, 5)
        hr += rng.uniform(-2.2, 3.2)
        hr = max(118, min(act.max_hr - 3, hr))
        laps.append({"distance": round(seg, 1), "duration": round(seg_t),
                     "averageHR": int(round(hr)), "elevationGain": round(gain, 1)})
        points.append({"km": round((i + 1) * seg / 1000, 2), "time_sec": round(t),
                       "pace_sec_per_km": round(seg_t / (seg / 1000)),
                       "hr": int(round(hr)), "altitude_m": round(alt, 1)})

    track = _loop_track(int(len(segs) * 1.6) + 40)
    return {"kind": "race" if race else "", "splits": laps,
            "series": {"points": points}, "track": track}


def _loop_track(n: int) -> list[list[float]]:
    lat0, lng0 = TRACK_CENTER
    pts = []
    for i in range(n):
        ang = 2 * math.pi * i / n
        r = rng.uniform(0.85, 1.15)
        pts.append([round(lat0 + 0.004 * r * math.cos(ang) + rng.uniform(-3e-5, 3e-5), 5),
                    round(lng0 + 0.0052 * r * math.sin(ang) + rng.uniform(-3e-5, 3e-5), 5)])
    pts.append(pts[0])
    return pts


def _temp_of(start: datetime) -> float:
    month = start.month
    return round(13 + 13 * math.sin((month - 3.2) / 12 * 2 * math.pi) + rng.uniform(-3, 3), 1)


def _weather_of(start: datetime) -> str:
    return rng.choices(["晴", "多云", "阴", "小雨", "雾"], weights=[5, 4, 3, 1, 0.5])[0]


# ---------------------------------------------------------------- 身体数据

def _seed_body_metrics(db) -> None:
    days = 183
    weight, fat, hrv_base, rhr_base = 64.9, 16.8, 62.0, 55.0
    for i in range(days, -1, -1):
        d = date.today() - timedelta(days=i)
        dow = d.weekday()
        progress = (days - i) / days
        weight -= 0.0093 * 1  # 线性减重 64.9 → 63.2
        fat = 16.8 - 1.6 * progress
        rhr_base = 55 - 2.6 * progress
        # 周日长距离后 HRV 下探、静息心率上浮
        weekend_effect = -5.5 if dow == 1 and rng.random() > 0.35 else 0.0
        hrv = hrv_base + 4 * progress + 3.2 * math.sin(i / 7 * 2 * math.pi) + rng.uniform(-3.2, 3.2) + weekend_effect
        rhr = rhr_base + rng.uniform(-1.5, 1.5) - weekend_effect * 0.22
        sleep = rng.uniform(6.4, 8.6) - (0.7 if rng.random() > 0.9 else 0)
        score = int(max(45, min(98, 40 + sleep * 6.6 + rng.uniform(-8, 8))))
        db.add(models.BodyMetric(
            athlete_id=ATHLETE_ID, date=d, weight_kg=round(weight + rng.uniform(-0.4, 0.4), 1),
            resting_hr=int(round(rhr)), hrv_rmssd=round(hrv, 1), sleep_hours=round(sleep, 2),
            sleep_score=score, body_fat_pct=round(fat + rng.uniform(-0.3, 0.3), 1),
            spo2=round(rng.uniform(96.2, 99.0), 1), resp_rate=round(rng.uniform(13.1, 15.9), 1),
            stress=int(rng.uniform(24, 52) + (6 if dow in (2, 3) else 0)),
            body_battery=int(max(30, min(98, 60 + hrv - hrv_base - rng.uniform(-18, 18)))),
        ))
    db.commit()


# ---------------------------------------------------------------- 力量 / 饮食

def _seed_strength(db) -> None:
    plans = [("深蹲", [100, 105, 110]), ("硬拉", [140, 145, 150]), ("卧推", [65, 68, 70]),
             ("站姿推举", [45, 47.5, 50]), ("杠铃划船", [60, 62.5, 65]), ("臀推", [120, 130, 140])]
    offsets = [timedelta(weeks=16), timedelta(weeks=8), timedelta(days=10)]
    for name, vals in plans:
        for off, val in zip(offsets, vals, strict=True):
            db.add(models.StrengthTest(athlete_id=ATHLETE_ID, exercise=name, best_weight_kg=val,
                                       reps=1, bodyweight_kg=63.5,
                                       date=date.today() - off,
                                       notes="1RM 折算"))
    for i in range(6):
        db.add(models.StrengthTest(athlete_id=ATHLETE_ID, exercise="引体向上", best_weight_kg=5 + i,
                                   reps=5, bodyweight_kg=63.5, date=date.today() - timedelta(weeks=14 - i * 2),
                                   notes="负重 5 次"))
    db.commit()


def _seed_diet(db) -> None:
    meals = [
        ("breakfast", ["燕麦+鸡蛋+牛奶", "全麦面包+咖啡+香蕉", "包子+豆浆+鸡蛋"], 480, 22, 62, 15),
        ("lunch", ["鸡胸肉糙米饭+西兰花", "牛肉面+卤蛋+青菜", "三文鱼藜麦沙拉"], 720, 42, 78, 20),
        ("dinner", ["瘦牛肉+红薯+蔬菜", "清蒸鱼+米饭+汤", "虾仁荞麦面"], 650, 40, 68, 16),
        ("snack", ["酸奶+坚果", "能量胶（长距离中）", "蛋白粉+香蕉"], 220, 12, 24, 8),
    ]
    for i in range(15):
        d = date.today() - timedelta(days=i)
        picks = rng.sample(meals, rng.choice([3, 3, 4]))
        for meal, contents, kcal, p, c, f in picks:
            factor = rng.uniform(0.85, 1.2)
            db.add(models.DietLog(
                athlete_id=ATHLETE_ID, date=d, meal=meal, description=rng.choice(contents),
                kcal=round(kcal * factor), protein_g=round(p * factor), carb_g=round(c * factor),
                fat_g=round(f * factor)))
    db.commit()


# ---------------------------------------------------------------- 计划

def _seed_plan(db, athlete: models.Athlete) -> None:
    """直接调用计划引擎：27 周全马破三课表（14 周已过去），并回填历史完成状态。"""
    from app.services import planner

    slots = db.scalars(select(models.WeeklySlot).where(
        models.WeeklySlot.athlete_id == ATHLETE_ID,
        models.WeeklySlot.kind == "available")).all()
    slots_d = [{"weekday": s.weekday, "start_time": s.start_time, "duration_minutes": s.duration_minutes}
               for s in slots]

    from app.data import athlete_dict, build_prediction
    pred = build_prediction(db, ATHLETE_ID)
    ev = build_evaluation(db, athlete)
    talent = (ev["dimensions"].get("talent") or {}).get("score")

    goal = db.scalar(select(models.Goal).where(models.Goal.athlete_id == ATHLETE_ID))
    plan = planner.generate_plan(
        athlete_dict(athlete) | {"id": ATHLETE_ID},
        {"race_type": "marathon", "target_time_sec": 10740, "target_label": "全马破三",
         "target_date": date.today() + timedelta(days=91),
         "start_date": date.today() - timedelta(days=98)},
        current_vdot=pred.current_vdot, talent_score=talent,
        weekly_km_now=ev["weekly_km_avg"] or 40,
        available_slots=slots_d,
    )

    row = models.TrainingPlan(
        athlete_id=ATHLETE_ID, goal_id=goal.id, name=plan["name"], race_type=plan["race_type"],
        target_time_sec=plan["target_time_sec"],
        start_date=date.fromisoformat(plan["start_date"]), race_date=date.fromisoformat(plan["race_date"]),
        weekly_km_peak=plan["weekly_km_peak"], feasibility=plan["feasibility"],
    )
    db.add(row)
    db.flush()
    for w in plan["weeks"]:
        pw = models.PlanWeek(plan_id=row.id, week_index=w["week_index"],
                             start_date=date.fromisoformat(w["start_date"]),
                             phase=w["phase"], phase_note=w["phase_note"], focus=w["focus"],
                             target_km=w["target_km"])
        db.add(pw)
        db.flush()
        for wo in w["workouts"]:
            db.add(models.PlanWorkout(
                week_id=pw.id, athlete_id=ATHLETE_ID, date=date.fromisoformat(wo["date"]),
                start_time=wo["start_time"], session_type=wo["session_type"], title=wo["title"],
                description=wo.get("description", ""), distance_km=wo["distance_km"],
                duration_min=wo["duration_min"], structured=wo["structured"],
                diet_tip=wo.get("diet_tip", "")))
    db.commit()

    # 已过去的课按 85% 执行率回填完成状态（供意志品质/执行率评估）
    today = date.today()
    for wo in db.scalars(select(models.PlanWorkout).join(models.PlanWeek)
                         .where(models.PlanWeek.plan_id == row.id)).all():
        if wo.date < today and rng.random() < 0.85:
            wo.status = "completed"
    db.commit()


if __name__ == "__main__":
    main(force="--force" in sys.argv)
