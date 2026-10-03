"""日志轮转的行为锁定：文件日志带轮转、超限追加写日志归档成 .old。

背景：本地应用常开数月，desktop-backend.log / llama-server.log 都是
append 永不轮转的单文件；文件日志此前也不存在。
"""
from __future__ import annotations

import logging

from app.logging_config import _LOG_FILE, _LOG_FORMAT, _reset_for_tests, rotate_big_log


def test_rotate_big_log_archives_oversized_file(tmp_path):
    big = tmp_path / "app.log"
    big.write_text("x" * 100, encoding="utf-8")
    rotate_big_log(big, max_bytes=50)
    assert not big.exists()
    old = tmp_path / "app.log.old"
    assert old.read_text(encoding="utf-8") == "x" * 100


def test_rotate_big_log_keeps_small_file(tmp_path):
    small = tmp_path / "app.log"
    small.write_text("tiny", encoding="utf-8")
    rotate_big_log(small, max_bytes=50)
    assert small.exists() and not small.with_suffix(".log.old").exists()


def test_rotate_big_log_replaces_previous_archive(tmp_path):
    big = tmp_path / "app.log"
    old = tmp_path / "app.log.old"
    old.write_text("上一代归档", encoding="utf-8")
    big.write_text("y" * 100, encoding="utf-8")
    rotate_big_log(big, max_bytes=50)
    # 新归档顶掉旧归档（只留一代），而不是撞 rename 抛错
    assert old.read_text(encoding="utf-8") == "y" * 100
    assert not big.exists()


def test_rotate_big_log_tolerates_missing_file(tmp_path):
    rotate_big_log(tmp_path / "nope.log", max_bytes=50)   # 不抛即通过


def test_setup_logging_adds_rotating_file_handler():
    _reset_for_tests()
    try:
        logging.getLogger().setLevel(logging.INFO)
        from app.logging_config import setup_logging
        setup_logging("INFO")
        logging.getLogger("runos.test").warning("落盘探针")
        for h in logging.getLogger().handlers:
            h.flush()
        assert _LOG_FILE.is_file()
        assert "落盘探针" in _LOG_FILE.read_text(encoding="utf-8")
        # 是带轮转的 handler，且上限与本模块常量同源
        from logging.handlers import RotatingFileHandler
        rotating = [h for h in logging.getLogger().handlers
                    if isinstance(h, RotatingFileHandler)]
        assert len(rotating) == 1
        assert rotating[0].maxBytes > 0
        assert rotating[0].baseFilename == str(_LOG_FILE)
    finally:
        _reset_for_tests()


def test_log_format_unchanged():
    """格式常量被 _reset_for_tests 用来识别自家 handler，改动需同步意识到连锁。"""
    assert "%(asctime)s" in _LOG_FORMAT and "%(levelname)-7s" in _LOG_FORMAT
