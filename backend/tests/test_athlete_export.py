"""/api/athlete/export 全量导出的行为锁定。

数据所有权出口的三条纪律：
· 无档案 404（不是空文件糊弄）；
· 导出物涵盖训练/身体/计划/AI 记忆等本人数据行，且只含本人；
· 第三方凭据（platform_connections.credentials 的 token）绝不入内。
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest
from app import models
from app.db import get_db
from app.main import app
from factories import make_athlete
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


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


def test_export_requires_athlete(client):
    assert client.get("/api/athlete/export").status_code == 404


def test_export_covers_all_my_data_and_headers(client, mem_db):
    a = make_athlete()
    mem_db.add(a)
    mem_db.commit()
    mem_db.add(models.Activity(athlete_id=a.id, title="晨跑", sport="run",
                               start_time=datetime.now(), duration_sec=1800,
                               distance_m=5000))
    mem_db.add(models.CoachNote(athlete_id=a.id, content="喜欢晨跑"))
    mem_db.add(models.PlatformConnection(
        athlete_id=a.id, platform="coros", credentials={"token": "should-not-leak"}))
    mem_db.commit()

    resp = client.get("/api/athlete/export")
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.headers["content-disposition"].endswith('.json"')

    data = json.loads(resp.content)
    assert data["app"] == "RunOS"
    assert data["athlete"]["id"] == a.id
    assert len(data["activities"]) == 1
    assert data["activities"][0]["title"] == "晨跑"
    assert data["coach_notes"][0]["content"] == "喜欢晨跑"
    # 空表也带键（消费方不用猜 schema），连接元数据在、凭据不在
    assert "diet_logs" in data
    assert data["platform_connections"][0]["platform"] == "coros"
    assert "credentials" not in data["platform_connections"][0]
    assert "should-not-leak" not in resp.text
