"""系统级：数据库备份（列表 / 立即备份 / 下载）。

备份是单用户本地应用的数据安全底线：每次启动自动快照一次（main.lifespan
调 services.backup.backup_db），这里补运行期的手动出口与存档领取。
恢复刻意不做端点——用备份覆盖在用的库文件是破坏性操作，必须发生在服务
停止后，交给用户按页面指引手动完成（停服 → 覆盖 sport_platform.db → 起服）。
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..services import backup

router = APIRouter(prefix="/api/system", tags=["system"])

# 备份文件名全部由 backup_db 生成（PREFIX + strftime）。下载端点只认这个形状：
# 名称来自 URL，带路径分隔符/非法字符的任何形状都在这里挡掉，不进文件路径。
_NAME_RE = re.compile(rf"^{backup.PREFIX}\d{{8}}_\d{{6}}\.db$")


def _safe_backup_path(name: str) -> Path:
    root = backup.backups_dir()
    if root is None or not _NAME_RE.match(name):
        raise HTTPException(status_code=404, detail="备份不存在")
    root = root.resolve()
    p = (root / name).resolve()
    # 归属校验防穿越/绝对路径重置（同 main.py SPA 回退的口径）
    if not p.is_relative_to(root) or not p.is_file():
        raise HTTPException(status_code=404, detail="备份不存在")
    return p


def _created_at(p: Path) -> str:
    """备份时间优先从文件名解析（复制/迁移不改名字）；解析不了退回 mtime。"""
    try:
        return datetime.strptime(p.stem[len(backup.PREFIX):], "%Y%m%d_%H%M%S").isoformat(
            timespec="seconds")
    except ValueError:
        return datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")


@router.get("/backups")
def list_backups():
    root = backup.backups_dir()
    return {
        "backup_dir": str(root) if root else "",
        "items": [{"name": p.name, "size_mb": round(p.stat().st_size / 1048576, 1),
                   "created_at": _created_at(p)} for p in backup.list_backups()],
    }


@router.post("/backups")
def run_backup():
    """立即做一次快照（训练刚落库、动大手术前手动留个还原点）。"""
    p = backup.backup_db()
    if p is None:
        raise HTTPException(status_code=400, detail="备份失败：未找到 SQLite 数据库文件")
    return {"ok": True, "name": p.name}


@router.get("/backups/{name}/download")
def download_backup(name: str):
    return FileResponse(_safe_backup_path(name), filename=name,
                        media_type="application/octet-stream")
