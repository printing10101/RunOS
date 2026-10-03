"""备份接线的行为锁定：启动/手动快照、列表、下载、路径安全。

背景（2026-10-03 审计发现）：services/backup.py 写好后从未被生产代码调用，
模块注释声称「后端每次启动时自动快照」，实际用户处于零备份状态。
本文件锁三件事：lifespan 确实调 backup_db；/api/system/backups 三端点可用；
下载端点对 URL 里的恶意文件名（穿越/绝对路径/乱名）一律 404。
"""
from __future__ import annotations

import sqlite3

import pytest
from app.main import app
from app.services import backup
from fastapi.testclient import TestClient

client = TestClient(app)


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """把库与备份目录都指到临时目录：真实库文件 + 可断言的备份落点。"""
    db_file = tmp_path / "sport_platform.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(backup.settings, "database_url", f"sqlite:///{db_file.as_posix()}")
    monkeypatch.setattr(backup.settings, "backup_dir", "")
    # last_check 是模块级状态，会跨测试残留——归零保证「备份前无校验记录」可断言
    monkeypatch.setattr(backup, "last_check", None)
    return db_file


def test_backup_dir_setting_redirects_snapshot(tmp_db, tmp_path, monkeypatch):
    """BACKUP_DIR 指到别处时快照落那里，而不是库同级——同盘备份扛不住盘坏。"""
    alt = tmp_path / "alt-disk"
    monkeypatch.setattr(backup.settings, "backup_dir", str(alt))
    dest = backup.backup_db()
    assert dest is not None and dest.parent == alt
    assert alt.glob(f"{backup.PREFIX}*.db")


def test_list_endpoint_reports_dir_and_items(tmp_db):
    resp = client.get("/api/system/backups")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["backup_dir"].endswith("backups")

    assert backup.backup_db() is not None
    body = client.get("/api/system/backups").json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["name"].startswith(backup.PREFIX)
    assert "T" in item["created_at"]          # 文件名解析出的 ISO 时间
    assert item["size_mb"] >= 0


def test_run_backup_endpoint_creates_snapshot(tmp_db):
    resp = client.post("/api/system/backups")
    assert resp.status_code == 200
    name = resp.json()["name"]
    assert name.startswith(backup.PREFIX)
    assert client.get("/api/system/backups").json()["items"]


def test_endpoints_surface_last_check(tmp_db):
    """备份「存在」之外还要看「可用」：GET 带出最近校验结果，POST 直接回本次校验。"""
    assert client.get("/api/system/backups").json()["last_check"] is None

    created = client.post("/api/system/backups").json()
    assert created["integrity"]["ok"] is True
    assert created["integrity"]["file"] == created["name"]

    body = client.get("/api/system/backups").json()
    assert body["last_check"]["file"] == created["name"]


def test_run_backup_without_db_returns_400(tmp_path, monkeypatch):
    monkeypatch.setattr(backup.settings, "database_url",
                        f"sqlite:///{(tmp_path / 'nope.db').as_posix()}")
    monkeypatch.setattr(backup.settings, "backup_dir", "")
    assert client.post("/api/system/backups").status_code == 400


def test_download_roundtrip(tmp_db):
    name = client.post("/api/system/backups").json()["name"]
    resp = client.get(f"/api/system/backups/{name}/download")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/octet-stream"
    # 下载内容是合法 SQLite 库（backup API 的一致性保证）
    assert resp.content[:15] == b"SQLite format 3"


@pytest.mark.parametrize("evil", [
    "..%2F..%2Fbackend%2F.env",              # 相对穿越
    "sport_platform.db",                     # 裸库名（不在备份目录形状内）
    "sport_platform_20990101_000000.db",     # 形状对但不存在
    "sport_platform_20260101_000000.db%00",  # 截断攻击
])
def test_download_rejects_evil_names(tmp_db, evil):
    backup.backup_db()   # 目录里放一个合法备份，证明 404 不是「目录为空」
    assert client.get(f"/api/system/backups/{evil}/download").status_code == 404
