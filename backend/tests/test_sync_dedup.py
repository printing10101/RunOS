"""同步去重路径的回归测试。

用内存 SQLite + 假适配器走完整 execute_sync 路径，不触网、不碰真实库；
覆盖「库中已有该平台带 external_id 的活动」这一纯函数测试覆盖不到的前提。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from app import models
from app.integrations.base import NormalizedActivity
from app.routers.connections import execute_sync
from factories import make_athlete
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


@pytest.fixture()
def coros_row(db):
    """一个已连接的高驰连接 + 一条带长 external_id 的历史活动。"""
    athlete = make_athlete(name="测试跑者", birth_year=1996)
    db.add(athlete)
    db.flush()
    row = models.PlatformConnection(athlete_id=athlete.id, platform="coros",
                                    credentials={}, status="connected")
    db.add(row)
    db.add(models.Activity(athlete_id=athlete.id, external_id="123456789012345678",
                           platform="coros", sport="run", title="已有活动",
                           start_time=datetime(2026, 9, 1, 8, 0),
                           duration_sec=3600, distance_m=8000))
    db.commit()
    return row


class FakeAdapter:
    """返回预设活动列表的假适配器，代替真实平台适配器。"""

    def __init__(self, credentials, activities=None):
        self.credentials = dict(credentials or {})
        self.activities = activities or []

    def check_config(self):
        pass

    def fetch_activities(self, since, until=None):
        return list(self.activities)


@pytest.fixture()
def fake_adapter(monkeypatch):
    def _install(activities):
        from app.routers import connections
        monkeypatch.setattr(connections, "ADAPTERS", {
            "coros": lambda creds: FakeAdapter(creds, activities)})
    return _install


def _norm(external_id, start):
    return NormalizedActivity(external_id=external_id, sport="run", title="Outdoor Run",
                              start_time=start, duration_sec=1800, distance_m=4000)


def test_sync_does_not_crash_when_db_has_activities(db, coros_row, fake_adapter):
    """库中已有该平台活动（18 位 external_id）时，同步正常完成。"""
    fake_adapter([])
    result = execute_sync(db, coros_row, since_days=14, detail_limit=5, backfill_detail=False)
    assert result["ok"] is True
    assert result["fetched"] == 0
    assert result["added"] == 0


def test_sync_dedup_skips_known_external_id(db, coros_row, fake_adapter):
    """同 external_id 的活动不重复入库；新 id 正常入库。"""
    fake_adapter([
        _norm("123456789012345678", datetime(2026, 9, 1, 8, 0)),   # 与库中重复
        _norm("NEWACTIVITY1", datetime.now() - timedelta(hours=2)),
    ])
    result = execute_sync(db, coros_row, since_days=14, detail_limit=5, backfill_detail=False)
    assert result["ok"] is True
    assert result["added"] == 1
    assert result["fetched"] == 2
