"""数据装配层：把 ORM 对象整理成服务层所需的 dict 形态，并组装复合计算。"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .models import utcnow_naive
from .services import evaluator, predictor, vdot, zones
from .services import strength as strength_svc
from .services.vocab import QUALITY_SESSION_TYPES


def get_default_athlete(db: Session) -> models.Athlete | None:
    """当前跑者档案（统一查询口径）。

    读路径即刷新画像：静息心率/体重/最大心率/训练年限等可解析字段会按最新训练与
    体测数据重算并回写到该 ORM 对象，因此下游 30+ 处 ``athlete.max_hr`` 之类的
    直接读取拿到的都是「按本人数据实时解析」的值，而不是建库时写死的那一组。

    这里不 commit：读取接口不该替调用方提交事务；值在本请求内已生效，
    需要落库的路径（档案读写、同步）会显式 commit。
    """
    a = db.scalar(select(models.Athlete).order_by(models.Athlete.id).limit(1))
    if a is not None:
        from .services import profile
        profile.refresh(db, a, commit=False)
    return a


def active_plan(db: Session) -> models.TrainingPlan | None:
    """当前有效训练计划（统一查询口径）。"""
    return db.scalar(select(models.TrainingPlan).where(models.TrainingPlan.status == "active")
                     .order_by(models.TrainingPlan.id.desc()))


def archive_plan(db: Session, plan: models.TrainingPlan) -> None:
    """归档计划，并清掉它「尚未执行」的课次（不 commit，由调用方统一提交）。

    为什么不能只改 status：总览页「今日训练」、打卡建议、饮食日型、执行率等链路
    都是按 athlete_id 查 plan_workouts（不限定 active 计划），归档计划里遗留的
    planned 课次会一并被查出来——表现为同一天出现多节重复课、「今天显示的课不是
    今天的」、执行率分母被旧计划虚高。作废计划的未执行课不再有效，直接删除；
    已完成的课次是历史执行记录，必须保留。
    """
    from sqlalchemy import delete

    plan.status = "archived"
    week_ids = select(models.PlanWeek.id).where(models.PlanWeek.plan_id == plan.id)
    db.execute(delete(models.PlanWorkout).where(
        models.PlanWorkout.week_id.in_(week_ids),
        models.PlanWorkout.status == "planned"))


def planned_workouts(db: Session, plan_id: int) -> tuple[list[dict], date | None]:
    """计划的未来课表（今天起）与首个比赛日，供前瞻负荷推演。"""
    rows = db.execute(
        select(models.PlanWorkout, models.TrainingPlan.race_date)
        .join(models.PlanWeek, models.PlanWorkout.week_id == models.PlanWeek.id)
        .join(models.TrainingPlan, models.PlanWeek.plan_id == models.TrainingPlan.id)
        .where(models.TrainingPlan.id == plan_id, models.PlanWorkout.date >= date.today())
        .order_by(models.PlanWorkout.date)).all()
    planned = [{"date": wo.date, "session_type": wo.session_type, "duration_min": wo.duration_min,
                "distance_km": wo.distance_km, "title": wo.title} for wo, _ in rows]
    race_date = next((rd for _, rd in rows if rd), None)
    return planned, race_date


def build_training_status_for(db: Session, athlete: models.Athlete,
                              acts: list[dict] | None = None) -> tuple[dict, list[dict]]:
    """训练状态 payload 的统一拼装入口（路由与 AI 工具共用，避免四处复制四件套）。

    返回 (payload, acts)：payload 已附带 vdot_trend；acts 回传给需要复用查询结果的调用方。
    """
    from .services import load_status

    acts = activities_dicts(db, athlete.id, days=200) if acts is None else acts
    metrics = body_metrics_dicts(db, athlete.id, days=60)
    ad = {"max_hr": athlete.max_hr, "resting_hr": athlete.resting_hr,
          "hrv_baseline": athlete.hrv_baseline, "weight_kg": athlete.weight_kg}
    plan = active_plan(db)
    trend = evaluator.vdot_trend(acts, date.today().year - athlete.birth_year, athlete.sex)
    payload = load_status.build_training_status(
        acts, ad, metrics, race_date=plan.race_date if plan else None, vdot_trend=trend)
    payload["vdot_trend"] = trend
    # 客观数据基线（28 天中位数 + 波动带 + 异常/漂移判定），展示与 AI 共用同一份
    from .services.baseline import build_baseline
    payload["baselines"] = build_baseline(metrics)
    return payload, acts


def athlete_dict(a: models.Athlete) -> dict:
    return {
        "id": a.id, "name": a.name, "sex": a.sex, "birth_year": a.birth_year,
        "age": date.today().year - a.birth_year,
        "height_cm": a.height_cm, "weight_kg": a.weight_kg,
        "resting_hr": a.resting_hr, "max_hr": a.max_hr, "hrv_baseline": a.hrv_baseline,
        "training_age_years": a.training_age_years,
    }


def activities_dicts(db: Session, athlete_id: int, days: int = 190,
                     include_raw: bool = False) -> list[dict]:
    since = datetime.now() - timedelta(days=days)
    rows = db.scalars(
        select(models.Activity)
        .where(models.Activity.athlete_id == athlete_id, models.Activity.start_time >= since)
        .order_by(models.Activity.start_time)
    ).all()
    out = [{
        "id": r.id, "sport": r.sport, "title": r.title, "platform": r.platform,
        "start_time": r.start_time, "duration_sec": r.duration_sec,
        "distance_m": r.distance_m, "avg_hr": r.avg_hr, "max_hr": r.max_hr,
        "avg_cadence": r.avg_cadence, "avg_power": r.avg_power,
        "training_load": r.training_load,
        "effort_score": r.effort_score, "elevation_m": r.elevation_m or 0,
        "calories": r.calories,
        "kind": (r.raw or {}).get("kind", ""),
    } for r in rows]
    if include_raw:
        # pro_insights 等需要设备明细（分段/心率区间/数据流/跑姿）的引擎专用；
        # 默认不带上，避免大 JSON 进常规链路白耗内存
        for d, r in zip(out, rows, strict=True):
            d["raw"] = r.raw or {}
            d["dynamics"] = r.dynamics or {}
    return out


def body_metrics_dicts(db: Session, athlete_id: int, days: int = 90) -> list[dict]:
    since = date.today() - timedelta(days=days)
    rows = db.scalars(
        select(models.BodyMetric)
        .where(models.BodyMetric.athlete_id == athlete_id, models.BodyMetric.date >= since)
        .order_by(models.BodyMetric.date)
    ).all()
    return [{"date": r.date, "weight_kg": r.weight_kg, "resting_hr": r.resting_hr,
             "hrv_rmssd": r.hrv_rmssd, "sleep_hours": r.sleep_hours, "body_fat_pct": r.body_fat_pct,
             "sleep_score": r.sleep_score, "spo2": r.spo2, "resp_rate": r.resp_rate,
             "stress": r.stress, "body_battery": r.body_battery,
             "recovery_pct": r.recovery_pct, "recovery_level": r.recovery_level,
             "recovery_hours": r.recovery_hours}
            for r in rows]


def strength_tests_dicts(db: Session, athlete_id: int) -> list[dict]:
    rows = db.scalars(
        select(models.StrengthTest).where(models.StrengthTest.athlete_id == athlete_id)
        .order_by(models.StrengthTest.date)
    ).all()
    return [{"exercise": r.exercise, "best_weight_kg": r.best_weight_kg, "reps": r.reps,
             "date": r.date.isoformat(), "bodyweight_kg": r.bodyweight_kg} for r in rows]


def plan_adherence_dict(db: Session, athlete_id: int) -> dict | None:
    """近 8 周「已到期」计划课的完成率（未来课程不计入）。"""
    today = date.today()
    since = today - timedelta(weeks=8)
    rows = db.scalars(
        select(models.PlanWorkout)
        .where(models.PlanWorkout.athlete_id == athlete_id,
               models.PlanWorkout.date >= since,
               models.PlanWorkout.date <= today,
               # 二代词汇（interval/tempo/fartlek/hill/race，方法库落库计划用）
               # 必须计入，否则方法库计划的执行率恒偏低并拉低意志品质分
               models.PlanWorkout.session_type.in_(
                   ["easy", "long", "strength", "cross"] + sorted(QUALITY_SESSION_TYPES)))
    ).all()
    if not rows:
        return None
    done = sum(1 for r in rows if r.status in ("completed", "synced"))
    return {"total": len(rows), "completed": done}


def best_efforts(db: Session, athlete_id: int) -> list[predictor.BestEffort]:
    """各标准距离的最好努力（同距离取最快）：训练活动 + 实测比赛成绩合并，
    比赛成绩是全力实测，同距离上若更快则以比赛为准。"""
    acts = activities_dicts(db, athlete_id, days=120)
    runs = [a for a in acts if a["sport"] == "run" and a["distance_m"] >= 800 and a["duration_sec"] > 120]
    best: dict[str, dict] = {}   # key -> {"distance_m", "time_sec", "date", "source"}
    for key, dist in vdot.RACE_DISTANCES.items():
        lo, hi = dist * 0.95, dist * 1.05
        cands = [a for a in runs if lo <= a["distance_m"] <= hi]
        if cands:
            a = min(cands, key=lambda x: x["duration_sec"] / x["distance_m"])  # 最快配速
            best[key] = {"distance_m": a["distance_m"], "time_sec": a["duration_sec"],
                         "date": a["start_time"].date().isoformat()}
    for r in race_results(db, athlete_id):
        for key, dist in vdot.RACE_DISTANCES.items():
            if dist * 0.95 <= r.distance_m <= dist * 1.05 and \
                    (key not in best or r.time_sec < best[key]["time_sec"]):
                best[key] = {"distance_m": r.distance_m, "time_sec": r.time_sec,
                             "date": r.date.isoformat()}
    return [predictor.BestEffort(
        distance_m=best[k]["distance_m"], time_sec=best[k]["time_sec"],
        date=best[k]["date"], label=k,
    ) for k in best]


def race_results(db: Session, athlete_id: int) -> list[models.RaceResult]:
    return db.scalars(
        select(models.RaceResult).where(models.RaceResult.athlete_id == athlete_id)
        .order_by(models.RaceResult.date)
    ).all()


def riegel_calibration(db: Session, athlete_id: int) -> dict | None:
    """由实测比赛成绩反推个人 Riegel 指数（≥2 场不同距离且相隔 ≤90 天才有效）。"""
    rows = race_results(db, athlete_id)
    efforts = [predictor.BestEffort(distance_m=r.distance_m, time_sec=r.time_sec,
                                    date=r.date.isoformat(), label=r.race_type) for r in rows]
    return predictor.calibrate_riegel(efforts)


def build_prediction(db: Session, athlete_id: int) -> predictor.PredictionResult:
    efforts = best_efforts(db, athlete_id)
    calib = riegel_calibration(db, athlete_id)
    pred = predictor.predict_performances(efforts, riegel_exp=calib["exponent"] if calib else None)
    if calib:
        pred.quality["riegel_pairs"] = calib["pairs"]
    return pred


def snapshot_race_predictions(db: Session, athlete_id: int,
                              pred: predictor.PredictionResult) -> int:
    """把当前预测按未来 90 天内的目标比赛留档（同场同日一条，幂等），自提交。

    预测只有被用户看到才有复盘价值，故挂在展示入口而非每个 build_prediction
    调用点。独立小事务：请求 session 此前无未提交变更（两处展示入口都是
    纯读后调用），快照写失败也不该影响预测展示本身。
    """
    from .services.vdot import RACE_DISTANCES

    today = date.today()
    goals = db.scalars(select(models.Goal).where(
        models.Goal.athlete_id == athlete_id, models.Goal.status == "active",
        models.Goal.target_date > today,
        models.Goal.target_date <= today + timedelta(days=90))).all()
    added = 0
    for g in goals:
        p = (pred.predictions or {}).get(g.race_type)
        if not p:
            continue
        dist = RACE_DISTANCES.get(g.race_type)
        if dist is None:
            continue
        dup = db.scalar(select(models.RacePrediction).where(
            models.RacePrediction.athlete_id == athlete_id,
            models.RacePrediction.race_date == g.target_date,
            models.RacePrediction.distance_m == dist,
            models.RacePrediction.predicted_on == today))
        if dup:
            continue
        db.add(models.RacePrediction(
            athlete_id=athlete_id, race_date=g.target_date, race_type=g.race_type,
            race_name=g.target_label or g.race_type, distance_m=dist,
            predicted_sec=p["time_sec"], predicted_on=today))
        added += 1
    if added:
        db.commit()
    return added


def backfill_race_prediction(db: Session, athlete_id: int, race: models.RaceResult) -> bool:
    """成绩录入/修正后回填赛前快照：delta_pct = (实测 − 预测) / 预测 × 100。

    正值 = 比预测慢。取比赛日当天或之前最近的一条快照（最贴近赛前状态的预测）；
    无快照返回 False，不构造凭空的对比。
    """
    row = db.scalar(select(models.RacePrediction).where(
        models.RacePrediction.athlete_id == athlete_id,
        models.RacePrediction.race_date == race.date,
        models.RacePrediction.distance_m >= race.distance_m * 0.95,
        models.RacePrediction.distance_m <= race.distance_m * 1.05,
        models.RacePrediction.predicted_on <= race.date)
        .order_by(models.RacePrediction.predicted_on.desc()))
    if not row:
        return False
    row.actual_sec = race.time_sec
    row.delta_pct = round((race.time_sec - row.predicted_sec) / row.predicted_sec * 100, 1)
    return True


def race_prediction_review(db: Session, athlete_id: int) -> dict[tuple, dict]:
    """已回填的偏差复盘，按 (date, distance_m 归档) 供比赛列表/AI 附挂。

    键用 (ISO 日期, 官方距离标签) 近似匹配——与 best_efforts 的 ±5% 归档口径一致。
    """
    rows = db.scalars(select(models.RacePrediction).where(
        models.RacePrediction.athlete_id == athlete_id,
        models.RacePrediction.actual_sec.is_not(None))).all()
    out: dict[tuple, dict] = {}
    for r in rows:
        for key, dist in vdot.RACE_DISTANCES.items():
            if dist * 0.95 <= r.distance_m <= dist * 1.05:
                out[(r.race_date.isoformat(), key)] = {
                    "predicted_str": vdot.time_str(r.predicted_sec),
                    "predicted_sec": r.predicted_sec,
                    "predicted_on": r.predicted_on.isoformat(),
                    "delta_pct": r.delta_pct,
                }
    return out


def weekly_km(activities: list[dict], weeks: int = 4) -> float:
    series = evaluator.weekly_km_series(activities, weeks)
    vals = [k for k in series if k > 0]
    return sum(vals) / max(1, len(vals))


def _priority_session_type(types: list[str]) -> str | None:
    """同一天多节课时按最「贵」的课取营养日型（质量课 > 长距离 > 其他 > 休息）。

    质量课集合必须引用 vocab.QUALITY_SESSION_TYPES，不得内联复刻：
    此前内联元组漏了二代词汇 fartlek/hill，fartlek+long 同日会被错按长距离取日型。
    """
    for st in types:
        if st in QUALITY_SESSION_TYPES:
            return st
    if "long" in types:
        return "long"
    for st in types:
        if st != "rest":
            return st
    return types[0] if types else None


def diet_analysis_payload(db: Session, athlete: models.Athlete) -> dict:
    """每日营养目标（按当天计划课型动态取值）+ 近 14 天习惯分析 + 能量平衡与补给状态。"""
    from .services import diet as diet_svc

    today = date.today()
    since = today - timedelta(days=13)
    acts = activities_dicts(db, athlete.id, days=28)
    wk = weekly_km(acts)
    ad = athlete_dict(athlete)

    # 近 14 天计划课型分布：营养日型跟随课表（质量课多碳水、休息日控碳）
    wo_rows = db.scalars(select(models.PlanWorkout).where(
        models.PlanWorkout.athlete_id == athlete.id,
        models.PlanWorkout.date >= since, models.PlanWorkout.date <= today,
        models.PlanWorkout.status == "planned")).all()
    types_by_date: dict[date, list[str]] = {}
    title_by_date: dict[date, str] = {}
    for w in wo_rows:
        types_by_date.setdefault(w.date, []).append(w.session_type)
        title_by_date.setdefault(w.date, w.title)

    def _day_type(d: date) -> str:
        return diet_svc.day_type_for(_priority_session_type(types_by_date.get(d) or []))

    def _day_targets(d: date) -> dict:
        return diet_svc.daily_targets(athlete.weight_kg, athlete.height_cm,
                                      ad["age"], athlete.sex, wk, _day_type(d))

    today_session = _priority_session_type(types_by_date.get(today) or [])
    targets = _day_targets(today)

    logs = db.scalars(
        select(models.DietLog)
        .where(models.DietLog.athlete_id == athlete.id, models.DietLog.date >= since)
        .order_by(models.DietLog.date)
    ).all()
    logs_d = [{"date": r.date.isoformat(), "kcal": r.kcal, "protein_g": r.protein_g,
               "carb_g": r.carb_g, "fat_g": r.fat_g, "meal": r.meal} for r in logs]

    # 逐日摄入；运动消耗优先取设备 calories，缺失的跑步按净耗能 ≈1 kcal/kg/km 估算
    intake_by_date: dict[date, dict] = {}
    for r in logs:
        agg = intake_by_date.setdefault(r.date, {"kcal": 0.0, "carb": 0.0})
        agg["kcal"] += r.kcal or 0
        agg["carb"] += r.carb_g or 0
    burn_by_date: dict[date, float] = {}
    for a in acts:
        d = a["start_time"].date()
        if d < since or d > today:
            continue
        kcal = float(a.get("calories") or 0)
        if not kcal and a.get("sport") == "run" and athlete.weight_kg and a.get("distance_m"):
            kcal = a["distance_m"] / 1000 * athlete.weight_kg
        if kcal:
            burn_by_date[d] = burn_by_date.get(d, 0) + kcal

    day_targets: dict[str, dict] = {}
    daily: list[dict] = []
    for i in range(14):
        d = since + timedelta(days=i)
        t = _day_targets(d)
        day_targets[d.isoformat()] = t
        intake = intake_by_date.get(d) or {}
        daily.append({"date": d.isoformat(), "day_type": _day_type(d),
                      "intake_kcal": intake.get("kcal", 0), "carb_g": intake.get("carb", 0),
                      "target_kcal": t["kcal"], "burned_kcal": round(burn_by_date.get(d, 0))})

    analysis = diet_svc.analyze_diet(logs_d, targets, day_targets)
    balance = diet_svc.energy_balance(daily)
    # 当天未结束，补给判定只看昨天及以前
    fueling = diet_svc.fueling_status([d for d in daily if d["date"] != today.isoformat()],
                                      diet_svc.day_type_for(today_session), athlete.weight_kg)

    return {"targets": targets, "analysis": analysis, "balance": balance, "fueling": fueling,
            "today": {"date": today.isoformat(), "day_type": _day_type(today),
                      "session_type": today_session, "workout_title": title_by_date.get(today, "")},
            "logs": [
        {"id": r.id, "date": r.date.isoformat(), "meal": r.meal, "description": r.description,
         "kcal": r.kcal, "protein_g": r.protein_g, "carb_g": r.carb_g, "fat_g": r.fat_g} for r in logs]}


def runner_type_payload(db: Session, athlete: models.Athlete,
                        pred: predictor.PredictionResult | None = None,
                        acts: list[dict] | None = None) -> dict:
    """跑者类型判定（项目倾向 × 水平阶段 × 训练风格）。

    pred / acts 可传入调用方已算好的结果：dashboard、athletes 都在同一请求里
    先算过 190 天活动与预测，复用可省掉一次全量重算（跑量窗口按当前时间
    倒推聚合，传更长的活动列表结果一致）。
    """
    from .services import runner_type as runner_type_svc

    pred = pred if pred is not None else build_prediction(db, athlete.id)
    acts = acts if acts is not None else activities_dicts(db, athlete.id, days=28)
    goal = db.scalar(select(models.Goal).where(models.Goal.athlete_id == athlete.id,
                                               models.Goal.status == "active"))
    return runner_type_svc.classify_runner(
        athlete_dict(athlete), pred, weekly_km(acts), has_goal=goal is not None)


def build_evaluation(db: Session, athlete: models.Athlete, with_persist: bool = False) -> dict:
    """完整多维评估（有氧/力量/意志/恢复/天赋）+ 短板 + 预测 + 生涯上限。"""
    ad = athlete_dict(athlete)
    acts = activities_dicts(db, athlete.id)
    metrics = body_metrics_dicts(db, athlete.id)
    pred = build_prediction(db, athlete.id)
    s_tests = strength_tests_dicts(db, athlete.id)
    strength_analysis = strength_svc.assess_strength(s_tests, athlete.sex, athlete.weight_kg)

    adherence = plan_adherence_dict(db, athlete.id)
    will = evaluator.eval_willpower(acts, adherence)
    aero = evaluator.eval_aerobic(pred, ad["age"], athlete.sex, acts)
    rec = evaluator.eval_recovery(metrics, acts)
    talent = evaluator.eval_talent(pred, acts, ad["age"], athlete.sex,
                                   athlete.training_age_years, athlete.weight_kg, athlete.height_cm)
    dist = zones.intensity_distribution(acts, max_hr=athlete.max_hr,
                                        resting_hr=athlete.resting_hr)

    dims = {"aerobic": aero, "strength": strength_analysis | {"score": strength_analysis.get("score")},
            "willpower": will, "recovery": rec, "talent": talent}
    total_parts = []
    for key, dim in dims.items():
        if dim.get("score") is not None:
            total_parts.append((dim["score"], evaluator.DIM_WEIGHTS[key]))
    total = round(sum(s * w for s, w in total_parts) / sum(w for _, w in total_parts)) if total_parts else 0

    evaluation = {
        "athlete": ad,
        "total_score": total,
        "grade": evaluator.grade_of(total),
        "percentile": evaluator.percentile_of_total(total),
        "dimensions": dims,
        "dimension_labels": evaluator.DIM_LABELS,
        "dimension_weights": evaluator.DIM_WEIGHTS,
        "intensity_distribution": dist,
        "current_vdot": pred.current_vdot,
        "paces": vdot.daniels_paces(pred.current_vdot),
        "hr_zones": zones.hr_zones(athlete.max_hr, athlete.resting_hr),
        "weekly_km_avg": round(weekly_km(acts), 1),
        "predictions": {k: p for k, p in pred.predictions.items()},
        "critical_speed": pred.critical_speed,
        "data_quality": pred.data_quality,
    }

    # 短板分析
    from .services import weakness as weakness_svc

    goal_row = db.scalars(
        select(models.Goal).where(models.Goal.athlete_id == athlete.id, models.Goal.status == "active")
        .order_by(models.Goal.id)
    ).first()
    goal = None
    if goal_row:
        goal = {"race_type": goal_row.race_type, "target_time_sec": goal_row.target_time_sec,
                "target_label": goal_row.target_label, "target_date": goal_row.target_date.isoformat() if goal_row.target_date else None}

    diet_payload = diet_analysis_payload(db, athlete)
    wk = weakness_svc.analyze(ad, evaluation, pred, strength_analysis, diet_payload.get("analysis"), goal,
                              evaluation["weekly_km_avg"])

    # 跑者类型（复用已算好的预测与跑量）
    from .services import runner_type as runner_type_svc
    evaluation["runner_type"] = runner_type_svc.classify_runner(
        ad, pred, evaluation["weekly_km_avg"], has_goal=goal is not None)

    # 生涯上限
    # 执行率缺失时传 None：让生涯上限把「执行一致性」这一项整块剔除并重新归一，
    # 而不是填一个 0.85 的假执行率（那是编造用户行为，会污染可开发空间的估计）
    ceiling = predictor.estimate_career_ceiling(
        pred, talent_score=talent["score"], age=ad["age"],
        weekly_km=evaluation["weekly_km_avg"],
        training_age_years=athlete.training_age_years,
        adherence=will.get("adherence"),
        race_type=(goal or {}).get("race_type") or "marathon",
    )

    evaluation["weakness"] = wk
    evaluation["career_ceiling"] = ceiling

    if with_persist:
        # 评估页/预测页每次打开都会调 /compute：分数没变且一周内已留档就跳过，
        # 否则历史趋势表会被页面浏览灌满，趋势图变成「访问记录」
        last = db.scalar(select(models.Assessment).order_by(models.Assessment.id.desc()).limit(1))
        week_old = last is None or last.created_at < utcnow_naive() - timedelta(days=7)
        if last is None or week_old or last.total_score != total:
            snap = models.Assessment(
                athlete_id=athlete.id, total_score=total, grade=evaluation["grade"],
                percentile=evaluation["percentile"],
                dimensions={k: {"score": d.get("score")} for k, d in dims.items()},
                weaknesses=wk["findings"], predictions=evaluation["predictions"],
                career_ceiling=ceiling,
            )
            db.add(snap)
            db.commit()
    return evaluation
