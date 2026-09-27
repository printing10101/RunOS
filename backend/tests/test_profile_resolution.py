"""运动画像解析层回归测试（``services/profile.py``）。

守住四条不变量：

1. **四级优先级** user > measured > derived > estimated，且实测能自动上调手填值；
2. **估计不覆盖手填**：没有实测依据时保留用户填的值，不拿群体公式盖掉它；
3. **没依据就 unknown**，绝不退回与用户无关的常数（历史上的 190/55/65/1995/2.0）；
4. **随数据实时变化**：同一档案，新增体测/训练后解析结果必须跟着变。

外加一条底线：**不同属性的用户必须解析出不同的画像**——这正是「按用户维度
动态生成」与「所有人共用一套默认值」的分水岭。
"""
from __future__ import annotations

import datetime as dt

import pytest
from app import models
from app.db import Base
from app.services import evaluator, load, predictor, profile, zones
from factories import ATHLETE_BASE, make_athlete
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

TODAY = dt.date.today()
EMPTY = {"activities": [], "metrics": []}


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _detached(**overrides) -> models.Athlete:
    """不在会话里的档案对象，配合显式 evidence 做纯解析测试。"""
    return models.Athlete(**{**ATHLETE_BASE, **overrides})


def _metrics(resting_hr=None, hrv=None, weight=None, n=20, days_back=1):
    return [{"date": TODAY - dt.timedelta(days=days_back + i),
             "weight_kg": weight, "resting_hr": resting_hr, "hrv_rmssd": hrv}
            for i in range(n)]


def _runs(count=6, max_hr=None, avg_hr=150, days=3):
    return [{"date": TODAY - dt.timedelta(days=i * days), "sport": "run",
             "avg_hr": avg_hr, "max_hr": max_hr, "distance_m": 10000,
             "duration_sec": 3300} for i in range(count)]


# ---------------------------------------------------------------- 不变量 3：不编造
def test_no_evidence_marks_unknown_instead_of_constants():
    """完全没有依据时必须是 unknown/None，且不得出现 190/55 这类常数。"""
    a = _detached(weight_kg=None, hrv_baseline=None, resting_hr=None, max_hr=None)
    prof = profile.resolve(None, a, evidence=EMPTY)

    assert prof["weight_kg"].source == profile.UNKNOWN
    assert prof["weight_kg"].value is None
    assert prof["weight_kg"].basis
    # 最大心率由本人年龄推导（Tanaka），而不是某个固定数字
    assert prof["max_hr"].value is not None
    assert prof["max_hr"].source == profile.DERIVED
    assert "Tanaka" in prof["max_hr"].basis
    # 静息心率也必须是「按本人属性估算」，且依据里写明参数
    assert prof["resting_hr"].source == profile.ESTIMATED
    assert "岁" in prof["resting_hr"].basis


def test_model_has_no_hardcoded_athlete_defaults():
    """模型层不得再出现写死的档案默认值（曾导致所有用户共用一套画像）。"""
    cols = {c.key: c for c in inspect(models.Athlete).columns}
    for field in ("sex", "birth_year", "height_cm", "weight_kg",
                  "resting_hr", "max_hr", "hrv_baseline", "training_age_years"):
        assert cols[field].default is None, f"{field} 又出现了写死的列默认值"


def test_hr_consumers_refuse_to_fabricate():
    """心率消费者的常数兜底必须消失：缺基准就明说，不猜。"""
    assert zones.hr_zones(None, None) == []
    assert zones.hr_zones(190, None) == []
    dist = zones.intensity_distribution(_runs(), max_hr=None, resting_hr=None)
    assert dist["easy_pct"] is None and "无法分档" in dist["verdict"]
    assert load.hr_reserve(150, {"max_hr": None, "resting_hr": None}) is None
    assert load.estimate_load(3600, 150, None, {})[1] == "unknown"


# ---------------------------------------------------------------- 不变量 1：优先级
def test_measured_resting_hr_beats_user_value():
    """近 4 周晨测中位数比单次手填更可靠 → 实测优先。"""
    a = _detached(resting_hr=52)
    prof = profile.resolve(None, a, evidence={"activities": [], "metrics": _metrics(resting_hr=48)})
    assert prof["resting_hr"].value == 48
    assert prof["resting_hr"].source == profile.MEASURED
    assert "中位数" in prof["resting_hr"].basis


def test_observed_peak_auto_raises_max_hr():
    """观测到的峰值心率是硬事实：高于档案值就自动上调。"""
    a = _detached(max_hr=180)
    prof = profile.resolve(None, a, evidence={"activities": _runs(max_hr=193), "metrics": []})
    assert prof["max_hr"].value == 193
    assert prof["max_hr"].source == profile.MEASURED


def test_observed_peak_below_profile_keeps_user_value():
    """实测峰值没超过手填值时保留手填值，并说明原因。"""
    a = _detached(max_hr=200)
    prof = profile.resolve(None, a, evidence={"activities": _runs(max_hr=185), "metrics": []})
    assert prof["max_hr"].value == 200
    assert "保留你的值" in prof["max_hr"].basis


# ---------------------------------------------------------------- 不变量 2：估计不盖手填
def test_estimated_value_does_not_override_user_input():
    """用户填过的心率，不能被年龄公式推出来的估计覆盖。"""
    a = _detached(max_hr=195)
    profile.mark_user_fields(a, ["max_hr"])
    prof = profile.resolve(None, a, evidence=EMPTY)
    assert prof["max_hr"].value == 195
    assert prof["max_hr"].source == profile.USER


def test_locked_field_never_auto_updates():
    """用户把字段钉死（关掉自动推算）后，实测数据也不得改动它。"""
    a = _detached(resting_hr=52)
    a.profile_meta = {"resting_hr": {"locked": True, "source": profile.USER}}
    prof = profile.resolve(None, a, evidence={"activities": [], "metrics": _metrics(resting_hr=45)})
    assert prof["resting_hr"].value == 52
    assert prof["resting_hr"].source == profile.USER


# ---------------------------------------------------------------- 不变量 4：随数据变化
def test_training_age_derived_from_activity_history():
    """训练年限从活动历史推导——不再是一个写死的 2.0 年。"""
    acts = []
    for w in range(60):                       # 60 周、每周 2 次
        for k in (0, 3):
            acts.append({"date": TODAY - dt.timedelta(days=w * 7 + k), "sport": "run",
                         "avg_hr": 145, "max_hr": None, "distance_m": 8000,
                         "duration_sec": 2700})
    a = _detached(training_age_years=0.0)
    prof = profile.resolve(None, a, evidence={"activities": acts, "metrics": []})
    assert prof["training_age_years"].source == profile.DERIVED
    assert prof["training_age_years"].value >= 1.0
    assert "活动记录跨度" in prof["training_age_years"].basis


def test_refresh_follows_new_data_in_real_time(db):
    """同一档案：新增体测后，落库字段必须跟着变。"""
    a = make_athlete(name="实时性", resting_hr=60)
    db.add(a)
    db.commit()

    p1 = profile.refresh(db, a, force=True)
    first = p1["resting_hr"].value

    # 写入 20 条静息心率 46 的体测 → 解析结果应立刻跟随
    for i in range(20):
        db.add(models.BodyMetric(athlete_id=a.id, date=TODAY - dt.timedelta(days=i + 1),
                                 resting_hr=46))
    db.commit()

    p2 = profile.refresh(db, a, force=True)
    assert p2["resting_hr"].value == 46
    assert a.resting_hr == 46                     # 已回写到库内字段
    assert first != a.resting_hr
    assert (a.profile_meta or {}).get("_refresh", {}).get("changed")


def test_refresh_records_provenance_for_every_auto_field(db):
    """每个可自动字段都要有来源与依据，前端才能解释「这个数字从哪来」。"""
    a = make_athlete(name="溯源")
    db.add(a)
    db.commit()
    db.add(models.BodyMetric(athlete_id=a.id, date=TODAY, weight_kg=72.5))
    db.commit()

    profile.refresh(db, a, force=True)
    meta = a.profile_meta or {}
    for field in profile.AUTO_FIELDS:
        entry = meta.get(field)
        assert entry, f"{field} 缺少来源记录"
        assert entry["source"] in (profile.USER, profile.MEASURED, profile.DERIVED,
                                   profile.ESTIMATED, profile.UNKNOWN)
        assert entry["basis"]
        assert "locked" in entry
    assert meta["weight_kg"]["value"] == 72.5


def test_weight_follows_latest_body_metric(db):
    """体重随最近一次体测滚动，不再固定为建库时那个数。"""
    a = make_athlete(name="体重", weight_kg=80.0)
    db.add(a)
    db.commit()
    db.add(models.BodyMetric(athlete_id=a.id, date=TODAY - dt.timedelta(days=2), weight_kg=77.0))
    db.commit()
    p = profile.refresh(db, a, force=True)
    assert p["weight_kg"].value == 77.0
    assert a.weight_kg == 77.0


# ---------------------------------------------------------------- 底线：按用户区分
def test_two_different_users_get_different_profiles():
    """不同属性的用户必须解析出不同画像——这是本机制存在的前提。"""
    # 两份都没有手填心率，全部交给「按本人属性估算」的路径
    young_beginner = _detached(birth_year=TODAY.year - 19, sex="male",
                               training_age_years=0.0, resting_hr=None, hrv_baseline=None)
    older_trained = _detached(birth_year=TODAY.year - 46, sex="female",
                              training_age_years=8.0, resting_hr=None, hrv_baseline=None)

    p1 = profile.resolve(None, young_beginner, evidence=EMPTY)
    p2 = profile.resolve(None, older_trained, evidence=EMPTY)

    assert p1["max_hr"].value != p2["max_hr"].value
    assert p1["resting_hr"].value != p2["resting_hr"].value
    assert p1["hrv_baseline"].value != p2["hrv_baseline"].value


def test_weekly_km_is_measured_not_assumed():
    a = _detached()
    prof = profile.resolve(None, a, evidence={"metrics": [], "activities": _runs(count=8, days=3)})
    assert prof["weekly_km_now"].source == profile.MEASURED
    assert prof["weekly_km_now"].value > 0


# ---------------------------------------------------------------- 评估层不伪造中值
def test_recovery_renormalizes_weights_when_signals_missing():
    """只有睡眠一项时，得分应等于该项本身——而不是被缺项拉低到 25%。"""
    metrics = [{"date": TODAY - dt.timedelta(days=i), "sleep_hours": 8.0,
                "hrv_rmssd": None, "resting_hr": None} for i in range(10)]
    r = evaluator.eval_recovery(metrics, [])
    assert r["score"] == 100                      # 睡眠 8h = 满分，权重归一后仍是满分
    assert r["coverage"] < 1.0
    assert any("HRV" in g for g in r["gaps"])

    empty = evaluator.eval_recovery([], [])
    assert empty["score"] is None and empty["gaps"]
    assert empty["acwr"] is None


def test_talent_drops_missing_components_instead_of_defaulting():
    """天赋评估缺项时整块剔除并报告缺口，不再填 0.55/0.4 这类伪中值。"""
    pred = predictor.PredictionResult(current_vdot=40.0)
    t = evaluator.eval_talent(pred, [], age=30, sex="male", training_age=0.0,
                              weight_kg=None, height_cm=None)
    assert "身体条件" not in t["components"]
    assert "训练响应率" not in t["components"]
    assert t["response_score"] is None
    assert any("身高" in g for g in t["gaps"])
    assert any("响应" in g for g in t["gaps"])
    assert t["coverage"] < 1.0


def test_career_ceiling_without_adherence_reports_gap():
    """没有计划执行数据时不得假装执行率是 85%。"""
    pred = predictor.PredictionResult(
        current_vdot=40.0,
        predictions={"5k": {"time_sec": 1500, "time_str": "25:00", "pace": "5:00/km"}},
    )
    res = predictor.estimate_career_ceiling(
        pred, talent_score=50.0, age=30, weekly_km=40.0,
        training_age_years=2.0, adherence=None, race_type="5k")
    assert "执行一致性" not in res["headroom_components"]
    assert any("执行" in g for g in res["gaps"])


# ---------------------------------------------------------------- API 层写入语义
@pytest.fixture()
def api(db):
    """覆盖 get_db 依赖的 TestClient（用上面的内存库，不碰真实库文件）。"""
    from app.db import get_db
    from app.main import app
    from fastapi.testclient import TestClient

    def _ov():
        yield db

    app.dependency_overrides[get_db] = _ov
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


_FULL = {"name": "接口测试", "sex": "male", "birth_year": 1996,
         "height_cm": 175.0, "weight_kg": 65.0}


def test_first_create_requires_identity_facts(api):
    """身份事实无法推导，首次建档缺了就明确报错，而不是替他编一个。"""
    r = api.put("/api/athlete", json={"name": "新用户"})
    assert r.status_code == 422
    detail = r.json()["detail"]
    for label in ("性别", "出生年份", "身高", "体重"):
        assert label in detail


def test_partial_update_does_not_touch_other_fields(api):
    """只传体重时，其余字段必须保持不动（历史实现会用 schema 默认值整体覆盖）。"""
    api.put("/api/athlete", json=_FULL)
    before = api.get("/api/athlete").json()["athlete"]

    r = api.put("/api/athlete", json={"weight_kg": 80.0})
    assert r.status_code == 200
    after = r.json()["athlete"]
    assert after["weight_kg"] == 80.0
    for k in ("max_hr", "resting_hr", "hrv_baseline", "training_age_years",
              "birth_year", "height_cm"):
        assert after[k] == before[k], f"{k} 被无关更新改动了"


def test_profile_endpoint_exposes_source_and_basis(api):
    """接口要能回答「这个数字从哪来」——来源、依据、锁定状态都要给到前端。"""
    api.put("/api/athlete", json=_FULL)
    p = api.get("/api/athlete").json()["profile"]
    assert p["auto_fields"]
    for key in ("max_hr", "resting_hr", "hrv_baseline", "weight_kg", "training_age_years"):
        f = p["fields"][key]
        assert f["source"] in ("user", "measured", "derived", "estimated", "unknown")
        assert f["basis"]
        assert isinstance(f["locked"], bool)
    assert p["fields"]["weight_kg"]["value"] == 65.0


def test_auto_toggle_pins_then_releases(api, db):
    """关掉自动 → 钉死；打开自动 → 实测数据立刻接管。"""
    api.put("/api/athlete", json=_FULL)
    for i in range(20):
        db.add(models.BodyMetric(athlete_id=1, date=TODAY - dt.timedelta(days=i + 1),
                                 resting_hr=47))
    db.commit()

    pinned = api.put("/api/athlete/auto", json={"field": "resting_hr", "auto": False}).json()
    f = pinned["profile"]["fields"]["resting_hr"]
    assert f["locked"] is True

    released = api.put("/api/athlete/auto", json={"field": "resting_hr", "auto": True}).json()
    f = released["profile"]["fields"]["resting_hr"]
    assert f["locked"] is False
    assert f["value"] == 47
    assert f["source"] == "measured"


def test_explicit_null_hands_field_back_to_auto(api):
    """显式传 null = 「清空，交回系统算」，不是「保留手动值」。"""
    api.put("/api/athlete", json=_FULL | {"max_hr": 175})
    assert api.get("/api/athlete").json()["profile"]["fields"]["max_hr"]["locked"] is False

    r = api.put("/api/athlete", json={"max_hr": None})
    f = r.json()["profile"]["fields"]["max_hr"]
    # 交回自动后由年龄/训练数据推导，不再是手填的 175
    assert f["source"] != "user"
    assert "Tanaka" in f["basis"] or "推算" in f["basis"]
