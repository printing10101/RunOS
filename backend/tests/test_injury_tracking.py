"""伤病结构化追踪（services/injury.py + /api/health/injuries）的口径锁定。

锚定三条：
- 恢复期建议只出「档位 + 理由」，不改课表；等级 × 天数决定档位，红线单独给；
- 关联分析只统计负荷窗口覆盖得到的伤病，样本 <2 如实 insufficient；
- 端点校验：痊愈日期早于起始日期必须 400。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from app import models
from app.db import get_db
from app.main import app
from app.services.injury import (
    active_injuries,
    injuries_dicts,
    load_correlation,
    return_to_run_advice,
)
from factories import make_athlete
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# injury 引擎内部用 date.today() 计算 days_since_start，夹具跟着真实日期走
# （硬编码日期会在跨天后集体失配）
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
    """无档案的空库客户端：路由的 empty 分支锁定。"""
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


def _add_injury(db, start: date, severity: int = 1, ended: date | None = None,
                part="膝盖") -> models.InjuryLog:
    row = models.InjuryLog(athlete_id=1, start_date=start, end_date=ended,
                           body_part=part, side="left", severity=severity,
                           cause="跑量激增", notes="")
    db.add(row)
    db.commit()
    return row


# ---------------------------------------------------------------- 渐进建议

def _adv(sev: int, days: int) -> dict:
    return return_to_run_advice({"id": 1, "start_date": (TODAY - timedelta(days=days)).isoformat(),
                                 "end_date": None, "body_part": "膝盖", "side": "",
                                 "severity": sev, "cause": "", "notes": "",
                                 "days_since_start": days}, today=TODAY)


def test_advice_severity_3_progression():
    assert _adv(3, 2)["level"] == "rest"
    assert _adv(3, 10)["level"] == "easy"
    assert _adv(3, 30)["level"] == "reduce"
    assert "就医" in _adv(3, 30)["why"]


def test_advice_severity_2_and_1_progression():
    assert _adv(2, 2)["level"] == "easy"
    assert _adv(2, 5)["level"] == "reduce"
    assert _adv(2, 15)["level"] == "normal"
    assert _adv(1, 1)["level"] == "easy"
    assert _adv(1, 5)["level"] == "normal"
    # 疼痛监控红线覆盖所有档位
    assert "3/10" in _adv(1, 5)["pain_rule"]


def test_advice_none_without_injury():
    assert return_to_run_advice(None) is None


def test_active_injuries_excludes_ended(mem_db):
    _add_injury(mem_db, TODAY - timedelta(days=40), ended=TODAY - timedelta(days=10))
    open_row = _add_injury(mem_db, TODAY - timedelta(days=3), severity=2)
    actives = active_injuries(mem_db, 1)
    assert [r["id"] for r in actives] == [open_row.id]
    assert actives[0]["days_since_start"] == 3


# ---------------------------------------------------------------- 负荷关联

def _loads(days: int, plan) -> list[dict]:
    out = []
    for i in range(days):
        out.append({"date": (TODAY - timedelta(days=days - 1 - i)).isoformat(),
                    "load": plan(i)})
    return out


def test_correlation_insufficient_below_two(mem_db):
    _add_injury(mem_db, TODAY - timedelta(days=10))
    c = load_correlation(mem_db, 1)
    assert c["n"] == 1 and c["insufficient"] is True and c["detail"] is None


def test_correlation_detects_high_load_onset(mem_db):
    """60 天窗口：一段 7 天负荷尖峰后的伤病记「高位段起病」，稳定期的另一场不记。"""
    _add_injury(mem_db, TODAY - timedelta(days=25))   # 起病前 7 天 ≈ 尖峰段
    _add_injury(mem_db, TODAY - timedelta(days=3))    # 起病前 7 天 ≈ 稳定段

    def plan(i):
        # 以「今天往前 60 天」计：尖峰在 T-32..T-26，伤病 A 起病 T-25
        days_ago = 59 - i
        if 32 >= days_ago >= 26:
            return 2000
        return 700

    loads = _loads(60, plan)
    c = load_correlation(mem_db, 1, daily_loads=loads)
    assert c["insufficient"] is False and c["measured"] == 2
    assert c["onset_high_acwr"] == 1
    assert "高位段" in c["detail"]


def test_correlation_skips_injuries_outside_load_window(mem_db):
    """伤病早于负荷序列（前 27 天窗口不完整无法算 ACWR）：不计入 measured。"""
    _add_injury(mem_db, TODAY - timedelta(days=120))
    _add_injury(mem_db, TODAY - timedelta(days=100))
    c = load_correlation(mem_db, 1, daily_loads=_loads(60, lambda i: 700))
    assert c["measured"] == 0 and c["insufficient"] is True


# ---------------------------------------------------------------- 端点

def test_injury_crud_flow(mem_db, client):
    r = client.post("/api/health/injuries", json={
        "start_date": TODAY.isoformat(), "body_part": "足底", "severity": 2,
        "side": "left", "cause": "跑量激增"})
    assert r.status_code == 200 and r.json()["ok"] is True
    injury_id = r.json()["id"]

    panel = client.get("/api/health/injuries").json()
    assert panel["empty"] is False and panel["active_count"] == 1
    assert panel["advice"]["body_part"] == "足底（left）"
    assert panel["items"][0]["body_part"] == "足底"

    # 标记痊愈（PUT end_date）后：active_count 归零，建议消失
    r2 = client.put(f"/api/health/injuries/{injury_id}", json={
        "start_date": TODAY.isoformat(), "end_date": TODAY.isoformat(),
        "body_part": "足底", "severity": 2})
    assert r2.status_code == 200
    panel2 = client.get("/api/health/injuries").json()
    assert panel2["active_count"] == 0 and panel2["advice"] is None

    r3 = client.delete(f"/api/health/injuries/{injury_id}")
    assert r3.status_code == 200
    assert client.get("/api/health/injuries").json()["items"] == []


def test_injury_end_before_start_rejected(mem_db, client):
    r = client.post("/api/health/injuries", json={
        "start_date": TODAY.isoformat(), "end_date": (TODAY - timedelta(days=1)).isoformat(),
        "body_part": "小腿"})
    assert r.status_code == 400


def test_injury_endpoint_without_athlete(client_empty):
    r = client_empty.get("/api/health/injuries")
    assert r.status_code == 200 and r.json()["empty"] is True


def test_injuries_dicts_order_and_label(mem_db):
    _add_injury(mem_db, TODAY - timedelta(days=5), severity=3)
    _add_injury(mem_db, TODAY - timedelta(days=2), severity=1, part="小腿")
    items = injuries_dicts(mem_db, 1)
    assert [i["body_part"] for i in items] == ["小腿", "膝盖"]   # 起始日倒序
    assert items[0]["severity_label"] == "轻微" and items[1]["severity_label"] == "严重"
