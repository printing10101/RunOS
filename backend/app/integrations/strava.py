"""Strava 官方 API 适配器 —— 高驰用户获取真实逐点数据的推荐路径。

背景：高驰开放平台 API 面向企业开发者，个人申请不到凭据；而高驰 App 自带
「同步到 Strava」，Strava 官方 API（v3）对个人开发者免费开放。因此链路为：

    高驰手表/APP →（自动同步）→ Strava →（本适配器，OAuth2）→ 本平台

能拿到的真实数据：
  - 活动列表：距离/时长/均心率/最大心率/步频/爬升 + GPS 轨迹（summary_polyline，列表自带）
  - 单次详情：卡路里、设备名等
  - 每公里分段（laps 接口）
  - 逐点数据流（streams 接口）：时间/距离/海拔/心率/步频/速度/经纬度 → 配速·心率·海拔曲线

使用前提：到 https://www.strava.com/settings/api 创建应用（免费、即时通过），
把 STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET 填入 backend/.env。
注意 Strava 默认限流：100 次/15分钟、1000 次/天，同步时仅对最近 N 次跑步做详情富化。
"""
from __future__ import annotations

import time
from datetime import datetime

import httpx

from ..logging_config import get_logger, swallowed
from ..services.activity_detail import decode_polyline
from .base import IntegrationError, NormalizedActivity, PlatformAdapter, maybe_float, maybe_int

logger = get_logger(__name__)

API_BASE = "https://www.strava.com/api/v3"
AUTH_BASE = "https://www.strava.com/oauth"

# Strava sport_type / type → 通用 sport
STRAVA_SPORT_MAP = {
    "run": "run", "trail_run": "run", "virtual_run": "run", "treadmill": "run",
    "ride": "ride", "gravel_ride": "ride", "mountain_bike_ride": "ride", "virtual_ride": "ride",
    "swim": "swim",
    "weight_training": "strength", "workout": "strength",
    "walk": "walk", "hike": "walk",
}
# 富化范围：只对最近 N 次跑步拉取 laps + streams（限流保护）
ENRICH_LIMIT = 25
STREAM_KEYS = "time,distance,altitude,heartrate,cadence,velocity_smooth,latlng"
MAX_SERIES_POINTS = 300


class StravaAdapter(PlatformAdapter):
    platform = "strava"

    def check_config(self) -> None:
        from ..config import settings
        if not (settings.strava_client_id and settings.strava_client_secret):
            raise IntegrationError(
                "Strava 凭据未配置。到 https://www.strava.com/settings/api 免费创建个人应用，"
                "把 STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET 填入 backend/.env（回调地址填 "
                f"{settings.strava_redirect_uri}），再点「授权接入」。")

    # ---------------------------------------------------------------- OAuth2
    def authorize_url(self, state: str = "strava") -> str:
        from ..config import settings
        self.check_config()
        return (f"{AUTH_BASE}/authorize?client_id={settings.strava_client_id}"
                f"&redirect_uri={settings.strava_redirect_uri}"
                f"&response_type=code&approval_prompt=auto&scope=activity:read_all&state={state}")

    def exchange_token(self, code: str) -> dict:
        from ..config import settings
        self.check_config()
        with httpx.Client(timeout=20) as client:
            resp = client.post(f"{AUTH_BASE}/token", data={
                "client_id": settings.strava_client_id,
                "client_secret": settings.strava_client_secret,
                "code": code,
                "grant_type": "authorization_code",
            })
        if resp.status_code != 200:
            raise IntegrationError(f"Strava 授权失败：HTTP {resp.status_code} {resp.text[:200]}")
        token = resp.json()
        token["obtained_at"] = int(time.time())
        return token

    def _access_token(self) -> str:
        token = self.credentials or {}
        if not token.get("access_token"):
            raise IntegrationError("尚未完成 Strava 授权，请先在「平台连接」页授权绑定")
        # access_token 有效期 6 小时，过期自动刷新（refresh_token 长期有效）
        if time.time() > float(token.get("expires_at") or 0) - 120:
            from ..config import settings
            with httpx.Client(timeout=20) as client:
                resp = client.post(f"{AUTH_BASE}/token", data={
                    "client_id": settings.strava_client_id,
                    "client_secret": settings.strava_client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": token["refresh_token"],
                })
            if resp.status_code != 200:
                raise IntegrationError("Strava token 刷新失败，请重新授权绑定")
            new = resp.json()
            new["obtained_at"] = int(time.time())
            self.credentials.clear()
            self.credentials.update(new)
        return self.credentials["access_token"]

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=API_BASE, timeout=30,
                            headers={"Authorization": f"Bearer {self._access_token()}"})

    # ---------------------------------------------------------------- 数据
    def fetch_activities(self, since: datetime) -> list[NormalizedActivity]:
        out: list[NormalizedActivity] = []
        with self._client() as client:
            page = 1
            while True:
                resp = client.get("/athlete/activities", params={"per_page": 50, "page": page})
                resp.raise_for_status()
                items = resp.json()
                if not items:
                    break
                for it in items:
                    start = _parse_strava_time(it.get("start_date"))
                    if start < since:
                        return out
                    out.append(_normalize(it))
                page += 1
                if page > 10:
                    break
            self._enrich(client, [a for a in out if a.sport == "run"][:ENRICH_LIMIT])
        return out

    def _enrich(self, client: httpx.Client, runs: list[NormalizedActivity]) -> None:
        """对最近 N 次跑步补拉详情/分段/数据流；单项失败不阻断同步。"""
        for na in runs:
            aid = na.external_id
            with swallowed("拉取活动详情", logger=logger, activity=aid):
                resp = client.get(f"/activities/{aid}")
                if resp.status_code == 200:
                    det = resp.json()
                    na.raw["detail"] = det
                    na.calories = na.calories or maybe_int(det.get("calories"))
                    na.temp_c = na.temp_c or maybe_float(det.get("average_temp"))

            with swallowed("拉取分段", logger=logger, activity=aid):
                resp = client.get(f"/activities/{aid}/laps")
                if resp.status_code == 200:
                    na.raw["splits"] = resp.json()

            with swallowed("拉取数据流", logger=logger, activity=aid):
                resp = client.get(f"/activities/{aid}/streams",
                                  params={"keys": STREAM_KEYS, "key_by_type": "true"})
                if resp.status_code == 200:
                    points = _streams_to_points(resp.json())
                    if points:
                        na.raw["series"] = {"source": "strava_streams", "points": points}

    def status(self) -> dict:
        return {"platform": "strava", "mode": "official", "ok": True}


# ---------------------------------------------------------------- 归一化

def _normalize(it: dict) -> NormalizedActivity:
    sport_key = (it.get("sport_type") or it.get("type") or "").lower()
    polyline = ((it.get("map") or {}).get("summary_polyline")) or ""
    raw = dict(it)
    if polyline:
        raw["track"] = decode_polyline(polyline)
    return NormalizedActivity(
        external_id=str(it.get("id", "")),
        sport=STRAVA_SPORT_MAP.get(sport_key, "other"),
        title=it.get("name") or "Strava 活动",
        start_time=_parse_strava_time(it.get("start_date")),
        duration_sec=int(float(it.get("elapsed_time") or it.get("moving_time") or 0)),
        distance_m=float(it.get("distance") or 0),
        avg_hr=maybe_int(it.get("average_heartrate")),
        max_hr=maybe_int(it.get("max_heartrate")),
        elevation_m=float(it.get("total_elevation_gain") or 0),
        avg_cadence=maybe_float(it.get("average_cadence")),
        avg_power=maybe_float(it.get("average_watts") if it.get("device_watts") is None
                               else it.get("weighted_average_watts")),
        raw=raw,
    )


def _streams_to_points(streams: dict) -> list[dict]:
    """把 Strava streams（逐点数组）转成详情页曲线格式，降采样到 ≤300 点。"""
    def arr(key):
        return (streams.get(key) or {}).get("data") or []

    t, dist = arr("time"), arr("distance")
    if len(t) < 2 or len(dist) < 2:
        return []
    hr, alt = arr("heartrate"), arr("altitude")
    vel = arr("velocity_smooth")
    n = len(t)
    step = max(1, n // MAX_SERIES_POINTS)
    points = []
    for i in range(0, n, step):
        dd = dist[i] - dist[i - 1] if i > 0 else dist[i]
        dt = t[i] - t[i - 1] if i > 0 else t[i]
        pace = round(dt / dd * 1000) if dd > 1 and dt > 0 else None
        points.append({
            "km": round(dist[i] / 1000, 3),
            "time_sec": int(t[i]),
            "pace_sec_per_km": pace,
            "hr": int(hr[i]) if i < len(hr) and hr[i] else None,
            "altitude_m": round(alt[i], 1) if i < len(alt) and alt[i] is not None else None,
            "_vel": round(vel[i], 2) if i < len(vel) else None,
        })
    return points


def _parse_strava_time(s: str | None) -> datetime:
    """'2026-09-07T23:15:00Z' → 本地时区 naive datetime（与库内其余活动一致）。"""
    if not s:
        return datetime.now()
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt.astimezone().replace(tzinfo=None)
