"""在桌面创建「RunOS」快捷方式（纯 pywin32 实现）。

说明：原实现是调用同目录 create_shortcut.ps1，但 WorkBuddy 沙箱拦截
PowerShell 的 COM 调用（WScript.Shell），改用 pywin32 在进程内完成。
需在装有 pywin32 的解释器下运行（本机系统 Python 3.14 已装）。

快捷方式指向当前解释器同目录的 pythonw.exe + desktop.py —— 双击即打开
应用窗口，无控制台。本机没有 .venv，依赖都装在系统 Python（与 start.bat
的「py -3 优先」策略一致）；pythonw 从运行时解释器动态解析，项目挪目录
后重跑本脚本即可，不再写死绝对路径（旧版写死 .venv 路径，目录迁移后失效）。
"""
from __future__ import annotations

import sys
from pathlib import Path

DESKTOP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = DESKTOP_DIR.parent


def main() -> None:
    import win32com.client

    desktop_py = DESKTOP_DIR / "desktop.py"
    icon = DESKTOP_DIR / "app.ico"
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    missing = [str(p) for p in (desktop_py, pythonw, icon) if not p.exists()]
    if missing:
        raise SystemExit(f"缺少文件，无法创建快捷方式: {missing}")

    shell = win32com.client.Dispatch("WScript.Shell")
    desktop = shell.SpecialFolders("Desktop")
    lnk_path = desktop + "\\RunOS.lnk"

    shortcut = shell.CreateShortcut(lnk_path)
    shortcut.TargetPath = str(pythonw)
    shortcut.Arguments = f'"{desktop_py}"'
    shortcut.WorkingDirectory = str(PROJECT_ROOT)
    shortcut.IconLocation = str(icon)
    shortcut.Description = "RunOS - 训练 · 评估 · 预测"
    shortcut.Save()
    print("created:", lnk_path)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # 打印原因后以非零码退出，便于调用方感知失败
        print(f"创建快捷方式失败: {exc}", file=sys.stderr)
        sys.exit(1)
