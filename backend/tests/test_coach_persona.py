"""教练人设（coach_persona）：按比赛目标匹配顶尖教练的语气与关注点。

风格层注入：对话 system_prompt 的【教练人设】段 + 两类点评 prompt 的
【教练风格】行共用同一份解析（活动目标 → 计划项目 → 默认）。
锁定解析规则、注入位置与 facts 装配；人设不得挤掉安全纪律段落。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from app import models
from app.services import ai_coach, coach_comment, coach_persona
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


# ---------------------------------------------------------------- 解析规则
def test_persona_registry_covers_all_goal_race_types():
    """注册表必须盖满 GOAL_RACE_TYPES——新目标项目落库后人设不能退化为默认。"""
    from app.services.vdot import GOAL_RACE_TYPES
    missing = set(GOAL_RACE_TYPES) - set(coach_persona.PERSONAS)
    assert not missing, f"人设注册表缺项目：{missing}"


def test_persona_for_known_unknown_and_none():
    assert "名帅" in coach_persona.persona_for("marathon")
    assert "5 公里" in coach_persona.persona_for("5k")
    assert coach_persona.persona_for("nosuch") == coach_persona.DEFAULT_PERSONA
    assert coach_persona.persona_for(None) == coach_persona.DEFAULT_PERSONA


def test_persona_follows_active_goal_then_plan(db):
    a = _athlete(db)
    # 无目标无计划 → 默认
    assert coach_persona.persona_for_athlete(db, a.id) == coach_persona.DEFAULT_PERSONA
    # 有进行中计划（无目标）→ 计划项目
    plan = models.TrainingPlan(athlete_id=a.id, name="测试计划", race_type="hm",
                               start_date=date.today() - timedelta(days=7),
                               race_date=date.today() + timedelta(weeks=6))
    db.add(plan)
    db.commit()
    assert "半马" in coach_persona.persona_for_athlete(db, a.id)
    # 设活动目标后目标优先
    db.add(models.Goal(athlete_id=a.id, race_type="marathon", status="active"))
    db.commit()
    assert "名帅" in coach_persona.persona_for_athlete(db, a.id)


# ---------------------------------------------------------------- 注入位置
def test_system_prompt_carries_persona_and_keeps_rules(db):
    """【教练人设】注入在用户上下文之前；数字纪律等安全段落原样保留。"""
    a = _athlete(db)
    db.add(models.Goal(athlete_id=a.id, race_type="marathon", status="active"))
    db.commit()
    prompt = ai_coach.system_prompt(db)
    assert "【教练人设】" in prompt and "名帅" in prompt
    assert "【数字纪律】" in prompt and "【变更提案（改课表的唯一通道）】" in prompt


def test_comment_prompts_accept_persona_fact():
    """两类点评模板都以 {persona} 槽位吃人设，装配不缺键。"""
    base = {"today": "2026-09-30", "persona": "以名帅身份说话"}
    coach_comment.COMMENT_PROMPT.format(
        plan_line="计划", steps="步骤", actual_line="实际", context_line="背景", **base)
    coach_comment.ACTIVITY_PROMPT.format(
        activity_line="活动", context_line="背景", **base)


def test_activity_facts_carry_persona(db):
    act = models.Activity(
        athlete_id=_athlete(db).id, sport="run", title="轻松跑", platform="manual",
        start_time=datetime(2026, 6, 14, 8, 0), duration_sec=1800, distance_m=5000)
    db.add(act)
    db.commit()
    facts = coach_comment.gather_activity_facts(db, _athlete(db), act)
    assert facts["persona"] and "路跑教练" in facts["persona"]
