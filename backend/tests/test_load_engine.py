"""训练负荷引擎回归测试。

本文件守住四条不变量：
  1. 个体心率参与计算（档案不同、同一活动结果必须不同）
  2. 缺数据不臆造（无心率且无 RPE → 0 + 来源 unknown）
  3. 强度对负荷非线性（Banister 相对线性公式的意义所在）
  4. 全项目共用同一心率口径（防止复制出第二套实现）
"""
from __future__ import annotations

from datetime import datetime

import pytest
from app.services.load import banister_trimp, estimate_load, hr_reserve, load_for_activity, session_rpe
from sqlalchemy import select

MALE = {"sex": "male", "max_hr": 190, "resting_hr": 55}
FEMALE = {"sex": "female", "max_hr": 190, "resting_hr": 55}


# ================================================================ 心率储备
def test_hr_reserve_endpoints_and_midpoint():
    assert hr_reserve(190, MALE) == pytest.approx(1.0)
    assert hr_reserve(55, MALE) == pytest.approx(0.0)
    assert hr_reserve(122.5, MALE) == pytest.approx(0.5)


def test_hr_reserve_clamps_out_of_range():
    """心率超出档案区间时必须裁剪，不能给出 >1 或 <0 的储备比。"""
    assert hr_reserve(220, MALE) == 1.0
    assert hr_reserve(30, MALE) == 0.0


def test_hr_reserve_missing_or_broken_profile():
    assert hr_reserve(None, MALE) is None
    assert hr_reserve(0, MALE) is None
    # 档案把静息填得 >= 最大：不得除零崩溃，返回 None 交由调用方降级
    assert hr_reserve(150, {"max_hr": 60, "resting_hr": 60}) is None
    assert hr_reserve(150, {"max_hr": 60, "resting_hr": 80}) is None


def test_hr_reserve_depends_on_individual_profile():
    """同一绝对心率，在不同个体档案下储备比必须不同。"""
    well_trained = {"max_hr": 180, "resting_hr": 45}
    average = {"max_hr": 200, "resting_hr": 70}
    assert hr_reserve(140, well_trained) > hr_reserve(140, average)


# ================================================================ Banister TRIMP
def test_banister_grows_nonlinearly_with_intensity():
    """强度对负荷应超线性：同等强度增幅带来的负荷增幅 > 时长同比增幅。"""
    easy = banister_trimp(3600, 130, MALE)
    hard = banister_trimp(3600, 175, MALE)
    doubled_duration = banister_trimp(7200, 130, MALE)

    assert easy and hard and doubled_duration
    assert hard > easy
    assert (hard / easy) > (doubled_duration / easy) > 1.9


def test_banister_uses_sex_coefficient():
    """男女系数不同（0.64/1.92 vs 0.86/1.67），结果必须不同。"""
    assert banister_trimp(3600, 160, FEMALE) != banister_trimp(3600, 160, MALE)


def test_banister_returns_none_instead_of_guessing():
    assert banister_trimp(3600, None, MALE) is None
    assert banister_trimp(None, 150, MALE) is None
    assert banister_trimp(0, 150, MALE) is None
    assert banister_trimp(-60, 150, MALE) is None


def test_banister_matches_hand_computed_reference():
    """黄金值：60min @ HR150，档案 190/55 → HRr=0.7037，TRIMP≈100.9。"""
    hrr = (150 - 55) / (190 - 55)
    expected = 60 * hrr * 0.64 * (2.718281828 ** (1.92 * hrr))
    assert banister_trimp(3600, 150, MALE) == pytest.approx(expected, rel=1e-6)


# ================================================================ sRPE
def test_session_rpe():
    assert session_rpe(3600, 5) == pytest.approx(300.0)   # 60min x RPE5
    assert session_rpe(5400, 4) == pytest.approx(360.0)   # 90min x RPE4
    assert session_rpe(3600, None) is None
    assert session_rpe(3600, 0) is None
    assert session_rpe(0, 5) is None


# ================================================================ estimate_load
def test_estimate_prefers_hr_then_rpe():
    assert estimate_load(3600, 150, 8, MALE)[1] == "trimp"
    assert estimate_load(3600, None, 6, MALE)[1] == "srpe"


def test_estimate_never_fabricates_load():
    """核心不变量：无心率、无 RPE 时必须 0 + unknown。"""
    load, source = estimate_load(3600, None, None, MALE)
    assert load == 0.0
    assert source == "unknown"


def test_estimate_without_athlete_profile_does_not_fabricate():
    """档案缺失时不得再用 190/55 顶上。

    历史实现用两个与用户无关的常数补心率基准，结果所有没填档案的用户都会
    拿到「看起来正常」的负荷数字。现在必须显式降级：能退 sRPE 就退，
    退不了就 unknown。
    """
    assert estimate_load(3600, 150, None, None) == (0.0, "unknown")
    assert estimate_load(3600, 150, 6, None) == (360.0, "srpe")
    # 半缺档案（只有 max_hr）同样不得臆造静息心率
    assert estimate_load(3600, 150, None, {"max_hr": 190})[1] == "unknown"
    assert estimate_load(3600, 150, None, {"resting_hr": 55})[1] == "unknown"


def test_hardcoded_hr_defaults_are_gone():
    """禁止重新引入 190/55 这类与用户无关的心率兜底常量。"""
    from app.services import load as load_mod

    assert not hasattr(load_mod, "DEFAULT_MAX_HR")
    assert not hasattr(load_mod, "DEFAULT_REST_HR")


def test_estimate_responds_to_athlete_profile():
    """同一活动（60min @ HR150）在不同档案下负荷必须不同。"""
    young = {"sex": "male", "max_hr": 200, "resting_hr": 45}
    older = {"sex": "male", "max_hr": 175, "resting_hr": 65}
    assert estimate_load(3600, 150, None, young)[0] < estimate_load(3600, 150, None, older)[0]


def test_estimate_accepts_orm_object_and_dict():
    """调用方传入的可能是 ORM 对象或 athlete_dict，两种形态都要支持。"""
    from app.models import Athlete

    orm = Athlete(name="t", sex="male", max_hr=190, resting_hr=55)
    dict_load = estimate_load(3600, 150, None, MALE)[0]
    orm_load = estimate_load(3600, 150, None, orm)[0]
    assert orm_load == pytest.approx(dict_load)


# ================================================================ 心率口径统一
def test_load_status_shares_the_same_hr_standard():
    """防止再复制一份心率换算，全项目只保留 load.hr_reserve 一处。"""
    import app.services.load_status as ls

    assert not hasattr(ls, "_hr_pct"), "load_status 不应再自带心率换算函数"
    assert ls.classify_intensity(185, "run", MALE) == "anaerobic"
    assert ls.classify_intensity(165, "run", MALE) == "high_aerobic"
    assert ls.classify_intensity(120, "run", MALE) == "low_aerobic"
    assert ls.classify_intensity(None, "run", MALE) == "other"
    assert ls.classify_intensity(180, "strength", MALE) == "strength"


def test_no_pseudo_trimp_formula_left_in_codebase():
    """全仓不得出现「时长 x 心率 / 140」这类写死 140 的伪 TRIMP 估算。

    用 AST 只匹配两种真正危险的结构：`X / 140` 与 `X or 140`：
      - 纯数值比较会误报：export/fit_workout.py 里 `0x8C`（FIT base type 编码）
        恰好等于 140，与负荷计算毫无关系；
      - docstring 里提到 140（说明历史问题）也不该算回归。
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    offenders = []
    for py in root.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
                    and isinstance(node.right, ast.Constant)
                    and node.right.value == 140):
                offenders.append(f"{py.relative_to(root)}:{node.lineno} 除以 140")
            if (isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)
                    and any(isinstance(v, ast.Constant) and v.value == 140
                            for v in node.values)):
                offenders.append(f"{py.relative_to(root)}:{node.lineno} 回退到 140")
    assert not offenders, f"仍存在写死 140 的伪 TRIMP 估算：{offenders}"


# ================================================================ 调用方接线
def _fresh_db():
    """内存库会话，绝不触碰用户的 sport_platform.db。"""
    from app import models  # noqa: F401  注册所有表
    from app.db import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _setup_athlete(db):
    from factories import make_athlete

    db.add(make_athlete(name="测试", max_hr=190, resting_hr=55))
    db.commit()


def _add(db, **kw):
    from app import schemas
    from app.routers.activities import add_activity

    payload = {"sport": "run", "start_time": datetime(2026, 9, 1, 8, 0),
               "duration_sec": 3600, **kw}
    return add_activity(schemas.ActivityIn(**payload), db)


def _source_of(db, activity_id: int) -> str:
    from app.models import Activity

    row = db.scalar(select(Activity).where(Activity.id == activity_id))
    return (row.raw or {}).get("training_load_source")


def test_manual_entry_wires_the_engine_and_tags_source():
    db = _fresh_db()
    _setup_athlete(db)

    by_hr = _add(db, avg_hr=150)
    assert by_hr["training_load"] > 0
    assert _source_of(db, by_hr["id"]) == "trimp"

    by_rpe = _add(db, avg_hr=None, rpe=7)
    assert by_rpe["training_load"] == pytest.approx(420.0)   # 60min x RPE7
    assert _source_of(db, by_rpe["id"]) == "srpe"

    nothing = _add(db, avg_hr=None)
    assert nothing["training_load"] == 0.0
    assert _source_of(db, nothing["id"]) == "unknown"


def test_strength_activity_without_distance_still_gets_load():
    """力量课没有距离，也应算出负荷。"""
    db = _fresh_db()
    _setup_athlete(db)

    out = _add(db, sport="strength", distance_m=0, avg_hr=130)
    assert out["training_load"] > 0


def test_manual_entry_uses_athlete_from_db_not_hardcoded_id():
    """手动录入不应写死 athlete_id=1（多档案场景会错挂到别人身上）。"""
    from app.models import Activity
    from factories import make_athlete

    db = _fresh_db()
    first = make_athlete(name="甲", max_hr=190, resting_hr=55)
    db.add(first)
    db.commit()
    second = make_athlete(name="乙", max_hr=190, resting_hr=55)
    db.add(second)
    db.commit()
    db.delete(first)          # 让 id=1 不存在，只留 id=2
    db.commit()

    out = _add(db, avg_hr=150)
    row = db.scalar(select(Activity).where(Activity.id == out["id"]))
    assert row.athlete_id == second.id


# ================================================================ 平台同步路径
def test_sync_prefers_platform_real_load_over_estimate():
    """平台给了真实负荷就必须用真实值，估算只做兜底。"""
    from app.routers.connections import _training_load

    class Norm:
        duration_sec = 3600
        avg_hr = 150
        rpe = None

        def __init__(self, raw):
            self.raw = raw

    real = Norm({"coros_training_load": 60.0})
    assert _training_load(real, MALE) == 60.0
    assert real.raw["training_load_source"] == "coros"

    estimated = Norm({})
    load = _training_load(estimated, MALE)
    assert load > 0
    assert estimated.raw["training_load_source"] == "trimp"


def test_sync_estimates_zero_when_no_hr_available():
    """平台未给真实值、活动又没心率 → 0。"""
    from app.routers.connections import _training_load

    class Norm:
        duration_sec = 3600
        avg_hr = None
        rpe = None
        raw: dict = {}

    a = Norm()
    assert _training_load(a, MALE) == 0.0
    assert a.raw["training_load_source"] == "unknown"


# ================================================== load_for_activity（唯一取值路径）
class _Stub:
    """最小活动替身：load_for_activity 只依赖这四个属性。"""

    def __init__(self, duration_sec=3600, avg_hr=None, rpe=None, raw=None):
        self.duration_sec = duration_sec
        self.avg_hr = avg_hr
        self.rpe = rpe
        self.raw = {} if raw is None else raw


def test_load_for_activity_prefers_platform_real_value():
    """平台真实值优先，不得被估算式覆盖。"""
    a = _Stub(avg_hr=150, raw={"coros_training_load": 60})
    load, source = load_for_activity(a, MALE)
    assert (load, source) == (60.0, "coros")
    assert a.raw["training_load_source"] == "coros"


def test_load_for_activity_falls_back_and_records_source():
    a = _Stub(avg_hr=150, raw={})
    load, source = load_for_activity(a, MALE)
    assert source == "trimp"
    assert load == estimate_load(3600, 150, None, MALE)[0]
    assert a.raw["training_load_source"] == "trimp"


def test_load_for_activity_non_numeric_platform_value_is_recorded():
    """平台给了非数值：不能静默丢弃，要留下痕迹再降级估算。"""
    a = _Stub(avg_hr=150, raw={"coros_training_load": "n/a"})
    load, source = load_for_activity(a, MALE)
    assert source == "trimp"
    assert a.raw["training_load_source"] == "trimp"
    assert a.raw["coros_training_load_invalid"] == "n/a"


def test_load_for_activity_reassigns_raw_so_orm_can_detect_change():
    """raw 必须换成新 dict：原地改 JSON 列 SQLAlchemy 可能不标记 dirty。"""
    original = {"coros_training_load": 60}
    a = _Stub(raw=original)
    load_for_activity(a, MALE)
    assert a.raw is not original
    assert "training_load_source" not in original  # 原字典未被原地污染


# ================================================== 历史重算工具
def test_recalc_tool_persists_value_not_only_source(tmp_path):
    """回归护栏：重算必须把新值写进 training_load 列（只写 source 不够），
    此用例从库里重新读取验证。
    """
    from app.db import Base
    from app.models import Activity
    from factories import make_athlete
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from tools.recalc_training_load import apply_changes, collect_changes

    engine = create_engine(f"sqlite:///{(tmp_path / 'recalc.db').as_posix()}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        # 与真实档案一致：max 192 / rest 52 → 储备 140
        athlete = make_athlete(id=1, name="测试", max_hr=192, resting_hr=52)
        db.add(athlete)
        # id=1：复刻真实库里的一条数据（核心力量 11:37、均心率 53、负荷值 4.0）
        db.add(Activity(id=1, athlete_id=1, platform="coros", sport="strength",
                        start_time=datetime(2026, 7, 4, 23, 36), duration_sec=697,
                        avg_hr=53, training_load=4.0, raw={}))
        # id=2：库里存着伪值 999，但平台真值是 60 → 重算后必须回到 60
        db.add(Activity(id=2, athlete_id=1, platform="coros", sport="run",
                        start_time=datetime(2026, 7, 5, 7, 0), duration_sec=3600,
                        avg_hr=150, training_load=999.0,
                        raw={"coros_training_load": 60}))
        db.commit()

        changes = collect_changes(db, athlete)
        assert {ch.activity.id for ch in changes} == {1, 2}

        bad = apply_changes(db, changes)
        assert bad == [], f"回读校验失败：{bad}"

        rows = {a.id: a for a in db.query(Activity).all()}
        assert rows[1].training_load == pytest.approx(0.1)
        assert rows[1].raw["training_load_source"] == "trimp"
        assert rows[2].training_load == pytest.approx(60.0)
        assert rows[2].raw["training_load_source"] == "coros"

        # 幂等：第二遍不得再挑出任何改动
        assert collect_changes(db, athlete) == []
    finally:
        db.close()
        engine.dispose()


def test_apply_changes_reports_rows_that_did_not_persist(tmp_path):
    """回读校验必须能发现「没落盘」的行，而不是永远报成功。"""
    from app.db import Base
    from app.models import Activity
    from factories import make_athlete
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from tools.recalc_training_load import apply_changes, collect_changes

    engine = create_engine(f"sqlite:///{(tmp_path / 'recalc2.db').as_posix()}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        athlete = make_athlete(id=1, name="测试", max_hr=192, resting_hr=52)
        db.add(athlete)
        db.add(Activity(id=1, athlete_id=1, platform="coros", sport="strength",
                        start_time=datetime(2026, 7, 4, 23, 36), duration_sec=697,
                        avg_hr=53, training_load=4.0, raw={}))
        db.commit()

        changes = collect_changes(db, athlete)
        assert len(changes) == 1
        db.rollback()  # 模拟「内存里改了、库里没写进去」
        bad = apply_changes(db, changes)
        assert bad, "回读校验没有发现未落盘的行 —— 校验器失效"
        assert bad[0][0] == 1 and bad[0][1] == pytest.approx(4.0)
    finally:
        db.close()
        engine.dispose()
