"""get_coach_briefing（跨域教练简报）：一次调用聚合各域要点的回归。

覆盖：空数据不崩且带引导 attention、正常数据下各域要点齐全、
attention 规则（疼痛打卡/HRV 偏离/装备告警/执行率低）逐项命中、
以及「系统提示词提到的工具必须真实存在且常驻」的注册表同步检查——
此前提示词铁律 14 要求必用 get_coach_briefing，但该工具从未实现，
模型按提示词调用只会拿到「未知工具」，正是那次「AI 教练鸡肋」的主因之一。
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

import pytest
from app import models
from app.services import ai_tools
from factories import make_athlete
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    a = make_athlete()
    s.add(a)
    s.commit()
    yield s
    s.close()


def _athlete(db) -> models.Athlete:
    return db.query(models.Athlete).first()


def test_briefing_empty_state(db):
    """空库不崩：attention 引导去同步/录入，各域要点仍齐备。"""
    r = ai_tools.execute_tool(db, _athlete(db), "get_coach_briefing", {})
    assert r["ok"] is True, r.get("reasons")
    assert r["today"] == date.today().isoformat()
    assert any("暂无训练/身体数据" in s for s in r["attention"])
    assert r["weekly_km_series_4w"] == [0.0, 0.0, 0.0, 0.0]
    assert r["today_workout"] is None


def test_briefing_full_data_attention(db):
    """有数据时：各域要点齐全，attention 逐项命中疼痛/装备/执行率/HRV。"""
    a = _athlete(db)
    today = date.today()

    # 装备超期（initial_km 650 > 默认退役里程 600）
    db.add(models.Gear(athlete_id=a.id, name="训练鞋", initial_km=650))

    # 近 30 天跑量：两周前 10km、本周 8km
    db.add(models.Activity(athlete_id=a.id, sport="run", start_time=datetime.now() - timedelta(days=14),
                           duration_sec=3000, distance_m=10000))
    db.add(models.Activity(athlete_id=a.id, sport="run", start_time=datetime.now() - timedelta(days=2),
                           duration_sec=2400, distance_m=8000))

    # 身体数据：HRV 显著低于基线（55 → 40），睡眠偏少
    db.add(models.BodyMetric(athlete_id=a.id, date=today, hrv_rmssd=40, sleep_hours=5.5))

    # 今日打卡：酸痛重 + 有疼痛部位（应触发建议档位与疼痛 attention）
    db.add(models.DailyCheckin(athlete_id=a.id, date=today, sleep_quality=2,
                               muscle_soreness=4, energy_level=1, motivation=2,
                               pain_area="膝盖"))

    # 执行率：近两周 4 节到期课全未完成
    plan = models.TrainingPlan(athlete_id=a.id, name="测试计划", race_type="5k",
                               start_date=today - timedelta(days=14),
                               race_date=today + timedelta(weeks=6))
    db.add(plan)
    db.flush()
    week = models.PlanWeek(plan_id=plan.id, week_index=1,
                           start_date=today - timedelta(days=14), phase="base", target_km=30)
    db.add(week)
    db.flush()
    for i in (1, 3, 6, 9, 11):
        db.add(models.PlanWorkout(week_id=week.id, athlete_id=a.id,
                                  date=today - timedelta(days=i), title="轻松跑",
                                  session_type="easy", status="planned"))
    db.commit()

    r = ai_tools.execute_tool(db, a, "get_coach_briefing", {})
    assert r["ok"] is True, r.get("reasons")

    # 各域要点齐全（acwr 需要慢性窗口数据，两周测试数据下为 None，只断言键存在）
    assert "acwr" in r["training_status"]
    assert r["body"]["hrv_vs_baseline_pct"] == -27          # 40 vs 基线 55
    assert r["body"]["sleep_avg_7d"] == 5.5
    assert r["plan_adherence_8w"]["total"] == 5
    assert r["plan_adherence_8w"]["completed"] == 0
    assert r["today_workout"] is None                        # 今天没排课
    assert sum(r["weekly_km_series_4w"]) >= 18.0

    attention = "\n".join(r["attention"])
    assert "膝盖" in attention
    assert "训练鞋" in attention
    assert "完成率" in attention
    assert r["today_advice"]["suggested_action"] in ("replace_easy", "reduce_volume", "skip_workout")
    # 空库引导项在有数据时不应出现
    assert not any("暂无训练/身体数据" in s for s in r["attention"])


def test_briefing_registered_and_resident():
    """schema/注册表/标签三者同步，且属于常驻集（不靠语汇命中才下发）。"""
    assert "get_coach_briefing" in ai_tools.TOOL_IMPLS
    schema_names = {t["function"]["name"] for t in ai_tools.TOOLS_SCHEMA}
    assert "get_coach_briefing" in schema_names
    assert "get_coach_briefing" in ai_tools.TOOL_LABELS
    from app.services import ai_coach
    assert "get_coach_briefing" not in ai_coach._OPTIONAL_TOOLS


def test_system_prompt_tools_all_exist():
    """系统提示词里点名要求调用的工具必须真实注册——防止再出现
    「提示词指挥模型调用不存在工具」的回归（这正是本轮要修的 bug 类）。"""
    from app.services.ai_coach import SYSTEM_PROMPT
    referenced = set(re.findall(
        r"\b(?:get|propose|precheck|search|recommend|resolve|preview|review|remember)_[a-z_]+\b",
        SYSTEM_PROMPT))
    assert referenced, "正则应至少匹配到提示词中的工具名"
    missing = referenced - set(ai_tools.TOOL_IMPLS)
    assert not missing, f"系统提示词引用了未注册的工具: {missing}"
