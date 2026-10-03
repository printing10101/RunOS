"""锁定 .env.example 与 config.py 的键覆盖：新增配置项必须同步进模板。

为什么值得测：换机恢复/新克隆时 .env.example 是唯一的配置清单。config.py 里
加了键而模板没跟上，用户就不知道它能调——CSRF_EXTRA_ORIGINS、BACKUP_DIR、
AI_CONTEXT_TOKENS 此前都只活在代码注释里（2026-10-03 审计补齐）。
注释掉的键（# KEY=...）也算覆盖：默认值合理、不需要动的项以注释形式存在，
关键是不让键从模板里消失。
"""
from __future__ import annotations

import re
from pathlib import Path

from app.config import Settings

EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


def test_env_example_covers_all_settings_fields():
    example_text = EXAMPLE.read_text(encoding="utf-8")
    missing = []
    for name in Settings.model_fields:
        # 行首锚定（允许注释形式），不能用子串包含——否则前缀相同的键互相误放行
        if not re.search(rf"^#?\s*{name.upper()}=", example_text, re.M):
            missing.append(name.upper())
    assert missing == [], f".env.example 缺少配置项说明：{missing}"
