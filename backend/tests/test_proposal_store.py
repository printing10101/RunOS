"""提案持久层与采纳端点（ai_proposals + /api/ai/proposals/*）。

覆盖：引擎建议挂卡与去重、用户忽略后不复活、采纳走两段式重检（课表真的被改）、
重复处理 409、课表状态已变时采纳自动撤回建议并给出原因。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from app import models
from app.db import Base, get_db
from app.main import app
from app.services import ai_tools, plan_drift
from factories import make_athlete
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

# 日期冻结点：周三，保证「昨天漏课」与「未来空档」在同一周框架内
FREEZE = date(2026, 9, 30)


class FakeDate(date):
    @classmethod
    def today(cls):
        return cls(FREEZE.year, FREEZE.month, FREEZE.day)


@pytest.fixture()
def mem_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch):
    monkeypatch.setattr(plan_drift, "date", FakeDate)
    monkeypatch.setattr(ai_tools, "date", FakeDate)


@pytest.fixture()
def api(mem_db):
    a = make_athlete(name="测试跑者", birth_year=1996, max_hr=190, resting_hr=55)
    mem_db.add(a)
    mem_db.flush()
    monday = FREEZE - timedelta(days=FREEZE.weekday())
    plan = models.TrainingPlan(athlete_id=a.id, name="测试计划", race_type="5k",
                               start_date=monday, race_date=monday + timedelta(weeks=10))
    mem_db.add(plan)
    mem_db.flush()
    week = models.PlanWeek(plan_id=plan.id, week_index=1, start_date=monday,
                           phase="build", target_km=40)
    mem_db.add(week)
    mem_db.flush()
    # 昨天漏掉的长距离（尚未执行）+ 明天的轻松跑
    mem_db.add(models.PlanWorkout(week_id=week.id, athlete_id=a.id,
                                  date=FREEZE - timedelta(days=1), session_type="long",
                                  title="长距离 14km", distance_km=14, duration_min=90))
    mem_db.add(models.PlanWorkout(week_id=week.id, athlete_id=a.id,
                                  date=FREEZE + timedelta(days=1), session_type="easy",
                                  title="轻松跑", distance_km=6, duration_min=36))
    for wd in range(7):
        mem_db.add(models.WeeklySlot(athlete_id=a.id, weekday=wd, start_time="19:00",
                                     duration_minutes=90, kind="available"))
    mem_db.commit()

    def _ov():
        yield mem_db

    app.dependency_overrides[get_db] = _ov
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)


def _pending(api):
    r = api.get("/api/ai/proposals/pending")
    assert r.status_code == 200, r.text
    return r.json()["proposals"]


def test_pending_move_suggestion_with_dedup(api):
    """漏课建议挂卡；重复拉取不产生第二张卡。"""
    first = _pending(api)
    assert len(first) == 1
    card = first[0]
    assert card["kind"] == "move_workout" and card["status"] == "pending"
    assert card["payload"]["workout_id"] and "target_weekday" in card["payload"]
    assert card["severity"] == "medium" and card["reasons"]
    again = _pending(api)
    assert len(again) == 1 and again[0]["id"] == card["id"]


def test_apply_moves_workout_for_real(api, mem_db):
    """采纳挪课建议：课表真实被改（两段式重检的落地证明），卡片标记 applied。"""
    card = _pending(api)[0]
    old = mem_db.query(models.PlanWorkout).filter_by(title="长距离 14km").first()
    r = api.post(f"/api/ai/proposals/{card['id']}/apply")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["result"]["workout_id"] == old.id
    mem_db.refresh(old)
    assert old.date != FREEZE - timedelta(days=1)      # 真的挪走了
    assert old.date == mem_db.query(models.PlanWeek).first().start_date + timedelta(
        days=card["payload"]["target_weekday"])
    row = mem_db.get(models.AiProposal, card["id"])
    assert row.status == "applied" and row.decided_at is not None


def test_apply_twice_returns_conflict(api):
    card = _pending(api)[0]
    assert api.post(f"/api/ai/proposals/{card['id']}/apply").status_code == 200
    r = api.post(f"/api/ai/proposals/{card['id']}/apply")
    assert r.status_code == 409


def test_dismiss_prevents_resurrection(api, mem_db):
    """用户忽略过的建议：条件仍然成立也不许再挂回来。"""
    card = _pending(api)[0]
    assert api.post(f"/api/ai/proposals/{card['id']}/dismiss").status_code == 200
    assert _pending(api) == []
    row = mem_db.get(models.AiProposal, card["id"])
    assert row.status == "dismissed" and row.decided_at is not None


def test_apply_after_state_changed_auto_withdraws(api, mem_db):
    """挂卡之后课表状态变了（长距离已被手动完成）：采纳时重检失败 → 自动撤回并说明。"""
    card = _pending(api)[0]
    wo = mem_db.query(models.PlanWorkout).filter_by(title="长距离 14km").first()
    wo.status = "completed"
    mem_db.commit()
    r = api.post(f"/api/ai/proposals/{card['id']}/apply")
    assert r.status_code == 400
    assert "自动撤回" in r.json()["detail"]
    row = mem_db.get(models.AiProposal, card["id"])
    assert row.status == "withdrawn"
    assert _pending(api) == []


# ---------------------------------------------------------------- 时间线（阶段 5.2）
def test_history_lists_decisions(api):
    """采纳与忽略都会进时间线；pending 不在内。"""
    card = _pending(api)[0]
    api.post(f"/api/ai/proposals/{card['id']}/apply")
    r = api.get("/api/ai/proposals/history")
    assert r.status_code == 200
    history = r.json()["history"]
    assert [h["status"] for h in history] == ["applied"]
    assert history[0]["title"] == card["title"] and history[0]["decided_at"]


# ---------------------------------------------------------------- 周复盘并入引擎决策（阶段 5.3）
def test_weekly_recap_includes_engine_summary(api, mem_db):
    card = _pending(api)[0]
    api.post(f"/api/ai/proposals/{card['id']}/dismiss")
    from app.services.weekly_recap import build_weekly_recap
    recap = build_weekly_recap(mem_db)
    assert recap["engine"]["dismissed"] == 1 and recap["engine"]["applied"] == 0


# ---------------------------------------------------------------- 加急跟轮（阶段 5.1）
def test_adaptive_fast_rounds(monkeypatch):
    """拉到新数据 → 接下来几轮用短间隔；空闲轮逐次退出后回到配置间隔。"""
    from app.services import syncer

    monkeypatch.setattr(syncer, "_fast_rounds_left", 0)
    assert syncer.effective_interval(30) == 30          # 平时按配置

    syncer._update_fast_rounds(True)                    # 拉到新活动：进入跟轮期
    assert syncer.effective_interval(30) == syncer.FAST_ROUND_MINUTES
    for _ in range(syncer.FAST_ROUNDS):
        syncer._update_fast_rounds(False)               # 空闲轮逐次退出
    assert syncer.effective_interval(30) == 30          # 回到配置间隔
