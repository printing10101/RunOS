"""数据探索端点（/api/explore/*）的行为锁定。

产品红线：端点只接受结构化参数——表/列/聚合组合三重白名单，未登记的
表、分组列、筛选列、聚合组合一律 400，不存在裸 SQL 通道。同时锁定：
档案圈定（B 档案数据不串入）、日期粒度分组（day/month/weekday）、
日期范围上界的「末日全天」语义、明细下钻与值中文映射。
"""
from __future__ import annotations

from datetime import date, datetime

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
    # StaticPool：TestClient 在另一线程跑端点，内存库必须全线程同一连接
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


def _seed(db):
    """档案 A（默认，id 最小）9 月三条活动 + 两条计划课；档案 B 一条活动。"""
    a = make_athlete(name="档案A")
    b = make_athlete(name="档案B")
    db.add_all([a, b])
    db.flush()
    acts = [
        models.Activity(athlete_id=a.id, sport="run", title="轻松跑", platform="coros",
                        start_time=datetime(2026, 9, 1, 7, 0), duration_sec=1800,
                        distance_m=5000, training_load=100),
        models.Activity(athlete_id=a.id, sport="run", title="长距离", platform="coros",
                        start_time=datetime(2026, 9, 15, 8, 0), duration_sec=3600,
                        distance_m=10000, training_load=200),
        models.Activity(athlete_id=a.id, sport="ride", title="骑行", platform="manual",
                        start_time=datetime(2026, 9, 20, 9, 0), duration_sec=3600,
                        distance_m=20000, training_load=150),
        models.Activity(athlete_id=b.id, sport="run", title="别人的跑", platform="coros",
                        start_time=datetime(2026, 9, 2, 7, 0), duration_sec=1800,
                        distance_m=99999, training_load=99),
    ]
    db.add_all(acts)
    db.add_all([
        models.PlanWorkout(week_id=0, athlete_id=a.id, date=date(2026, 9, 1),
                           session_type="easy", title="轻松跑6km", distance_km=6,
                           duration_min=36, status="completed", completed_source="manual"),
        models.PlanWorkout(week_id=0, athlete_id=a.id, date=date(2026, 9, 10),
                           session_type="tempo", title="节奏跑8km", distance_km=8,
                           duration_min=48, status="planned"),
    ])
    db.commit()
    return a


def _query(client, **overrides):
    body = {"table": "activities", "metrics": [{"col": "*", "agg": "count"}], **overrides}
    return client.post("/api/explore/query", json=body)


# ---------------------------------------------------------------- schema
def test_schema_lists_whitelisted_tables_and_grain_dims(client, mem_db):
    _seed(mem_db)
    d = client.get("/api/explore/schema").json()
    keys = {t["key"] for t in d["tables"]}
    assert keys == {"activities", "plan_workouts", "daily_checkins",
                    "body_metrics", "fitness_snapshots", "injury_logs"}
    act = next(t for t in d["tables"] if t["key"] == "activities")
    dim_keys = {x["key"] for x in act["dims"]}
    assert {"sport", "platform", "day", "month", "weekday"} <= dim_keys
    assert {"distance_m", "training_load"} <= {m["key"] for m in act["measures"]}
    sport_dim = next(x for x in act["dims"] if x["key"] == "sport")
    assert sport_dim["labels"]["run"] == "跑步"


# ---------------------------------------------------------------- 聚合
def test_group_by_sport_with_count_and_sum(client, mem_db):
    _seed(mem_db)
    d = _query(client, group_by=["sport"],
               metrics=[{"col": "*", "agg": "count"}, {"col": "distance_m", "agg": "sum"}]).json()
    rows = {r["sport"]: r for r in d["rows"]}
    assert rows["run"]["*_count"] == 2 and rows["run"]["distance_m_sum"] == 15000
    assert rows["ride"]["*_count"] == 1 and rows["ride"]["distance_m_sum"] == 20000
    # 首指标降序：run（2 条）排前
    assert d["rows"][0]["sport"] == "run"
    assert d["label_maps"]["sport"]["ride"] == "骑行"


def test_no_group_by_returns_single_total_row(client, mem_db):
    _seed(mem_db)
    d = _query(client, metrics=[{"col": "*", "agg": "count"},
                                {"col": "training_load", "agg": "sum"}]).json()
    assert len(d["rows"]) == 1
    assert d["rows"][0]["*_count"] == 3
    assert d["rows"][0]["training_load_sum"] == 450


def test_month_and_weekday_grain_grouping(client, mem_db):
    _seed(mem_db)
    d = _query(client, group_by=["month"]).json()
    assert d["rows"] == [{"month": "2026-09", "*_count": 3}]
    d2 = _query(client, group_by=["weekday"]).json()
    # 9/1 周二、9/15 周二、9/20 周日
    by_day = {r["weekday"]: r["*_count"] for r in d2["rows"]}
    assert by_day == {"2": 2, "0": 1}
    assert d2["label_maps"]["weekday"]["2"] == "周二"


def test_scoped_to_default_athlete(client, mem_db):
    _seed(mem_db)
    d = _query(client).json()
    assert d["rows"][0]["*_count"] == 3  # 档案 B 的第 4 条不串入


# ---------------------------------------------------------------- 筛选与日期
def test_date_range_and_eq_filter(client, mem_db):
    _seed(mem_db)
    d = _query(client, date_from=date(2026, 9, 10).isoformat()).json()
    assert d["rows"][0]["*_count"] == 2  # 9/15 与 9/20；上界语义见 test_date_to_includes_full_last_day
    d2 = _query(client, filters=[{"col": "sport", "op": "eq", "value": "run"}]).json()
    assert d2["rows"][0]["*_count"] == 2


def test_date_to_includes_full_last_day(client, mem_db):
    """date_to 是 DateTime 列：上界必须含末日全天（< 次日），不能 <= 当日零点。"""
    _seed(mem_db)
    d = _query(client, date_from="2026-09-15", date_to="2026-09-15").json()
    assert d["rows"][0]["*_count"] == 1  # 9/15 08:00 的活动不能被字符串比较裁掉


def test_grain_dim_filter_drills_down(client, mem_db):
    _seed(mem_db)
    d = _query(client, filters=[{"col": "month", "op": "eq", "value": "2026-09"}]).json()
    assert d["rows"][0]["*_count"] == 3


def test_detail_rows_ordered_desc_and_plain_values(client, mem_db):
    _seed(mem_db)
    d = client.get("/api/explore/rows", params={"table": "activities"}).json()
    assert [r["title"] for r in d["rows"]] == ["骑行", "长距离", "轻松跑"]
    assert isinstance(d["rows"][0]["start_time"], str)
    labels = {c["key"]: c["label"] for c in d["columns"]}
    assert labels["title"] == "标题"


def test_detail_rows_accepts_drilldown_filters(client, mem_db):
    _seed(mem_db)
    d = client.get("/api/explore/rows", params={
        "table": "plan_workouts",
        "filters": '[{"col": "session_type", "op": "eq", "value": "tempo"}]',
    }).json()
    assert len(d["rows"]) == 1 and d["rows"][0]["title"] == "节奏跑8km"


# ---------------------------------------------------------------- 白名单红线
@pytest.mark.parametrize("bad_body", [
    {"table": "athletes"},                                        # 表不在白名单
    {"group_by": ["title"]},                                      # 未登记的分组列
    {"filters": [{"col": "external_id", "op": "eq", "value": "x"}]},  # 未登记的筛选列
    {"metrics": [{"col": "*", "agg": "sum"}]},                    # * 只配 count
    {"metrics": [{"col": "title", "agg": "sum"}]},                # 非指标列
])
def test_whitelist_rejects_unknown_inputs(client, mem_db, bad_body):
    _seed(mem_db)
    body = {"table": "activities", "metrics": [{"col": "*", "agg": "count"}], **bad_body}
    resp = client.post("/api/explore/query", json=body)
    assert resp.status_code == 400


def test_unknown_group_key_rejected(client, mem_db):
    _seed(mem_db)
    resp = _query(client, group_by=["nosuch"])
    assert resp.status_code == 400


def test_invalid_filter_op_is_422(client, mem_db):
    """op 走 pydantic Literal：词汇外直接 422（参数校验层就挡住）。"""
    _seed(mem_db)
    resp = _query(client, filters=[{"col": "sport", "op": "regex", "value": "x"}])
    assert resp.status_code == 422


def test_like_filter_is_literal_not_pattern(client, mem_db):
    """like 的值按字面量匹配：% 不当通配符解释，转义后不放大匹配范围。"""
    _seed(mem_db)
    d = _query(client, filters=[{"col": "sport", "op": "like", "value": "%"}]).json()
    assert d["rows"][0]["*_count"] == 0
