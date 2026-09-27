"""测试夹具工厂。

``athletes`` 表已去掉「写死的列默认值」（见 ``app/models.py``）：档案字段不再
有静默兜底，任何创建路径都必须显式给值。这里统一补全一具中性的测试身体参数，
避免每处重复一长串字段，也让「这个测试对象的身体参数是什么」一目了然。

需要特定体型的测试直接在调用处覆盖即可：``make_athlete(weight_kg=70)``。
"""
from __future__ import annotations

from app import models

ATHLETE_BASE = {
    "name": "测试跑者", "sex": "male", "birth_year": 1996, "height_cm": 175.0,
    "weight_kg": 65.0, "resting_hr": 55, "max_hr": 190, "hrv_baseline": 55,
    "training_age_years": 2.0,
}


def make_athlete(**overrides) -> models.Athlete:
    """构造字段完整的 Athlete（可解析字段给出测试用的中性初值）。"""
    return models.Athlete(**{**ATHLETE_BASE, **overrides})
