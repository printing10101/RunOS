"""本地 AI 运行时一键接线脚本（Windows / LM Studio + Qwen3）。

背景：本机网络无法直连 GitHub，改用可直连的 LM Studio 官方 CDN +
ModelScope（魔搭）模型源。注意：模型运行时是项目自带的 llama.cpp
llama-server（随平台自动拉起），本脚本只负责下载/放置 GGUF 模型文件。

前置：以下两个文件已通过 curl 下载完成（或重新执行注释中的命令）：
  1) %USERPROFILE%\\Downloads\\LM-Studio-1.1.2-x64.exe
     (https://bionic-installers.lmstudio.ai/win32/x64/1.1.2-11/Bionic-1.1.2-11-x64.exe)
  2) %USERPROFILE%\\Downloads\\qwen3-model\\Qwen3-8B-Q4_K_M.gguf
     (https://modelscope.cn/models/Qwen/Qwen3-8B-GGUF/resolve/master/Qwen3-8B-Q4_K_M.gguf)

本脚本完成（全程不调用外部进程、不访问网络）：
  A. 校验安装包完整性（官方 CDN Content-Length 比对 + MZ 头）
  B. 引导安装（自动拉起安装程序，用户在窗口中点完即可）
  C. 把 GGUF 放入 LM Studio 模型目录
  D. 打印需要用户执行的 lms 加载/启动命令，等用户回车确认
  E. 写 backend/.env（AI_BASE_URL；AI_MODEL 写配置值即可，后端会自动
     回退到本地服务上唯一可用的模型，因此无需关心确切 id）
"""
import os
import re
import secrets
import shutil
from pathlib import Path

HOME = Path(os.environ["USERPROFILE"])
DL = HOME / "Downloads"
INSTALLER_NAME = "LM-Studio-1.1.2-x64.exe"
INSTALLER_SIZE = 649_973_344          # 官方 CDN Content-Length
GGUF_DIR = DL / "qwen3-model"
LMS_MODELS = HOME / ".lmstudio" / "models"
GGUF_NAMES = ("Qwen3-8B-Q4_K_M.gguf", "Qwen3-4B-Q4_K_M.gguf")
LMS_CANDIDATES = (
    HOME / "AppData" / "Local" / "Programs" / "LM Studio" / "lms.exe",
    HOME / ".lmstudio" / "bin" / "lms.exe",
)
GGUF_RE = r"Qwen3-[48]B-Q4_K_M\.gguf"
INSTALLER_RE = r"LM-Studio-[\d.]+-x64\.exe"


def _validate_path(p: Path, expected_parent: Path, pattern: str) -> Path:
    """只允许白名单目录下、匹配白名单文件名的路径进入后续流程。"""
    resolved = p.resolve()
    if expected_parent.resolve() not in resolved.parents:
        raise SystemExit(f"路径越界，已拒绝: {resolved}")
    if not re.fullmatch(pattern, resolved.name):
        raise SystemExit(f"文件名不在白名单，已拒绝: {resolved.name}")
    if not resolved.is_file():
        raise SystemExit(f"文件不存在: {resolved}")
    return resolved


def find_lms():
    which = shutil.which("lms")
    if which:
        return Path(which)
    for cand in LMS_CANDIDATES:
        if cand.is_file():
            return cand
    return None


def step_a_verify_installer():
    installer = _validate_path(DL / INSTALLER_NAME, DL, INSTALLER_RE)
    size = installer.stat().st_size
    if size != INSTALLER_SIZE:
        raise SystemExit(f"安装包大小 {size} 与官方 Content-Length {INSTALLER_SIZE} 不符，已中止")
    if installer.read_bytes()[:2] != b"MZ":
        raise SystemExit("安装包不是合法的 Windows 可执行文件，已中止")
    print(f"[A] 安装包完整性 OK（{size} 字节，官方 CDN HTTPS 直下）")


def step_b_install():
    if find_lms():
        print("[B] LM Studio 已安装，跳过")
        return
    print("[B] 拉起安装程序，请在弹出的窗口中完成安装（默认选项即可）…")
    os.startfile(str(DL / INSTALLER_NAME))   # ShellExecute 直接拉起，不经过命令行
    input("[B] 安装完成后按回车继续…")
    if not find_lms():
        raise SystemExit("安装后仍未找到 lms.exe，请确认安装位置后重跑本脚本")


def step_c_place_model():
    gguf = None
    for name in GGUF_NAMES:
        p = _validate_path(GGUF_DIR / name, GGUF_DIR, re.escape(name))
        if p.stat().st_size > 1024 ** 3:
            gguf = p
            break
    if not gguf:
        raise SystemExit(f"未找到大于 1GB 的已下载 GGUF（{GGUF_DIR}）")
    tag = "qwen3-8b" if "8B" in gguf.name else "qwen3-4b"
    dest_dir = LMS_MODELS / "qwen" / tag
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / gguf.name
    if not dest.exists():
        print(f"[C] 移动模型到 {dest} …")
        shutil.move(str(gguf), str(dest))
    print(f"[C] 模型就绪: {dest}")
    return gguf.name


def step_d_instructions(lms: Path, gguf_name: str):
    lms = _validate_path(lms, lms.parent, r"lms\.exe")
    if not re.fullmatch(GGUF_RE, gguf_name):
        raise SystemExit(f"模型名不在白名单: {gguf_name}")
    print(f"""[D] 请在另一个终端执行以下两条命令（或直接在 LM Studio 界面加载模型并启动服务）：

  "{lms}" load {gguf_name} --gpu max --exact
  "{lms}" server start

完成后回到这里按回车。""")
    input()


def step_e_env():
    env_path = Path(__file__).resolve().parent / "backend" / ".env"
    lines = (env_path.read_text(encoding="utf-8").splitlines()
             if env_path.exists() else [])
    lines = [line for line in lines if not line.startswith(("AI_BASE_URL", "AI_MODEL", "AI_TIMEOUT", "AI_API_KEY"))]
    local_key = "sk-local-" + secrets.token_hex(16)
    lines += ["", "# 本地 AI（llama.cpp llama-server，OpenAI 兼容端口 8080）",
              "AI_BASE_URL=http://localhost:8080/v1",
              "AI_MODEL=qwen3-instruct-30b   # 配置值仅为偏好；后端会自动使用本地服务上唯一可用的模型",
              f"AI_API_KEY={local_key}   # Bearer Key（每次安装随机生成；密钥只放 .env，绝不写进代码）",
              "AI_TIMEOUT=120"]
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[E] 已写入 {env_path}")


if __name__ == "__main__":
    step_a_verify_installer()
    step_b_install()
    gguf_name = step_c_place_model()
    step_d_instructions(find_lms(), gguf_name)
    step_e_env()
    print("\n完成 ✔ 重启平台后端（start.bat 或 python run.py），打开「AI 教练」页即可使用。")
