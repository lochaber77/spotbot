"""Shared test fixtures: full per-test isolation.

The app is built around module-level singletons — one SQLite engine and one
AsyncIOScheduler — which is correct in production but leaks state across tests
(shared rows; a scheduler that caches its first event loop and never rebinds).

This autouse fixture gives every test its own temp database and a fresh
scheduler bound to it, rebinding the module globals so `db.session_scope`,
`scheduler.schedule_reminder`, etc. all use the per-test instances.
"""
import os
import tempfile

import pytest


@pytest.fixture(autouse=True)
def _isolated_app():
    # Imported lazily: test modules set env before importing config/db.
    import config
    import db
    import scheduler as sched_mod
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    tmp = tempfile.mkdtemp(prefix="spotbot-test-")
    path = os.path.join(tmp, "app.sqlite")
    url = f"sqlite:///{path}"

    config.DATA_DIR, config.DB_PATH, config.DB_URL = tmp, path, url

    engine = create_engine(
        url, connect_args={"check_same_thread": False, "timeout": 30}, future=True
    )
    db.engine = engine
    db.SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    db.Base.metadata.create_all(engine)

    if sched_mod.scheduler.running:
        sched_mod.scheduler.shutdown(wait=False)
    sched_mod.scheduler = AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=url, engine=engine)},
        job_defaults={"coalesce": True, "misfire_grace_time": sched_mod.MISFIRE_GRACE_SECONDS},
        timezone="UTC",
    )

    yield

    try:
        if sched_mod.scheduler.running:
            sched_mod.scheduler.shutdown(wait=False)
    except Exception:
        pass
