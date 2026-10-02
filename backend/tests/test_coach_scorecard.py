"""教练成绩单的口径锁定：提案采纳漏斗、采纳后执行落点、逐周执行率对照。

锚定三条纪律（改 coach_scorecard.py 先跑这里）：
- 自动撤回（withdrawn）不算用户的决定，不进采纳率；
- skip 类提案的「落地」是课被跳过（status=skipped），按落地计；
- 逐周执行率分母与 plan_adherence_dict 同源：已到期、非 rest、completed/synced。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from app import models
from app.db import get_db
from app.main import app
from app.services import coach_scorecard
from factories import make_athlete
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


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


@pytest.fixture()
def mem_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
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


def _athlete(db) -> models.Athlete:
    return db.query(models.Athlete).first()


def _proposal(db, athlete, kind: str, status: str, payload: dict | None = None,
              decided_at: datetime | None = None) -> models.AiProposal:
    p = models.AiProposal(athlete_id=athlete.id, kind=kind, payload=payload or {},
                          title=f"{kind} 建议", source="engine", status=status)
    if decided_at:
        p.decided_at = decided_at
    db.add(p)
    db.commit()
    return p


def _seed_plan(db, athlete, workout_rows: list[tuple[date, str, str]]) -> None:
    """workout_rows: [(date, session_type, status)]，按周一归周建 week。"""
    days = [d for d, *_ in workout_rows]
    plan = models.TrainingPlan(athlete_id=athlete.id, name="测试计划", race_type="10k",
                               start_date=min(days), race_date=max(days) + timedelta(days=30))
    db.add(plan)
    db.flush()
    weeks: dict[date, models.PlanWeek] = {}
    for d, st, status in workout_rows:
        ws = d - timedelta(days=d.weekday())
        if ws not in weeks:
            weeks[ws] = models.PlanWeek(plan_id=plan.id, week_index=len(weeks) + 1,
                                        start_date=ws, phase="base")
            db.add(weeks[ws])
            db.flush()
        db.add(models.PlanWorkout(week_id=weeks[ws].id, athlete_id=athlete.id, date=d,
                                  session_type=st, title=st, status=status))
    db.commit()


# ---------------------------------------------------------------- 采纳漏斗
def test_empty_scorecard(db):
    card = coach_scorecard.build_scorecard(db, _athlete(db))
    assert card["empty"] is True
    assert card["proposals"]["acceptance_rate"] is None
    assert card["execution"]["tracked"] == 0 and card["execution"]["landed_rate"] is None
    assert card["weeks"] == []
    assert "还没有提案记录" in card["headline"]


def test_acceptance_funnel_excludes_withdrawn(db):
    """采纳率分母 = 采纳 + 忽略；自动撤回是系统行为，不算用户的决定。"""
    a = _athlete(db)
    _proposal(db, a, "move_workout", "applied")
    _proposal(db, a, "easy_replacement", "applied")
    _proposal(db, a, "quality_adjustment", "dismissed")
    _proposal(db, a, "easy_replacement", "withdrawn")
    _proposal(db, a, "add_workout", "pending")
    s = coach_scorecard.proposal_stats(db, a)
    assert s["total"] == 5 and s["decided"] == 3
    assert s["applied"] == 2 and s["dismissed"] == 1
    assert s["acceptance_rate"] == round(2 / 3, 3)
    assert s["withdrawn"] == 1 and s["pending"] == 1
    kinds = {k["kind"]: k for k in s["by_kind"]}
    assert kinds["easy_replacement"]["emitted"] == 2
    assert kinds["easy_replacement"]["acceptance_rate"] == 1.0   # 1 采纳 + 1 撤回
    assert kinds["quality_adjustment"]["acceptance_rate"] == 0.0


def test_chat_emitted_counts_message_cards(db):
    """对话提案卡只在消息 JSON 里：数「发过几张」，不影响采纳率口径。"""
    a = _athlete(db)
    conv = models.AiConversation(athlete_id=a.id, title="会话")
    db.add(conv)
    db.flush()
    db.add(models.AiMessage(conversation_id=conv.id, role="assistant", content="好",
                            proposals=[{"kind": "move_workout"}, {"kind": "add_workout"}]))
    db.commit()
    s = coach_scorecard.proposal_stats(db, a)
    assert s["chat_emitted"] == 2
    assert s["total"] == 0 and s["acceptance_rate"] is None


# ---------------------------------------------------------------- 执行落点
def test_execution_landing_outcomes(db):
    a = _athlete(db)
    today = date.today()
    _seed_plan(db, a, [
        (today - timedelta(days=2), "easy", "completed"),     # 挪课后已完成 → 落地
        (today - timedelta(days=1), "interval", "skipped"),   # 跳课提案 → 落地
        (today + timedelta(days=1), "tempo", "planned"),      # 还没到期 → 待执行
    ])
    _proposal(db, a, "move_workout", "applied", payload={"workout_id": 1})
    _proposal(db, a, "skip_workout", "applied", payload={"workout_id": 2})
    _proposal(db, a, "quality_adjustment", "applied", payload={"workout_id": 3})
    _proposal(db, a, "profile_update", "applied", payload={"changes": {}})  # 无单课，不进口径
    _proposal(db, a, "move_workout", "applied", payload={"workout_id": 999})  # 课已不在，不编落点
    ex = coach_scorecard.execution_after_adoption(db, a)
    assert ex["tracked"] == 3
    assert ex["landed"] == 2 and ex["pending_execution"] == 1
    assert ex["landed_rate"] == round(2 / 3, 3)
    outcomes = {d["kind"]: d["outcome"] for d in ex["details"]}
    assert outcomes["move_workout"] == "completed"
    assert outcomes["skip_workout"] == "skipped"
    assert outcomes["quality_adjustment"] == "pending_execution"


# ---------------------------------------------------------------- 逐周对照
def test_weekly_series_rates_and_decision_attribution(db):
    a = _athlete(db)
    mon = date.today() - timedelta(days=date.today().weekday())   # 本周一
    last_mon = mon - timedelta(days=7)
    _seed_plan(db, a, [
        (last_mon, "easy", "completed"),
        (last_mon + timedelta(days=1), "interval", "completed"),
        (last_mon + timedelta(days=2), "long", "planned"),
        (last_mon - timedelta(days=7), "easy", "completed"),   # 上上周 1/1
    ])
    _proposal(db, a, "move_workout", "applied",
              decided_at=datetime.combine(last_mon + timedelta(days=1), time(2, 0)))
    _proposal(db, a, "quality_adjustment", "dismissed",
              decided_at=datetime.combine(last_mon + timedelta(days=2), time(3, 0)))
    series = coach_scorecard.weekly_series(db, a)
    assert [w["week_start"] for w in series] == [
        (last_mon - timedelta(days=7)).isoformat(), last_mon.isoformat()]
    older, last = series
    assert older["planned"] == 1 and older["execution_rate"] == 1.0
    assert last["planned"] == 3 and last["completed"] == 2
    assert last["execution_rate"] == round(2 / 3, 3)
    # UTC 凌晨的决定 +1 天松弛后仍归上周
    assert last["applied"] == 1 and last["dismissed"] == 1


def test_weekly_series_ignores_rest_and_future(db):
    """rest 课不进分母；本周未到期课不计入（分母同 plan_adherence_dict）。"""
    a = _athlete(db)
    mon = date.today() - timedelta(days=date.today().weekday())
    _seed_plan(db, a, [
        (mon, "easy", "completed"),
        (mon, "rest", "planned"),
        # 未来课不算分母。锚「明天」而不是「周一+N 天」：后者隐含「今天是周初」，
        # 周六/周日跑时 N=5/6 正好落在今天（已到期）被计入分母，测试日历脆弱。
        (date.today() + timedelta(days=1), "long", "planned"),
    ])
    series = coach_scorecard.weekly_series(db, a)
    assert len(series) == 1
    assert series[0]["planned"] == 1 and series[0]["completed"] == 1


# ---------------------------------------------------------------- 聚合与端点
def test_headline_states_facts(db):
    a = _athlete(db)
    _proposal(db, a, "move_workout", "applied")
    _proposal(db, a, "quality_adjustment", "dismissed")
    card = coach_scorecard.build_scorecard(db, a)
    assert card["empty"] is False
    assert "采纳 1 条" in card["headline"] and "采纳率 50%" in card["headline"]


def test_scorecard_endpoint(mem_db, client):
    mem_db.add(make_athlete())
    mem_db.commit()
    r = client.get("/api/ai/coach-scorecard")
    assert r.status_code == 200
    body = r.json()
    assert body["empty"] is True
    assert set(body.keys()) >= {"proposals", "execution", "weeks", "headline"}
