"""饮食→训练联动测试。

规则：补给状态由确定性规则判定：近 3 个有记录日摄入/目标 < 75% 判 deficit（建议档位封顶 easy）；
昨日摄入 < 80% 目标且当天是质量课/长距离判 low（封顶 reduce）；长距离日昨日碳水 < 5g/kg 只提示不降档。
补给降档只降不升，不覆盖主观状态的结论。营养目标按当天计划课型动态取值。
"""
from datetime import date, datetime, timedelta

from app.services import diet as diet_svc
from app.services.checkin_advice import build_checkin_advice
from factories import make_athlete

# ---------------------------------------------------------------- 课型映射

def test_day_type_for_mapping():
    """强度课→quality，长距离→long，休息→rest，其余/未知→easy。"""
    assert diet_svc.day_type_for("interval") == "quality"
    assert diet_svc.day_type_for("tempo") == "quality"
    assert diet_svc.day_type_for("quality") == "quality"
    assert diet_svc.day_type_for("long") == "long"
    assert diet_svc.day_type_for("rest") == "rest"
    assert diet_svc.day_type_for("easy") == "easy"
    assert diet_svc.day_type_for("strength") == "easy"
    assert diet_svc.day_type_for("cross") == "easy"
    assert diet_svc.day_type_for(None) == "easy"
    assert diet_svc.day_type_for("unknown") == "easy"


# ---------------------------------------------------------------- 补给状态判定

def _h(date_iso, kcal, target, carb=400.0):
    return {"date": date_iso, "intake_kcal": kcal, "target_kcal": target, "carb_g": carb}


def test_fueling_unknown_without_records():
    """无饮食记录 → unknown，不给降档。"""
    out = diet_svc.fueling_status([], "quality", 70)
    assert out["level"] == "unknown"
    assert out["cap_level"] is None


def test_fueling_deficit_persistent():
    """近 3 个有记录日摄入/目标 < 75% → deficit，封顶 easy。"""
    hist = [_h("2026-09-08", 1500, 2400), _h("2026-09-09", 1600, 2400), _h("2026-09-10", 1500, 2400)]
    out = diet_svc.fueling_status(hist, "easy", 70)
    assert out["level"] == "deficit"
    assert out["cap_level"] == "easy"
    assert any("能量缺口" in r for r in out["reasons"])


def test_fueling_deficit_needs_two_days():
    """只有 1 个有记录日不判持续缺口（证据不足），但昨日规则照常生效。"""
    hist = [_h("2026-09-10", 1000, 2400)]
    out = diet_svc.fueling_status(hist, "quality", 70)
    assert out["cap_level"] == "reduce"   # 昨日 <80% + 今天质量课
    assert out["level"] == "low"


def test_fueling_low_yesterday_before_quality_day():
    """昨日摄入 <80% 目标 + 今天质量课 → low，封顶 reduce。"""
    hist = [_h("2026-09-09", 2300, 2400), _h("2026-09-10", 1500, 2400)]
    out = diet_svc.fueling_status(hist, "quality", 70)
    assert out["level"] == "low"
    assert out["cap_level"] == "reduce"


def test_fueling_yesterday_low_but_easy_day_ok():
    """昨日偏少但今天是常规日 → 不降档。"""
    hist = [_h("2026-09-09", 2300, 2400), _h("2026-09-10", 1500, 2400)]
    out = diet_svc.fueling_status(hist, "easy", 70)
    assert out["level"] == "ok"
    assert out["cap_level"] is None


def test_fueling_carb_note_for_long_day():
    """长距离日昨日碳水 < 5g/kg → 只出提示，不降档。"""
    hist = [_h("2026-09-09", 2300, 2400, carb=200), _h("2026-09-10", 2300, 2400, carb=200)]
    out = diet_svc.fueling_status(hist, "long", 70)
    assert out["cap_level"] is None
    assert any("碳水" in n for n in out["notes"])


def test_fueling_ok_when_intake_adequate():
    """摄入达标 → ok + 正向提示。"""
    hist = [_h("2026-09-09", 2400, 2400), _h("2026-09-10", 2500, 2400)]
    out = diet_svc.fueling_status(hist, "quality", 70)
    assert out["level"] == "ok"
    assert out["cap_level"] is None


def test_fueling_deficit_wins_over_low():
    """同时满足持续缺口与昨日不足 → 取更严的 easy。"""
    hist = [_h("2026-09-08", 1500, 2400), _h("2026-09-09", 1500, 2400), _h("2026-09-10", 1500, 2400)]
    out = diet_svc.fueling_status(hist, "quality", 70)
    assert out["cap_level"] == "easy"
    assert len(out["reasons"]) >= 2


# ---------------------------------------------------------------- 能量平衡

def test_energy_balance_series_and_avg7():
    """balance = 摄入 − 当日目标；均值只统计有记录的天。"""
    daily = [
        {"date": "2026-09-10", "day_type": "easy", "intake_kcal": 2400, "target_kcal": 2300, "burned_kcal": 700},
        {"date": "2026-09-11", "day_type": "quality", "intake_kcal": 0, "target_kcal": 2400, "burned_kcal": 0},
        {"date": "2026-09-12", "day_type": "long", "intake_kcal": 1800, "target_kcal": 2500, "burned_kcal": 1200},
    ]
    out = diet_svc.energy_balance(daily)
    assert out["series"][0]["balance"] == 100
    assert out["series"][1]["balance"] == -2400   # 未记录按 0 摄入计
    assert out["recorded_days"] == 2
    assert out["avg7"]["intake_kcal"] == round((2400 + 1800) / 2)
    assert out["avg7"]["balance"] == round((100 - 700) / 2)


# ---------------------------------------------------------------- 建议档位联动

_GOOD_CHECKIN = {"sleep_quality": 5, "energy_level": 5, "muscle_soreness": 0, "motivation": 5, "pain_area": ""}
_QUALITY_WO = {"session_type": "interval", "title": "间歇课"}


def test_advice_diet_cap_downgrades_level():
    """主观状态很好但持续能量缺口 → 档位降到 easy，触发换轻松跑提案，理由并入结论。"""
    diet = {"level": "deficit", "cap_level": "easy",
            "reasons": ["近 3 个记录日摄入仅为目标 60%，存在持续能量缺口"], "notes": []}
    out = build_checkin_advice(_GOOD_CHECKIN, _QUALITY_WO, diet)
    assert out["level"] == "easy"
    assert out["suggested_action"] == "replace_easy"
    assert "能量缺口" in out["verdict"]
    assert out["fueling_level"] == "deficit"
    assert "主观状态" in out["verdict"]   # 主观结论保留不覆盖


def test_advice_diet_cap_only_downgrades():
    """主观状态很差（rest）时补给降档不会反向升档。"""
    bad = {"sleep_quality": 1, "energy_level": 1, "muscle_soreness": 4, "motivation": 1, "pain_area": ""}
    diet = {"level": "low", "cap_level": "reduce", "reasons": ["昨日摄入不足"], "notes": []}
    out = build_checkin_advice(bad, _QUALITY_WO, diet)
    assert out["level"] == "rest"


def test_advice_diet_note_without_cap():
    """补给仅提示（不降档）时档位不变，提示仍出现在结论里。"""
    diet = {"level": "ok", "cap_level": None, "reasons": [], "notes": ["昨日碳水偏低，注意补给"]}
    out = build_checkin_advice(_GOOD_CHECKIN, _QUALITY_WO, diet)
    assert out["level"] == "normal"
    assert "碳水" in out["verdict"]


def test_advice_without_checkin_ignores_diet():
    """没打卡时不出档位，不因饮食单独给建议。"""
    diet = {"level": "deficit", "cap_level": "easy", "reasons": ["缺口"], "notes": []}
    out = build_checkin_advice(None, _QUALITY_WO, diet)
    assert out["level"] is None


# ---------------------------------------------------------------- 集成：payload 装配

def _fresh_db():
    from app.db import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_payload_day_type_targets_and_fueling():
    """集成：今日质量课 → 碳水目标按 6.5g/kg；昨日少吃 → fueling=low；跑步无 calories 时按 1 kcal/kg/km 估算消耗。"""
    from app import models
    from app.data import diet_analysis_payload

    db = _fresh_db()
    a = make_athlete(name="测跑者", birth_year=1990,
                     height_cm=175, weight_kg=70, max_hr=190, resting_hr=52)
    db.add(a)
    db.commit()
    today = date.today()
    plan = models.TrainingPlan(athlete_id=a.id, name="测试计划", race_type="10k",
                               start_date=today - timedelta(days=7), race_date=today + timedelta(days=60))
    db.add(plan)
    db.commit()
    week = models.PlanWeek(plan_id=plan.id, week_index=1, start_date=today - timedelta(days=3), phase="build")
    db.add(week)
    db.commit()
    db.add(models.PlanWorkout(week_id=week.id, athlete_id=a.id, date=today,
                              session_type="quality", title="间歇 6x800"))
    # 昨日：摄入 1000 kcal（远低于目标）、碳水 100g；跑步 10km 无 calories（应估算 700）
    db.add(models.DietLog(athlete_id=a.id, date=today - timedelta(days=1), meal="lunch",
                          description="少量", kcal=1000, protein_g=40, carb_g=100, fat_g=20))
    db.add(models.Activity(athlete_id=a.id, sport="run", title="Easy 10k",
                           start_time=datetime.combine(today - timedelta(days=1), datetime.min.time()).replace(hour=18),
                           duration_sec=3600, distance_m=10000))
    db.commit()

    p = diet_analysis_payload(db, a)
    assert p["today"]["day_type"] == "quality"
    assert p["targets"]["carb_g"] == round(70 * 6.5)          # 质量课碳水倍数
    assert p["fueling"]["level"] == "low"
    assert p["fueling"]["cap_level"] == "reduce"
    assert any("昨日" in r for r in p["fueling"]["reasons"])
    yesterday = next(s for s in p["balance"]["series"] if s["date"] == (today - timedelta(days=1)).isoformat())
    assert yesterday["burned_kcal"] == 700                    # 10km × 70kg 估算
    assert p["balance"]["avg7"] is not None


def test_payload_rest_day_targets_and_ok_fueling():
    """无计划课 → 常规日目标；摄入达标 → fueling=ok。"""
    from app import models
    from app.data import diet_analysis_payload

    db = _fresh_db()
    a = make_athlete(name="测跑者", birth_year=1990,
                     height_cm=175, weight_kg=70, max_hr=190, resting_hr=52)
    db.add(a)
    db.commit()
    today = date.today()
    for d in (1, 2):
        db.add(models.DietLog(athlete_id=a.id, date=today - timedelta(days=d), meal="lunch",
                              description="正餐", kcal=2300, protein_g=140, carb_g=350, fat_g=70))
    db.commit()

    p = diet_analysis_payload(db, a)
    assert p["today"]["day_type"] == "easy"
    assert p["fueling"]["level"] == "ok"
    assert p["fueling"]["cap_level"] is None
