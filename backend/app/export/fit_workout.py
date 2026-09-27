"""结构化训练课 → FIT 二进制文件（可导入高驰 / 佳明 App）。

实现 FIT 协议 16 的最小可用子集：只写 FileId / Workout / WorkoutStep 三类
消息，足以让佳明与高驰 App 把文件识别为一节可执行的结构化训练。步骤输入
是 planner 的结构化步骤格式：
- duration_type "time" → duration_value 分钟；"distance" → 米；其余按 open；
- target.type "hr" → from/to 是 %maxhr 小数，写入目标心率区间（uint8 百分比）；
  "pace" 暂不写入——FIT WorkoutStep 消息没有通用配速目标字段，
  配速意图保留在步骤名与备注里。
"""
from __future__ import annotations

import struct
from datetime import UTC, datetime

# FIT 基础类型编码（base type）
_ENUM, _STRING8, _UINT8, _UINT16, _UINT32 = 0x00, 0x07, 0x84, 0x86, 0x87
# WorkoutStep.intensity 枚举
_INTENSITY = {"warmup": 2, "active": 0, "rest": 1, "cooldown": 3}
# WorkoutStep.duration_type 枚举
_DUR_OPEN, _DUR_DISTANCE, _DUR_TIME = 0, 1, 2
# WorkoutStep.target_type 枚举
_TARGET_OPEN, _TARGET_HR = 0, 2
# Workout.sport 枚举
_SPORT_RUNNING = 1

_FIT_EPOCH = datetime(1989, 12, 31, tzinfo=UTC)
_NAME_BYTES = 32   # 步骤名字段的定长槽位（null 填充）

_CRC_TABLE: list[int] = []


def _crc16(data: bytes) -> int:
    if not _CRC_TABLE:
        for byte in range(256):
            crc = byte << 8
            for _ in range(8):
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
            _CRC_TABLE.append(crc)
    crc = 0
    for b in data:
        crc = ((crc << 8) & 0xFFFF) ^ _CRC_TABLE[((crc >> 8) ^ b) & 0xFF]
    return crc


def _def_msg(local_id: int, mesg_num: int,
             fields: list[tuple[int, int, int]]) -> bytes:
    """定义消息：fields 按 field_num 升序 [(num, base_type, size)]。"""
    out = bytearray([0x40 | local_id, 0, 0x08,   # 定义头 + 小端架构位
                     mesg_num & 0xFF, (mesg_num >> 8) & 0xFF, len(fields)])
    for num, base, size in fields:
        out += bytes([num, size, base])
    return bytes(out)


def _data_msg(local_id: int, payload: bytes) -> bytes:
    return bytes([local_id]) + payload


def _fit_name(name: str) -> bytes:
    """步骤名 → 定长 null 填充槽位；UTF-8 截断不劈开多字节。"""
    raw = (name or "").encode("utf-8", errors="ignore")[:_NAME_BYTES - 1]
    return raw.ljust(_NAME_BYTES, b"\x00")


def _step_row(index: int, step: dict) -> bytes:
    """一条结构化步骤 → WorkoutStep 数据消息（定义见 _STEPS_DEF）。"""
    dur_type_raw = (step.get("duration_type") or "open").lower()
    value = step.get("duration_value") or 0
    if dur_type_raw == "time":
        dur_type, dist, secs = _DUR_TIME, 0, int(round(float(value) * 60))
    elif dur_type_raw == "distance":
        dur_type, dist, secs = _DUR_DISTANCE, int(round(float(value))), 0
    else:
        dur_type, dist, secs = _DUR_OPEN, 0, 0

    target = step.get("target") or {}
    ttype = target.get("type")
    if ttype == "hr" and target.get("from") is not None and target.get("to") is not None:
        target_type = _TARGET_HR
        hr_lo = max(0, min(100, int(round(float(target["from"]) * 100))))
        hr_hi = max(hr_lo, min(100, int(round(float(target["to"]) * 100))))
    else:
        target_type, hr_lo, hr_hi = _TARGET_OPEN, 0, 100

    intensity = _INTENSITY.get((step.get("step_type") or "active").lower(), 0)
    # 字段顺序必须与 _STEPS_DEF 的声明一一对应
    payload = struct.pack("<H", index)                       # 0 message_index
    payload += _fit_name(step.get("name") or "")             # 1 name (32B)
    payload += struct.pack("<B", dur_type)                   # 2 duration_type
    payload += struct.pack("<I", dist)                       # 3 duration_distance
    payload += struct.pack("<I", secs)                       # 4 duration_time
    payload += struct.pack("<B", target_type)                # 6 target_type
    payload += struct.pack("<B", hr_lo)                      # 7 hr zone low
    payload += struct.pack("<B", hr_hi)                      # 8 hr zone high
    payload += struct.pack("<B", intensity)                  # 9 intensity
    return _data_msg(2, payload)


# 消息定义（字段号升序）；local_id: FileId=0, Workout=1, WorkoutStep=2
_FILE_ID_DEF = _def_msg(0, 0, [(3, _UINT32, 4), (4, _UINT32, 4)])
#   3 time_created (uint32) / 4 number (uint32，占位)
_WORKOUT_DEF = _def_msg(1, 26, [(3, _UINT16, 2), (4, _ENUM, 1)])
#   3 num_valid_steps (uint16) / 4 sport (enum)
_STEPS_DEF = _def_msg(2, 27, [
    (0, _UINT16, 2),   # message_index
    (1, _STRING8, _NAME_BYTES),   # workout_step_name
    (2, _ENUM, 1),     # duration_type
    (3, _UINT32, 4),   # duration_distance (m)
    (4, _UINT32, 4),   # duration_time (s)
    (6, _ENUM, 1),     # target_type
    (7, _UINT8, 1),    # target_hr_zone_low
    (8, _UINT8, 1),    # target_hr_zone_high
    (9, _ENUM, 1),     # intensity
])


def build_workout_fit(title: str, structured: list[dict], max_hr: int | None = None) -> bytes:
    """结构化步骤 → 完整 FIT 文件字节。max_hr 暂只用于校验，心率目标
    按 %maxhr 编码不依赖绝对值。"""
    steps = [s for s in (structured or []) if isinstance(s, dict)]
    if not steps:
        raise ValueError("没有可导出的结构化步骤")

    now_secs = int((datetime.now(UTC) - _FIT_EPOCH).total_seconds())
    file_id = _data_msg(0, struct.pack("<II", now_secs, 0))

    workout = _data_msg(1, struct.pack("<HB", len(steps), _SPORT_RUNNING))

    body = _FILE_ID_DEF + file_id + _WORKOUT_DEF + workout
    for i, step in enumerate(steps):
        body += _STEPS_DEF + _step_row(i, step)

    # 14 字节文件头：头长 0x0C / 协议 16 / profile 2130 / 数据长度 / ".FIT" / CRC 占位
    header = struct.pack("<BBH", 0x0C, 0x10, 2130) + struct.pack("<I", len(body)) + b".FIT"
    header += struct.pack("<H", _crc16(header))
    return header + body + struct.pack("<H", _crc16(header + body))
