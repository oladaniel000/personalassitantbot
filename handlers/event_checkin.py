"""
handlers/event_checkin.py
Handles the inline keyboard callbacks from post-event check-in prompts.
Patterns: ci_done_<id>, ci_notdone_<id>, ci_partial_<id>
Sub-flows: reschedule, skip, remove.
"""

import json
import logging
import re
from datetime import datetime, timedelta

import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, ConversationHandler, CallbackQueryHandler,
    MessageHandler, filters,
)

from database.db import get_db, get_or_create_user
from database.models import Event, Checkin
from services import reminder_service, calendar_service

log = logging.getLogger(__name__)

# States for reschedule sub-flow
RESCHEDULE_AWAIT_DT = 100


async def cb_checkin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Router for all ci_* callbacks."""
    q = update.callback_query
    await q.answer()
    parts    = q.data.split("_")   # e.g. ['ci', 'done', '42']
    action   = parts[1]
    event_id = int(parts[2])

    db    = get_db()
    event = db.query(Event).filter(Event.id == event_id).first()
    chat_id = str(update.effective_chat.id)
    user  = get_or_create_user(db, chat_id)

    if not event:
        await q.edit_message_text("⚠️ Event not found — it may have been deleted.")
        db.close()
        return

    if action == "done":
        event.completed     = True
        event.completion_dt = datetime.utcnow()
        _record_checkin(db, user.id, event_id, "completed")
        db.commit()
        db.close()
        await q.edit_message_text(
            f"✅ *{event.title}* marked as complete! Great work! 🎉",
            parse_mode="Markdown",
        )

    elif action == "partial":
        db.close()
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("📅 Reschedule remainder", callback_data=f"ci_reschedule_{event_id}"),
            InlineKeyboardButton("✅ Mark as done",         callback_data=f"ci_done_{event_id}"),
        ]])
        await q.edit_message_text(
            f"🔄 *{event.title}* partially done.\n\nReschedule the remainder?",
            reply_markup=keyboard, parse_mode="Markdown",
        )

    elif action == "notdone":
        db.close()
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("📅 Reschedule", callback_data=f"ci_reschedule_{event_id}"),
            InlineKeyboardButton("⏭ Skip",        callback_data=f"ci_skip_{event_id}"),
            InlineKeyboardButton("🗑 Remove",      callback_data=f"ci_remove_{event_id}"),
        ]])
        await q.edit_message_text(
            f"❌ *{event.title}* not completed.\n\nWhat would you like to do?",
            reply_markup=keyboard, parse_mode="Markdown",
        )

    elif action == "reschedule":
        ctx.user_data["reschedule_event_id"] = event_id
        db.close()
        await q.edit_message_text(
            "📅 When would you like to reschedule it?\n"
            "Format: `DD/MM/YYYY HH:MM` — or just `HH:MM` for today.",
            parse_mode="Markdown",
        )
        return RESCHEDULE_AWAIT_DT

    elif action == "skip":
        _record_checkin(db, user.id, event_id, "skipped")
        db.commit()
        db.close()
        await q.edit_message_text(f"⏭ Skipped *{event.title}*. No changes made.", parse_mode="Markdown")

    elif action == "remove":
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Yes, remove it", callback_data=f"ci_confirm_remove_{event_id}"),
            InlineKeyboardButton("Cancel",         callback_data=f"ci_skip_{event_id}"),
        ]])
        db.close()
        await q.edit_message_text(
            f"🗑 Remove *{event.title}* from your schedule and Google Calendar?",
            reply_markup=keyboard, parse_mode="Markdown",
        )

    elif action == "confirm" and parts[2] == "remove":
        event_id = int(parts[3])
        ev = db.query(Event).filter(Event.id == event_id).first()
        if ev:
            reminder_service.cancel_reminders(ev.id)
            if ev.google_event_id and user.google_token:
                calendar_service.delete_event(json.loads(user.google_token), ev.google_event_id)
            db.delete(ev)
            db.commit()
        db.close()
        await q.edit_message_text("🗑 Event removed from your schedule and Google Calendar.")


async def got_reschedule_dt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle reschedule datetime input."""
    event_id = ctx.user_data.get("reschedule_event_id")
    if not event_id:
        return ConversationHandler.END

    text    = update.message.text.strip()
    chat_id = str(update.effective_chat.id)
    db      = get_db()
    user    = get_or_create_user(db, chat_id)
    tz      = pytz.timezone(user.timezone)
    now_local = datetime.now(tz)

    # Parse datetime
    new_dt = None
    for fmt in ["%d/%m/%Y %H:%M", "%H:%M"]:
        try:
            parsed = datetime.strptime(text, fmt)
            if fmt == "%H:%M":
                parsed = datetime(now_local.year, now_local.month, now_local.day, parsed.hour, parsed.minute)
            new_dt = tz.localize(parsed).astimezone(pytz.utc).replace(tzinfo=None)
            break
        except ValueError:
            continue

    if not new_dt:
        await update.message.reply_text("❌ Format: `DD/MM/YYYY HH:MM` or `HH:MM` for today.", parse_mode="Markdown")
        db.close()
        return RESCHEDULE_AWAIT_DT

    ev = db.query(Event).filter(Event.id == event_id).first()
    if not ev:
        await update.message.reply_text("⚠️ Event not found.")
        db.close()
        return ConversationHandler.END

    # Conflict check
    duration  = (ev.end_dt - ev.start_dt).total_seconds() / 60
    new_end   = new_dt + timedelta(minutes=duration)
    conflicts = db.query(Event).filter(
        Event.user_id  == user.id,
        Event.id       != ev.id,
        Event.start_dt <  new_end,
        Event.end_dt   >  new_dt,
    ).all()

    if conflicts:
        c = conflicts[0]
        await update.message.reply_text(
            f"⚠️ That time conflicts with *{c.title}* ({c.start_dt.strftime('%H:%M')}). "
            "Please choose another time.",
            parse_mode="Markdown",
        )
        db.close()
        return RESCHEDULE_AWAIT_DT

    # Update event
    reminder_service.cancel_reminders(ev.id)
    ev.start_dt  = new_dt
    ev.end_dt    = new_end
    ev.is_synced = False
    _record_checkin(db, user.id, event_id, "rescheduled", rescheduled_to=new_dt)
    db.commit()

    # Reschedule reminders
    reminder_service.schedule_reminders(ev, chat_id, ctx.application)

    # Sync to GCal
    if user.google_token:
        gid = calendar_service.push_event(json.loads(user.google_token), ev, user.timezone)
        if gid:
            ev.google_event_id = gid
            ev.is_synced = True
            db.commit()

    new_local = new_dt.replace(tzinfo=pytz.utc).astimezone(tz)
    db.close()

    await update.message.reply_text(
        f"📅 *{ev.title}* rescheduled to *{new_local.strftime('%A, %d %B at %H:%M')}* ✅",
        parse_mode="Markdown",
    )
    ctx.user_data.pop("reschedule_event_id", None)
    return ConversationHandler.END


def _record_checkin(db, user_id, event_id, response, rescheduled_to=None):
    c = Checkin(
        user_id=user_id,
        event_id=event_id,
        response=response,
        rescheduled_to=rescheduled_to,
    )
    db.add(c)


def get_checkin_handler():
    """
    Returns a ConversationHandler that manages the full check-in + reschedule flow.
    """
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cb_checkin, pattern=r"^ci_(done|notdone|partial|skip|remove|reschedule|confirm)_")
        ],
        states={
            RESCHEDULE_AWAIT_DT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_reschedule_dt)
            ],
        },
        fallbacks=[],
        name="checkin",
        persistent=False,
    )
