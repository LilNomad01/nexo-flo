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
    else:
        columns = {column["name"] for column in inspect(engine).get_columns("campaign_steps")}
        if "delay_seconds" not in columns:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE campaign_steps "
                        "ADD COLUMN delay_seconds INTEGER NOT NULL DEFAULT 4"
                    )
                )
