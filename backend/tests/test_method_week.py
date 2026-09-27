"""体验周生成与落库测试（build_week_plan_from_method / apply_method_week）。

回归重点：
1. 模板固定距离必须按用户跑量缩放，否则 30km 模板会原样写进初跑者的周计划；
2. 超出时段的质量课必须先减组数再换模板，不能静默丢弃或原样硬塞；
3. race 模板一律排除；一周内质量课 ≤2；
4. apply 是「单 active」语义：旧计划必须归档。
5. 长距离必须固定排在「最长时段」：用固定 today 跨 7 天锚点逐日验证，
   保证排课结果与今天是周几无关。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from app import models
from app.db import Base
from app.services.method_library import apply_method_week, build_week_plan_from_method
from factories import make_athlete
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()

    athlete = make_athlete(name="测试跑者", birth_year=2000)
    s.add(athlete)
    s.flush()
    # 近 28 天 8 条 10km → 周跑量约 20km
    for i in range(8):
        s.add(models.Activity(athlete_id=athlete.id, sport="run", title="t",
                              start_time=datetime.now() - timedelta(days=i * 3 + 1),
                              distance_m=10000, duration_sec=3300))
    # 可训练时段：周六 120min（最长→长距离）、周一 60、周三 60、周五 75
    for wd, dur in [(5, 120), (0, 60), (2, 60), (4, 75)]:
        s.add(models.WeeklySlot(athlete_id=athlete.id, weekday=wd,
                                start_time="19:00", duration_minutes=dur))
    # 方法样本：q1 超长间歇（需被减组）、q2 短节奏、race（必须被排除）、long 30km（需被缩放）、easy
    m = models.TrainingMethod(code="t-club", name_zh="测试跑团", quality_days_per_week=3,
                              level="elite", weekly_km_range={"min": 100, "max": 200},
                              intensity_distribution={"low": 80, "moderate": 12, "high": 8})
    s.add(m)
    s.flush()

    def reps(n):
        out = []
        for i in range(n):
            out.append({"step_type": "active", "name": f"重复 {i+1}/{n}", "duration_type": "distance",
                        "duration_value": 1000, "target": {"type": "pace_zone", "zone": "I"}, "note": ""})
            out.append({"step_type": "rest", "name": "恢复", "duration_type": "time",
                        "duration_value": 2, "target": {"type": "none", "label": "-"}, "note": ""})
        return out

    def mk(**kw):
        return models.WorkoutTemplate(method_id=m.id, **kw)

    s.add_all([
        mk(code="t-q1", name_zh="1000m × 8", session_type="interval", purpose="p",
           intensity_anchor={"type": "pace_zone", "zone": "I"}, distance_km_range={"min": 10, "max": 16},
           structure=[{"step_type": "warmup", "name": "热身", "duration_type": "time", "duration_value": 15,
                       "target": {"type": "none", "label": "-"}}] + reps(8)
                      + [{"step_type": "cooldown", "name": "冷身", "duration_type": "time", "duration_value": 10,
                          "target": {"type": "none", "label": "-"}}]),
        mk(code="t-q2", name_zh="节奏跑 8km", session_type="tempo", purpose="p",
           intensity_anchor={"type": "pace_zone", "zone": "M"}, distance_km_range={"min": 6, "max": 10},
           structure=[{"step_type": "warmup", "name": "热身", "duration_type": "time", "duration_value": 10,
                       "target": {"type": "none", "label": "-"}},
                      {"step_type": "active", "name": "节奏跑", "duration_type": "distance", "duration_value": 8000,
                       "target": {"type": "pace_zone", "zone": "M"}, "note": ""},
                      {"step_type": "cooldown", "name": "冷身", "duration_type": "time", "duration_value": 8,
                       "target": {"type": "none", "label": "-"}}]),
        mk(code="t-race", name_zh="半马比赛", session_type="race", purpose="p",
           intensity_anchor={"type": "pace_zone", "zone": "M"}, distance_km_range={"min": 21, "max": 22},
           structure=[{"step_type": "active", "name": "比赛", "duration_type": "distance", "duration_value": 21097,
                       "target": {"type": "pace_zone", "zone": "M"}, "note": ""}]),
        mk(code="t-long", name_zh="30km 距离走", session_type="long", purpose="p",
           intensity_anchor={"type": "pace_zone", "zone": "M"}, distance_km_range={"min": 25, "max": 32},
           structure=[{"step_type": "active", "name": "距离走", "duration_type": "distance", "duration_value": 30000,
                       "target": {"type": "pace_zone", "zone": "M"}, "note": ""}]),
        mk(code="t-easy", name_zh="轻松跑", session_type="easy", purpose="p",
           intensity_anchor={"type": "hr", "from": 0.6, "to": 0.7},
           distance_km_range={"min": 3, "max": 10},
           structure=[{"step_type": "active", "name": "轻松跑", "duration_type": "time", "duration_value": 40,
                       "target": {"type": "hr", "from": 0.6, "to": 0.7}, "note": ""}]),
    ])
    s.commit()

    # VDOT 全部走固定 mock：测试只关心排课逻辑，不关心预测引擎
    monkeypatch.setattr("app.data.build_prediction",
                        lambda *a, **k: SimpleNamespace(current_vdot=45.0))
    yield s
    s.close()


def test_week_plan_structure(db):
    draft = build_week_plan_from_method(db, db.scalar(select(models.Athlete)), "t-club")
    # 一天一课、总课数 = 可练天数；质量课 ≤2；race 被排除
    assert len(draft["workouts"]) == 4
    stypes = [w["session_type"] for w in draft["workouts"]]
    assert stypes.count("interval") + stypes.count("tempo") <= 2
    assert all(w["session_type"] != "race" for w in draft["workouts"])
    assert "race" not in {w["source_template"] for w in draft["workouts"]}
    # 长距离：20km 周跑量 → 7km（35%）
    long = next(w for w in draft["workouts"] if w["session_type"] == "long")
    assert 6 <= long["distance_km"] <= 8, f"长距离未按跑量缩放: {long['distance_km']}"
    # 长距离排在最长时段（周六 120min）对应的日期
    assert date.fromisoformat(long["date"]).weekday() == 5
    # 日期全部从明天起，不含过去
    assert all(date.fromisoformat(w["date"]) >= date.today() + timedelta(days=1) for w in draft["workouts"])


def test_quality_session_shrunk_to_fit_slot(db):
    draft = build_week_plan_from_method(db, db.scalar(select(models.Athlete)), "t-club")
    # 1000m×8 原始约 75min，60min 时段放不下 → 必须减组或被短模板替换，而不是硬塞
    for w in draft["workouts"]:
        if w["session_type"] in ("interval", "tempo"):
            # 找得到真实配速
            paces = [s["target"] for s in w["structured"]
                     if s.get("step_type") == "active" and s["target"].get("type") == "pace"]
            assert paces, "质量课缺少引擎解析的配速"
    # 任一质量课时长都不得超过对应时段的 1.1 倍
    slots = {s.weekday: s.duration_minutes for s in db.scalars(select(models.WeeklySlot))}
    for w in draft["workouts"]:
        wd = date.fromisoformat(w["date"]).weekday()
        if w["session_type"] in ("interval", "tempo"):
            assert w["duration_min"] <= slots[wd] * 1.1 + 1, \
                f"{w['title']} 时长 {w['duration_min']}min 超出时段 {slots[wd]}min"


def test_downgrade_notes_present(db):
    draft = build_week_plan_from_method(db, db.scalar(select(models.Athlete)), "t-club")
    assert any("降档" in n for n in draft["notes"]), "必须有可解释的降档说明"


def test_apply_archives_old_active_plan(db):
    a = db.scalar(select(models.Athlete))
    old = models.TrainingPlan(athlete_id=a.id, name="旧计划", race_type="5k",
                              start_date=date.today(), race_date=date.today() + timedelta(days=30),
                              status="active")
    db.add(old)
    db.commit()
    out = apply_method_week(db, a, "t-club")
    assert out["ok"] is True
    # 单 active：旧计划归档、新计划唯一 active
    assert db.scalar(select(models.TrainingPlan).where(
        models.TrainingPlan.status == "active")).id != old.id
    n_active = db.scalar(select(func.count(models.TrainingPlan.id)).where(
        models.TrainingPlan.status == "active"))
    assert n_active == 1
    # 课表已落库且带体验周描述
    wos = db.scalars(select(models.PlanWorkout).where(
        models.PlanWorkout.week_id.in_(select(models.PlanWeek.id).where(
            models.PlanWeek.plan_id == out["plan_id"])))).all()
    assert len(wos) == 4
    assert all("体验周" in w.description for w in wos)
    # 生成重复调用同样安全（再次单 active）
    out2 = apply_method_week(db, a, "t-club")
    assert out2["ok"] and out2["plan_id"] != out["plan_id"]
    assert db.scalar(select(func.count(models.TrainingPlan.id)).where(
        models.TrainingPlan.status == "active")) == 1


def test_apply_requires_vdot(db, monkeypatch):
    monkeypatch.setattr("app.data.build_prediction",
                        lambda *a, **k: SimpleNamespace(current_vdot=None))
    a = db.scalar(select(models.Athlete))
    with pytest.raises(ValueError, match="VDOT"):
        build_week_plan_from_method(db, a, "t-club")


def test_archive_clears_unexecuted_workouts(db):
    """归档旧计划必须删掉它「未执行」的课次、保留「已完成」的记录。

    归档只改 status 的话，旧计划的 planned 课会留在 plan_workouts 里，
    而总览页今日训练 / 打卡建议 / 饮食日型 / 执行率都按 athlete_id 查课次
    （不限定 active 计划），作废的课会串进来，表现为同一天多节重复课。
    """
    a = db.scalar(select(models.Athlete))
    old = models.TrainingPlan(athlete_id=a.id, name="旧计划", race_type="5k",
                              start_date=date.today(), race_date=date.today() + timedelta(days=30),
                              status="active")
    db.add(old)
    db.flush()
    pw = models.PlanWeek(plan_id=old.id, week_index=1, start_date=date.today(),
                         phase="base", target_km=30)
    db.add(pw)
    db.flush()
    db.add_all([
        models.PlanWorkout(week_id=pw.id, athlete_id=a.id, date=date.today(),
                           session_type="easy", title="未执行", status="planned"),
        models.PlanWorkout(week_id=pw.id, athlete_id=a.id, date=date.today(),
                           session_type="easy", title="已完成", status="completed"),
    ])
    db.commit()
    apply_method_week(db, a, "t-club")
    left = db.scalars(select(models.PlanWorkout).where(
        models.PlanWorkout.week_id == pw.id)).all()
    assert [w.title for w in left] == ["已完成"], \
        f"归档未清理未执行课次（或误删已完成记录），残留：{[w.title for w in left]}"


def test_season_factor_relaxes_paces(db):
    """实测气温 32℃ → 湿热口径：notes 提示 + 配速整体放慢 4%。"""
    a = db.scalar(select(models.Athlete))
    acts = db.scalars(select(models.Activity)).all()
    for act in acts:
        act.temp_c = 32.0
    db.commit()
    draft = build_week_plan_from_method(db, a, "t-club")
    assert any("季节因子" in n for n in draft["notes"])
    assert draft["season"]["hot"] and draft["season"]["relax_pct"] == 4.0
    # 对比非湿热环境：同一模板解析出的配速应被放慢约 4%
    relaxed = next(s for w in draft["workouts"] if w["session_type"] == "long"
                   for s in w["structured"] if s["target"].get("type") == "pace")
    from app.services.method_library import resolve_template_steps
    base = next(s for s in resolve_template_steps(
        db.scalar(select(models.WorkoutTemplate).where(
            models.WorkoutTemplate.code == "t-long")).structure, 45.0)
        if s["target"].get("type") == "pace")
    ratio = relaxed["target"]["from"] / base["target"]["from"]
    assert 1.03 <= ratio <= 1.05, f"湿热放慢比例异常: {ratio}"


def test_review_week_falls_back_when_nothing_completed(db):
    a = db.scalar(select(models.Athlete))
    apply_method_week(db, a, "t-club")
    from app.services.ai_tools import execute_tool
    out = execute_tool(db, a, "review_method_week", {})
    assert out["ok"] is True
    assert out["stats"]["total"] == 4
    assert out["verdict"] == "fall_back_default"   # 0% 完成率
    assert any("完成率" in r for r in out["reasons"])


def test_long_run_lands_on_longest_slot_every_weekday(db, monkeypatch):
    """把「今天」固定到一周 7 天分别排课，长距离都必须落在最长时段（周六 120min）。"""
    import datetime as real_dt
    from types import SimpleNamespace

    from app.services import method_library as ml
    a = db.scalar(select(models.Athlete))
    base = date.today()
    for offset in range(7):
        fixed = base + timedelta(days=offset)

        class _FixedDate(date):
            @classmethod
            def today(cls, _fixed=fixed):   # 默认参数绑定本次迭代的日期，防闭包读到最后一个 fixed
                return date(_fixed.year, _fixed.month, _fixed.day)

        # 只替换 method_library 眼里的 datetime 模块（垫片），不动全局：
        # 全局改 date.today 会让 fromisoformat 等类方法构造出子类实例漏进 SQL 绑定参数
        monkeypatch.setattr(ml, "_dt", SimpleNamespace(
            date=_FixedDate, timedelta=real_dt.timedelta, datetime=real_dt.datetime))
        draft = build_week_plan_from_method(db, a, "t-club")
        long_w = next(w for w in draft["workouts"] if w["session_type"] == "long")
        wd = date.fromisoformat(long_w["date"]).weekday()
        assert wd == 5, (
            f"today={fixed}（周{fixed.weekday() + 1}）时长距离排在周{wd + 1}，"
            "未落在最长时段（周六）")
        # 排课窗口不得包含今天及过去（明天起的 7 天）
        assert all(date.fromisoformat(w["date"]) >= fixed + timedelta(days=1)
                   for w in draft["workouts"])


# ---------------------------------------------------------------- 日期基准（week_start 必须周一）


def test_week_start_is_monday_all_weekdays(db, monkeypatch):
    """把「今天」固定到一周 7 天，「week_start」都必须是周一（全系统的周约定）。

    这是本文件最容易回归的一条：week_start 会直接落库成 PlanWeek/TrainingPlan.start_date，
    总览页「本周」定位（start_date <= today <= start_date+6）、AI 挪课按
    start_date + weekday 推日期都建立在「它是周一」这个前提上。
    此前取的是 workouts[0]（长距离那天）的日期，等于周中任意一天，会导致
    总览页算不出本周、计划起始日期与课次日期对不上。
    """
    import datetime as real_dt
    from types import SimpleNamespace

    from app.services import method_library as ml
    a = db.scalar(select(models.Athlete))
    base = date.today()
    for offset in range(7):
        fixed = base + timedelta(days=offset)

        class _FixedDate(date):
            @classmethod
            def today(cls, _fixed=fixed):   # 默认参数绑定本次迭代的日期，防闭包读到最后一个 fixed
                return date(_fixed.year, _fixed.month, _fixed.day)

        monkeypatch.setattr(ml, "_dt", SimpleNamespace(
            date=_FixedDate, timedelta=real_dt.timedelta, datetime=real_dt.datetime))
        draft = build_week_plan_from_method(db, a, "t-club")
        ws = date.fromisoformat(draft["week_start"])
        assert ws.weekday() == 0, (
            f"today={fixed}（周{fixed.weekday() + 1}）时 week_start={ws} 是周{ws.weekday() + 1}，不是周一")
        # 全部课次必须落在 week_start 起的同一自然周内，否则「这一周」的语义不成立
        for w in draft["workouts"]:
            d = date.fromisoformat(w["date"])
            assert ws <= d <= ws + timedelta(days=6), (
                f"课次 {d} 落在 week_start {ws} 所在自然周之外")


def test_week_start_covers_today_when_generated_midweek(db, monkeypatch):
    """周中生成时，窗口顺延到下周一，week_start 与最早课次必须同一天。"""
    import datetime as real_dt
    from types import SimpleNamespace

    from app.services import method_library as ml
    a = db.scalar(select(models.Athlete))
    # 固定为周三（weekday=2），此时「明天所在周的周一」已过去，必须顺延
    fixed = date.today() + timedelta(days=(2 - date.today().weekday()) % 7 + 7)

    class _FixedDate(date):
        @classmethod
        def today(cls):
            return date(fixed.year, fixed.month, fixed.day)

    monkeypatch.setattr(ml, "_dt", SimpleNamespace(
        date=_FixedDate, timedelta=real_dt.timedelta, datetime=real_dt.datetime))
    draft = build_week_plan_from_method(db, a, "t-club")
    ws = date.fromisoformat(draft["week_start"])
    assert ws.weekday() == 0, f"week_start={ws} 不是周一"
    earliest = min(date.fromisoformat(w["date"]) for w in draft["workouts"])
    assert earliest >= fixed + timedelta(days=1), "排课窗口包含了今天或过去"
    assert ws <= earliest <= ws + timedelta(days=6), "课次不在 week_start 所在周内"


# ---------------------------------------------------------------- 强度/负荷（防过度训练）


def test_beginner_quality_capped_at_one(db):
    """初跑者（周跑量 20km）每周质量课上限 1 次：强度课占比 ≤20%。"""
    draft = build_week_plan_from_method(db, db.scalar(select(models.Athlete)), "t-club")
    n_q = sum(1 for w in draft["workouts"] if w["session_type"] in ("interval", "tempo"))
    assert n_q <= 1, f"初跑者被排了 {n_q} 次质量课（原体系 3 次/周，降档不彻底）"
    assert draft["n_quality"] == n_q


def test_long_run_scales_with_volume_not_fixed_floor(db, monkeypatch):
    """周跑量 8km 时，长距离必须按跑量缩放，不能被 6km 硬底顶成周跑量的 75%。"""
    from app.services import method_library as ml
    real = ml.build_athlete_profile

    def fake(db_, athlete):
        p = real(db_, athlete)
        p["weekly_km"], p["level"] = 8.0, "beginner"
        return p

    monkeypatch.setattr(ml, "build_athlete_profile", fake)
    draft = build_week_plan_from_method(db, db.scalar(select(models.Athlete)), "t-club")
    long_w = next(w for w in draft["workouts"] if w["session_type"] == "long")
    assert long_w["distance_km"] <= 8.0 * 0.45 + 1e-6, (
        f"长距离 {long_w['distance_km']}km 超过周跑量 45%（6km 硬底回归？）")


def test_quality_cap_grows_with_level(db, monkeypatch):
    """水平提升后质量课上限放宽到 2（分级生效，不是一律砍到 1）。"""
    from app.services import method_library as ml
    real = ml.build_athlete_profile

    def fake(db_, athlete):
        p = real(db_, athlete)
        p["weekly_km"], p["level"] = 80.0, "advanced"
        return p

    monkeypatch.setattr(ml, "build_athlete_profile", fake)
    draft = build_week_plan_from_method(db, db.scalar(select(models.Athlete)), "t-club")
    n_q = sum(1 for w in draft["workouts"] if w["session_type"] in ("interval", "tempo"))
    assert n_q <= 2
    # 高级跑者允许排到 2 次（4 个可练日里最多占 2 天）
    assert draft["n_quality"] == n_q

