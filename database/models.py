"""
database/models.py — SQLAlchemy ORM models.
Every table column, its type, and its purpose is documented inline.
"""

from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, Text, Float, Time,
    ForeignKey, Enum as SAEnum
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class UserState(Base):
    """
    One row per Telegram chat_id. Stores all personalisation and OAuth tokens.
    """
    __tablename__ = "user_state"

    id                = Column(Integer, primary_key=True)
    telegram_chat_id  = Column(String, unique=True, nullable=False)
    name              = Column(String, nullable=True)

    # Google OAuth token (JSON-serialised credential dict)
    google_token      = Column(Text, nullable=True)

    # Timezone string — must be a valid pytz tz name
    timezone          = Column(String, default="Africa/Lagos")

    # Addresses and geocoded coordinates
    home_address      = Column(String, nullable=True)
    home_lat          = Column(Float,  nullable=True)
    home_lon          = Column(Float,  nullable=True)

    work_address      = Column(String, nullable=True)
    work_lat          = Column(Float,  nullable=True)
    work_lon          = Column(Float,  nullable=True)

    # Scheduled message times (stored as "HH:MM" strings)
    morning_time      = Column(String, default="07:00")
    evening_time      = Column(String, default="21:00")

    # Optional: wake time recorded on a per-day basis (time of /woke command)
    wake_time_today   = Column(DateTime, nullable=True)

    last_sync_dt      = Column(DateTime, nullable=True)
    setup_complete    = Column(Boolean, default=False)
    created_at        = Column(DateTime, default=datetime.utcnow)

    events    = relationship("Event",    back_populates="user", cascade="all, delete-orphan")
    checkins  = relationship("Checkin",  back_populates="user", cascade="all, delete-orphan")


class Event(Base):
    """
    Central events table. Covers meetings, tasks, and habits.
    Recurring events store an RRULE string; each occurrence is NOT a separate row —
    occurrences are computed at runtime from the RRULE and scheduled as APScheduler jobs.
    """
    __tablename__ = "events"

    id                = Column(Integer, primary_key=True)
    user_id           = Column(Integer, ForeignKey("user_state.id"), nullable=False)

    title             = Column(String, nullable=False)
    category          = Column(SAEnum("meeting", "task", "habit", name="cat_enum"), nullable=False)
    gravity           = Column(SAEnum("low", "medium", "high", name="grav_enum"), default="medium")
    is_priority       = Column(Boolean, default=False)

    # Datetimes stored in UTC. Display is converted to user timezone at output time.
    start_dt          = Column(DateTime, nullable=False)
    end_dt            = Column(DateTime, nullable=False)

    # Recurrence (RRULE — RFC 5545 format, e.g. "RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR")
    recur_rule        = Column(String,  nullable=True)
    recur_days        = Column(String,  nullable=True)   # CSV: "MO,WE,FR"

    # Flexible time window for habits (stored as "HH:MM" strings)
    time_is_fixed     = Column(Boolean, default=True)
    time_range_start  = Column(String,  nullable=True)   # "06:00"
    time_range_end    = Column(String,  nullable=True)   # "08:00"

    # Google Calendar sync
    google_event_id   = Column(String,  nullable=True)
    is_synced         = Column(Boolean, default=False)

    # Completion tracking
    completed         = Column(Boolean, default=False)
    completion_dt     = Column(DateTime, nullable=True)
    completion_note   = Column(String,  nullable=True)   # "partial", "skipped", etc.

    notes             = Column(Text, nullable=True)
    created_at        = Column(DateTime, default=datetime.utcnow)

    user      = relationship("UserState", back_populates="events")
    reminders = relationship("Reminder",  back_populates="event", cascade="all, delete-orphan")
    checkins  = relationship("Checkin",   back_populates="event", cascade="all, delete-orphan")


class Reminder(Base):
    """
    Each row is one scheduled alert for one event.
    APScheduler job IDs are stored here so jobs can be cancelled on reschedule/delete.
    """
    __tablename__ = "reminders"

    id          = Column(Integer, primary_key=True)
    event_id    = Column(Integer, ForeignKey("events.id"), nullable=False)

    trigger_dt  = Column(DateTime, nullable=False)   # UTC fire time
    # Type labels: days_before_3, days_before_1, hourly_3h, hourly_2h, hourly_1h,
    #              30min, 15min, checkin, morning
    rtype       = Column(String,  nullable=False)
    sent        = Column(Boolean, default=False)
    job_id      = Column(String,  unique=True, nullable=True)   # APScheduler job ID

    event = relationship("Event", back_populates="reminders")


class Checkin(Base):
    """
    Records the outcome of every post-event check-in prompt.
    """
    __tablename__ = "checkins"

    id              = Column(Integer, primary_key=True)
    user_id         = Column(Integer, ForeignKey("user_state.id"), nullable=False)
    event_id        = Column(Integer, ForeignKey("events.id"),      nullable=False)

    asked_at        = Column(DateTime, default=datetime.utcnow)
    # completed | rescheduled | skipped | partial
    response        = Column(String, nullable=True)
    rescheduled_to  = Column(DateTime, nullable=True)

    user  = relationship("UserState", back_populates="checkins")
    event = relationship("Event",     back_populates="checkins")
