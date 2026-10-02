"""教练晨报（services/morning_brief.py）的口径锁定。

锚定三条：
- 事实由引擎装配（今日课/打卡/环境/伤病/状态/提案），规则版只复述不编造；
- 当天幂等一条：已生成直接返回缓存，不重复写；
- 总览页触发不阻塞：没有晨报时返回 generating=True，且同档案线程在飞时不重复拉起。
"""
from __future__ import annotations

from datetime import date

import pytest
from app import models
from app.db import get_db
from app.main import app
from app.services import morning_brief
from app.services.ai_coach import WEEKDAY_CN
from factories import make_athlete
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# 晨报引擎内部用 date.today() 查询「今天」，夹具必须跟着真实日期走；
# 硬编码日期会在跨天后集体失配（此前就发生过 2026-09-30 硬编码在 10-01 全红）
TODAY = date.today()


@pytest.fixture()
def mem_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    s.add(make_athlete())
    s.commit()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture()
def client(mem_db):
    def _ov():
        yield mem_db

    app.dependency_overrides[get_db] = _ov
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture()
def client_empty():
    """无档案的空库客户端：总览页 empty 分支锁定。"""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()

    def _ov():
        yield s

    app.dependency_overrides[get_db] = _ov
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)
    s.close()
    engine.dispose()


def _athlete(db) -> models.Athlete:
    return db.query(models.Athlete).first()


@pytest.fixture()
def no_llm(monkeypatch):
    """模型不可用路径：直接让 generate_text 抛错（避免测试真去连推理栈）。"""
    def _boom(facts, prompt_template=None, **kwargs):
        raise ValueError("本地模型不可用：单测模拟")

    monkeypatch.setattr("app.services.coach_comment.generate_text", _boom)


def _seed_today_workout(db, athlete):
    plan = models.TrainingPlan(athlete_id=athlete.id, name="备赛", race_type="10k",
                               start_date=TODAY, race_date=TODAY)
    db.add(plan)
    db.flush()
    week = models.PlanWeek(plan_id=plan.id, week_index=1, start_date=TODAY, phase="build")
    db.add(week)
    db.flush()
    db.add(models.PlanWorkout(week_id=week.id, athlete_id=athlete.id, date=TODAY,
                              session_type="interval", title="6×800", distance_km=10,
                              duration_min=55))
    db.commit()


# ---------------------------------------------------------------- 事实装配

def test_gather_facts_assembles_engines(mem_db):
    a = _athlete(mem_db)
    _seed_today_workout(mem_db, a)
    db_row = models.BodyMetric(athlete_id=a.id, date=TODAY, hrv_rmssd=65,
                               sleep_hours=7.5, sleep_score=80)
    mem_db.add(db_row)
    for _ in range(27):   # 客观封顶需要 28 天基线
        mem_db.add(models.BodyMetric(athlete_id=a.id, date=TODAY, hrv_rmssd=65,
                                     sleep_hours=7.5, sleep_score=80))
    mem_db.add(models.DailyCheckin(athlete_id=a.id, date=TODAY, sleep_quality=5,
                                   muscle_soreness=1, energy_level=5, motivation=5))
    mem_db.add(models.InjuryLog(athlete_id=a.id, start_date=TODAY, body_part="足底",
                                severity=2))
    mem_db.commit()
    facts = morning_brief.gather_facts(mem_db, a)
    assert "间歇跑" in facts["workout_line"] and "6×800" in facts["workout_line"]
    assert facts["checkin_line"].startswith("建议档位")
    assert facts["injury_line"].startswith("足底")
    assert facts["weekday"] == WEEKDAY_CN[TODAY.weekday()]
    assert "提案" in facts["proposal_line"]


def test_gather_facts_without_checkin_prompts_checkin(mem_db):
    facts = morning_brief.gather_facts(mem_db, _athlete(mem_db))
    assert "打卡" in facts["checkin_line"]


# ---------------------------------------------------------------- 生成与幂等

def test_generate_falls_back_to_rule_and_persists(mem_db, no_llm):
    a = _athlete(mem_db)
    _seed_today_workout(mem_db, a)
    out = morning_brief.generate_brief(mem_db, a)
    assert out["source"] == "rule" and out["cached"] is False
    assert "间歇跑" in out["content"] and "6×800" in out["content"]

    row = morning_brief.get_today_brief(mem_db, a.id)
    assert row is not None and row.source == "rule"
    assert row.date == TODAY


def test_generate_is_idempotent_per_day(mem_db, no_llm):
    a = _athlete(mem_db)
    first = morning_brief.generate_brief(mem_db, a)
    second = morning_brief.generate_brief(mem_db, a)
    assert second["cached"] is True
    assert second["content"] == first["content"]


def test_fallback_brief_prompts_checkin(mem_db):
    facts = morning_brief.gather_facts(mem_db, _athlete(mem_db))
    text = morning_brief.fallback_brief(facts)
    assert "先完成晨间打卡" in text
    assert "休息日或暂无排课" in text


# ---------------------------------------------------------------- 总览页挂接

def test_dashboard_payload_generating_and_cached(mem_db, client, monkeypatch, no_llm):
    a = mem_db.query(models.Athlete).first()
    # 不让总览页请求真的拉起后台线程（线程用的是 SessionLocal 不是测试库）
    monkeypatch.setattr(morning_brief, "ensure_today_brief_async", lambda aid: True)

    body = client.get("/api/dashboard").json()
    assert body["morning_brief"] == {"generating": True, "brief": None}

    morning_brief.generate_brief(mem_db, a, force=False)
    body2 = client.get("/api/dashboard").json()
    assert body2["morning_brief"]["generating"] is False
    assert body2["morning_brief"]["brief"]["content"]


def test_in_flight_guard_prevents_duplicate_threads(mem_db, monkeypatch):
    a = _athlete(mem_db)
    assert morning_brief.ensure_today_brief_async(a.id) is True   # 首次拉起
    morning_brief._in_flight.add(a.id)   # 模拟线程仍在生成
    assert morning_brief.ensure_today_brief_async(a.id) is False
    morning_brief._in_flight.clear()


def test_dashboard_without_athlete(client_empty):
    r = client_empty.get("/api/dashboard")
    assert r.status_code == 200 and r.json()["empty"] is True
