"""每周自动复盘模块测试。

覆盖范围：
  - _week_start 日期计算正确性（周一开局）
  - 空数据场景处理
  - 强度划分（hard_pct）计算
  - lead_line 文案生成逻辑（各种情况都要有产出不崩溃）
  - 集成测试：带活动数据、带计划下周时的完整构建
"""
from datetime import date, datetime, timedelta

from app.services import weekly_recap

# ---------------------------------------------------------------- _week_start 基础正确性

def test_week_start_on_midweek():
    """周中某天 → 回到本周一（weekday==0）。"""
    # 2026-09-11 是周五 → 本周一 2026-09-07
    d = date(2026, 9, 11)
    ws = weekly_recap._week_start(d)
    assert ws == date(2026, 9, 7)
    assert ws.weekday() == 0  # 必须是周一

def test_week_start_already_monday():
    d = date(2026, 9, 7)
    ws = weekly_recap._week_start(d)
    assert ws == d
    assert ws.weekday() == 0

def test_week_start_on_sunday():
    # 2026-09-13 是周日 → 本周一还是 09-07
    d = date(2026, 9, 13)
    ws = weekly_recap._week_start(d)
    assert ws == date(2026, 9, 7)
    assert ws.weekday() == 0


# ---------------------------------------------------------------- 强度划分计算

def test_split_hard_pct_correct():
    """hard_pct = 高强度公里 / 总跑步公里。强度判断复用 load_status.classify_intensity。"""
    import app.services.load_status as ls
    athlete = {"max_hr": 190, "resting_hr": 55}

    # 复刻生产逻辑（_split 是闭包依赖 ad，直接调用不可行），重点验证 hard 判断分流
    def _hard_pct_full(items, ad):
        hard = sum(a["distance_m"] / 1000 for a in items
                   if ls.classify_intensity(a.get("avg_hr"), a.get("sport"), ad)
                   in ("anaerobic", "high_aerobic"))
        km = sum(a["distance_m"] / 1000 for a in items if a.get("sport") == "run")
        return {"km": round(km, 1), "hard_km": round(hard, 1),
                "hard_pct": round(hard / km * 100) if km > 0 else 0}

    # easy run 10km，avg_hr 140 → low_aerobic → 不计入 hard
    easy = [{"sport": "run", "distance_m": 10000, "avg_hr": 140, "training_load": 100}]
    res_easy = _hard_pct_full(easy, athlete)
    assert res_easy["km"] == 10.0
    assert res_easy["hard_km"] == 0.0
    assert res_easy["hard_pct"] == 0

    # 混合：5km easy + 5km interval
    mixed = [{"sport": "run", "distance_m": 5000, "avg_hr": 140},
             {"sport": "run", "distance_m": 5000, "avg_hr": 175}]
    res_mixed = _hard_pct_full(mixed, athlete)
    assert res_mixed["km"] == 10.0
    assert res_mixed["hard_pct"] == 50  # 一半是高强度

    # 非跑步项目不计入 km 分母
    mixed_non_run = [{"sport": "run", "distance_m": 5000, "avg_hr": 175},
                     {"sport": "strength", "distance_m": 0}]
    res_mnr = _hard_pct_full(mixed_non_run, athlete)
    assert res_mnr["km"] == 5.0
    assert res_mnr["hard_pct"] == 100

    # 零公里 → 零百分比（不要除以零）
    zero = [{"sport": "strength", "distance_m": 0}]
    res_zero = _hard_pct_full(zero, athlete)
    assert res_zero["km"] == 0.0
    assert res_zero["hard_pct"] == 0


# ---------------------------------------------------------------- 引导文案 _lead_line 各种分支都正常生成

def test_lead_line_no_activity():
    """本周没有训练时文案正常。"""
    line = weekly_recap._lead_line([], {"km": 0}, {}, {"status": {"label": "中断"}}, None)
    assert "本周还没有训练记录" in line
    assert "训练状态「中断」" in line
    assert line.endswith("，继续稳稳推进")

def test_lead_line_with_activity_low_hard_pct():
    """强度占比偏低时给出提示。"""
    this = [{"sport": "run"}] * 3
    this_s = {"km": 21, "hard_pct": 15}
    st = {"status": {"label": "效率良好"}}
    line = weekly_recap._lead_line(this, this_s, None, st, None)
    assert "本周完成 3 次训练、21 km" in line
    assert "强度占比 15% 略低" in line
    assert "训练状态「效率良好」" in line

def test_lead_line_with_activity_high_hard_pct():
    """强度占比偏高时给出提示。"""
    this = [{"sport": "run"}] * 4
    this_s = {"km": 30, "hard_pct": 35}
    st = {"status": {"label": "负荷过高"}}
    line = weekly_recap._lead_line(this, this_s, None, st, None)
    assert "强度占比 35% 略高" in line
    assert "注意用轻松日把节奏拉回来" in line

def test_lead_line_with_activity_good_hard_pct():
    """强度占比在 20-30 之间 → 符合 80/20。"""
    this = [{"sport": "run"}] * 4
    this_s = {"km": 25, "hard_pct": 25}
    st = {"status": {"label": "维持"}}
    line = weekly_recap._lead_line(this, this_s, None, st, None)
    assert "强度节奏合理，符合 80/20 原则" in line

def test_lead_line_with_next_plan():
    """有下周计划时，计划信息要出现在文案里。"""
    this = [{"sport": "run"}] * 3
    this_s = {"km": 20, "hard_pct": 20}
    st = {"status": {"label": "效率良好"}}
    next_week = {
        "week_index": 8,
        "phase": "build",
        "target_km": 45,
        "focus": "乳酸阈值提升",
    }
    line = weekly_recap._lead_line(this, this_s, None, st, next_week)
    assert "第 8 周" in line
    assert "强化期" in line
    assert "目标 45 km" in line
    assert "重点：乳酸阈值提升" in line


# ---------------------------------------------------------------- 集成测试：从空库到有数据构建

def _fresh_db():
    from app.db import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()

def _setup_test_data(db):
    from datetime import date as dt_date

    from app.models import Activity, PlanWeek, PlanWorkout, TrainingPlan
    from factories import make_athlete

    this_start = weekly_recap._week_start(date.today())
    prev_start = this_start - timedelta(days=7)

    # 1. 创建运动员
    a = make_athlete(name="测试跑者", max_hr=190, resting_hr=52, birth_year=1990)
    db.add(a)
    db.commit()

    # 2. 上一周：本周窗口外的训练（不会计入「本周」）
    for i in range(3):
        dt = datetime.combine(prev_start + timedelta(days=i), datetime.min.time())
        avg_hr = 145 if i != 2 else 168
        dist = 5000 if i != 2 else 6000
        load = 100 if i != 2 else 150
        db.add(Activity(athlete_id=a.id, sport="run", start_time=dt, distance_m=dist,
                        duration_sec=int(dist * 5 * 60 / 1000), avg_hr=avg_hr, training_load=load))

    # 3. 本周：4 次跑 + 1 次力量（跑 4*7km，力量不计里程）
    for i in range(4):
        dt = datetime.combine(this_start + timedelta(days=i), datetime.min.time())
        avg_hr = 145 if i % 2 == 0 else 165
        dist = 7000
        db.add(Activity(athlete_id=a.id, sport="run", start_time=dt, distance_m=dist,
                        duration_sec=int(dist * 5 * 60 / 1000), avg_hr=avg_hr,
                        training_load=100 + (25 if avg_hr >= 160 else 0)))
    db.add(Activity(athlete_id=a.id, sport="strength",
                    start_time=datetime.combine(this_start + timedelta(days=4), datetime.min.time()),
                    distance_m=0, duration_sec=3600, training_load=80))

    # 4. 训练计划 + 下周（start_date >= 下周一）
    plan = TrainingPlan(athlete_id=a.id, race_type="marathon", target_time_sec=3 * 3600,
                        name="全马破三", start_date=dt_date.today() - timedelta(days=21),
                        race_date=dt_date.today() + timedelta(days=63))
    db.add(plan)
    db.commit()
    next_start = this_start + timedelta(days=7)
    pw = PlanWeek(plan_id=plan.id, week_index=4, start_date=next_start,
                  phase="build", target_km=40, focus="长距离打底")
    db.add(pw)
    db.commit()
    db.add(PlanWorkout(week_id=pw.id, athlete_id=a.id, date=next_start,
                       session_type="easy", title="轻松跑 10km"))
    db.add(PlanWorkout(week_id=pw.id, athlete_id=a.id, date=next_start + timedelta(days=6),
                       session_type="long", title="长距离 30km"))
    db.commit()

    return db, a.id


def test_build_weekly_recap_integration():
    """集成测试：完整构建路径，字段全有值，结构不崩。"""
    db = _fresh_db()
    _setup_test_data(db)
    result = weekly_recap.build_weekly_recap(db)

    assert not result.get("empty")
    # 顶级键齐全
    for key in ("week", "this_week", "prev_week", "split", "load", "recovery", "next", "lead"):
        assert key in result

    # 本周统计正确：4 次跑 + 1 次力量 = 5 sessions
    assert result["this_week"]["sessions"] == 5
    assert 27 <= result["this_week"]["km"] <= 29  # 4 * 7 = 28

    # 负荷指标存在
    assert "acwr" in result["load"]
    assert "readiness" in result["recovery"]
    assert "status" in result["recovery"]

    # 下周计划存在且结构正确
    assert result["next"] is not None
    assert result["next"]["week_index"] == 4
    assert result["next"]["phase"] == "build"
    assert len(result["next"]["titles"]) == 2

    # 引导文案非空
    assert len(result["lead"]) > 20

def test_build_weekly_recap_empty_no_athlete():
    """空库没有运动员 → 返回 empty。"""
    db = _fresh_db()
    result = weekly_recap.build_weekly_recap(db)
    assert result == {"empty": True}

def test_build_weekly_recap_empty_no_activity():
    """有运动员但没训练 → 不是 error，返回结构带空数据。"""
    db = _fresh_db()
    from factories import make_athlete
    a = make_athlete(name="新人", max_hr=190, resting_hr=55)
    db.add(a)
    db.commit()
    result = weekly_recap.build_weekly_recap(db)
    assert not result.get("empty")
    assert result["this_week"]["sessions"] == 0
    assert "本周还没有训练记录" in result["lead"]
