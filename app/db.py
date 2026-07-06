"""SQLite datastore (M3).

A single SQLite file under DATA_DIR (see config). The APScheduler jobstore
points at the same file, so reminders and their scheduled jobs persist together
and rehydrate on restart. On cutover it's one file to copy.

Datetime convention: fire_at_utc / created_at / sent_at are stored as naive
datetimes that are *always* UTC. We display in the member's local timezone
(see brain._utc_to_local_str). Members default to the family TZ (config.TZ);
the per-member column leaves room for the open "per-member overrides" decision
without committing to it yet.
"""
import os
from contextlib import contextmanager
from datetime import datetime, timezone

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

import config

# Reminder status values.
STATUS_SCHEDULED = "scheduled"
STATUS_SENT = "sent"
STATUS_CANCELLED = "cancelled"

# Pending-confirmation status values (confirm-first flow).
PC_PENDING = "pending"
PC_DECLINED = "declined"
PC_EXECUTED = "executed"


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
    recurrence: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default=STATUS_SCHEDULED, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: utcnow())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    member: Mapped[FamilyMember] = relationship(back_populates="reminders")


class Message(Base):
    """Optional log of inbound/outbound messages (handy for debugging)."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int | None] = mapped_column(ForeignKey("family_members.id"), nullable=True)
    direction: Mapped[str] = mapped_column(String, nullable=False)  # "in" | "out"
    text: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: utcnow())


class CalendarEvent(Base):
    """Cache of shared-calendar events the bot created (M3).

    Times are naive UTC, same convention as reminders.
    """

    __tablename__ = "calendar_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    google_event_id: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    start_utc: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_utc: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    attendees: Mapped[str | None] = mapped_column(String, nullable=True)  # comma-separated
    created_by: Mapped[int] = mapped_column(ForeignKey("family_members.id"), nullable=False)
    html_link: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: utcnow())


class EmailDraft(Base):
    """Record of a Gmail draft the bot created for a member (M4).

    The bot only ever creates drafts — it never sends (spec §9/§10).
    """

    __tablename__ = "email_drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("family_members.id"), nullable=False)
    gmail_draft_id: Mapped[str] = mapped_column(String, nullable=False)
    recipient: Mapped[str | None] = mapped_column(String, nullable=True)
    subject: Mapped[str | None] = mapped_column(String, nullable=True)
    body: Mapped[str | None] = mapped_column(String, nullable=True)
    in_reply_to: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="created", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: utcnow())


class PendingConfirmation(Base):
    """A consequential action (e.g. a shared calendar write) awaiting a yes/no.

    The brain injects the latest pending row for a member into the system prompt,
    so confirm-first works across messages without conversation memory.
    """

    __tablename__ = "pending_confirmations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("family_members.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)  # e.g. "calendar_event"
    payload: Mapped[str] = mapped_column(String, nullable=False)  # JSON
    status: Mapped[str] = mapped_column(String, default=PC_PENDING, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: utcnow())
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


# check_same_thread=False: APScheduler and the request loop touch the file from
# different threads; the SQLAlchemy jobstore reuses this same engine.
engine = create_engine(
    config.DB_URL,
    connect_args={"check_same_thread": False, "timeout": 30},
    future=True,
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def utcnow() -> datetime:
    """Naive UTC 'now', matching how datetimes are stored."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def init_db() -> None:
    """Create the data directory and tables if they don't exist yet."""
    if config.DATA_DIR:
        os.makedirs(config.DATA_DIR, exist_ok=True)
    Base.metadata.create_all(engine)


@contextmanager
def session_scope():
    """A transactional session usable from a request or the scheduler."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_or_create_member(session: Session, number: str) -> FamilyMember:
    """Look up a member by WhatsApp number, creating a minimal row if new.

    Callers must already have passed the allow-list gate. New members default to
    the family timezone (config.TZ).
    """
    member = session.scalar(
        select(FamilyMember).where(FamilyMember.whatsapp_number == number)
    )
    if member is None:
        member = FamilyMember(
            name=number,  # placeholder until we learn their name
            whatsapp_number=number,
            timezone=config.TZ,
            active=True,
        )
        session.add(member)
        session.flush()  # populate member.id
    return member
