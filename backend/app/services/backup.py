"""数据库自动备份：后端每次启动时对 SQLite 做一次在线快照，滚动保留最近 7 份。

设计要点：
- 用 sqlite3 的 backup API（而不是直接拷文件）：即使有未 checkpoint 的 WAL
  事务，也能拿到一致性快照，不会拷出半截库；
- 备份目录与库文件同级（backend/backups/），跟数据放在一起，随项目目录一起被
  用户备份/迁移；文件名带时间戳，可直观看到每次备份时间；
- 尽力而为：任何失败只记日志，不阻断启动（调用方在 lifespan 里再兜一层）。
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from ..config import settings

logger = logging.getLogger(__name__)

KEEP = 7          # 滚动保留份数
PREFIX = "sport_platform_"


def _db_path() -> Path | None:
    url = settings.database_url.split("?")[0]   # 去掉可能的查询参数
    if not url.startswith("sqlite:///"):
        return None
    raw = url[len("sqlite:///"):]
    if not raw:
        return None
    return Path(raw)


def backup_db() -> Path | None:
    """执行一次备份，返回备份文件路径；无需备份/失败返回 None。"""
    db_path = _db_path()
    if db_path is None or not db_path.is_file():
        logger.info("跳过数据库备份：未找到 SQLite 库文件（%s）", db_path)
        return None

    dest_dir = db_path.parent / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{PREFIX}{time.strftime('%Y%m%d_%H%M%S')}.db"

    src_conn = sqlite3.connect(str(db_path))
    try:
        dst_conn = sqlite3.connect(str(dest))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()

    # 滚动清理：只保留最近 KEEP 份。KEEP<=0 时 [:-0] 会退化成 [:0]（什么都不删）
    # 甚至把备份全删光——保留份数下限 1，这是测试抓出来的退化切片。
    keep = max(1, KEEP)
    backups = sorted(dest_dir.glob(f"{PREFIX}*.db"))
    for old in backups[:-keep]:
        try:
            old.unlink()
            logger.debug("清理过期备份：%s", old)
        except OSError as e:
            logger.warning("清理过期备份失败（可忽略）：%s，%s", old, e)

    logger.info("数据库已备份：%s（保留最近 %d 份）", dest, KEEP)
    return dest


def list_backups() -> list[Path]:
    """按时间倒序列出现存备份文件（供设置页展示；目录不存在时返回空）。"""
    db_path = _db_path()
    if db_path is None:
        return []
    dest_dir = db_path.parent / "backups"
    if not dest_dir.is_dir():
        return []
    return sorted(dest_dir.glob(f"{PREFIX}*.db"), reverse=True)
