from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

if settings.database_url.startswith("sqlite"):
    db_path = settings.database_url.split("///")[-1]
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)


def _sqlite_connect_pragma(dbapi_conn, _record):
    """每个新连接执行的 PRAGMA。

    WAL：后台线程（同步调度器、点评生成）与请求线程并发读写同一库文件，
    默认 journal 模式下写会阻塞读，长事务直接撞 database is locked；
    WAL 允许读写并行。NORMAL 同步级别与 WAL 搭配够安全且更快。
    busy_timeout：拿不到写锁时等待而不是立刻抛错。
    """
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.close()


engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
)
if settings.database_url.startswith("sqlite"):
    event.listen(engine, "connect", _sqlite_connect_pragma)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# 历史库轻量迁移：新增列的字面量 DDL 白名单。
# 键为 (表名, 列名)，值为完整静态 SQL，均为编译期常量，无动态拼接。
_MIGRATION_DDL = {
    ("activities", "calories"): "ALTER TABLE activities ADD COLUMN calories INTEGER",
    ("activities", "temp_c"): "ALTER TABLE activities ADD COLUMN temp_c FLOAT",
    ("activities", "weather"): "ALTER TABLE activities ADD COLUMN weather VARCHAR(32)",
    ("activities", "te_aerobic"): "ALTER TABLE activities ADD COLUMN te_aerobic FLOAT",
    ("activities", "te_anaerobic"): "ALTER TABLE activities ADD COLUMN te_anaerobic FLOAT",
    ("activities", "dynamics"): "ALTER TABLE activities ADD COLUMN dynamics JSON",
    ("body_metrics", "sleep_score"): "ALTER TABLE body_metrics ADD COLUMN sleep_score INTEGER",
    ("body_metrics", "spo2"): "ALTER TABLE body_metrics ADD COLUMN spo2 FLOAT",
    ("body_metrics", "resp_rate"): "ALTER TABLE body_metrics ADD COLUMN resp_rate FLOAT",
    ("body_metrics", "stress"): "ALTER TABLE body_metrics ADD COLUMN stress INTEGER",
    ("body_metrics", "body_battery"): "ALTER TABLE body_metrics ADD COLUMN body_battery INTEGER",
    ("activities", "gear_id"): "ALTER TABLE activities ADD COLUMN gear_id INTEGER",
    ("plan_workouts", "coach_comment"): "ALTER TABLE plan_workouts ADD COLUMN coach_comment TEXT",
    ("plan_workouts", "coach_comment_at"): "ALTER TABLE plan_workouts ADD COLUMN coach_comment_at DATETIME",
    # 运动画像来源溯源（services/profile.py）：记录每个可解析字段的来源/依据/锁定状态
    ("athletes", "profile_meta"): "ALTER TABLE athletes ADD COLUMN profile_meta JSON",
    # 官方恢复状态（高驰 queryRecoveryStatus → 当日 body metric 行）
    ("body_metrics", "recovery_pct"): "ALTER TABLE body_metrics ADD COLUMN recovery_pct FLOAT",
    ("body_metrics", "recovery_level"): "ALTER TABLE body_metrics ADD COLUMN recovery_level VARCHAR(32)",
    ("body_metrics", "recovery_hours"): "ALTER TABLE body_metrics ADD COLUMN recovery_hours INTEGER",
    # 计划来源标记 / 完成方式标记（09-21 后的库结构）
    ("training_plans", "source"): "ALTER TABLE training_plans ADD COLUMN source VARCHAR(16) DEFAULT 'ai'",
    ("plan_workouts", "completed_source"): "ALTER TABLE plan_workouts ADD COLUMN completed_source VARCHAR(16)",
    # 单课分析（services/workout_analysis.py：处方 vs 实际执行的结构化对照）
    ("plan_workouts", "analysis"): "ALTER TABLE plan_workouts ADD COLUMN analysis JSON",
    # AI 会话滚动摘要（services/ai_coach._compress_and_trim：旧对话裁剪前先压缩成要点）
    ("ai_conversations", "summary"): "ALTER TABLE ai_conversations ADD COLUMN summary TEXT",
}


# 老库补建索引：新库由 models 的 __table_args__ 在 create_all 时创建，
# 老库（索引声明进模型之前建的）在这里补齐。DDL 均为静态字面量。
_MIGRATION_INDEXES: dict[str, tuple[str, ...]] = {
    "activities": (
        # 建唯一索引前先清存量重复（每组保留最早一条）；external_id 为空的手动记录不参与
        "DELETE FROM activities WHERE external_id != '' AND id NOT IN ("
        "SELECT MIN(id) FROM activities WHERE external_id != '' "
        "GROUP BY platform, external_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_activities_platform_external "
        "ON activities (platform, external_id) "
        "WHERE external_id IS NOT NULL AND external_id != ''",
    ),
    "plan_workouts": (
        "CREATE INDEX IF NOT EXISTS ix_plan_workouts_athlete_date "
        "ON plan_workouts (athlete_id, date)",
    ),
}


def init_db():
    from . import models  # noqa: F401  确保模型注册

    Base.metadata.create_all(engine)
    _auto_migrate()
    _assert_schema_matches_models()


def _assert_schema_matches_models():
    """启动时 diff 模型列与实际表列，缺列立即 fail fast。

    背景：_MIGRATION_DDL 与 models.py 是双源真相，加列漏登记白名单时
    create_all 对老表无能为力，过去要等运行期 AttributeError 才暴露。
    这里用迁移完成后的全新 Inspector（避开反射缓存读到迁移前的旧 schema）。
    """
    from sqlalchemy import inspect

    from . import models

    insp = inspect(engine)
    problems = []
    for table in models.Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue  # 新表由 create_all 创建，不会缺
        actual = {c["name"] for c in insp.get_columns(table.name)}
        missing = [c.name for c in table.columns if c.name not in actual]
        if missing:
            problems.append(f"  {table.name}: 缺列 {', '.join(missing)}")
    if problems:
        raise RuntimeError(
            "数据库 schema 落后于模型（_MIGRATION_DDL 漏登记？老库请补列或从备份恢复）：\n"
            + "\n".join(problems)
        )


def _auto_migrate():
    """SQLite 轻量迁移：为已存在的表补齐模型新增的列（CREATE TABLE 不会改老表）。"""
    from sqlalchemy import inspect

    if not engine.url.get_backend_name().startswith("sqlite"):
        return
    insp = inspect(engine)
    with engine.begin() as conn:
        for (table, col), ddl in _MIGRATION_DDL.items():
            if not insp.has_table(table):
                continue
            if col not in {c["name"] for c in insp.get_columns(table)}:
                conn.exec_driver_sql(ddl)
        for table, ddls in _MIGRATION_INDEXES.items():
            if insp.has_table(table):
                for ddl in ddls:
                    conn.exec_driver_sql(ddl)
