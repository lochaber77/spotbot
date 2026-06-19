"""SQLite datastore (SQLAlchemy).

The DB file lives on the mounted volume (/data/app.sqlite) and is shared with
APScheduler's jobstore, so reminders and their scheduled jobs persist together
across restarts.

Datetime convention: `fire_at_utc` / `created_at` / `sent_at` are stored as
naive datetimes that are *always* in UTC. Convert to a member's local timezone
only for display.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    create_engine,
    select,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from .config import clean_number, settings


# --- Status constants for reminders ---
STATUS_SCHEDULED = "scheduled"
STATUS_SENT = "sent"
STATUS_CANCELLED = "cancelled"


class Base(DeclarativeBase):
    pass


class FamilyMember(Base):
    __tablename__ = "family_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    whatsapp_number: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    timezone: Mapped[str] = mapped_column(String, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    reminders: Mapped[list["Reminder"]] = relationship(back_populates="member")


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("family_members.id"), nullable=False)
    text: Mapped[str] = mapped_column(String, nullable=False)
    fire_at_utc: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    recurrence: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default=STATUS_SCHEDULED, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    member: Mapped[FamilyMember] = relationship(back_populates="reminders")


class MessageLog(Base):
    """Optional convenience log of inbound/outbound messages."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[Optional[int]] = mapped_column(ForeignKey("family_members.id"), nullable=True)
    direction: Mapped[str] = mapped_column(String, nullable=False)  # "in" | "out"
    text: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))


# A SQLite file needs check_same_thread=False because APScheduler and the
# request loop touch it from different threads.
engine = create_engine(
    settings.db_url,
    connect_args={"check_same_thread": False, "timeout": 30},
    future=True,
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db() -> None:
    """Create the data directory and tables if they do not yet exist."""
    db_dir = os.path.dirname(settings.db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    Base.metadata.create_all(engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    """A transactional session usable from any context (request or scheduler)."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_or_create_member(session: Session, number: str, *, default_tz: str) -> FamilyMember:
    """Look up a member by WhatsApp number, creating a minimal row if needed.

    Callers must already have verified the number is allow-listed.
    """
    digits = clean_number(number)
    member = session.scalar(
        select(FamilyMember).where(FamilyMember.whatsapp_number == digits)
    )
    if member is None:
        member = FamilyMember(
            name=digits,  # placeholder; can be edited later
            whatsapp_number=digits,
            timezone=default_tz,
            active=True,
        )
        session.add(member)
        session.flush()  # populate member.id
    return member


def utcnow() -> datetime:
    """Naive UTC 'now', matching how we store datetimes."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
