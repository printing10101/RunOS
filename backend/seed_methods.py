"""把 backend/data/ 下的知识库种子 JSON 导入（按 code upsert，可重复执行）。

用法： cd backend && python seed_methods.py
支持文件：
  training_methods_*.json    → 训练方法/体系（含课表模板、出处、适配规则）
  training_principles.json   → 训练原理（出处写入 knowledge_sources, ref_type=principle）
  training_plans_library.json→ 参考训练计划（出处写入 knowledge_sources, ref_type=plan）
数据维护方式：新增/修改只需编辑 data/ 下 JSON，再重跑本脚本。导入幂等。
"""
from __future__ import annotations

import json
from pathlib import Path

from app import models
from app.db import SessionLocal, init_db
from sqlalchemy import select

DATA_DIR = Path(__file__).resolve().parent / "data"

METHOD_FIELDS = [
    "code", "name_zh", "name_ja", "name_en", "origin", "category", "summary",
    "principles", "intensity_distribution", "weekly_km_range", "sessions_per_week",
    "quality_days_per_week", "level", "target_races", "phases",
    "pros", "cons", "cautions", "key_figures", "tags",
    "evidence_level", "evidence_note", "asian_fit", "asian_fit_basis",
]
WORKOUT_FIELDS = [
    "code", "name_zh", "name_ja", "session_type", "purpose", "intensity_anchor",
    "structure", "distance_km_range", "duration_min_range", "weekly_km_min",
    "phases", "frequency_hint", "progression", "cautions", "source_url",
]
PRINCIPLE_FIELDS = [
    "code", "name_zh", "name_en", "category", "summary", "mechanism",
    "practical_rules", "platform_usage", "evidence_level", "evidence_note",
]
PLAN_FIELDS = [
    "code", "name_zh", "name_en", "author", "target_race", "level", "weeks",
    "weekly_km_range", "days_per_week", "phases", "week_template",
    "long_run_progression", "key_workouts", "taper_plan", "fit_condition",
    "fit_notes", "pros", "cons", "cautions", "evidence_level", "evidence_note",
]


def _upsert_method(db, data: dict) -> models.TrainingMethod:
    row = db.scalar(select(models.TrainingMethod).where(models.TrainingMethod.code == data["code"]))
    if row is None:
        row = models.TrainingMethod(**{k: data.get(k) for k in METHOD_FIELDS})
        db.add(row)
    else:
        for k in METHOD_FIELDS:
            if k in data:
                setattr(row, k, data[k])
    db.flush()
    return row


def _sync_workouts(db, method: models.TrainingMethod, items: list[dict]) -> int:
    keep = []
    for w in items or []:
        row = db.scalar(select(models.WorkoutTemplate).where(models.WorkoutTemplate.code == w["code"]))
        if row is None:
            row = models.WorkoutTemplate(method_id=method.id,
                                         **{k: w.get(k) for k in WORKOUT_FIELDS})
            db.add(row)
        else:
            row.method_id = method.id
            for k in WORKOUT_FIELDS:
                if k in w:
                    setattr(row, k, w[k])
        keep.append(w["code"])
    # 删除本方法下已不在种子数据里的模板（数据被移除时同步清理）
    for old in list(method.workouts):
        if old.code not in keep:
            db.delete(old)
    db.flush()
    return len(keep)


def _sync_sources(db, ref_type: str, ref_code: str, items: list[dict]) -> int:
    """knowledge_sources：按 (ref_type, ref_code) 先删后插，保证与 JSON 一致。"""
    old = db.scalars(select(models.KnowledgeSource).where(
        models.KnowledgeSource.ref_type == ref_type,
        models.KnowledgeSource.ref_code == ref_code)).all()
    for row in old:
        db.delete(row)
    for e in items or []:
        db.add(models.KnowledgeSource(
            ref_type=ref_type, ref_code=ref_code, title=e.get("title", ""),
            url=e.get("url", ""), source_type=e.get("source_type", "media"),
            year=e.get("year"), level=e.get("level", "C"), note=e.get("note", "")))
    return len(items or [])


def run() -> None:
    init_db()
    files = sorted(DATA_DIR.glob("training_*.json"))
    if not files:
        print(f"[seed] 未找到种子文件：{DATA_DIR}")
        return
    n_m = n_w = n_e = n_r = n_p = n_pl = n_src = 0
    with SessionLocal() as db:
        for fp in files:
            doc = json.loads(fp.read_text(encoding="utf-8"))
            if fp.name.startswith("training_methods"):
                for m in doc.get("methods", []):
                    row = _upsert_method(db, m)
                    n_w += _sync_workouts(db, row, m.get("workouts") or [])
                    for e in list(row.evidences):
                        db.delete(e)
                    for e in m.get("evidences") or []:
                        db.add(models.MethodEvidence(
                            method_id=row.id, title=e.get("title", ""), url=e.get("url", ""),
                            source_type=e.get("source_type", "media"), year=e.get("year"),
                            level=e.get("level", "C"), note=e.get("note", "")))
                        n_e += 1
                    for r in list(row.fit_rules):
                        db.delete(r)
                    for r in m.get("fit_rules") or []:
                        db.add(models.MethodFitRule(
                            method_id=row.id, code=r.get("code", ""),
                            condition=r.get("condition") or {},
                            weight=float(r.get("weight", 10)), reason=r.get("reason", "")))
                        n_r += 1
                    n_m += 1
            elif fp.name == "training_principles.json":
                for p in doc.get("principles", []):
                    row = db.scalar(select(models.TrainingPrinciple).where(
                        models.TrainingPrinciple.code == p["code"]))
                    if row is None:
                        row = models.TrainingPrinciple(**{k: p.get(k) for k in PRINCIPLE_FIELDS})
                        db.add(row)
                    else:
                        for k in PRINCIPLE_FIELDS:
                            if k in p:
                                setattr(row, k, p[k])
                    db.flush()
                    n_src += _sync_sources(db, "principle", p["code"], p.get("sources"))
                    n_p += 1
            elif fp.name == "training_plans_library.json":
                for pl in doc.get("plans", []):
                    row = db.scalar(select(models.PlanTemplate).where(
                        models.PlanTemplate.code == pl["code"]))
                    if row is None:
                        row = models.PlanTemplate(**{k: pl.get(k) for k in PLAN_FIELDS})
                        db.add(row)
                    else:
                        for k in PLAN_FIELDS:
                            if k in pl:
                                setattr(row, k, pl[k])
                    db.flush()
                    n_src += _sync_sources(db, "plan", pl["code"], pl.get("sources"))
                    n_pl += 1
        db.commit()
    print(f"[seed] 完成：方法 {n_m} / 模板 {n_w} / 方法出处 {n_e} / 适配规则 {n_r} / "
          f"原理 {n_p} / 参考计划 {n_pl} / 通用出处 {n_src}")


if __name__ == "__main__":
    run()
