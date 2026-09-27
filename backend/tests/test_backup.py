"""备份模块测试。

为什么值得单独测：这些备份是训练数据唯一的本地副本（SQLite 单文件，没有异地、
没有云端），而备份逻辑里两处失效都不会报错——拷出半截库、或者清理逻辑把备份
删光。这类「静默失效」正是本项目其它模块踩过的模式，这里用测试钉住。
"""
from __future__ import annotations

import sqlite3

import pytest
from app.services import backup


class _FakeTime:
    """让每次 backup_db() 拿到不同的时间戳（真实 strftime 到秒，
    同一秒内多次调用会撞文件名、互相覆盖）。"""

    def __init__(self) -> None:
        self.n = 0

    def strftime(self, _fmt: str) -> str:
        self.n += 1
        return f"20260101_0000{self.n:02d}"


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """把 database_url 指向临时库，并写入一行可验证的数据。"""
    db_file = tmp_path / "sport_platform.db"
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO t (v) VALUES ('可验证内容')")
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(backup.settings, "database_url", f"sqlite:///{db_file.as_posix()}")
    monkeypatch.setattr(backup, "time", _FakeTime())
    return db_file


def test_backup_produces_readable_consistent_copy(tmp_db):
    """不只是「文件存在」——要能打开并读出源库的数据。

    用 sqlite3 backup API 的意义就在这里：即使源库有未 checkpoint 的 WAL
    事务，快照也应是一致的。拷文件做不到这点。
    """
    dest = backup.backup_db()
    assert dest is not None and dest.is_file()

    conn = sqlite3.connect(f"file:{dest.as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT v FROM t").fetchall()
    finally:
        conn.close()
    assert rows == [("可验证内容",)]


def test_backup_returns_none_when_db_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(backup.settings, "database_url", f"sqlite:///{tmp_path / 'nope.db'}")
    assert backup.backup_db() is None


def test_backup_returns_none_for_non_sqlite_url(monkeypatch):
    # 换了数据库后端时不该尝试备份，也不该抛异常
    monkeypatch.setattr(backup.settings, "database_url", "postgresql://localhost/x")
    assert backup._db_path() is None


def test_rolling_retention_keeps_newest(tmp_db, monkeypatch):
    """保留最近 KEEP 份，且留下的是最新的那几份（按名字排序=按时间排序）。"""
    monkeypatch.setattr(backup, "KEEP", 3)
    for _ in range(5):
        backup.backup_db()

    left = backup.list_backups()
    assert len(left) == 3
    # 文件名里的时间戳是最新的三个（_FakeTime 递增到 5）
    stamps = sorted(p.name[-9:-3] for p in left)
    assert stamps == ["000003", "000004", "000005"]


def test_keep_zero_does_not_wipe_all_backups(tmp_db, monkeypatch):
    """KEEP<=0 时 [:-0] 会退化成整个列表，把备份全删光——必须挡住。

    这是写这组测试时发现的退化切片：负零等于零，切片就不是「去掉末尾 KEEP 个」
    而是「从头到尾」。保留至少 1 份。
    """
    monkeypatch.setattr(backup, "KEEP", 0)
    for _ in range(3):
        backup.backup_db()

    assert len(backup.list_backups()) == 1


def test_list_backups_newest_first_and_empty_dir(tmp_db, tmp_path):
    assert backup.list_backups() == []  # 还没备份过

    backup.backup_db()
    backup.backup_db()
    left = backup.list_backups()
    assert [p.name for p in left] == sorted((p.name for p in left), reverse=True)
    assert left[0].name > left[1].name
