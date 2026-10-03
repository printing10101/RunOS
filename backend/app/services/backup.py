"""数据库自动备份：后端每次启动时对 SQLite 做一次在线快照，滚动保留最近 7 份。

设计要点：
- 用 sqlite3 的 backup API（而不是直接拷文件）：即使有未 checkpoint 的 WAL
  事务，也能拿到一致性快照，不会拷出半截库；
- 写完即校验：quick_check 确认快照能作为 SQLite 打开（磁盘满/进程被杀可能落下
  坏文件），结果记在 last_check，由备份端点带出——备份的价值在恢复那天能用，
  不能等人要恢复时才发现快照是坏的；
- 备份目录默认与库文件同级（backend/backups/），可用 settings.backup_dir
  （backend/.env 的 BACKUP_DIR）指到另一块盘或网盘同步目录——同盘备份扛不住盘坏；文件名带时间戳，可直观看到每次备份时间；
- 尽力而为：任何失败只记日志，不阻断启动（调用方在 lifespan 里再兜一层）。
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from ..config import settings

logger = logging.getLogger(__name__)

KEEP = 7          # 滚动保留份数
PREFIX = "sport_platform_"

# 最近一次备份的可用性校验结果：{file, ok, detail, at}；进程启动后的首次
# backup_db（lifespan 自动触发）即有值。备份端点原样带出给设置页展示。
last_check: dict | None = None


def _db_path() -> Path | None:
    url = settings.database_url.split("?")[0]   # 去掉可能的查询参数
    if not url.startswith("sqlite:///"):
        return None
    raw = url[len("sqlite:///"):]
    if not raw:
        return None
    return Path(raw)


def backups_dir() -> Path | None:
    """备份根目录：settings.backup_dir 优先（可指到另一块盘/网盘同步目录），
    缺省与库文件同级（backend/backups/）。未用 SQLite 返回 None。"""
    if settings.backup_dir.strip():
        return Path(settings.backup_dir.strip())
    db_path = _db_path()
    if db_path is None:
        return None
    return db_path.parent / "backups"


def verify_backup(path: Path) -> tuple[bool, str]:
    """只读打开快照跑 quick_check，确认它能作为 SQLite 库打开且页结构完整。

    quick_check 是 integrity_check 的降配版（不做索引交叉核对），对小库毫秒级。
    返回 (是否可用, 说明)；坏文件不删——删数据性的兜底永远留给滚动清理，
    但失败必须在日志里响亮留痕。
    """
    try:
        conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        return False, str(e)
    if row and row[0] == "ok":
        return True, "ok"
    return False, str(row[0]) if row else "quick_check 无结果"


def backup_db() -> Path | None:
    """执行一次备份，返回备份文件路径；无需备份/失败返回 None。"""
    global last_check
    db_path = _db_path()
    dest_dir = backups_dir()
    if db_path is None or not db_path.is_file() or dest_dir is None:
        logger.info("跳过数据库备份：未找到 SQLite 库文件（%s）", db_path)
        return None

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

    ok, detail = verify_backup(dest)
    # 校验时间戳刻意不走 time.strftime：它同时是文件名序号的来源（测试里被假时钟
    # 替换），多调一次会把「同名冲突防护」的序列挤乱。
    last_check = {"file": dest.name, "ok": ok, "detail": detail,
                  "at": datetime.now().isoformat(timespec="seconds")}
    if not ok:
        logger.warning("备份校验失败：%s（%s）——该快照可能无法用于恢复", dest, detail)

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
    """按时间倒序列出现存备份文件（设置区展示；目录不存在时返回空）。"""
    dest_dir = backups_dir()
    if dest_dir is None or not dest_dir.is_dir():
        return []
    return sorted(dest_dir.glob(f"{PREFIX}*.db"), reverse=True)
