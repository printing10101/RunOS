import sys
from pathlib import Path

# 保证 `import app` 可用（无论从仓库根目录还是 backend 目录执行 pytest）
BACKEND_DIR = str(Path(__file__).parent)
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)
