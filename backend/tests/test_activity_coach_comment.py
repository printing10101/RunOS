"""活动级 AI 教练点评：每条活动记录都有分析（训练状态 + 近期强度综合判断）。

背景：此前点评只挂在关联课表课上，且详情 payload 从未带出 plan_workout——
前端点评卡恒为空，是「打开记录没有 AI 分析」的根因。这里锁定活动级路径：
规则兜底与 LLM 落库、关联已完成课自动路由到课表点评、缓存幂等、
上下文事实装配（上次同类训练/比赛日）、详情 payload 带出。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from app import models
from app.services import coach_comment
from app.services.activity_detail import build_activity_detail
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


def _seed(db) -> models.Activity:
    """固定日期的两条跑（昨天语义换成固定日，避免时钟数字混进事实源影响核验）：
    06-14 一次 5km + 06-10 一次 10km（作为「上一次同类训练」对照锚点）。"""
    a = _athlete(db)
    act = models.Activity(athlete_id=a.id, sport="run", title="轻松跑", platform="manual",
                          start_time=datetime(2026, 6, 14, 8, 0), duration_sec=1800,
                          distance_m=5000, avg_hr=150, training_load=100)
    prev = models.Activity(athlete_id=a.id, sport="run", title="长距离", platform="manual",
                           start_time=datetime(2026, 6, 10, 8, 0), duration_sec=3900,
                           distance_m=10000, avg_hr=155)
    db.add_all([act, prev])
    db.commit()
    return act


# ---------------------------------------------------------------- 生成路径
def test_rule_fallback_stored_on_activity(db, monkeypatch):
    """模型不可用退化为规则点评：落 activities.coach_comment，带训练背景事实。"""
    act = _seed(db)
    monkeypatch.setattr(coach_comment, "generate_text",
                        lambda *a, **kw: (_ for _ in ()).throw(ValueError("本地模型不可用")))
    r = coach_comment.generate_for_activity(db, _athlete(db), act.id)
    assert r["ok"] is True and r["target"] == "activity" and r["source"] == "rule"
    db.refresh(act)
    assert act.coach_comment and "轻松跑" in act.coach_comment
    assert "上一次同类训练" in act.coach_comment   # 规则版也复述引擎事实，不缺席
    assert act.coach_comment_at is not None


def test_llm_comment_passes_guard_and_stored(db, monkeypatch):
    """LLM 文案过数字核验后落库：引用的数字都能溯源到装配事实。"""
    act = _seed(db)
    monkeypatch.setattr(
        coach_comment, "generate_text",
        lambda facts, prompt_template=None: "本次 5.0km 完成质量不错，心率控制得住，下次保持。")
    r = coach_comment.generate_for_activity(db, _athlete(db), act.id)
    assert r["source"] == "llm"
    db.refresh(act)
    assert act.coach_comment == "本次 5.0km 完成质量不错，心率控制得住，下次保持。"


def test_llm_fabricated_numbers_fall_back_to_rule(db, monkeypatch):
    """点评引用事实之外的数字（编造的里程）→ 核验拦下，退规则版。"""
    act = _seed(db)
    monkeypatch.setattr(
        coach_comment, "generate_text",
        lambda facts, prompt_template=None: "本次 487.3km 的完成质量很扎实，继续保持。")
    r = coach_comment.generate_for_activity(db, _athlete(db), act.id)
    assert r["source"] == "rule"   # 487.3 不在事实里，两两推导也够不着


def test_cached_comment_is_idempotent(db, monkeypatch):
    act = _seed(db)
    act.coach_comment = "已有点评"
    db.commit()

    def _boom(*a, **kw):
        raise AssertionError("已有点评时不应再调模型")
    monkeypatch.setattr(coach_comment, "generate_text", _boom)
    r = coach_comment.generate_for_activity(db, _athlete(db), act.id)
    assert r["cached"] is True and r["comment"] == "已有点评"


# ---------------------------------------------------------------- 路由：关联已完成课走课表点评
def test_linked_completed_workout_delegates(db, monkeypatch):
    """关联了已完成课表课的活动：走「计划 vs 实际」课表点评，不写活动级字段。"""
    act = _seed(db)
    a = _athlete(db)
    wo = models.PlanWorkout(week_id=0, athlete_id=a.id, date=act.start_time.date(),
                            session_type="easy", title="轻松跑6km", status="completed",
                            completed_source="linked", completed_activity_id=act.id)
    db.add(wo)
    db.commit()

    calls = []

    def _fake_generate(db_, athlete, workout_id, force=False):
        calls.append((workout_id, force))
        return {"ok": True, "cached": False, "source": "llm", "comment": "课表点评", "at": None}

    monkeypatch.setattr(coach_comment, "generate_for_workout", _fake_generate)
    r = coach_comment.generate_for_activity(db, a, act.id)
    db.refresh(act)
    assert calls == [(wo.id, False)]
    assert r["target"] == "workout" and r["comment"] == "课表点评"
    assert act.coach_comment is None   # 课表路径不落活动级，避免双份存储


# ---------------------------------------------------------------- 事实装配
def test_activity_facts_carry_context(db):
    """上下文要能让模型做「和之前比」的判断：上次同类训练的数字必须进 facts。"""
    act = _seed(db)
    facts = coach_comment.gather_activity_facts(db, _athlete(db), act)
    assert "轻松跑" in facts["activity_line"]
    assert "上一次同类训练" in facts["context_line"]
    assert "10.0km" in facts["context_line"]
    assert "155" in facts["context_line"]   # 上次平均心率


def test_last_same_sport_pace_unit(db):
    """上次配速必须是 6:30/km 口径（3900s/10km）。

    回归锁定：曾把 sec_per_km 喂给吃 m/min 的 pace_str，输出 2:33/km——
    差一个数量级的假配速进了 prompt facts，模型会当事实复述给用户。"""
    act = _seed(db)
    line = coach_comment._last_same_sport_line(db, _athlete(db), act)
    assert line and "6:30/km" in line
    assert "2:33" not in line


# ---------------------------------------------------------------- 详情 payload
def test_detail_payload_carries_comment_and_linked_workout(db):
    """详情接口要带出点评与关联课摘要——此前 plan_workout 缺失导致点评卡恒空。"""
    act = _seed(db)
    a = _athlete(db)
    detail = build_activity_detail(act, a, db=db)
    assert detail["coach_comment"] is None and detail["plan_workout"] is None

    act.coach_comment = "状态不错，负荷合理。"
    wo = models.PlanWorkout(week_id=0, athlete_id=a.id, date=act.start_time.date(),
                            session_type="easy", title="轻松跑6km", status="completed",
                            completed_activity_id=act.id)
    db.add_all([act, wo])
    db.commit()
    detail = build_activity_detail(act, a, db=db)
    assert detail["coach_comment"] == "状态不错，负荷合理。"
    assert detail["plan_workout"]["workout_id"] == wo.id
    assert detail["plan_workout"]["title"] == "轻松跑6km"
