"""
handlers/misc.py
Utility command handlers: /help, /sync, /done, /snooze, /woke, /delete.
"""

import json
import logging
from datetime import datetime, timedelta

import pytz
from telegram import Update
from telegram.ext import ContextTypes

from database.db import get_db, get_or_create_user
from database.models import Event
from services import calendar_service, reminder_service

log = logging.getLogger(__name__)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Daily Personal Assistant — Commands*\n\n"
        "*/start* — Initial setup (run once)\n"
        "*/add* — Add a new event (meeting, task, or habit)\n"
        "*/today* — See today's full schedule\n"
        "*/tomorrow* — See tomorrow's schedule + commute estimate\n"
        "*/recap* — Request the evening summary now\n"
        "*/done <title>* — Mark an event as completed\n"
        "*/snooze <title>* — Push an event forward by 30 minutes\n"
        "*/delete <title>* — Delete an event\n"
        "*/sync* — Force sync with Google Calendar now\n"
        "*/woke* — Record your wake time for today's recap\n"
        "*/help* — Show this message\n"
        "*/cancel* — Cancel the current action\n\n"
        "_Events are colour-coded: 🟢 Low · 🟡 Medium · 🔴 High_\n"
        "_Priority events are flagged with ⚡_",
        parse_mode="Markdown",
    )


async def cmd_sync(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    db      = get_db()
    user    = get_or_create_user(db, chat_id)

    if not user.google_token:
        await update.message.reply_text("⚠️ Google Calendar not connected. Run /start to set up.")
        db.close()
        return

    await update.message.reply_text("🔄 Syncing with Google Calendar…")
    token_dict = json.loads(user.google_token)
    count      = calendar_service.sync_pending_events(db, token_dict, user.timezone)
    user.last_sync_dt = datetime.utcnow()
    db.commit()
    db.close()

    await update.message.reply_text(
        f"✅ Sync complete! {count} event(s) pushed to Google Calendar."
    )


async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args    = " ".join(ctx.args).strip() if ctx.args else ""
    chat_id = str(update.effective_chat.id)
    db      = get_db()
    user    = get_or_create_user(db, chat_id)

    if not args:
        await update.message.reply_text("Usage: `/done <event title>`", parse_mode="Markdown")
        db.close()
        return

    ev = (
        db.query(Event)
        .filter(Event.user_id == user.id, Event.title.ilike(f"%{args}%"))
        .first()
    )
    if not ev:
        await update.message.reply_text(f"❌ No event found matching '{args}'.")
        db.close()
        return

    ev.completed     = True
    ev.completion_dt = datetime.utcnow()
    db.commit()
    db.close()
    await update.message.reply_text(f"✅ *{ev.title}* marked as complete!", parse_mode="Markdown")


async def cmd_snooze(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args    = " ".join(ctx.args).strip() if ctx.args else ""
    chat_id = str(update.effective_chat.id)
    db      = get_db()
    user    = get_or_create_user(db, chat_id)
    tz      = pytz.timezone(user.timezone)

    if not args:
        await update.message.reply_text("Usage: `/snooze <event title>`", parse_mode="Markdown")
        db.close()
        return

    ev = (
        db.query(Event)
        .filter(Event.user_id == user.id, Event.title.ilike(f"%{args}%"))
        .first()
    )
    if not ev:
        await update.message.reply_text(f"❌ No event found matching '{args}'.")
        db.close()
        return

    # Conflict check with 30-min shift
    new_start = ev.start_dt + timedelta(minutes=30)
    new_end   = ev.end_dt   + timedelta(minutes=30)
    conflicts = db.query(Event).filter(
        Event.user_id  == user.id,
        Event.id       != ev.id,
        Event.start_dt <  new_end,
        Event.end_dt   >  new_start,
    ).all()

    if conflicts:
        c = conflicts[0]
        await update.message.reply_text(
            f"⚠️ Snoozed time conflicts with *{c.title}*. Snooze cancelled.",
            parse_mode="Markdown",
        )
        db.close()
        return

    reminder_service.cancel_reminders(ev.id)
    ev.start_dt  = new_start
    ev.end_dt    = new_end
    ev.is_synced = False
    db.commit()
    reminder_service.schedule_reminders(ev, chat_id, ctx.application)

    new_local = new_start.replace(tzinfo=pytz.utc).astimezone(tz)
    db.close()
    await update.message.reply_text(
        f"⏰ *{ev.title}* snoozed to *{new_local.strftime('%H:%M')}* (+30 min) ✅",
        parse_mode="Markdown",
    )


async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args    = " ".join(ctx.args).strip() if ctx.args else ""
    chat_id = str(update.effective_chat.id)
    db      = get_db()
    user    = get_or_create_user(db, chat_id)

    if not args:
        await update.message.reply_text("Usage: `/delete <event title>`", parse_mode="Markdown")
        db.close()
        return

    ev = (
        db.query(Event)
        .filter(Event.user_id == user.id, Event.title.ilike(f"%{args}%"))
        .first()
    )
    if not ev:
        await update.message.reply_text(f"❌ No event found matching '{args}'.")
        db.close()
        return

    reminder_service.cancel_reminders(ev.id)
    if ev.google_event_id and user.google_token:
        calendar_service.delete_event(json.loads(user.google_token), ev.google_event_id)
    db.delete(ev)
    db.commit()
    db.close()
    await update.message.reply_text(f"🗑 *{ev.title}* deleted from schedule and Google Calendar.", parse_mode="Markdown")


async def cmd_woke(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Record wake time for today's evening recap."""
    chat_id = str(update.effective_chat.id)
    db      = get_db()
    user    = get_or_create_user(db, chat_id)
    user.wake_time_today = datetime.utcnow()
    db.commit()
    db.close()

    tz    = pytz.timezone(user.timezone if user.timezone else "UTC")
    local = datetime.now(tz)
    await update.message.reply_text(
        f"⏰ Wake time recorded: *{local.strftime('%H:%M')}* ✅\n"
        "I'll mention this in tonight's recap.",
        parse_mode="Markdown",
    )
