from contextlib import contextmanager

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings

engine_options = {"pool_pre_ping": True}
if settings.database_url.startswith("sqlite"):
    engine_options["connect_args"] = {"check_same_thread": False}

engine = create_engine(settings.database_url, **engine_options)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)
Base = declarative_base()


@contextmanager
def session_scope():
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)

    if settings.database_url.startswith("postgresql"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE campaign_steps "
                    "ADD COLUMN IF NOT EXISTS delay_seconds "
                    "INTEGER NOT NULL DEFAULT 4"
                )
            )
            connection.execute(text("ALTER TABLE campaign_steps ADD COLUMN IF NOT EXISTS media_type VARCHAR(24)"))
            connection.execute(text("ALTER TABLE campaign_steps ADD COLUMN IF NOT EXISTS media_mime VARCHAR(120)"))
            connection.execute(text("ALTER TABLE campaign_steps ADD COLUMN IF NOT EXISTS media_filename VARCHAR(220)"))
            connection.execute(text("ALTER TABLE campaign_steps ADD COLUMN IF NOT EXISTS media_data_base64 TEXT"))
            connection.execute(text("ALTER TABLE messages ADD COLUMN IF NOT EXISTS campaign_step_id VARCHAR(48)"))
    else:
        step_columns = {column["name"] for column in inspect(engine).get_columns("campaign_steps")}
        message_columns = {column["name"] for column in inspect(engine).get_columns("messages")}
        migrations = []
        if "delay_seconds" not in step_columns:
            migrations.append("ALTER TABLE campaign_steps ADD COLUMN delay_seconds INTEGER NOT NULL DEFAULT 4")
        if "media_type" not in step_columns:
            migrations.append("ALTER TABLE campaign_steps ADD COLUMN media_type VARCHAR(24)")
        if "media_mime" not in step_columns:
            migrations.append("ALTER TABLE campaign_steps ADD COLUMN media_mime VARCHAR(120)")
        if "media_filename" not in step_columns:
            migrations.append("ALTER TABLE campaign_steps ADD COLUMN media_filename VARCHAR(220)")
        if "media_data_base64" not in step_columns:
            migrations.append("ALTER TABLE campaign_steps ADD COLUMN media_data_base64 TEXT")
        if "campaign_step_id" not in message_columns:
            migrations.append("ALTER TABLE messages ADD COLUMN campaign_step_id VARCHAR(48)")
        if migrations:
            with engine.begin() as connection:
                for statement in migrations:
                    connection.execute(text(statement))
