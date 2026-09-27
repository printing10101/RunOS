"""db 层迁移与索引的回归测试。

覆盖两件事：
1. 新库 create_all 后，activities 的平台去重唯一索引存在
   （_insert_activity 的 IntegrityError 并发兜底依赖它，见 models.Activity）；
2. 模拟「索引声明进模型之前」的老库（有表、无索引、有存量重复行），
   _auto_migrate 清理重复（每组保留最早一条）并补建索引；
   external_id 为空的手动录入不参与去重、不受影响。
"""
from __future__ import annotations

import pytest
from app import db as app_db
from app import models
from sqlalchemy import create_engine, inspect, text


def _tmp_engine(tmp_path, name="db"):
    # 用临时文件库而不是内存库：内存 + StaticPool 是单连接，
    # _auto_migrate 里 Inspector 反射与迁移事务共用同一 DBAPI 连接会互相干扰，
    # 与生产（文件库、多连接）行为不一致
    return create_engine(f"sqlite:///{tmp_path / (name + '.db')}",
                         connect_args={"check_same_thread": False})


def test_create_all_builds_dedupe_index(tmp_path):
    engine = _tmp_engine(tmp_path)
    models.Base.metadata.create_all(engine)
    names = {i["name"] for i in inspect(engine).get_indexes("activities")}
    assert "ux_activities_platform_external" in names


def test_auto_migrate_backfills_dedupe_index(tmp_path, monkeypatch):
    engine = _tmp_engine(tmp_path)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE activities (id INTEGER PRIMARY KEY, platform VARCHAR(16), "
            "external_id VARCHAR(128), start_time DATETIME)"
        )
        conn.exec_driver_sql(
            "INSERT INTO activities (platform, external_id, start_time) VALUES "
            "('coros', 'ext-a', '2026-01-01 08:00:00'),"
            "('coros', 'ext-a', '2026-01-02 08:00:00'),"
            "('manual', '', '2026-01-03 08:00:00'),"
            "('manual', '', '2026-01-04 08:00:00')"
        )
    monkeypatch.setattr(app_db, "engine", engine)
    app_db._auto_migrate()

    with engine.connect() as conn:
        dup_count = conn.execute(text(
            "SELECT COUNT(*) FROM activities "
            "WHERE platform='coros' AND external_id='ext-a'"
        )).scalar_one()
        manual_count = conn.execute(text(
            "SELECT COUNT(*) FROM activities WHERE external_id=''"
        )).scalar_one()
        kept = conn.execute(text(
            "SELECT start_time FROM activities WHERE external_id='ext-a'"
        )).scalar_one()

    assert dup_count == 1
    assert manual_count == 2  # 手动录入（external_id 为空）不受唯一索引影响
    assert kept == "2026-01-01 08:00:00"  # 保留最早一条

    index_names = {i["name"] for i in inspect(engine).get_indexes("activities")}
    assert "ux_activities_platform_external" in index_names


def test_schema_diff_fails_fast_on_missing_column(tmp_path, monkeypatch):
    """模型有列、老库没有、又不在 _MIGRATION_DDL 白名单 → init_db 必须报错，
    而不是等运行期 AttributeError。用 activities 表去掉 rpe 列模拟老库
    （rpe 从未登记过白名单）。"""
    engine = _tmp_engine(tmp_path, "old")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE activities (id INTEGER PRIMARY KEY, platform VARCHAR(16), "
            "external_id VARCHAR(128), start_time DATETIME)"
        )
    monkeypatch.setattr(app_db, "engine", engine)
    # _auto_migrate 只补白名单列，rpe 不在其中，diff 校验应当拦下
    with pytest.raises(RuntimeError, match="rpe"):
        app_db.init_db()


def test_schema_diff_passes_on_fresh_db(tmp_path, monkeypatch):
    engine = _tmp_engine(tmp_path, "fresh")
    monkeypatch.setattr(app_db, "engine", engine)
    app_db.init_db()  # 不抛即通过：新库 create_all 后模型列齐全
