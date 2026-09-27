"""backend 运维脚本包。

导入路径说明：脚本以 `py -3 tools/xxx.py` 直接运行时，Python 会把 tools/ 放进
sys.path[0]，导致 `import app` 失败；包内模块统一用下列方式把 backend/ 补进来。
测试则通过 `from tools.xxx import ...` 导入（cwd=backend 时 tools 可导入）。
"""
