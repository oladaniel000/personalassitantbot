"""
services/reminder_service.py
Manages all scheduled reminders using APScheduler with SQLite persistence.
Jobs survive server restarts and offline periods.
"""

import logging
import uuid
from datetime import datetime, timedelta
from typing import Optional

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

from config import DB_URL

log = logging.getLogger(__name__)

# ── Scheduler singleton — initialised in bot.py ───────────────────────────────
scheduler: Optional[AsyncIOScheduler] = None


def init_scheduler() -> AsyncIOScheduler:
    global scheduler
    jobstores = {"default": SQLAlchemyJobStore(url=DB_URL)}
    scheduler = AsyncIOScheduler(jobstores=jobstores, timezone=pytz.utc)
    scheduler.start()
    log.info("APScheduler started with SQLite persistence.")
    return scheduler


def schedule_reminders(event, chat_id: str, app) -> None:
    """
    Calculate all reminder trigger times for `event` based on its gravity,
    create APScheduler jobs, and store job IDs in the Reminder table.
    """
    from database.db import get_db
    from database.models import Reminder

    db   = get_db()
    now  = datetime.utcnow().replace(tzinfo=pytz.utc)
    start = event.start_dt.replace(tzinfo=pytz.utc) if event.start_dt.tzinfo is None else event.start_dt.astimezone(pytz.utc)
    end   = event.end_dt.replace(tzinfo=pytz.utc)   if event.end_dt.tzinfo is None   else event.end_dt.astimezone(pytz.utc)

    scheduled_times = []  # list of (trigger_dt, rtype)

    if event.gravity == "high":
        # 3 days before at 09:00 UTC
        for days in [3, 1]:
            t = (start - timedelta(days=days)).replace(hour=9, minute=0, second=0, microsecond=0)
            scheduled_times.append((t, f"days_before_{days}"))

        # Hourly from 3h before event
        for h in range(3, 0, -1):
            t = start - timedelta(hours=h)
            scheduled_times.append((t, f"hourly_{h}h"))

        # 15-minute final alert
        scheduled_times.append((start - timedelta(minutes=15), "15min"))

    elif event.gravity == "medium":
        # 1 day before at 09:00 UTC
        t = (start - timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        scheduled_times.append((t, "days_before_1"))
        # 30-minute alert
        scheduled_times.append((start - timedelta(minutes=30), "30min"))

    # Low gravity: only shows up in the morning itinerary (no extra reminders)

    # Post-event check-in for ALL gravity levels
    checkin_delay = 5 if event.gravity == "high" else 15
    scheduled_times.append((end + timedelta(minutes=checkin_delay), "checkin"))

    for trigger_dt, rtype in scheduled_times:
        if trigger_dt <= now:
            continue  # Skip reminders that are already in the past

        job_id = f"ev{event.id}_{rtype}_{uuid.uuid4().hex[:8]}"

        try:
            scheduler.add_job(
                _fire_reminder,
                trigger="date",
                run_date=trigger_dt,
                args=[event.id, rtype, chat_id, app],
                id=job_id,
                replace_existing=True,
            )
            reminder = Reminder(
                event_id=event.id,
                trigger_dt=trigger_dt.replace(tzinfo=None),
                rtype=rtype,
                job_id=job_id,
            )
            db.add(reminder)
        except Exception as e:
            log.warning(f"Could not schedule reminder {rtype} for event {event.id}: {e}")

    db.commit()
    db.close()


def cancel_reminders(event_id: int) -> None:
    """
    Cancel all APScheduler jobs for the given event and remove Reminder rows.
    Called before rescheduling or deleting an event.
    """
    from database.db import get_db
    from database.models import Reminder

    db = get_db()
    reminders = db.query(Reminder).filter(
        Reminder.event_id == event_id,
        Reminder.sent == False,
    ).all()

    for r in reminders:
        if r.job_id and scheduler:
            try:
                scheduler.remove_job(r.job_id)
            except Exception:
                pass
        db.delete(r)

    db.commit()
    db.close()


async def _fire_reminder(event_id: int, rtype: str, chat_id: str, app) -> None:
    """
    Called by APScheduler at the scheduled time.
    Sends the correct Telegram message based on rtype.
    """
    from database.db import get_db
    from database.models import Event, Reminder

    db    = get_db()
    event = db.query(Event).filter(Event.id == event_id).first()

    if not event:
        db.close()
        return

    # Mark reminder as sent
    reminder = db.query(Reminder).filter(
        Reminder.event_id == event_id,
        Reminder.rtype == rtype,
        Reminder.sent == False,
    ).first()
    if reminder:
        reminder.sent = True
        db.commit()

    db.close()

    # Build and send the message
    msg = _build_reminder_message(event, rtype)

    if rtype == "checkin":
        await _send_checkin_prompt(event, chat_id, app)
    else:
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text=msg,
                parse_mode="Markdown",
            )
        except Exception as e:
            log.warning(f"Failed to send reminder: {e}")


def _build_reminder_message(event, rtype: str) -> str:
    gravity_emoji = {"low": "🟢", "medium": "🟡", "high": "🔴"}
    cat_emoji     = {"meeting": "🤝", "task": "✅", "habit": "🔁"}
    g  = gravity_emoji.get(event.gravity, "🟡")
    c  = cat_emoji.get(event.category, "📌")
    priority = "⚡ " if event.is_priority else ""

    if rtype.startswith("days_before"):
        days = rtype.split("_")[-1]
        return (
            f"{g} *Upcoming {event.category.title()} — {days} day(s) away*\n\n"
            f"{priority}{c} *{event.title}*\n"
            f"📅 {_fmt_dt(event.start_dt)} — {_fmt_dt(event.end_dt)}\n"
            f"⚠️ This is a *{event.gravity.upper()} priority* event."
        )
    elif rtype.startswith("hourly"):
        h = rtype.split("_")[1]
        return (
            f"⏰ *{h} until: {event.title}*\n"
            f"{priority}{c} Starts at {_fmt_time(event.start_dt)}\n"
            f"{g} Gravity: {event.gravity.title()}"
        )
    elif rtype == "15min":
        return (
            f"🚨 *{event.title} starts in 15 minutes!*\n"
            f"{priority}{c} {_fmt_time(event.start_dt)} → {_fmt_time(event.end_dt)}"
        )
    elif rtype == "30min":
        return (
            f"⏳ *Reminder: {event.title} in 30 minutes*\n"
            f"{priority}{c} {_fmt_time(event.start_dt)} → {_fmt_time(event.end_dt)}"
        )
    else:
        return f"📌 Reminder: *{event.title}*"


async def _send_checkin_prompt(event, chat_id: str, app) -> None:
    """Send the post-event check-in inline keyboard."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Done!", callback_data=f"ci_done_{event.id}"),
            InlineKeyboardButton("❌ Not completed", callback_data=f"ci_notdone_{event.id}"),
            InlineKeyboardButton("🔄 Partially", callback_data=f"ci_partial_{event.id}"),
        ]
    ])
    try:
        await app.bot.send_message(
            chat_id=chat_id,
            text=(
                f"⏱ *{event.title}* just ended.\n\n"
                f"Did you complete it?"
            ),
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
    except Exception as e:
        log.warning(f"Failed to send check-in prompt: {e}")


def _fmt_dt(dt: datetime) -> str:
    return dt.strftime("%a %d %b, %H:%M") if dt else "?"


def _fmt_time(dt: datetime) -> str:
    return dt.strftime("%H:%M") if dt else "?"
