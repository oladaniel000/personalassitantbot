"""
handlers/event_add.py
Multi-step ConversationHandler for adding new events.
Covers: category → title → date → start time → duration → gravity →
        priority → category-specific config (habit days/time, task recurrence) →
        conflict check → confirmation → save + schedule reminders.
"""

import json
import logging
import re
from datetime import datetime, timedelta, date

import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    MessageHandler, CallbackQueryHandler, filters,
)

from database.db import get_db, get_or_create_user
from database.models import Event
from services import reminder_service, calendar_service

log = logging.getLogger(__name__)

# States
(
    S_CATEGORY, S_TITLE, S_DATE, S_START, S_DURATION,
    S_GRAVITY, S_PRIORITY,
    S_HABIT_DAYS, S_HABIT_TIME_TYPE, S_HABIT_TIME_FIXED,
    S_HABIT_TIME_RANGE_START, S_HABIT_TIME_RANGE_END,
    S_TASK_RECUR,
    S_CONFLICT, S_CONFIRM,
) = range(15)

GRAVITY_LABELS = {"🟢 Low": "low", "🟡 Medium": "medium", "🔴 High": "high"}
DURATION_MAP   = {"15 min": 15, "30 min": 30, "1 hour": 60, "2 hours": 120, "3 hours": 180}
DAY_MAP        = {"Mon": "MO", "Tue": "TU", "Wed": "WE", "Thu": "TH", "Fri": "FR", "Sat": "SA", "Sun": "SU"}


# ── Entry point ──────────────────────────────────────────────────────────────

async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🤝 Meeting", callback_data="cat_meeting"),
        InlineKeyboardButton("✅ Task",    callback_data="cat_task"),
        InlineKeyboardButton("🔁 Habit",  callback_data="cat_habit"),
    ]])
    await update.message.reply_text(
        "➕ *Add a new event*\n\nWhat type of event would you like to add?",
        reply_markup=keyboard, parse_mode="Markdown",
    )
    return S_CATEGORY


async def cb_category(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["category"] = q.data.split("_")[1]
    await q.edit_message_text(
        f"Category: *{ctx.user_data['category'].title()}* ✅\n\n"
        "Give it a *title* — e.g. 'Team standup', 'Morning run', 'Write Q3 report':",
        parse_mode="Markdown",
    )
    return S_TITLE


async def got_title(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["title"] = update.message.text.strip()
    await update.message.reply_text(
        f"*{ctx.user_data['title']}* — got it!\n\n"
        "📅 What *date*? Format: `DD/MM/YYYY` — or type `today` or `tomorrow`.",
        parse_mode="Markdown",
    )
    return S_DATE


async def got_date(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    chat_id = str(update.effective_chat.id)
    db = get_db()
    user = get_or_create_user(db, chat_id)
    db.close()
    tz = pytz.timezone(user.timezone)
    today = datetime.now(tz).date()

    if text == "today":
        d = today
    elif text == "tomorrow":
        d = today + timedelta(days=1)
    else:
        try:
            d = datetime.strptime(text, "%d/%m/%Y").date()
        except ValueError:
            await update.message.reply_text("❌ Couldn't parse that. Use `DD/MM/YYYY`, `today`, or `tomorrow`.", parse_mode="Markdown")
            return S_DATE

    ctx.user_data["event_date"] = d
    await update.message.reply_text(
        f"📅 {d.strftime('%A, %d %B %Y')} ✅\n\n⏰ What *start time*? Format: `HH:MM` (24-hour).",
        parse_mode="Markdown",
    )
    return S_START


async def got_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = _parse_time(update.message.text.strip())
    if not t:
        await update.message.reply_text("❌ Please use `HH:MM` format, e.g. `09:00`.", parse_mode="Markdown")
        return S_START
    ctx.user_data["start_h"], ctx.user_data["start_m"] = t
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("15 min", callback_data="dur_15 min"),
        InlineKeyboardButton("30 min", callback_data="dur_30 min"),
        InlineKeyboardButton("1 hour", callback_data="dur_1 hour"),
    ], [
        InlineKeyboardButton("2 hours", callback_data="dur_2 hours"),
        InlineKeyboardButton("3 hours", callback_data="dur_3 hours"),
        InlineKeyboardButton("Custom",  callback_data="dur_custom"),
    ]])
    await update.message.reply_text(
        f"⏰ Start: *{update.message.text.strip()}* ✅\n\nHow long will it last?",
        reply_markup=keyboard, parse_mode="Markdown",
    )
    return S_DURATION


async def cb_duration(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    choice = q.data[4:]  # strip "dur_"
    if choice == "custom":
        await q.edit_message_text("⏱ How many minutes will it last? (Enter a number, e.g. `45`)", parse_mode="Markdown")
        ctx.user_data["awaiting_custom_duration"] = True
        return S_DURATION
    ctx.user_data["duration_min"] = DURATION_MAP[choice]
    ctx.user_data.pop("awaiting_custom_duration", None)
    return await _ask_gravity(q)


async def got_custom_duration(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("awaiting_custom_duration"):
        return S_DURATION
    try:
        mins = int(update.message.text.strip())
        assert 1 <= mins <= 1440
    except Exception:
        await update.message.reply_text("❌ Please enter a number between 1 and 1440.")
        return S_DURATION
    ctx.user_data["duration_min"] = mins
    ctx.user_data.pop("awaiting_custom_duration", None)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🟢 Low",    callback_data="grav_low"),
        InlineKeyboardButton("🟡 Medium", callback_data="grav_medium"),
        InlineKeyboardButton("🔴 High",   callback_data="grav_high"),
    ]])
    await update.message.reply_text(
        "How *important* is this event?",
        reply_markup=keyboard, parse_mode="Markdown",
    )
    return S_GRAVITY


async def _ask_gravity(q):
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🟢 Low",    callback_data="grav_low"),
        InlineKeyboardButton("🟡 Medium", callback_data="grav_medium"),
        InlineKeyboardButton("🔴 High",   callback_data="grav_high"),
    ]])
    await q.edit_message_text(
        "How *important* is this event?",
        reply_markup=keyboard, parse_mode="Markdown",
    )
    return S_GRAVITY


async def cb_gravity(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["gravity"] = q.data.split("_")[1]
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("⚡ Yes, flag it", callback_data="pri_yes"),
        InlineKeyboardButton("No",             callback_data="pri_no"),
    ]])
    await q.edit_message_text(
        f"Gravity: *{ctx.user_data['gravity'].title()}* ✅\n\n"
        "⚡ Should this be a *priority event*? It will appear at the top of your morning briefing.",
        reply_markup=keyboard, parse_mode="Markdown",
    )
    return S_PRIORITY


async def cb_priority(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["is_priority"] = (q.data == "pri_yes")
    cat = ctx.user_data.get("category")

    if cat == "habit":
        day_buttons = [
            [InlineKeyboardButton(d, callback_data=f"day_{d}") for d in ["Mon", "Tue", "Wed", "Thu"]],
            [InlineKeyboardButton(d, callback_data=f"day_{d}") for d in ["Fri", "Sat", "Sun"]],
            [InlineKeyboardButton("✅ Every day", callback_data="day_every"),
             InlineKeyboardButton("➡️ Done selecting", callback_data="day_done")],
        ]
        ctx.user_data["habit_days"] = []
        await q.edit_message_text(
            "📅 Which *days* should this habit recur?\nTap each day to select, then tap *Done selecting*.",
            reply_markup=InlineKeyboardMarkup(day_buttons), parse_mode="Markdown",
        )
        return S_HABIT_DAYS
    elif cat == "task":
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("One-time", callback_data="rec_once"),
            InlineKeyboardButton("Daily",    callback_data="rec_daily"),
            InlineKeyboardButton("Weekly",   callback_data="rec_weekly"),
            InlineKeyboardButton("Custom",   callback_data="rec_custom"),
        ]])
        await q.edit_message_text("Does this task *repeat*?", reply_markup=keyboard, parse_mode="Markdown")
        return S_TASK_RECUR
    else:
        # Meeting: no recurrence, go straight to confirm
        return await _finalize_and_confirm(q, ctx)


# ── Habit days ───────────────────────────────────────────────────────────────

async def cb_habit_days(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "day_every":
        ctx.user_data["habit_days"] = list(DAY_MAP.values())
        ctx.user_data["recur_days"] = "MO,TU,WE,TH,FR,SA,SU"
    elif data == "day_done":
        if not ctx.user_data.get("habit_days"):
            await q.answer("Please select at least one day!", show_alert=True)
            return S_HABIT_DAYS
        ctx.user_data["recur_days"] = ",".join(ctx.user_data["habit_days"])
    else:
        day_short = data.split("_")[1]
        day_code  = DAY_MAP.get(day_short, "MO")
        if day_code not in ctx.user_data["habit_days"]:
            ctx.user_data["habit_days"].append(day_code)
        else:
            ctx.user_data["habit_days"].remove(day_code)
        selected = ", ".join(ctx.user_data["habit_days"])
        await q.answer(f"Selected: {selected or 'none'}")
        return S_HABIT_DAYS

    # Ask fixed or flexible
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🕐 Fixed time", callback_data="ttype_fixed"),
        InlineKeyboardButton("🔄 Flexible range", callback_data="ttype_range"),
    ]])
    await q.edit_message_text(
        f"Days set ✅\n\nIs the time *fixed* or *flexible*?",
        reply_markup=keyboard, parse_mode="Markdown",
    )
    return S_HABIT_TIME_TYPE


async def cb_habit_time_type(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "ttype_fixed":
        ctx.user_data["time_is_fixed"] = True
        await q.edit_message_text("🕐 What *fixed time* should it happen? Format: `HH:MM`", parse_mode="Markdown")
        return S_HABIT_TIME_FIXED
    else:
        ctx.user_data["time_is_fixed"] = False
        await q.edit_message_text("What's the *earliest* time it could start? Format: `HH:MM`", parse_mode="Markdown")
        return S_HABIT_TIME_RANGE_START


async def got_habit_fixed_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = _parse_time(update.message.text.strip())
    if not t:
        await update.message.reply_text("❌ Use HH:MM format.")
        return S_HABIT_TIME_FIXED
    ctx.user_data["start_h"], ctx.user_data["start_m"] = t
    return await _finalize_and_confirm(update, ctx)


async def got_habit_range_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = _parse_time(update.message.text.strip())
    if not t:
        await update.message.reply_text("❌ Use HH:MM format.")
        return S_HABIT_TIME_RANGE_START
    ctx.user_data["range_start"] = update.message.text.strip()
    await update.message.reply_text("What's the *latest* time it can start? Format: `HH:MM`", parse_mode="Markdown")
    return S_HABIT_TIME_RANGE_END


async def got_habit_range_end(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = _parse_time(update.message.text.strip())
    if not t:
        await update.message.reply_text("❌ Use HH:MM format.")
        return S_HABIT_TIME_RANGE_END
    ctx.user_data["range_end"] = update.message.text.strip()
    # Use range_start as the scheduled start for reminder purposes
    rs = ctx.user_data["range_start"]
    ctx.user_data["start_h"], ctx.user_data["start_m"] = _parse_time(rs)
    return await _finalize_and_confirm(update, ctx)


# ── Task recurrence ──────────────────────────────────────────────────────────

async def cb_task_recur(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    choice = q.data.split("_")[1]
    if choice == "once":
        ctx.user_data["recur_rule"] = None
    elif choice == "daily":
        ctx.user_data["recur_rule"] = "RRULE:FREQ=DAILY"
    elif choice == "weekly":
        ctx.user_data["recur_rule"] = "RRULE:FREQ=WEEKLY"
    elif choice == "custom":
        await q.edit_message_text("Repeat every how many *days*? (Enter a number)", parse_mode="Markdown")
        ctx.user_data["awaiting_custom_recur"] = True
        return S_TASK_RECUR
    return await _finalize_and_confirm(q, ctx)


async def got_custom_recur(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("awaiting_custom_recur"):
        return S_TASK_RECUR
    try:
        n = int(update.message.text.strip())
        assert n >= 1
        ctx.user_data["recur_rule"] = f"RRULE:FREQ=DAILY;INTERVAL={n}"
        ctx.user_data.pop("awaiting_custom_recur", None)
    except Exception:
        await update.message.reply_text("❌ Please enter a positive integer.")
        return S_TASK_RECUR
    return await _finalize_and_confirm(update, ctx)


# ── Conflict check + confirmation ────────────────────────────────────────────

async def _finalize_and_confirm(trigger, ctx):
    """Compute start/end datetimes, run conflict check, show summary."""
    d    = ctx.user_data["event_date"]
    h, m = ctx.user_data["start_h"], ctx.user_data["start_m"]
    dur  = ctx.user_data.get("duration_min", 60)

    # Build UTC datetimes — store everything in UTC internally
    start_local = datetime(d.year, d.month, d.day, h, m)
    end_local   = start_local + timedelta(minutes=dur)

    # Get user timezone
    chat_id = None
    if hasattr(trigger, "effective_chat"):
        chat_id = str(trigger.effective_chat.id)
    elif hasattr(trigger, "message") and trigger.message:
        chat_id = str(trigger.message.chat_id)

    db   = get_db()
    user = get_or_create_user(db, chat_id or "0")
    tz   = pytz.timezone(user.timezone)

    start_utc = tz.localize(start_local).astimezone(pytz.utc).replace(tzinfo=None)
    end_utc   = tz.localize(end_local).astimezone(pytz.utc).replace(tzinfo=None)

    ctx.user_data["start_utc"] = start_utc
    ctx.user_data["end_utc"]   = end_utc

    # Conflict check
    conflicts = db.query(Event).filter(
        Event.user_id  == user.id,
        Event.start_dt <  end_utc,
        Event.end_dt   >  start_utc,
    ).all()
    db.close()

    if conflicts:
        conflict = conflicts[0]
        ctx.user_data["conflict_id"] = conflict.id
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Move new event",      callback_data="conf_move_new"),
            InlineKeyboardButton("Move existing event", callback_data="conf_move_old"),
            InlineKeyboardButton("Keep both",           callback_data="conf_keep"),
        ]])
        conf_start = conflict.start_dt.strftime("%H:%M")
        conf_end   = conflict.end_dt.strftime("%H:%M")
        msg = (
            f"⚠️ *Overlap detected!*\n\n"
            f"*New:* {ctx.user_data['title']} at {start_local.strftime('%H:%M')}\n"
            f"*Conflicts with:* {conflict.title} ({conf_start}–{conf_end})\n\n"
            "Which one should I adjust?"
        )
        if hasattr(trigger, "edit_message_text"):
            await trigger.edit_message_text(msg, reply_markup=keyboard, parse_mode="Markdown")
        else:
            await trigger.message.reply_text(msg, reply_markup=keyboard, parse_mode="Markdown")
        return S_CONFLICT

    return await _show_summary(trigger, ctx, start_local)


async def cb_conflict(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    choice = q.data.split("_", 1)[1]

    if choice == "keep":
        ctx.user_data["keep_conflict"] = True
        return await _show_summary(q, ctx, _local_start(ctx))
    elif choice == "move_new":
        await q.edit_message_text(
            "📅 Enter a *new start time* for the new event: `HH:MM`",
            parse_mode="Markdown",
        )
        ctx.user_data["resolving"] = "new"
        return S_CONFLICT
    elif choice == "move_old":
        await q.edit_message_text(
            "📅 Enter a *new start time* for the existing event: `HH:MM`",
            parse_mode="Markdown",
        )
        ctx.user_data["resolving"] = "old"
        return S_CONFLICT
    return S_CONFLICT


async def got_conflict_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle new time entry after conflict resolution choice."""
    resolving = ctx.user_data.get("resolving")
    t = _parse_time(update.message.text.strip())
    if not t:
        await update.message.reply_text("❌ Use HH:MM format.")
        return S_CONFLICT

    if resolving == "new":
        ctx.user_data["start_h"], ctx.user_data["start_m"] = t
        return await _finalize_and_confirm(update, ctx)
    elif resolving == "old":
        # Update the existing conflicting event
        chat_id = str(update.effective_chat.id)
        db   = get_db()
        user = get_or_create_user(db, chat_id)
        tz   = pytz.timezone(user.timezone)
        ev   = db.query(Event).filter(Event.id == ctx.user_data["conflict_id"]).first()
        if ev:
            d = ev.start_dt.date()
            new_start_local = datetime(d.year, d.month, d.day, t[0], t[1])
            new_start_utc   = tz.localize(new_start_local).astimezone(pytz.utc).replace(tzinfo=None)
            duration        = (ev.end_dt - ev.start_dt).total_seconds() / 60
            new_end_utc     = new_start_utc + timedelta(minutes=duration)
            ev.start_dt     = new_start_utc
            ev.end_dt       = new_end_utc
            ev.is_synced    = False
            reminder_service.cancel_reminders(ev.id)
            db.commit()
            reminder_service.schedule_reminders(ev, chat_id, ctx.application)
        db.close()
        return await _show_summary(update, ctx, _local_start(ctx))


async def _show_summary(trigger, ctx, start_local):
    d   = ctx.user_data["event_date"]
    dur = ctx.user_data.get("duration_min", 60)
    end_local = start_local + timedelta(minutes=dur)

    cat   = ctx.user_data.get("category", "meeting")
    grav  = ctx.user_data.get("gravity", "medium")
    pri   = "⚡ Yes" if ctx.user_data.get("is_priority") else "No"
    grav_e = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(grav, "🟡")

    recur_info = ""
    if ctx.user_data.get("recur_rule"):
        recur_info = f"\n🔁 Repeats: {ctx.user_data['recur_rule']}"
    if ctx.user_data.get("recur_days"):
        recur_info += f"\n📅 Days: {ctx.user_data['recur_days']}"
    if not ctx.user_data.get("time_is_fixed", True):
        recur_info += f"\n🕐 Flexible: {ctx.user_data.get('range_start')} – {ctx.user_data.get('range_end')}"

    summary = (
        f"📋 *Here's what I'll save:*\n\n"
        f"📌 *Title:* {ctx.user_data['title']}\n"
        f"🏷 *Category:* {cat.title()}\n"
        f"📅 *Date:* {d.strftime('%A, %d %B %Y')}\n"
        f"⏰ *Time:* {start_local.strftime('%H:%M')} → {end_local.strftime('%H:%M')} ({dur} min)\n"
        f"{grav_e} *Gravity:* {grav.title()}\n"
        f"⚡ *Priority:* {pri}"
        f"{recur_info}\n\n"
        "Save this?"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Save", callback_data="save_yes"),
        InlineKeyboardButton("✏️ Start over", callback_data="save_no"),
    ]])

    if hasattr(trigger, "edit_message_text"):
        await trigger.edit_message_text(summary, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await trigger.message.reply_text(summary, reply_markup=keyboard, parse_mode="Markdown")

    return S_CONFIRM


async def cb_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "save_no":
        await q.edit_message_text("❌ Cancelled. Send /add to start over.")
        ctx.user_data.clear()
        return ConversationHandler.END

    # Save event
    chat_id = str(update.effective_chat.id)
    db   = get_db()
    user = get_or_create_user(db, chat_id)

    ev = Event(
        user_id      = user.id,
        title        = ctx.user_data["title"],
        category     = ctx.user_data["category"],
        gravity      = ctx.user_data.get("gravity", "medium"),
        is_priority  = ctx.user_data.get("is_priority", False),
        start_dt     = ctx.user_data["start_utc"],
        end_dt       = ctx.user_data["end_utc"],
        recur_rule   = ctx.user_data.get("recur_rule"),
        recur_days   = ctx.user_data.get("recur_days"),
        time_is_fixed= ctx.user_data.get("time_is_fixed", True),
        time_range_start = ctx.user_data.get("range_start"),
        time_range_end   = ctx.user_data.get("range_end"),
        is_synced    = False,
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)
    db.close()

    # Schedule reminders
    reminder_service.schedule_reminders(ev, chat_id, ctx.application)

    await q.edit_message_text(
        f"✅ *{ev.title}* saved!\n\n"
        f"Reminders have been scheduled based on *{ev.gravity}* gravity.\n"
        "I'll also sync this to Google Calendar shortly.",
        parse_mode="Markdown",
    )

    # Async sync attempt
    from services.calendar_service import push_event as _push
    db2  = get_db()
    user2 = get_or_create_user(db2, chat_id)
    td   = json.loads(user2.google_token) if user2.google_token else {}
    ev2  = db2.query(Event).filter(Event.id == ev.id).first()
    if ev2 and td:
        gid = _push(td, ev2, user2.timezone)
        if gid:
            ev2.google_event_id = gid
            ev2.is_synced = True
            db2.commit()
    db2.close()

    ctx.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled. Send /add when you're ready.")
    ctx.user_data.clear()
    return ConversationHandler.END


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_time(s: str):
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if not m:
        return None
    h, mn = int(m.group(1)), int(m.group(2))
    if 0 <= h <= 23 and 0 <= mn <= 59:
        return h, mn
    return None


def _local_start(ctx) -> datetime:
    d = ctx.user_data["event_date"]
    h, m = ctx.user_data["start_h"], ctx.user_data["start_m"]
    return datetime(d.year, d.month, d.day, h, m)


def get_add_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("add", cmd_add)],
        states={
            S_CATEGORY: [CallbackQueryHandler(cb_category, pattern="^cat_")],
            S_TITLE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, got_title)],
            S_DATE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, got_date)],
            S_START:    [MessageHandler(filters.TEXT & ~filters.COMMAND, got_start)],
            S_DURATION: [
                CallbackQueryHandler(cb_duration, pattern="^dur_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_custom_duration),
            ],
            S_GRAVITY:  [CallbackQueryHandler(cb_gravity, pattern="^grav_")],
            S_PRIORITY: [CallbackQueryHandler(cb_priority, pattern="^pri_")],
            S_HABIT_DAYS:       [CallbackQueryHandler(cb_habit_days, pattern="^day_")],
            S_HABIT_TIME_TYPE:  [CallbackQueryHandler(cb_habit_time_type, pattern="^ttype_")],
            S_HABIT_TIME_FIXED: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_habit_fixed_time)],
            S_HABIT_TIME_RANGE_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_habit_range_start)],
            S_HABIT_TIME_RANGE_END:   [MessageHandler(filters.TEXT & ~filters.COMMAND, got_habit_range_end)],
            S_TASK_RECUR: [
                CallbackQueryHandler(cb_task_recur, pattern="^rec_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_custom_recur),
            ],
            S_CONFLICT: [
                CallbackQueryHandler(cb_conflict, pattern="^conf_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_conflict_time),
            ],
            S_CONFIRM:  [CallbackQueryHandler(cb_confirm, pattern="^save_")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        name="add_event",
        persistent=False,
    )
