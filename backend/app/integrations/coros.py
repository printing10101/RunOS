"""高驰（COROS）适配器：官方 MCP 通道（端点按账号区域分流 cn/eu/us）。
Streamable HTTP / JSON-RPC 2.0，响应可能是 application/json 或 SSE；
认证为 OAuth 2.0 授权码 + PKCE(S256)，动态客户端注册见 RFC 7591。
官方限制：单用户授权、无 webhook、无 GPX 导入导出、训练计划写入 coming soon，
FIT 原始文件（含 GPS 轨迹与逐秒数据）每账号每自然日最多 50 条。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import httpx

from ..logging_config import get_logger, swallowed
from .base import (
    IntegrationError,
    NormalizedActivity,
    PlatformAdapter,
    maybe_float,
    maybe_int,
)

# base.maybe_int 的本文件别名
_maybe_int = maybe_int

logger = get_logger(__name__)

CLIENT_NAME = "Sport Platform (local)"
SCOPE = "openid mcp.tools offline_access"
PROTOCOL_VERSION = "2025-06-18"
TOKEN_REFRESH_MARGIN_SEC = 120

COROS_MCP_BASES: dict[str, str] = {
    "cn": "https://mcpcn.coros.com",
    "eu": "https://mcpeu.coros.com",
    "us": "https://mcpus.coros.com",
    "auto": "https://mcp.coros.com",
}

# 高驰 MCP 的 SportType 数值编码 → 平台通用 sport。
# 官方工具说明给出的编码段：100-103 跑步、200-205/299 骑行、300/301 游泳；
# 实测另有 402 = Strength（力量）。文本输出里带运动名称时优先用名称判断。
COROS_SPORT_TYPE: dict[int, str] = {
    100: "run", 101: "run", 102: "run", 103: "run",
    200: "ride", 201: "ride", 202: "ride", 203: "ride",
    204: "ride", 205: "ride", 299: "ride",
    300: "swim", 301: "swim",
    402: "strength",
}

# 运动名称（高驰文本输出里的英文名）→ 平台通用 sport
COROS_SPORT_NAME: dict[str, str] = {
    "outdoor run": "run", "indoor run": "run", "track run": "run",
    "trail run": "run", "treadmill": "run", "running": "run", "run": "run",
    "road bike": "ride", "indoor bike": "ride", "mountain bike": "ride",
    "cycling": "ride", "bike": "ride",
    "pool swim": "swim", "open water swim": "swim", "swimming": "swim", "swim": "swim",
    "strength": "strength", "strength training": "strength", "gym cardio": "strength",
    "walking": "walk", "walk": "walk", "hiking": "walk", "hike": "walk",
}

# 高驰 MCP 检测到异常调用时返回的提示串（命中即显式报错）
ANOMALY_MARK = "Tool call anomalies detected"


def sport_key(name: str | None, code: int | None) -> str:
    """运动名称优先、编码兜底，归一为平台 sport。"""
    text = (name or "").strip().lower()
    if text in COROS_SPORT_NAME:
        return COROS_SPORT_NAME[text]
    for label, mapped in COROS_SPORT_NAME.items():
        if label in text:
            return mapped
    if code is not None:
        if code in COROS_SPORT_TYPE:
            return COROS_SPORT_TYPE[code]
        band = code // 100
        if band == 1:
            return "run"
        if band == 2:
            return "ride"
        if band == 3:
            return "swim"
        if band == 4:
            return "strength"
    return "other"

# 高驰 MCP 工具名（官方 GitHub 工具列表）
TOOL_SPORT_RECORDS = "querySportRecords"
TOOL_ACTIVITY_DETAIL = "getActivityDetail"
TOOL_LAPS = "queryActivityLapData"
TOOL_DAILY_HEALTH = "queryDailyHealthData"
TOOL_SLEEP = "querySleepData"
TOOL_RESTING_HR = "queryRestingHeartRate"
TOOL_RECOVERY = "queryRecoveryStatus"
TOOL_FITNESS = "queryFitnessAssessmentOverview"
TOOL_LOAD_ASSESSMENT = "queryTrainingLoadAssessment"
# 官方课表读取（2026-09 已上线；写入类 generateTrainingPlan/updateTrainingPlan 仍 coming soon）
TOOL_TRAINING_SCHEDULE = "queryTrainingSchedule"


def coros_base_url() -> str:
    """按配置的区域返回高驰 MCP 服务基址。"""
    from ..config import settings

    region = (settings.coros_region or "cn").strip().lower()
    return COROS_MCP_BASES.get(region, COROS_MCP_BASES["cn"])


def _http(timeout: float = 30.0) -> httpx.Client:
    """统一的 httpx 客户端。trust_env=False：不继承 HTTP_PROXY/HTTPS_PROXY，
    高驰为国内直连服务，走系统代理会导致请求失败或证书校验失败。
    """

    return httpx.Client(timeout=timeout, trust_env=False,
                        headers={"User-Agent": "sport-platform/1.0"})


def pkce_pair() -> tuple[str, str]:
    """生成 PKCE 的 (code_verifier, code_challenge)，使用 S256。"""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def register_client(redirect_uri: str, base: str | None = None) -> dict:
    """动态客户端注册（RFC 7591）：POST {base}/connect/register 返回 201 + client_id，
    公共客户端（token_endpoint_auth_method=none），无需预先申请。
    """
    base = base or coros_base_url()
    body = {
        "client_name": CLIENT_NAME,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": SCOPE,
    }
    try:
        with _http(25) as client:
            resp = client.post(f"{base}/connect/register", json=body)
    except httpx.HTTPError as e:
        raise IntegrationError(f"无法连接高驰服务：{e}") from e
    if resp.status_code not in (200, 201):
        raise IntegrationError(
            f"高驰客户端注册失败：HTTP {resp.status_code} {resp.text[:200]}")
    try:
        data = resp.json()
    except ValueError as e:
        raise IntegrationError(f"高驰客户端注册返回非 JSON：{resp.text[:200]}") from e
    if not data.get("client_id"):
        raise IntegrationError(f"高驰客户端注册未返回 client_id：{data}")
    return data


def build_authorize_url(base: str, client_id: str, redirect_uri: str,
                        state: str, code_challenge: str) -> str:
    """构造授权跳转 URL（PKCE，S256）。"""
    query = urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    })
    return f"{base}/oauth2/authorize?{query}"


def exchange_code(base: str, client_id: str, redirect_uri: str,
                  code: str, code_verifier: str) -> dict:
    """授权码换取令牌。"""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    payload = _post_token(base, data)
    payload["obtained_at"] = int(time.time())
    return payload


def refresh_access_token(base: str, client_id: str, refresh_token: str) -> dict:
    """刷新令牌。"""
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    payload = _post_token(base, data)
    payload["obtained_at"] = int(time.time())
    return payload


def _post_token(base: str, data: dict) -> dict:
    try:
        with _http(25) as client:
            resp = client.post(f"{base}/oauth2/token", data=data,
                               headers={"Accept": "application/json"})
    except httpx.HTTPError as e:
        raise IntegrationError(f"无法连接高驰授权服务：{e}") from e
    if resp.status_code != 200:
        raise IntegrationError(
            f"高驰令牌请求失败：HTTP {resp.status_code} {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as e:
        raise IntegrationError(f"高驰令牌响应非 JSON：{resp.text[:200]}") from e


# ============================================================ MCP 客户端
def _parse_sse(text: str) -> list[dict]:
    """从 SSE 响应体里抽出所有 JSON-RPC 消息。"""
    out: list[dict] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        data_lines = [ln[5:].strip() for ln in block.split("\n")
                      if ln.startswith("data:")]
        if not data_lines:
            continue
        try:
            msg = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            continue
        if isinstance(msg, dict):
            out.append(msg)
    return out


class McpHttpClient:
    """MCP Streamable HTTP 客户端（JSON-RPC 2.0）。"""

    def __init__(self, endpoint: str, access_token: str, timeout: float = 40.0):
        self.endpoint = endpoint
        self.access_token = access_token
        self.timeout = timeout
        self.session_id: str | None = None
        self._seq = 0
        self._initialized = False


    def _headers(self) -> dict:
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def _send(self, payload: dict) -> dict:
        try:
            with _http(self.timeout) as client:
                resp = client.post(self.endpoint, json=payload, headers=self._headers())
        except httpx.HTTPError as e:
            raise IntegrationError(f"无法连接高驰 MCP 服务：{e}") from e

        sid = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid

        if resp.status_code in (401, 403):
            raise IntegrationError("高驰授权已失效或权限不足，请在「平台连接」页重新授权")
        if resp.status_code == 406:
            raise IntegrationError(
                "高驰 MCP 拒绝了请求头（Accept 需同时包含 application/json 与 text/event-stream）")
        if resp.status_code >= 400:
            raise IntegrationError(
                f"高驰 MCP 调用失败：HTTP {resp.status_code} {resp.text[:200]}")

        ctype = (resp.headers.get("content-type") or "").lower()
        if "text/event-stream" in ctype:
            messages = _parse_sse(resp.text)
            for m in messages:
                if m.get("id") == payload.get("id"):
                    return m
            return messages[-1] if messages else {}
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}

    def _request(self, method: str, params: dict | None = None) -> Any:
        self._seq += 1
        payload = {"jsonrpc": "2.0", "id": self._seq, "method": method}
        if params is not None:
            payload["params"] = params
        message = self._send(payload)
        if isinstance(message, dict) and message.get("error"):
            err = message["error"] or {}
            raise IntegrationError(
                f"高驰 MCP 返回错误：{err.get('message') or json.dumps(err, ensure_ascii=False)[:200]}")
        return message.get("result") if isinstance(message, dict) else {}

    def _notify(self, method: str, params: dict | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        try:
            self._send(payload)
        except IntegrationError:
            return None


    def initialize(self) -> dict:
        if self._initialized:
            return {}
        result = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "sport-platform", "version": "1.0"},
        })
        self._notify("notifications/initialized")
        self._initialized = True
        return result if isinstance(result, dict) else {}

    def list_tools(self) -> list[dict]:
        result = self._request("tools/list", {})
        tools = (result or {}).get("tools") if isinstance(result, dict) else None
        return tools if isinstance(tools, list) else []

    def call_tool(self, name: str, arguments: dict | None = None) -> Any:
        self.initialize()
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        return _unwrap_tool_result(result)


def _unwrap_tool_result(result: Any) -> Any:
    """解开 MCP 工具返回的包装（isError / structuredContent / content[].text）。"""
    if not isinstance(result, dict):
        return result
    if result.get("isError"):
        raise IntegrationError(f"高驰 MCP 工具执行失败：{_content_text(result)[:300]}")
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    text = _content_text(result)
    if not text:
        return result
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}


def _content_text(result: dict) -> str:
    parts = []
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
            parts.append(str(item["text"]))
    return "\n".join(parts)


# ============================================================ 取值工具
def _pick_key(props: dict, *names: str) -> str | None:
    """在 JSON Schema properties 里挑出实际存在的参数名（大小写不敏感）。"""
    if not props:
        return None
    lowered = {str(k).lower(): k for k in props}
    for name in names:
        if name in props:
            return name
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def _format_time_arg(spec: dict, value: datetime) -> Any:
    """按 schema 声明的类型与描述格式化时间参数。"""
    spec = spec or {}
    declared = str(spec.get("type", "")).lower()
    # 高驰的日期参数要求 yyyyMMdd（传 ISO 会触发 MCP 的异常调用拦截）
    if "yyyymmdd" in str(spec.get("description", "")).lower():
        return value.strftime("%Y%m%d")
    if declared in ("integer", "number"):
        return int(value.timestamp() * 1000) if value.year > 2001 else int(value.timestamp())
    if declared == "string":
        fmt = str((spec or {}).get("format", "")).lower()
        if "date-time" in fmt:
            return value.strftime("%Y-%m-%dT%H:%M:%S")
        return value.strftime("%Y-%m-%d")
    return value.strftime("%Y-%m-%d")


def _to_datetime(value: Any) -> datetime | None:
    """把时间戳（秒/毫秒）或多种字符串格式统一成 datetime。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e11:  # 毫秒
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit() and len(text) >= 10:
        # 10 位以上才可能是时间戳；8 位是 YYYYMMDD，交给下面的 strptime 处理
        return _to_datetime(int(text))
    cleaned = text.replace("Z", "").replace("+00:00", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return None


def _first(data: dict, *keys: str, default: Any = None) -> Any:
    """按优先顺序取第一个非空字段值（兼容不同命名风格）。"""
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return default


# ============================================================ 文本解析层
# 高驰 MCP 的 22 个工具返回给人阅读的排版文本（不是 JSON），
# 以下解析器按各工具的输出格式逐个编写。
_TS_RE = re.compile(r"startTimestamp=(\d{10,13})")
_COORD_RE = re.compile(r"(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)")
_MH_RE = re.compile(r"(\d+)\s*h\s*(\d+)\s*min")
_MIN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*min")
_HR_LINE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\s*:\s*(\d+)\s*bpm")
_DAY_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DAY_BLOCK_RE = re.compile(r"^---\s*(\d{4})(\d{2})(\d{2})\s*---$")
_RECORD_HEAD_RE = re.compile(r"^(\d+)\.\s+(.+?)\s+[—\-–]\s+(\d{4})-(\d{2})-(\d{2})\s*$")
_PLACE_RE = re.compile(r"^[\u4e00-\u9fff]{2,6}(市|省|区|县|镇|乡|路|街|大道|公园|广场)")


def _as_text(payload: Any) -> str:
    """把工具返回统一成纯文本（高驰 MCP 输出为可读文本，非 JSON）。"""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("text", "content", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
        for value in payload.values():
            if isinstance(value, str) and "\n" in value:
                return value
    if isinstance(payload, list):
        return "\n".join(part for part in (_as_text(i) for i in payload) if part)
    return ""


def _tool_text(payload: Any, tool: str) -> str:
    """取文本并拦截高驰的异常调用提示，命中即抛 IntegrationError。"""
    text = _as_text(payload)
    if ANOMALY_MARK in text:
        raise IntegrationError(
            f"高驰 MCP 拒绝了 {tool} 调用（入参格式不符或上下文异常）。"
            "请确认日期参数为 yyyyMMdd 格式后重试。")
    return text


def _duration_sec(text: Any) -> int | None:
    """'26:49' → 1609；'1:05:01' → 3901。"""
    if not text:
        return None
    nums = [int(p) for p in re.findall(r"\d+", str(text))[:3]]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return nums[0] if nums else None


def _km_to_m(text: Any) -> float | None:
    """带单位感知的距离换算：'4.16 km' → 4160.0；'861 m' → 861.0。
    高驰对 GPS 漂移碎片活动（几十秒/几百米）返回米而非公里，
    无视单位一律 ×1000 会把米当公里，污染 PB 与统计。
    """
    if not text:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(km|m)?\s*$", str(text).strip(), re.I)
    if not m:
        return None
    val = float(m.group(1))
    unit = (m.group(2) or "km").lower()
    return round(val * 1000 if unit == "km" else val, 1)


def _pace_to_sec(text: Any) -> int | None:
    """'6:27 /km' → 387"""
    return _duration_sec(str(text).split("/")[0].strip()) if text else None


def _int_in(text: Any) -> int | None:
    """从 '454 kcal' / '13,344' 里取整数。"""
    if text is None:
        return None
    m = re.search(r"(\d+)", str(text).replace(",", ""))
    return int(m.group(1)) if m else None


def _float_in(text: Any) -> float | None:
    """从 'Load Ratio: 1.21' 里取浮点数。"""
    if text is None:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", str(text))
    return float(m.group(1)) if m else None


def _hours_from(text: Any) -> float | None:
    """'6h 22min' → 6.37；'58 min' → 0.97。"""
    if not text:
        return None
    m = _MH_RE.search(str(text))
    if m:
        return round(int(m.group(1)) + int(m.group(2)) / 60.0, 2)
    m = _MIN_RE.search(str(text))
    return round(float(m.group(1)) / 60.0, 2) if m else None


def _is_place(text: str) -> bool:
    """高驰 Location 字段：是地名还是用户自定义的训练名称。"""
    return bool(_PLACE_RE.match(text or ""))


def _kv_segments(line: str) -> list[tuple[str, str]]:
    """'Duration: 26:49 | Distance: 4.16 km' → [('duration','26:49'), ('distance','4.16 km')]"""
    out: list[tuple[str, str]] = []
    for segment in line.split("|"):
        segment = segment.strip()
        if not segment:
            continue
        m = re.match(r"^([^:=]{1,40}?)\s*[:=]\s*(.+)$", segment)
        if m:
            out.append((m.group(1).strip().lower(), m.group(2).strip()))
    return out


def parse_sport_records(text: str) -> list[dict]:
    """解析 querySportRecords 的文本输出。

    形如：
        1. Outdoor Run — 2026-01-01
           Location: 示例市
           Time Window: startTimestamp=... | endTimestamp=...
           Duration: 30:00 | Distance: 5.00 km
           Average Pace: 6:00 /km | Avg HR: 150 bpm | Calories: 400 kcal
           LabelId: 123456789012345678 | SportType: 100
    注意：条目内可能有以 '|' 开头的续行（如力量训练的 Sets 行），需并入上一行。
    """
    blocks: list[list[str]] = []
    buffer: list[str] = []
    started = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _RECORD_HEAD_RE.match(line):
            if buffer:
                blocks.append(buffer)
            buffer = [line]
            started = True
            continue
        if not started:
            continue
        if line.startswith("|") and buffer:
            buffer[-1] += " " + line
            continue
        buffer.append(line)
    if buffer:
        blocks.append(buffer)

    records: list[dict] = []
    for block in blocks:
        head = _RECORD_HEAD_RE.match(block[0])
        if not head:
            continue
        fields: dict[str, str] = {}
        for line in block[1:]:
            for key, value in _kv_segments(line):
                fields.setdefault(key, value)
        stamp = _TS_RE.search(fields.get("time window", ""))
        coord = _COORD_RE.search(fields.get("start coordinates", ""))
        records.append({
            "sport_name": head.group(2).strip(),
            "record_date": f"{head.group(3)}-{head.group(4)}-{head.group(5)}",
            "label_id": fields.get("labelid", ""),
            "sport_type": _int_in(fields.get("sporttype")),
            "location": fields.get("location", ""),
            "latitude": float(coord.group(1)) if coord else None,
            "longitude": float(coord.group(2)) if coord else None,
            "start_ts": int(stamp.group(1)[:10]) if stamp else None,
            "end_ts": _int_in(fields.get("endtimestamp")),
            "duration_sec": _duration_sec(fields.get("duration")),
            "distance_m": _km_to_m(fields.get("distance")),
            "pace_sec": _pace_to_sec(fields.get("average pace")),
            "avg_hr": _int_in(fields.get("avg hr")),
            "max_hr": _int_in(fields.get("max hr")),
            "calories": _int_in(fields.get("calories")),
            "elevation_gain": _int_in(fields.get("elevation gain") or fields.get("total ascent")),
            "sets": _int_in(fields.get("sets")),
            "cadence": _int_in(fields.get("avg cadence") or fields.get("cadence")),
            "power": _int_in(fields.get("avg power")),
            "raw_fields": fields,
        })
    return records


_DETAIL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("training_load", re.compile(r"^\s*Training Load:\s*(\d+(?:\.\d+)?)", re.M | re.I)),
    ("te_aerobic", re.compile(r"^\s*Aerobic TE:\s*(\d+(?:\.\d+)?)", re.M | re.I)),
    ("te_anaerobic", re.compile(r"^\s*Anaerobic TE:\s*(\d+(?:\.\d+)?)", re.M | re.I)),
    ("cadence", re.compile(r"Average Cadence:\s*(\d+(?:\.\d+)?)\s*spm", re.I)),
    ("power", re.compile(r"Average Power:\s*(\d+(?:\.\d+)?)\s*W\b", re.I)),
    ("avg_hr", re.compile(r"Average Heart Rate:\s*(\d+)\s*bpm", re.I)),
    ("max_hr", re.compile(r"(?:Max|Maximum) Heart Rate:\s*(\d+)\s*bpm", re.I)),
    ("stride_m", re.compile(r"Average Stride Length:\s*(\d+(?:\.\d+)?)\s*m\b", re.I)),
    ("sets", re.compile(r"^\s*Sets:\s*(\d+)", re.M | re.I)),
    ("workout_time_sec", re.compile(r"Workout Time:\s*(\d+):(\d+)", re.I)),
)
_DETAIL_ELEV_RE = re.compile(
    r"Elevation Gain\s*/\s*Loss:\s*(\d+(?:\.\d+)?)\s*m\s*/\s*(\d+(?:\.\d+)?)\s*m", re.I)
_DETAIL_BEST_KM_RE = re.compile(r"Best Kilometer:\s*(\d+):(\d+)", re.I)
_DETAIL_TEXT_RE: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("perceived_effort", re.compile(r"Perceived Effort:\s*(.+)", re.I)),
    ("focus", re.compile(r"Training Focus:\s*(.+)", re.I)),
    ("performance", re.compile(r"Performance:\s*(.+)", re.I)),
)


def parse_activity_detail(text: str) -> dict[str, Any]:
    """解析 getActivityDetail 的可读文本，抽取列表接口不提供的指标。
    querySportRecords 只返回距离/配速/平均心率/卡路里，步频、功率、爬升、
    训练效果与训练负荷只存在于详情文本，需靠本函数回填；
    解析失败或不命中任何字段时返回空字典，不抛异常。
    """
    if not text:
        return {}
    out: dict[str, Any] = {}
    for key, pattern in _DETAIL_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        if key == "workout_time_sec":
            out[key] = int(m.group(1)) * 60 + int(m.group(2))
        elif key in ("avg_hr", "max_hr", "sets"):
            out[key] = int(m.group(1))
        else:
            out[key] = float(m.group(1))
    elev = _DETAIL_ELEV_RE.search(text)
    if elev:
        out["elevation_gain"] = float(elev.group(1))
        out["elevation_loss"] = float(elev.group(2))
    best = _DETAIL_BEST_KM_RE.search(text)
    if best:
        out["best_km_pace_sec"] = int(best.group(1)) * 60 + int(best.group(2))
    for key, pattern in _DETAIL_TEXT_RE:
        m = pattern.search(text)
        if m:
            value = m.group(1).strip()
            if value:
                out[key] = value
    return out


def parse_resting_hr(text: str) -> dict[str, int]:
    """解析 queryRestingHeartRate：'2026-09-10: 54 bpm'。"""
    out: dict[str, int] = {}
    for raw_line in text.splitlines():
        m = _HR_LINE_RE.match(raw_line.strip())
        if m:
            out[f"{m.group(1)}-{m.group(2)}-{m.group(3)}"] = int(m.group(4))
    return out


def parse_sleep(text: str) -> dict[str, dict]:
    """解析 querySleepData：日期行 + Key: Value 块。"""
    raw: dict[str, dict] = {}
    current: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = _DAY_RE.match(line)
        if m:
            current = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
            raw.setdefault(current, {})
            continue
        if current is None or line.startswith("=") or line.lower().startswith("note"):
            continue
        for key, value in _kv_segments(line):
            raw[current].setdefault(key, value)

    out: dict[str, dict] = {}
    for day, fields in raw.items():
        entry: dict[str, Any] = {"date": day}
        score = _int_in(fields.get("sleep score"))
        if score is not None:
            entry["sleep_score"] = score
        hours = _hours_from(fields.get("main sleep") or fields.get("total sleep"))
        if hours is not None:
            entry["sleep_hours"] = hours
        hrv = _int_in(fields.get("sleep hrv") or fields.get("hrv") or fields.get("average hrv"))
        if hrv is not None:
            entry["hrv_rmssd"] = hrv
        if len(entry) > 1:
            out[day] = entry
    return out


def parse_daily_health(text: str) -> dict[str, dict]:
    """解析 queryDailyHealthData：'--- 20260904 ---' 分块的 Key: Value。"""
    raw: dict[str, dict] = {}
    current: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = _DAY_BLOCK_RE.match(line)
        if m:
            current = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
            raw.setdefault(current, {})
            continue
        if current is None or line.startswith("=") or line.lower().startswith("note"):
            continue
        for key, value in _kv_segments(line):
            raw[current].setdefault(key, value)

    out: dict[str, dict] = {}
    for day, fields in raw.items():
        entry: dict[str, Any] = {"date": day}
        stress = _int_in(fields.get("stress"))
        if stress is not None:
            entry["stress"] = stress
        hours = _hours_from(fields.get("total"))
        if hours is not None:
            entry["sleep_hours"] = hours
        if len(entry) > 1:
            out[day] = entry
    return out


def parse_fitness_assessment(text: str) -> dict[str, Any]:
    """解析 queryFitnessAssessmentOverview：官方体能评估（无日期的当期快照）。

    VO2max: 51 / Running Level: 75 / Threshold Pace: 4:57 /km /
    5 km Prediction: 23:50 / Marathon Prediction: 4:01:09 …
    解析不到的字段一律缺省，不编造。
    """
    if not text:
        return {}
    out: dict[str, Any] = {}
    fields: dict[str, str] = {}
    for raw_line in text.splitlines():
        for key, value in _kv_segments(raw_line):
            fields.setdefault(key.lower(), value)
    vo2 = _int_in(fields.get("vo2max"))
    if vo2 is not None:
        out["vo2max"] = vo2
    level = _int_in(fields.get("running level"))
    if level is not None:
        out["running_level"] = level
    pace = _pace_to_sec(fields.get("threshold pace"))
    if pace is not None:
        out["threshold_pace_sec"] = pace
    predictions = {}
    for key, names in (("5k", ("5 km prediction", "5k prediction")),
                       ("10k", ("10 km prediction", "10k prediction")),
                       ("half_marathon", ("half marathon prediction",)),
                       ("marathon", ("marathon prediction",))):
        for name in names:
            sec = _duration_sec(fields.get(name))
            if sec is not None:
                predictions[key] = sec
                break
    if predictions:
        out["race_predictions"] = predictions
    return out


def parse_training_load(text: str) -> dict[str, dict]:
    """解析 queryTrainingLoadAssessment：日期行 + 指标块，返回逐日负荷比。

    2026-09-17 / Comment: Optimized / Short-Term Load: 62 /
    Long-Term Load: 51 / Load Ratio: 1.21
    """
    raw: dict[str, dict] = {}
    current: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = _DAY_RE.match(line)
        if m:
            current = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
            raw.setdefault(current, {})
            continue
        if current is None or line.startswith("="):
            continue
        for key, value in _kv_segments(line):
            raw[current].setdefault(key, value)

    out: dict[str, dict] = {}
    for day, fields in raw.items():
        entry: dict[str, Any] = {"date": day}
        comment = fields.get("comment")
        if comment:
            entry["comment"] = comment
        short = _int_in(fields.get("short-term load"))
        if short is not None:
            entry["short_load"] = short
        long = _int_in(fields.get("long-term load"))
        if long is not None:
            entry["long_load"] = long
        ratio = _float_in(fields.get("load ratio"))
        if ratio is not None:
            entry["load_ratio"] = ratio
        if len(entry) > 1:
            out[day] = entry
    return out


def parse_recovery_status(text: str) -> dict[str, Any]:
    """解析 queryRecoveryStatus：Recovery: 93% / Level: … / Estimated Full Recovery: 14h。"""
    if not text:
        return {}
    out: dict[str, Any] = {}
    fields: dict[str, str] = {}
    for raw_line in text.splitlines():
        for key, value in _kv_segments(raw_line):
            fields.setdefault(key.lower(), value)
    pct = _int_in(fields.get("recovery"))
    if pct is not None:
        out["recovery_pct"] = pct
    level = fields.get("level")
    if level:
        out["level"] = level
    m = re.search(r"(\d+)\s*h", fields.get("estimated full recovery") or "", re.I)
    if m:
        out["full_recovery_hours"] = int(m.group(1))
    return out


# 高驰圈数据的原始单位（queryActivityLapData 结构化返回）：
# distance/lapDistance = 0.01km、avgStrideLength = cm、strideHeight = mm（垂直振幅）、
# groundTime = ms、strideRatio = 0.1%、时间/配速 = 秒（s、s/km）、功率 W、心率 bpm。
_LAP_GROUP_AUTO_KM = 10    # 自动 1km 圈，粒度最适合分段/解耦
_LAP_GROUP_MANUAL = 2
_LAP_GROUP_TOTAL = -1


def _lap_num(lap: dict, key: str, minimum: float | None = None) -> float | None:
    """取圈数据数值；0 视为设备无该传感器而缺省（formPower/legStiffness 常年 0）。"""
    v = lap.get(key)
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v == 0 or (minimum is not None and v < minimum):
        return None
    return v


def _normalize_lap(lap: dict, index: int) -> dict:
    """单圈归一成 canonical 形状（与 _parse_device_laps 消费端字段对齐）。"""
    out: dict[str, Any] = {"index": index}
    dist = _lap_num(lap, "distance")
    time = _lap_num(lap, "time")
    if dist is not None:
        out["distance_m"] = round(dist / 100, 1)          # 0.01km → m
    if time is not None:
        out["duration_sec"] = round(time, 1)
        if out.get("distance_m"):
            out["pace_sec_per_km"] = round(time / (out["distance_m"] / 1000))
    for src, dst, scale in (("avgHr", "avg_hr", 1), ("maxHr", "max_hr", 1),
                            ("avgPower", "avg_power", 1), ("avgCadence", "avg_cadence", 1),
                            ("elevGain", "elev_gain_m", 1), ("totalDescent", "elev_loss_m", 1)):
        v = _lap_num(lap, src)
        if v is not None:
            out[dst] = round(v / scale, 1)
    gap = _lap_num(lap, "adjustedPace")
    if gap is not None and 60 < gap < 2400:
        out["gap_sec_per_km"] = round(gap)                # 官方坡度调整配速，优先于本地估算
    stride = _lap_num(lap, "avgStrideLength")
    if stride is not None:
        out["stride_m"] = round(stride / 100, 2)          # cm → m
    vosc = _lap_num(lap, "strideHeight")
    if vosc is not None:
        out["vosc_mm"] = round(vosc)                      # 垂直振幅 mm
    gct = _lap_num(lap, "groundTime")
    if gct is not None:
        out["gct_ms"] = round(gct)                        # 踏地时间 ms
    vr = _lap_num(lap, "strideRatio")
    if vr is not None:
        out["vr_pct"] = round(vr / 10, 1)                 # 垂直步幅比 0.1% → %
    return out


def _normalize_strength_lap(lap: dict, index: int) -> dict:
    """力量训练圈：动作代码 + 组数/次数/重量（exerciseNameKey 是官方动作码）。"""
    out: dict[str, Any] = {"index": index, "exercise_name_key": lap.get("exerciseNameKey")}
    for src, dst, caster in (("sets", "sets", int), ("reps", "reps", int),
                             ("weight", "weight", float), ("avgHr", "avg_hr", int),
                             ("maxHr", "max_hr", int)):
        v = _lap_num(lap, src)
        if v is not None:
            out[dst] = caster(v)
    time = _lap_num(lap, "time")
    if time is not None:
        out["time_sec"] = round(time, 1)
    return out


def parse_laps(payload: Any) -> dict[str, Any] | None:
    """归一 queryActivityLapData 的结构化返回 → {'groups': [...]}。

    圈组按 type 分（10=自动1km、2=手动、11=5km、-1=整程汇总），全部保留；
    消费方（activity_detail/_build_splits 等）优先取自动 1km 组。
    非法载荷返回 None。此处只归一不裁剪——raw JSON 存储成本每活动约几 KB。
    """
    if not isinstance(payload, dict):
        return None
    groups_out: list[dict] = []
    for group in payload.get("lapGroups") or []:
        if not isinstance(group, dict):
            continue
        laps_raw = group.get("laps") or []
        if not laps_raw:
            continue
        is_strength = bool(laps_raw[0].get("exerciseNameKey"))
        laps = [_normalize_strength_lap(lap, i + 1) if is_strength else _normalize_lap(lap, i + 1)
                for i, lap in enumerate(laps_raw) if isinstance(lap, dict)]
        lap_distance = _lap_num(group, "lapDistance")
        groups_out.append({
            "type": group.get("type"),
            "lap_distance_m": round(lap_distance / 100, 1) if lap_distance else None,
            "laps": laps,
        })
    return {"groups": groups_out} if groups_out else None


def best_lap_group(groups: list[dict]) -> dict | None:
    """挑最佳圈组：自动 1km 优先，其次手动圈，最后取圈数最多的组。"""
    if not groups:
        return None
    by_type = {g.get("type"): g for g in groups}
    for wanted in (_LAP_GROUP_AUTO_KM, _LAP_GROUP_MANUAL):
        g = by_type.get(wanted)
        if g and len(g.get("laps") or []) >= 2:
            return g
    return max(groups, key=lambda g: len(g.get("laps") or []))


# queryTrainingSchedule 的课次块头（一个日期一行，同一天可有多节课）
_SCHEDULE_DAY_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_SCHEDULE_CODE_RE = re.compile(r"^(S\d+)$", re.I)


def parse_training_schedule(text: str) -> list[dict]:
    """解析 queryTrainingSchedule 的文本输出。

    形如（官方 MCP 现状：只有模板码，没有课次名称；queryTrainingPlanDetail
    上线后可考虑补名称与结构化步骤）：
        Training Schedule
        ========================
        2026-09-15
        S5808
        Plan ID: 123456789012345678
        idInPlan: 7
        Distance: 8.00 km
        Estimated Time: 1:02:04
        Load: 81 TL
        ...（尾注行忽略）
    返回 list[dict]：date(ISO 字符串) / code / plan_id / id_in_plan /
    distance_km / duration_min / load_tl，缺失字段缺省。
    """
    workouts: list[dict] = []
    current: dict | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("="):
            continue
        m = _SCHEDULE_DAY_RE.match(line)
        if m:
            if current:
                workouts.append(current)
            # 三个度量字段显式缺省，调用方不必逐键判断存在性
            current = {"date": f"{m.group(1)}-{m.group(2)}-{m.group(3)}",
                       "distance_km": None, "duration_min": None, "load_tl": None}
            continue
        if current is None:
            continue  # 「Training Schedule」等头部与尾注
        m = _SCHEDULE_CODE_RE.match(line)
        if m:
            current["code"] = m.group(1).upper()
            continue
        for key, value in _kv_segments(line):
            if key == "plan id":
                current["plan_id"] = value
            elif key == "idinplan":
                current["id_in_plan"] = _maybe_int(value)
            elif key == "distance":
                km = _km_to_m(value)
                if km is not None:
                    current["distance_km"] = round(km / 1000, 2)
            elif key in ("estimated time", "duration"):
                sec = _duration_sec(value)
                if sec is not None:
                    current["duration_min"] = round(sec / 60, 1)
            elif key == "load":
                current["load_tl"] = _int_in(value)
    if current:
        workouts.append(current)
    return [w for w in workouts if w.get("code") or w.get("id_in_plan") is not None]


def _extract_rows(payload: Any) -> list[dict]:
    """从工具返回里挖出记录列表（不同工具包装层级不一）。"""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("records", "list", "items", "data", "activities", "rows", "result", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            rows = [r for r in value if isinstance(r, dict)]
            if rows:
                return rows
        elif isinstance(value, dict):
            rows = _extract_rows(value)
            if rows:
                return rows
    for value in payload.values():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value
    return []


# ============================================================ 适配器
class CorosAdapter(PlatformAdapter):
    platform = "coros"

    def __init__(self, credentials: dict | None = None):
        super().__init__(credentials or {})
        self.base = coros_base_url()
        # 凭据被原地修改（如 token 刷新）时置位，供路由层持久化
        self.dirty = False
        # 复用 MCP 会话与工具 schema，避免每条活动都重新握手
        self._mcp_client = None
        self._schemas = {}
        # labelId → sportType 编码（getActivityDetail 强制要求同时传两者）
        self._sport_codes: dict[str, int] = {}

    # -------------------------------------------------- 配置
    def check_config(self) -> None:
        if not self.credentials.get("access_token"):
            raise IntegrationError(
                "尚未连接高驰账号。请点击「连接高驰」，在浏览器登录并授权后即可同步。")

    # -------------------------------------------------- OAuth（实例侧）
    def ensure_client(self) -> str:
        """确保存在 OAuth 客户端；没有则动态注册并写入凭据。"""
        client_id = self.credentials.get("client_id")
        if client_id:
            return str(client_id)
        from ..config import settings

        registered = register_client(settings.coros_redirect_uri, self.base)
        self.credentials["client_id"] = registered["client_id"]
        self.credentials["client_name"] = registered.get("client_name", "")
        self.credentials["registered_at"] = int(time.time())
        self.dirty = True
        return str(registered["client_id"])

    def begin_authorize(self, state: str | None = None) -> dict:
        """准备授权：确保客户端已注册，生成本次 PKCE 参数与跳转链接。"""
        from ..config import settings

        client_id = self.ensure_client()
        verifier, challenge = pkce_pair()
        state = state or secrets.token_urlsafe(24)
        self.credentials["code_verifier"] = verifier
        self.credentials["oauth_state"] = state
        self.dirty = True
        return {
            "url": build_authorize_url(self.base, client_id, settings.coros_redirect_uri,
                                       state, challenge),
            "state": state,
            "client_id": client_id,
        }

    def complete_authorize(self, code: str, state: str | None = None) -> dict:
        """处理回调：校验 state 并用授权码换取令牌。"""
        from ..config import settings

        # fail-closed 校验：没有待校验的 state，或回调未携带 state、state 不匹配，一律拒绝
        expected = self.credentials.get("oauth_state")
        if not expected:
            raise IntegrationError("未找到待校验的授权会话，请重新发起授权")
        if not state or not secrets.compare_digest(str(state), str(expected)):
            raise IntegrationError("授权回调校验失败（state 不匹配），请重新发起授权")
        client_id = self.ensure_client()
        verifier = self.credentials.get("code_verifier")
        if not verifier:
            raise IntegrationError("缺少 PKCE 校验串，请重新发起授权")
        token = exchange_code(self.base, client_id, settings.coros_redirect_uri, code, verifier)
        self.credentials.update(token)
        for stale in ("code_verifier", "oauth_state"):
            self.credentials.pop(stale, None)
        self.dirty = True
        return token

    def _access_token(self) -> str:
        token = self.credentials.get("access_token")
        if not token:
            self.check_config()
            raise IntegrationError("尚未完成高驰授权")
        expires_in = _maybe_int(self.credentials.get("expires_in")) or 0
        obtained_at = maybe_float(self.credentials.get("obtained_at")) or 0
        if expires_in and time.time() - obtained_at > expires_in - TOKEN_REFRESH_MARGIN_SEC:
            refresh = self.credentials.get("refresh_token")
            if refresh:
                fresh = refresh_access_token(self.base, self.ensure_client(), str(refresh))
                self.credentials.update(fresh)
                self.dirty = True
        return str(self.credentials["access_token"])

    # -------------------------------------------------- MCP
    def client(self) -> McpHttpClient:
        """复用同一 MCP 会话，避免每次调用都重新 initialize / tools/list。"""
        if self._mcp_client is None:
            self._mcp_client = McpHttpClient(f"{self.base}/mcp", self._access_token())
        return self._mcp_client

    def list_tools(self) -> list[dict]:
        """列出高驰 MCP 当前可用工具（用于诊断与自适配）。"""
        return self.client().list_tools()

    def call_tool(self, name: str, arguments: dict | None = None) -> Any:
        return self.client().call_tool(name, arguments)

    def _schema(self, name: str) -> dict:
        """取工具入参 JSON Schema（带缓存），用于自适应不同版本的参数名。"""
        if name not in self._schemas:
            schema = {}
            for tool in self.client().list_tools():
                if tool.get("name") == name:
                    schema = tool.get("inputSchema") or {}
                    break
            self._schemas[name] = schema
        return self._schemas[name]


    def fetch_activities(self, since: datetime, until: datetime | None = None) -> list[NormalizedActivity]:
        arguments = _build_sport_record_args(self._schema(TOOL_SPORT_RECORDS), since, until)
        text = _tool_text(self.call_tool(TOOL_SPORT_RECORDS, arguments), TOOL_SPORT_RECORDS)
        rows = parse_sport_records(text)
        for row in rows:  # 缓存编码，供 fetch_detail 使用
            if row.get("label_id") and row.get("sport_type") is not None:
                self._sport_codes[str(row["label_id"])] = int(row["sport_type"])
        return [_normalize_activity(row) for row in rows]

    def fetch_detail(self, external_id: str, sport_type: int | None = None) -> dict | None:
        """拉取单次活动详情（含逐公里分段等文本分析），失败返回 None 不阻断同步。

        注意：getActivityDetail 的 schema 强制要求同时传 labelId 与 sportType，
        缺 sportType 会被服务端抛 NullPointerException。故这里从活动列表缓存里补。
        """
        if not external_id:
            return None
        try:
            props = (self._schema(TOOL_ACTIVITY_DETAIL) or {}).get("properties") or {}
            args: dict[str, Any] = {}
            label_key = _pick_key(props, "labelId", "activityId", "sportId", "id")
            if label_key:
                args[label_key] = _coerce_like(props.get(label_key), str(external_id))
            code_key = _pick_key(props, "sportType", "sportTypeCode")
            code = sport_type if sport_type is not None else self._sport_codes.get(str(external_id))
            if code_key and code is not None:
                args[code_key] = int(code)
            if not args:
                return None
            text = _tool_text(self.call_tool(TOOL_ACTIVITY_DETAIL, args), TOOL_ACTIVITY_DETAIL)
            return {"text": text[:20000]} if text else None
        except Exception:
            return None

    def fetch_laps(self, external_id: str, sport_type: int | None = None) -> dict | None:
        """拉取单次活动的逐圈结构化数据（分段/功率/心率/跑步动态），失败返回 None。

        高驰活动的解耦、疲劳抗性、真实分段此前全靠均值合成——圈数据是
        这些指标从「估算」升级为「实测」的数据源。入参要求同 fetch_detail。
        """
        if not external_id:
            return None
        try:
            props = (self._schema(TOOL_LAPS) or {}).get("properties") or {}
            args: dict[str, Any] = {}
            label_key = _pick_key(props, "labelId", "activityId", "sportId", "id")
            if label_key:
                args[label_key] = _coerce_like(props.get(label_key), str(external_id))
            code_key = _pick_key(props, "sportType", "sportTypeCode")
            code = sport_type if sport_type is not None else self._sport_codes.get(str(external_id))
            if code_key and code is not None:
                args[code_key] = int(code)
            if not args:
                return None
            return parse_laps(self.call_tool(TOOL_LAPS, args))
        except Exception:
            return None

    def apply_laps(self, activity: Any, laps: dict | None) -> bool:
        """把归一圈数据写入活动 raw：最佳组投影到 raw["splits"]（canonical，
        分段/解耦/疲劳抗性等既有消费方直接吃到），全组保留在 raw["coros_laps"]。
        返回是否写入了有效分段。已有设备分段（佳明/Strava）不覆盖。"""
        if not laps:
            return False
        raw = dict(getattr(activity, "raw", None) or {})
        groups = laps.get("groups") or []
        raw["coros_laps"] = laps
        best = best_lap_group(groups)
        projected = False
        if best and not raw.get("splits"):
            items = []
            for lap in best.get("laps") or []:
                if not (lap.get("distance_m") and lap.get("duration_sec")):
                    continue
                item = {"index": lap["index"], "distance_m": lap["distance_m"],
                        "duration_sec": round(lap["duration_sec"]),
                        "pace_sec_per_km": lap.get("pace_sec_per_km"),
                        "avg_hr": lap.get("avg_hr"), "max_hr": lap.get("max_hr"),
                        "avg_power": lap.get("avg_power"), "elev_gain_m": lap.get("elev_gain_m")}
                if lap.get("gap_sec_per_km"):
                    item["gap_sec_per_km"] = lap["gap_sec_per_km"]
                item = {k: v for k, v in item.items() if v is not None}
                if item.get("pace_sec_per_km"):
                    items.append(item)
            if len(items) >= 2:
                raw["splits"] = items
                raw["splits_source"] = "coros_laps"
                projected = True
        activity.raw = raw

        # 逐圈跑步动态按圈时长加权均值补进 dynamics（详情文本只有步幅，
        # 垂直振幅/触地时间只存在于圈数据；缺 key 才补，不覆盖设备值）
        laps = (best or {}).get("laps") or []
        weighted: dict[str, tuple[float, float]] = {}
        for lap in laps:
            t = lap.get("duration_sec")
            if not t:
                continue
            for src in ("vosc_mm", "gct_ms", "vr_pct"):
                v = lap.get(src)
                if v is not None:
                    acc = weighted.setdefault(src, (0.0, 0.0))
                    weighted[src] = (acc[0] + v * t, acc[1] + t)
        if weighted:
            dynamics = dict(getattr(activity, "dynamics", None) or {})
            scale = {"vosc_mm": ("vosc_cm", 10.0), "gct_ms": ("gct_ms", 1.0),
                     "vr_pct": ("vosc_ratio_pct", 1.0)}
            for src, (dst, div) in scale.items():
                total, tsum = weighted.get(src, (0.0, 0.0))
                if tsum and dst not in dynamics:
                    dynamics[dst] = round(total / tsum / div, 1)
            activity.dynamics = dynamics
        return projected

    def apply_detail(self, activity: Any, text: str) -> dict[str, Any]:
        """把详情文本里的指标回填到活动对象（只补缺失，不覆盖已有值）。

        传入对象需具备 NormalizedActivity 或 models.Activity 的字段名
        （avg_cadence / avg_power / elevation_m / te_aerobic / te_anaerobic /
        avg_hr / max_hr），并带有可写的 raw 与 dynamics。返回被回填的字段。
        """
        metrics = parse_activity_detail(text)
        if not metrics:
            return {}
        filled: dict[str, Any] = {}

        for src, attr, caster in (
            ("cadence", "avg_cadence", float),
            ("power", "avg_power", float),
            ("te_aerobic", "te_aerobic", float),
            ("te_anaerobic", "te_anaerobic", float),
            ("max_hr", "max_hr", int),
            ("avg_hr", "avg_hr", int),
        ):
            if metrics.get(src) is None or getattr(activity, attr, None) is not None:
                continue
            setattr(activity, attr, caster(metrics[src]))
            filled[attr] = getattr(activity, attr)

        gain = metrics.get("elevation_gain")
        if gain and not getattr(activity, "elevation_m", None):
            activity.elevation_m = float(gain)
            filled["elevation_m"] = activity.elevation_m

        # raw 可能是 SQLAlchemy JSON 列，必须整体重新赋值才能被检测到变更
        raw = dict(getattr(activity, "raw", None) or {})
        if metrics.get("training_load") is not None:
            raw["coros_training_load"] = float(metrics["training_load"])
        raw["detail_metrics"] = metrics
        activity.raw = raw

        extras = {k: metrics[k] for k in (
            "stride_m", "best_km_pace_sec", "elevation_loss", "sets",
            "perceived_effort", "focus", "performance", "workout_time_sec")
            if metrics.get(k) is not None}
        if extras:
            dynamics = dict(getattr(activity, "dynamics", None) or {})
            for key, value in extras.items():
                dynamics.setdefault(key, value)
            activity.dynamics = dynamics
        return filled

    def fetch_body_metrics(self, since_days: int = 30) -> list[dict]:
        """拉取每日身体数据（静息心率 / 睡眠 / 压力），合并成按日期的字典。

        高驰 MCP 返回可读文本，因此逐工具按其文本格式解析。
        任一项失败只跳过该项，不影响其余指标。
        """
        merged: dict[str, dict] = {}

        def bucket(day: str) -> dict:
            return merged.setdefault(day, {"date": day})

        # 1) 每日健康总览：压力、睡眠时长
        with swallowed("拉取每日健康总览", logger=logger,
                       tool=TOOL_DAILY_HEALTH, since_days=since_days):
            text = _tool_text(
                self.call_tool(TOOL_DAILY_HEALTH,
                               _build_daily_args(self._schema(TOOL_DAILY_HEALTH), since_days)),
                TOOL_DAILY_HEALTH)
            for day, fields in parse_daily_health(text).items():
                target = bucket(day)
                for key in ("stress", "sleep_hours"):
                    if fields.get(key) is not None:
                        target.setdefault(key, fields[key])

        # 2) 静息心率
        with swallowed("拉取静息心率", logger=logger,
                       tool=TOOL_RESTING_HR, since_days=since_days):
            text = _tool_text(
                self.call_tool(TOOL_RESTING_HR,
                               _build_daily_args(self._schema(TOOL_RESTING_HR), since_days)),
                TOOL_RESTING_HR)
            for day, rhr in parse_resting_hr(text).items():
                bucket(day)["resting_hr"] = rhr

        # 3) 睡眠评分 / 睡眠 HRV / 睡眠时长（缺失时补齐）
        with swallowed("拉取睡眠数据", logger=logger,
                       tool=TOOL_SLEEP, since_days=since_days):
            text = _tool_text(
                self.call_tool(TOOL_SLEEP,
                               _build_daily_args(self._schema(TOOL_SLEEP), since_days)),
                TOOL_SLEEP)
            for day, fields in parse_sleep(text).items():
                target = bucket(day)
                for key in ("sleep_score", "hrv_rmssd", "sleep_hours"):
                    if fields.get(key) is not None and target.get(key) is None:
                        target[key] = fields[key]

        return [v for v in merged.values() if len(v) > 1]

    def fetch_fitness_assessment(self, since_days: int = 90) -> dict:
        """高驰官方体能评估 + 训练负荷评估（实时透传，不落库）。
        返回 {"fitness": 原文, "load": 原文}；单项失败为 None 不抛异常。
        """
        out: dict[str, str | None] = {"fitness": None, "load": None}
        for key, tool in (("fitness", TOOL_FITNESS), ("load", TOOL_LOAD_ASSESSMENT)):
            try:
                schema = self._schema(tool)
                args = _build_daily_args(schema, since_days) if (schema or {}).get("properties") else {}
                out[key] = _tool_text(self.call_tool(tool, args), tool) or None
            except Exception as exc:
                logger.warning("官方体能评估拉取失败 tool=%s: %s", tool, exc)
        return out

    def fetch_recovery_status(self) -> dict:
        """高驰官方恢复状态（当期快照：恢复百分比/等级/完全恢复小时数）。

        queryRecoveryStatus 无入参；失败返回空 dict 不抛异常。
        """
        try:
            text = _tool_text(self.call_tool(TOOL_RECOVERY, {}), TOOL_RECOVERY) or ""
            return parse_recovery_status(text)
        except Exception as exc:
            logger.warning("官方恢复状态拉取失败: %s", exc)
            return {}

    def fetch_training_schedule(self, since, until) -> list[dict]:
        """拉取官方训练课表（含每日多节课），返回 parse_training_schedule 的行。

        官方入参是 yyyyMMdd 必填格式——传 ISO 会被服务端以「异常调用」拒绝。
        """
        schema = self._schema(TOOL_TRAINING_SCHEDULE)
        props = (schema or {}).get("properties") or {}
        start_key = _pick_key(props, "startDate", "start", "fromDate", "beginDate") or "startDate"
        end_key = _pick_key(props, "endDate", "end", "toDate") or "endDate"
        fmt = "%Y%m%d"
        text = _tool_text(
            self.call_tool(TOOL_TRAINING_SCHEDULE,
                           {start_key: since.strftime(fmt), end_key: until.strftime(fmt)}),
            TOOL_TRAINING_SCHEDULE)
        rows = parse_training_schedule(text)
        if not rows:
            # 解析出 0 行多半是官方改了输出格式：把原文留进日志排查，但不算错误
            logger.warning("queryTrainingSchedule 返回未解析出课次，原文前 500 字符：%s",
                           text[:500])
        return rows

    def push_workout(self, workout_name: str, structured_steps: list[dict],
                     sport: str = "run") -> str:
        raise IntegrationError(
            "高驰官方 MCP 暂未开放训练计划写入（generateTrainingPlan / updateTrainingPlan "
            "官方标注 coming soon）。可先在「训练计划」页下载 FIT 文件，在高驰 App 内导入。")

    def status(self) -> dict:
        return {"platform": "coros", "mode": "mcp", "ok": bool(self.credentials.get("access_token")),
                "base": self.base}


# ============================================================ 参数与归一
def _coerce_like(spec: dict | None, value: str) -> Any:
    """按 schema 类型把字符串参数转成整数等。"""
    declared = str((spec or {}).get("type", "")).lower()
    if declared in ("integer", "number"):
        return _maybe_int(value) if str(value).isdigit() else value
    return value


def _build_sport_record_args(schema: dict, since: datetime, until: datetime | None = None) -> dict:
    """按工具 inputSchema 的实际参数名构造运动记录查询参数。

    until：窗口截止时间，默认现在。分段滚动拉取时需要非默认值，
    否则每段窗口相同，返回内容也相同，分段失去意义。
    """
    props = (schema or {}).get("properties") or {}
    args: dict[str, Any] = {}
    end = until or datetime.now()
    start_key = _pick_key(props, "startDate", "startTime", "startDay", "start", "fromDate", "beginDate")
    end_key = _pick_key(props, "endDate", "endTime", "endDay", "end", "toDate")
    if start_key:
        args[start_key] = _format_time_arg(props.get(start_key) or {}, since)
    if end_key:
        args[end_key] = _format_time_arg(props.get(end_key) or {}, end)
    if not start_key:
        # 无 schema 可依时的兜底
        args.setdefault("startDate", since.strftime("%Y-%m-%d"))
        args.setdefault("endDate", end.strftime("%Y-%m-%d"))
    for key in ("limit", "pageSize", "size", "count"):
        found = _pick_key(props, key)
        if found:
            args[found] = 200
            break
    return args


def _build_daily_args(schema: dict, since_days: int) -> dict:
    """构造按天查询健康/心率/睡眠的工具参数。"""
    props = (schema or {}).get("properties") or {}
    args: dict[str, Any] = {}
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start = start.fromordinal(start.toordinal() - max(0, since_days - 1))
    start_key = _pick_key(props, "startDate", "startTime", "startDay", "fromDate", "beginDate")
    end_key = _pick_key(props, "endDate", "endTime", "endDay", "toDate")
    if start_key:
        args[start_key] = _format_time_arg(props.get(start_key) or {}, start)
    if end_key:
        args[end_key] = _format_time_arg(props.get(end_key) or {}, datetime.now())
    days_key = _pick_key(props, "days", "n", "recentDays", "weekCount", "weeks")
    if not start_key and days_key:
        args[days_key] = since_days
    return args


def _normalize_activity(row: dict) -> NormalizedActivity:
    """把 parse_sport_records 解析出的运动记录归一化为平台活动。"""
    start_ts = row.get("start_ts")
    start = _to_datetime(start_ts) if start_ts else None
    if start is None:
        start = _to_datetime(row.get("record_date")) or datetime.now()

    sport_name = row.get("sport_name") or ""
    location = str(row.get("location") or "").strip()
    # Location 字段可能是地名（如「示例市」），也可能是用户自定义课名（如「8k轻松跑」）
    title = location if location and not _is_place(location) else (sport_name or "COROS 活动")

    dynamics: dict[str, Any] = {}
    for src_key, out_key in (("location", "location"), ("latitude", "latitude"),
                             ("longitude", "longitude"), ("pace_sec", "avg_pace_sec"),
                             ("end_ts", "end_ts"), ("sets", "sets")):
        if row.get(src_key) is not None:
            dynamics[out_key] = row[src_key]

    return NormalizedActivity(
        external_id=str(row.get("label_id") or ""),
        sport=sport_key(sport_name, _maybe_int(row.get("sport_type"))),
        title=str(title)[:120],
        start_time=start,
        duration_sec=_maybe_int(row.get("duration_sec")) or 0,
        distance_m=maybe_float(row.get("distance_m")) or 0.0,
        avg_hr=_maybe_int(row.get("avg_hr")),
        max_hr=_maybe_int(row.get("max_hr")),
        elevation_m=maybe_float(row.get("elevation_gain")) or 0.0,
        avg_cadence=maybe_float(row.get("cadence")),
        avg_power=maybe_float(row.get("power")),
        calories=_maybe_int(row.get("calories")),
        temp_c=None,
        weather=None,
        te_aerobic=None,
        te_anaerobic=None,
        dynamics=dynamics,
        raw={"source": "coros_mcp", "sport_name": sport_name,
             "sport_type": _maybe_int(row.get("sport_type")),
             "record_date": row.get("record_date"), "fields": row.get("raw_fields") or {}},
    )
