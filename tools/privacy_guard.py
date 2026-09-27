#!/usr/bin/env python
"""隐私守卫：提交/推送前扫描个人隐私与凭据痕迹（AGENTS.md 规定的强制闸门）。

用法：
  python tools/privacy_guard.py --staged   # pre-commit：只扫暂存区内容
  python tools/privacy_guard.py --tree     # pre-push：扫工作树全部被跟踪文件
  python tools/privacy_guard.py FILE...    # 手动扫描指定文件

命中即非零退出并阻止提交。拦截对象（详见 _RULES）：GPS 坐标对、16 位以上
平台 ID（如 COROS LabelId）、各类 API Key、凭据赋值、个人邮箱/手机号、
Windows 用户路径、敏感文件路径。

白名单只放行指定的合成数据（测试夹具）：坐标 12.345678, 98.765432、
LabelId 123456789012345678、时间戳 1000000000/1000001800、用户名 testuser。
禁止把真实接口响应的样例文本复制进代码或测试。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# 文本类后缀；二进制文件直接跳过（db/图片等）
TEXT_SUFFIXES = {
    ".py", ".js", ".mjs", ".vue", ".ts", ".json", ".md", ".txt", ".yml", ".yaml",
    ".toml", ".cfg", ".ini", ".html", ".css", ".sh", ".ps1", ".bat", ".sql",
    ".env.example", "",  # 无后缀脚本
}
SKIP_DIR_PARTS = {".git", "node_modules", "__pycache__", ".venv", "venv",
                  ".pytest_cache", ".ruff_cache", ".mimosa", ".workbuddy",
                  ".workbuddy-ai", ".zcode", "dist", "llama", "backups"}

# 敏感文件名（出现即拦截，不论内容）
SENSITIVE_PATH_RE = re.compile(
    r"(^|/|\\)("
    r"\.env|tools\.json|_raw_records\.txt"
    r"|[^/\\]*\.db|[^/\\]*\.db-journal"
    r")($|/|\\)|(^|/|\\)backend/_[^/\\]*", re.I)

# 白名单：只允许这些指定的合成值出现（测试夹具纪律，见模块 docstring）
WHITELIST = {
    "12.345678, 98.765432", "12.345678,98.765432",
    "98.765432, 12.345678", "98.765432,12.345678",
    "123456789012345678",
    "1000000000", "1000001800",
    "testuser",
}

def _make_rules():
    return [
        # GPS 裸坐标对（两个四位小数数字以逗号并列）：两个捕获组同时命中才算
        ("gps坐标对", re.compile(r"\b([1-9]\d?\d?\.\d{4,})\s*,\s*([1-9]\d?\d?\.\d{4,})\b")),
        # lat/lon 键值形式（冒号/等号可省）：单边命中即拦
        ("gps键值坐标", re.compile(
            r"\b(?:lat|latitude|lon|lng|longitude)\"?\s*[:=]?\s*\"?-?\d{1,3}\.\d{4,}", re.I)),
        # 16 位以上纯数字 ID（COROS LabelId / Strava / Garmin 长 ID）
        ("16位以上平台ID", re.compile(r"(?<![\w.])\d{16,}(?![\w.])")),
        # API Key 前缀族
        ("API Key", re.compile(
            r"\b(sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{8,}|gho_[A-Za-z0-9]{8,}"
            r"|AKIA[0-9A-Z]{12,}|AIza[0-9A-Za-z_-]{8,}|xox[baprs]-[0-9A-Za-z-]{8,})")),
        # 凭据赋值（含 .env 写法 / Python kwarg / YAML）
        ("凭据赋值", re.compile(
            r"\b(GARMIN_PASSWORD|STRAVA_CLIENT_SECRET|AI_API_KEY|LLAMA_API_KEY|"
            r"API_KEY|CLIENT_SECRET|ACCESS_TOKEN|REFRESH_TOKEN|PASSWORD)\s*[=:]\s*"
            r"[\"']?[A-Za-z0-9+/_-]{8,}", re.I)),
        # 国内常见个人邮箱域
        ("个人邮箱", re.compile(r"[A-Za-z0-9._%+-]+@(?:qq|163|126|gmail|foxmail|hotmail|outlook)\.(?:com|net)", re.I)),
        # 手机号（宽松 1[3-9] 开头 11 位，避免误报限死边界）
        ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
        # Windows 用户路径（泄露用户名）
        ("Windows用户路径", re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+[^/\\\"':\s]+")),
    ]

_RULES = None


def rules():
    global _RULES
    if _RULES is None:
        _RULES = _make_rules()
    return _RULES


def scan_line(path: str, line: str) -> list[str]:
    """返回命中的规则名列表；行内容绝不原样输出（打码后再打印）。"""
    stripped = line.rstrip("\n")
    if any(w in stripped for w in WHITELIST):
        return []
    hits = []
    # 敏感路径规则对「模板/清单类文件」豁免：.gitignore 与 .env.example 本来
    # 就要逐字写出这些文件名，命中是正常的
    is_template = path.replace("\\", "/").endswith((".gitignore", ".env.example"))
    if not is_template and SENSITIVE_PATH_RE.search(stripped):
        hits.append("敏感文件路径")
    for name, rx in rules():
        m = rx.search(stripped)
        if not m:
            continue
        # 裸坐标对规则要求两个捕获组都命中；其余规则单点命中即拦
        if name == "gps坐标对" and (not m.lastindex or m.lastindex < 2):
            continue
        # 凭据赋值：右边全是标识符/属性链（变量转发，含逗号解包）不算泄密
        if name == "凭据赋值":
            rhs = stripped[m.start():].split("=", 1)[-1].split(":", 1)[-1].strip().strip("\"'")
            parts = [p.strip().strip("\"'") for p in rhs.split(",")]
            if parts and all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.\[\]]*", p or "_")
                             for p in parts):
                continue
        # Windows 用户路径：<占位符> 形式（文档示例）不算
        if name == "Windows用户路径" and "<" in m.group(0):
            continue
        hits.append(name)
    return hits


def _redact(line: str) -> str:
    """打码：数字串留首尾 2 位，其余用 *；坐标整段打码。"""
    s = re.sub(r"\d{4,}", lambda m: m.group(0)[:2] + "*" * (len(m.group(0)) - 4) + m.group(0)[-2:], line)
    s = re.sub(r"[A-Za-z0-9._%+-]+@", "***@", s)
    return s.strip()[:120]


def _is_text(path: Path) -> bool:
    if path.suffix.lower() in TEXT_SUFFIXES or path.name in (".env.example", "Dockerfile"):
        return True
    try:
        head = path.read_bytes()[:1024]
        return b"\x00" not in head
    except OSError:
        return False


def scan_content(path: str, text: str, findings: list[tuple[str, int, str, str]]) -> None:
    for lineno, line in enumerate(text.splitlines(), 1):
        for hit in scan_line(path, line):
            findings.append((path, lineno, hit, _redact(line)))


def scan_staged(findings) -> int:
    """扫描暂存区内容（不是工作区文件——提交的是暂存内容）。"""
    names = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True, text=True, check=True).stdout.split()
    count = 0
    for name in names:
        if not _path_scan_allowed(name):
            continue
        blob = subprocess.run(["git", "show", f":{name}"],
                              capture_output=True, check=False)
        if blob.returncode != 0:
            continue
        # 二进制内容（图片/字体等）不做文本扫描——按字节判空字节，与 scan_tree
        # 的 _is_text 同口径；此前 text=True 直解 UTF-8，暂存 .ico 即崩钩子。
        if b"\x00" in blob.stdout[:1024]:
            continue
        before = len(findings)
        scan_content(name, blob.stdout.decode("utf-8", errors="replace"), findings)
        count += 1 if len(findings) > before else 0
    return count


def scan_tree(findings) -> int:
    """扫描全部被跟踪文件（pre-push 的推送快照口径）。"""
    names = subprocess.run(["git", "ls-files"],
                           capture_output=True, text=True, check=True).stdout.split()
    count = 0
    for name in names:
        if not _path_scan_allowed(name):
            continue
        p = Path(name)
        if not p.is_file() or not _is_text(p):
            continue
        before = len(findings)
        scan_content(name, p.read_text(encoding="utf-8", errors="replace"), findings)
        count += 1 if len(findings) > before else 0
    return count


def _path_scan_allowed(name: str) -> bool:
    """.env / db / llama / 探针脚本等敏感路径根本不该被跟踪；命中即在
    内容扫描前直接判负（由调用方统一报告），不进来做无谓解析。"""
    parts = set(Path(name).parts)
    if parts & SKIP_DIR_PARTS:
        return False
    return True


def check_sensitive_paths_tracked(findings) -> None:
    """被跟踪文件名本身命中敏感路径模式 → 直接拦截（不论内容）。"""
    names = subprocess.run(["git", "ls-files"],
                           capture_output=True, text=True, check=True).stdout.split()
    for name in names:
        if SENSITIVE_PATH_RE.search(name.replace("\\", "/")):
            findings.append((name, 0, "敏感文件被跟踪", name))


def main() -> int:
    ap = argparse.ArgumentParser(description="隐私守卫：拦截个人数据与凭据入库")
    ap.add_argument("--staged", action="store_true", help="扫描暂存区（pre-commit）")
    ap.add_argument("--tree", action="store_true", help="扫描全部跟踪文件（pre-push）")
    ap.add_argument("files", nargs="*", help="手动扫描指定文件")
    args = ap.parse_args()

    if not (args.staged or args.tree or args.files):
        ap.error("请指定 --staged / --tree 或文件列表")

    findings: list[tuple[str, int, str, str]] = []
    if args.files:
        for f in args.files:
            p = Path(f)
            if p.is_file() and _is_text(p):
                scan_content(str(p), p.read_text(encoding="utf-8", errors="replace"), findings)
    if args.staged:
        scan_staged(findings)
        check_sensitive_paths_tracked(findings)
    if args.tree:
        scan_tree(findings)
        check_sensitive_paths_tracked(findings)

    if findings:
        print(f"✗ 隐私守卫拦截：{len(findings)} 处疑似隐私/凭据痕迹\n", file=sys.stderr)
        for path, lineno, hit, ctx in findings[:40]:
            loc = f"{path}:{lineno}" if lineno else path
            print(f"  [{hit}] {loc}\n      {ctx}", file=sys.stderr)
        if len(findings) > 40:
            print(f"  ...（其余 {len(findings) - 40} 条省略）", file=sys.stderr)
        print("\n处理：删除/脱敏后重试。白名单仅限 AGENTS.md 指定的合成测试数据。",
              file=sys.stderr)
        return 1
    print("✓ 隐私扫描通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
