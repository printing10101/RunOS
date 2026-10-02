"""点评的环境注记：高温天不冤枉人。

锁三条：活动自带气温/天气进数据行（手表同步路径）；活动缺气温时手动天气
日志补「当日环境」行；两条规则兜底都把环境说出来（模型不可用时也不缺席）。
"""
from __future__ import annotations

from datetime import date, datetime

import pytest
from app import models
from app.services import coach_comment, weather
from factories import make_athlete
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    s.add(make_athlete())
    s.commit()
    yield s
    s.close()


def _athlete(db) -> models.Athlete:
    return db.query(models.Athlete).first()


def _activity(db, athlete, **kw) -> models.Activity:
    fields = dict(athlete_id=athlete.id, sport="run", title="节奏跑",
                  start_time=datetime(2026, 6, 14, 8, 0), duration_sec=2400,
                  distance_m=7000)
    fields.update(kw)
    act = models.Activity(**fields)
    db.add(act)
    db.commit()
    return act


def test_watch_temp_and_condition_enter_activity_line(db):
    a = _athlete(db)
    act = _activity(db, a, temp_c=31.0, weather="晴热")
    facts = coach_comment.gather_activity_facts(db, a, act)
    assert "气温 31℃" in facts["activity_line"]
    assert "天气 晴热" in facts["activity_line"]
    assert facts["env_line"] == ""          # 活动自带气温，环境行不重复（避免两个温度打架）
    body = coach_comment.fallback_activity_comment(facts)
    assert "气温 31℃" in body


def test_weather_log_fills_env_when_activity_lacks_temp(db):
    a = _athlete(db)
    act = _activity(db, a)                  # 无 temp_c（列表接口同步的常见形态）
    weather.upsert_day_weather(db, a, date(2026, 6, 14), temp_c=33.0, condition="闷热")
    facts = coach_comment.gather_activity_facts(db, a, act)
    assert "当日环境" in facts["context_line"]
    assert "气温 33℃" in facts["env_line"] and "闷热" in facts["env_line"]
    body = coach_comment.fallback_activity_comment(facts)
    assert "当日环境" in body and "对比时把它算进去" in body


def test_no_weather_no_env_line(db):
    a = _athlete(db)
    act = _activity(db, a)
    facts = coach_comment.gather_activity_facts(db, a, act)
    assert facts["env_line"] == ""
    assert "当日环境" not in facts["context_line"]
    assert "当日环境" not in coach_comment.fallback_activity_comment(facts)


def test_env_note_passes_review_guard(db):
    """环境数字进了 facts，LLM 引用它们不会被核验误杀。"""
    from app.services import review_guard
    facts = {"activity_line": "气温 31℃，天气 晴热", "env_line": "当日环境：气温 31℃",
             "context_line": "当日环境：气温 31℃"}
    review_guard.check("31℃ 高温下配速偏慢属正常现象，不影响质量判断。", facts)
