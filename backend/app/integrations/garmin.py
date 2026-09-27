"""佳明（Garmin）适配器，两种方式：

1) 个人方式（推荐自用）：非官方库 garminconnect，用 Garmin Connect 账号登录，
   可拉取活动并可把结构化训练下发到训练库，再由 Garmin Connect App 同步到手表。
   pip install garminconnect
2) 官方 Health API：需加入 Garmin Health/Connect 开发者计划（面向企业/研究）。

凭据放 backend/.env：GARMIN_EMAIL / GARMIN_PASSWORD（方式1）。
登录 token 缓存在 PlatformConnection.credentials（garth tokens），避免重复登录。
"""
from __future__ import annotations

from datetime import datetime

from ..logging_config import get_logger, swallowed
from .base import (
    IntegrationError,
    NormalizedActivity,
    PlatformAdapter,
    maybe_float,
    maybe_int,
)

logger = get_logger(__name__)

# Garmin Connect 训练库 DTO 常量（对照 Connect 网页端请求，若有变动仅需改这里）
CONDITION_TIME = {"conditionTypeId": 26, "conditionTypeKey": "time.duration"}
CONDITION_DISTANCE = {"conditionTypeId": 28, "conditionTypeKey": "distance"}
CONDITION_LAP = {"conditionTypeId": 24, "conditionTypeKey": "lap.button"}
STEP_TYPES = {"warmup": 1, "cooldown": 2, "interval": 3, "recovery": 4, "rest": 5, "run": 6}
TARGET_TYPES = {"none": 1, "pace": 4, "hr": 2, "power": 3}


class GarminAdapter(PlatformAdapter):
    platform = "garmin"

    def check_config(self) -> None:
        try:
            import garminconnect  # noqa: F401
        except ImportError:
            # from None：这里要报的是「库没装」，ImportError 本身的文本是噪声
            raise IntegrationError("未安装 garminconnect 库：请执行 pip install garminconnect") from None
        from ..config import settings
        if not (settings.garmin_email and settings.garmin_password) and not self.credentials.get("oauth1"):
            raise IntegrationError(
                "Garmin 凭据未配置。请在 backend/.env 填入 GARMIN_EMAIL / GARMIN_PASSWORD"
                "（Garmin Connect 个人账号，非官方接口，个人自用）")

    def _client(self):
        import garminconnect

        from ..config import settings

        client = garminconnect.Garmin()
        saved1 = self.credentials.get("oauth1")
        saved2 = self.credentials.get("oauth2")
        if saved1 and saved2:
            # 缓存 token 优先：避免每次同步都全量密码登录（易触发风控/MFA）。
            # garth/garminconnect 各版本恢复 API 有差异，逐个已知形态尝试，
            # 恢复后用轻量请求验证会话真实可用，失败一律回退密码登录。
            if self._resume_session(client, saved1, saved2):
                return client
            logger.info("Garmin 缓存 token 不可用，改用密码登录")
        email, password = settings.garmin_email, settings.garmin_password
        if not (email and password):
            raise IntegrationError(
                "Garmin 缓存登录已失效，且 .env 未配置 GARMIN_EMAIL/GARMIN_PASSWORD，"
                "无法重新登录。请补齐账号密码后重试。")
        try:
            client.login(email, password)
        except Exception as exc:
            raise IntegrationError(
                f"Garmin 登录失败（请检查账号密码，或是否触发风控/两步验证）：{exc}") from exc
        try:
            tokens = client.garth.to_dict()
            self.credentials.update({"oauth1": tokens.get("oauth1"), "oauth2": tokens.get("oauth2")})
        except Exception as exc:
            logger.warning("保存 Garmin token 缓存失败（下次同步需重新登录）：%s", exc)
        return client

    @staticmethod
    def _resume_session(client, oauth1: dict, oauth2: dict) -> bool:
        """用缓存的 garth token 恢复会话；成功（会话验证通过）返回 True。"""
        garth_obj = getattr(client, "garth", None)
        if garth_obj is None:
            return False
        data = {"oauth1": oauth1, "oauth2": oauth2}
        attempts = []
        loads = getattr(garth_obj, "loads", None)
        if callable(loads):
            attempts.append(loads)
        resume = getattr(garth_obj, "resume", None)
        if callable(resume):
            attempts.append(resume)
        for fn in attempts:
            try:
                fn(data)
                client.get_username()   # 轻量请求：验证 token 未被服务端吊销
                return True
            except Exception as exc:
                logger.debug("Garmin 会话恢复方式 %s 失败：%s", getattr(fn, "__name__", fn), exc)
        return False

    # ---------------------------------------------------------------- 数据
    DETAIL_FETCH_LIMIT = 60  # 同步时最近 N 条新增活动做详情富化（分段/动态/心率区间）

    def fetch_activities(self, since: datetime, until: datetime | None = None) -> list[NormalizedActivity]:
        try:
            client = self._client()
            out: list[NormalizedActivity] = []
            start = 0
            limit = 50
            while True:
                batch = client.get_activities(start, limit) or []
                if not batch:
                    break
                for it in batch:
                    begin = it.get("startTimeLocal") or it.get("startTimeGMT")
                    start_dt = datetime.fromisoformat(str(begin).replace("Z", "+00:00").split(".")[0])
                    # 统一存本地墙钟的 naive 时间，与 coros(datetime.fromtimestamp) 对齐：
                    # startTimeLocal 通常是本地 naive；startTimeGMT 是 UTC → 先转成本地时区再剥 tzinfo，
                    # 避免 aware/naive 混存以及"把 UTC 当本地"导致的跨日统计错位。
                    if start_dt.tzinfo is not None:
                        start_dt = start_dt.astimezone().replace(tzinfo=None)
                    if start_dt < since:
                        return out
                    if until is not None and start_dt > until:
                        continue  # 窗口外（比 until 更新）的段不入结果，继续向前翻页
                    na = NormalizedActivity(
                        external_id=str(it.get("activityId", "")),
                        sport=_map_sport(it.get("activityType", {}).get("typeKey", "other")),
                        title=it.get("activityName", "Garmin 活动"),
                        start_time=start_dt,
                        duration_sec=int(float(it.get("duration", 0))),
                        distance_m=float(it.get("distance", 0) or 0),
                        avg_hr=maybe_int(it.get("averageHR")),
                        max_hr=maybe_int(it.get("maxHR")),
                        elevation_m=float(it.get("elevationGain", 0) or 0),
                        avg_cadence=maybe_float(it.get("averageRunningCadenceInStepsPerMinute")),
                        avg_power=maybe_float(it.get("averagePower")),
                        calories=maybe_int(it.get("calories")),
                        te_aerobic=maybe_float(it.get("aerobicTrainingEffect")),
                        te_anaerobic=maybe_float(it.get("anaerobicTrainingEffect")),
                        raw=it,
                    )
                    out.append(na)
                start += limit
                if len(out) > 2000:
                    break
        except IntegrationError:
            raise
        except Exception as exc:
            # garminconnect 的网络/接口异常不归一为 IntegrationError 时，
            # 路由层只捕 IntegrationError，用户会看到裸 500 而非可读文案。
            raise IntegrationError(f"Garmin 拉取活动失败：{exc}") from exc
        # 只富化即将入库的新活动，避免每次同步对老活动重复拉 3 个接口
        # （Garmin 对高频访问有封禁风险），detail_limit 上限由同步入口控制。
        todo = [a for a in out[:self.DETAIL_FETCH_LIMIT] if self._should_enrich(a)]
        _enrich_details(client, todo)
        return out

    def fetch_body_metrics(self, days: int = 30) -> list[dict]:
        """拉取每日身体数据（HRV/睡眠/静息心率/血氧/压力/身体电量/体重）。

        garminconnect 各接口随账号权限可能有差异，单项失败即跳过该字段。
        返回 [{date, hrv_rmssd, sleep_hours, sleep_score, resting_hr, spo2,
               resp_rate, stress, body_battery, weight_kg}]，字段可缺省。
        """
        from datetime import date as _date
        from datetime import timedelta as _td

        client = self._client()
        out = []
        for i in range(days):
            d = _date.today() - _td(days=i)
            ds = d.isoformat()
            row: dict = {"date": ds}
            with swallowed("拉取夜间 HRV", logger=logger, day=ds):
                hrv = client.get_hrv_data(ds)
                if hrv and hrv.get("hrvSummary"):
                    row["hrv_rmssd"] = maybe_float(hrv["hrvSummary"].get("lastNightAvg"))

            with swallowed("拉取睡眠", logger=logger, day=ds):
                sleep = client.get_sleep_data(ds)
                s = (sleep or {}).get("dailySleepDTO") or {}
                if s.get("sleepTimeSeconds"):
                    row["sleep_hours"] = round(s["sleepTimeSeconds"] / 3600, 2)
                score = ((s.get("sleepScores") or {}).get("overall") or {}).get("value")
                row["sleep_score"] = maybe_int(score)

            with swallowed("拉取当日统计", logger=logger, day=ds):
                st = client.get_stats(ds) or {}
                if st.get("restingHeartRate"):
                    row["resting_hr"] = maybe_int(st["restingHeartRate"])
                if st.get("averageSpO2") is not None:
                    row["spo2"] = maybe_float(st["averageSpO2"])
                if st.get("averageRespirationValue") is not None:
                    row["resp_rate"] = maybe_float(st["averageRespirationValue"])
                if st.get("averageStressLevel") is not None:
                    row["stress"] = maybe_int(st["averageStressLevel"])
                if st.get("bodyBatteryMostRecentValue") is not None:
                    row["body_battery"] = maybe_int(st["bodyBatteryMostRecentValue"])

            with swallowed("拉取体重", logger=logger, day=ds):
                # `or {}` 不能省：该接口无数据时返回 None，直接 w.get(...) 会抛 AttributeError
                w = client.get_weigh_ins(ds, ds) or {}
                ws = w.get("dateWeightList") or w.get("weightList") or []
                # 先取再判空：体脂秤记录缺 weight 字段时，None / 1000 会抛 TypeError
                grams = ws[-1].get("weight") if ws else None
                if grams is not None:
                    row["weight_kg"] = maybe_float(grams / 1000)  # g → kg
            if len(row) > 1:
                out.append(row)
        return out

    # ---------------------------------------------------------------- 下发
    # Garmin Connect 训练库 sportType（id 以 Connect API 常用编号为准，
    # key 是主要判据；若平台侧调整，仅需改这里）
    GARMIN_SPORTS = {
        "run": {"sportTypeId": 1, "sportTypeKey": "running"},
        "ride": {"sportTypeId": 2, "sportTypeKey": "cycling"},
        "walk": {"sportTypeId": 3, "sportTypeKey": "hiking"},
        "swim": {"sportTypeId": 4, "sportTypeKey": "swimming"},
        "strength": {"sportTypeId": 5, "sportTypeKey": "strength_training"},
    }

    def push_workout(self, workout_name: str, structured_steps: list[dict],
                     sport: str = "run", max_hr: int | None = None) -> str:
        """把结构化训练下发到 Garmin Connect 训练库（watch 同步由 Connect 完成）。"""
        client = self._client()
        payload = {
            "sportType": self.GARMIN_SPORTS.get(sport, self.GARMIN_SPORTS["run"]),
            "workoutName": workout_name,
            "workoutSections": [{"sectionType": "ALL",
                                 "workoutSteps": self._to_garmin_steps(structured_steps, max_hr)}],
        }
        resp = client.upload_workout(payload)
        wid = str(resp.get("workoutId") or resp.get("id") or "")
        if not wid:
            # 上传接口对"能收但没建成"的情况也可能返回 200，必须检查结果
            raise IntegrationError(f"Garmin 训练下发未返回 workoutId：{str(resp)[:200]}")
        return wid

    def _to_garmin_steps(self, steps: list[dict], max_hr: int | None = None) -> list[dict]:
        max_hr = int(max_hr or 190)
        out = []
        for i, s in enumerate(steps, 1):
            target = s.get("target") or {}
            step_type_key = {"warmup": "warmup", "cooldown": "cooldown", "rest": "rest",
                             "strength": "rest"}.get(s.get("step_type"), "interval")
            if s.get("duration_type") == "time":
                # 通用格式 time 单位为分钟 → Connect 需要秒
                cond, val = CONDITION_TIME, int(round(float(s.get("duration_value") or 0) * 60))
            elif s.get("duration_type") == "distance":
                cond, val = CONDITION_DISTANCE, int(s.get("duration_value") or 0)
            else:
                cond, val = CONDITION_LAP, 1

            step = {
                "type": "ExecutableStepDTO",
                "stepId": i,
                "stepOrder": i,
                "stepType": {"stepTypeId": STEP_TYPES.get(step_type_key, 3),
                             "stepTypeKey": step_type_key,
                             "displayable": s.get("name", step_type_key)},
                "endCondition": {**cond, "displayOrder": 1, "waitingForTime": cond is CONDITION_TIME},
                "endConditionValue": val,
                "targetType": {"workoutTargetTypeId": TARGET_TYPES.get(target.get("type", "none"), 1),
                               "workoutTargetTypeKey": {"none": "no.target", "pace": "speed.range",
                                                        "hr": "heart.rate.range",
                                                        "power": "power.range"}.get(target.get("type", "none"),
                                                                                    "no.target")},
            }
            if target.get("type") == "pace" and target.get("from"):
                # mm/s：sec/km → m/s ×1000
                step["preferredTargetConditionKey"] = "speed.range"
                step["targetSpeedLow"] = round(1000 / target["to"]) if target.get("to") else None
                step["targetSpeedHigh"] = round(1000 / target["from"]) if target.get("from") else None
            elif target.get("type") == "hr" and target.get("from") is not None and target["from"] != "":
                step["preferredTargetConditionKey"] = "heart.rate.range"
                if float(target["from"]) < 3:
                    # 小数是最大心率比例 → 与 FIT 导出同一口径换算成绝对 bpm
                    low = round(float(target["from"]) * max_hr)
                    high = round(float(target.get("to") or 1.0) * max_hr)
                    if low < 60:   # 比例 0 表示不设下限，钳到合法下界而非 0 bpm
                        low = 60
                    step["targetHeartRateLow"] = low
                    step["targetHeartRateHigh"] = high
                else:
                    step["targetHeartRateLow"] = target["from"]
                    step["targetHeartRateHigh"] = target["to"]
            out.append(step)
        return out

    def status(self) -> dict:
        return {"platform": "garmin", "mode": "official", "ok": True}


def _enrich_details(client, acts: list[NormalizedActivity]) -> None:
    """为最近若干条活动补拉详情：分段 laps、跑步动态、心率区间时间分布。

    每项独立 best-effort：任一接口失败只跳过该项，不影响整体同步。
    """
    for na in acts:
        aid = na.external_id
        if not aid:
            continue
        with swallowed("拉取活动详情", logger=logger, activity=aid):
            det = client.get_activity(aid)
            if det:
                na.raw["detail"] = det
                na.dynamics = {
                    "stride_m": maybe_float(det.get("avgStrideLength")),
                    "vosc_cm": maybe_float(det.get("avgVerticalOscillation")),
                    "gct_ms": maybe_float(det.get("avgGroundContactTime")),
                    "gct_balance_pct": maybe_float(det.get("avgGctBalance")),
                    "vertical_ratio": maybe_float(det.get("avgVerticalRatio")),
                    "avg_temperature_c": maybe_float(det.get("averageTemperature")),
                    "vo2max_native": maybe_float(det.get("vO2MaxValue")),
                }
                na.dynamics = {k: v for k, v in na.dynamics.items() if v is not None}
                if det.get("averagePower") and not na.avg_power:
                    na.avg_power = maybe_float(det.get("averagePower"))
                if det.get("calories") and not na.calories:
                    na.calories = maybe_int(det.get("calories"))

        with swallowed("拉取每公里分段", logger=logger, activity=aid):
            splits = client.get_activity_splits(aid)
            if splits and splits.get("lapDTOs"):
                na.raw["splits"] = splits["lapDTOs"]

        with swallowed("拉取心率区间", logger=logger, activity=aid):
            zones = client.get_activity_hr_in_zones(aid)
            if zones:
                na.raw["hr_zones"] = zones


def _map_sport(key: str) -> str:
    m = {"running": "run", "trail_running": "run", "treadmill_running": "run",
         "cycling": "ride", "road_biking": "ride", "swimming": "swim", "lap_swimming": "swim",
         "strength_training": "strength", "walking": "walk"}
    return m.get(key, "other")
