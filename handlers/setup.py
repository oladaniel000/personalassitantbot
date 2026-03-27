"""
handlers/setup.py
One-time onboarding conversation.
Collects: name → timezone → home address → work address →
          morning time → evening time → Google OAuth → first sync.
"""

import json
import logging
import re
from datetime import time as dtime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    MessageHandler, CallbackQueryHandler, filters,
)

from database.db import get_db, get_or_create_user
from services import calendar_service, weather_service
from config import USER_TIMEZONE, MORNING_SUMMARY_TIME, EVENING_RECAP_TIME

log = logging.getLogger(__name__)

# Conversation states
(
    ASK_NAME, ASK_TZ, ASK_HOME, ASK_WORK,
    ASK_MORNING, ASK_EVENING, ASK_GOOGLE_CODE,
) = range(7)


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    db = get_db()
    user = get_or_create_user(db, chat_id)
    db.close()

    if user.setup_complete:
        await update.message.reply_text(
            "👋 You're already set up! Use /today, /add, or /help to get started.",
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "👋 *Welcome to your Telegram Daily Assistant!*\n\n"
        "I'll be your personal scheduler, reminder engine, and daily recap partner.\n\n"
        "Let's get you set up — just answer a few quick questions.\n\n"
        "*First: What's your name?*",
        parse_mode="Markdown",
    )
    return ASK_NAME


async def ask_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["name"] = update.message.text.strip()
    await update.message.reply_text(
        f"Nice to meet you, *{ctx.user_data['name']}*! 🎉\n\n"
        "What's your timezone? Use the standard tz database name.\n"
        "Examples: `Africa/Lagos` · `Europe/London` · `America/New_York` · `Asia/Kolkata`",
        parse_mode="Markdown",
    )
    return ASK_TZ


async def ask_tz(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    import pytz
    tz_str = update.message.text.strip()
    if tz_str not in pytz.all_timezones:
        await update.message.reply_text(
            "❌ I don't recognise that timezone. Please use a valid tz name like `Africa/Lagos`.\n"
            "Full list: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones",
            parse_mode="Markdown",
        )
        return ASK_TZ
    ctx.user_data["timezone"] = tz_str
    await update.message.reply_text(
        f"✅ Timezone set to *{tz_str}*.\n\n"
        "Now, what's your *home address*? (I'll use this for weather and commute estimates.)\n\n"
        "Example: `123 Main Street, Lagos, Nigeria`",
        parse_mode="Markdown",
    )
    return ASK_HOME


async def ask_home(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    addr = update.message.text.strip()
    await update.message.reply_text("📍 Geocoding your home address…")
    coords = weather_service.geocode_address(addr)
    if not coords:
        await update.message.reply_text(
            "❌ I couldn't find that address. Please try a more specific address or city name."
        )
        return ASK_HOME
    ctx.user_data["home_address"] = addr
    ctx.user_data["home_lat"] = coords[0]
    ctx.user_data["home_lon"] = coords[1]
    await update.message.reply_text(
        f"✅ Home found! ({coords[0]:.3f}, {coords[1]:.3f})\n\n"
        "What's your *work/office address*? (For commute estimates.)\n"
        "Type `same` if you work from home.",
        parse_mode="Markdown",
    )
    return ASK_WORK


async def ask_work(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.lower() == "same":
        ctx.user_data["work_address"] = ctx.user_data["home_address"]
        ctx.user_data["work_lat"] = ctx.user_data["home_lat"]
        ctx.user_data["work_lon"] = ctx.user_data["home_lon"]
    else:
        await update.message.reply_text("📍 Geocoding your work address…")
        coords = weather_service.geocode_address(text)
        if not coords:
            await update.message.reply_text(
                "❌ Couldn't find that address. Please try again or type `same` for work from home.",
                parse_mode="Markdown",
            )
            return ASK_WORK
        ctx.user_data["work_address"] = text
        ctx.user_data["work_lat"] = coords[0]
        ctx.user_data["work_lon"] = coords[1]

    await update.message.reply_text(
        "✅ Got it!\n\n"
        "What time do you want your *morning briefing*?\n"
        "Format: `HH:MM` (24-hour). Default is `07:00`.",
        parse_mode="Markdown",
    )
    return ASK_MORNING


async def ask_morning(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = _parse_time(update.message.text.strip())
    if not t:
        await update.message.reply_text("❌ Please use HH:MM format, e.g. `07:00`.", parse_mode="Markdown")
        return ASK_MORNING
    ctx.user_data["morning_time"] = update.message.text.strip()
    await update.message.reply_text(
        f"✅ Morning briefing set for *{ctx.user_data['morning_time']}*.\n\n"
        "What time for your *evening recap*? Default is `21:00`.",
        parse_mode="Markdown",
    )
    return ASK_EVENING


async def ask_evening(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = _parse_time(update.message.text.strip())
    if not t:
        await update.message.reply_text("❌ Please use HH:MM format, e.g. `21:00`.", parse_mode="Markdown")
        return ASK_EVENING
    ctx.user_data["evening_time"] = update.message.text.strip()

    # Generate Google OAuth URL
    auth_url = calendar_service.build_oauth_url()
    await update.message.reply_text(
        "Almost done! Now let's connect your *Google Calendar*. 📅\n\n"
        "1️⃣ Open this link and log in with your Google account:\n"
        f"`{auth_url}`\n\n"
        "2️⃣ Approve the permissions.\n"
        "3️⃣ Copy the authorisation code Google gives you.\n"
        "4️⃣ Paste it here and press Send.",
        parse_mode="Markdown",
    )
    return ASK_GOOGLE_CODE


async def ask_google_code(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip()
    await update.message.reply_text("🔄 Verifying your Google authorisation code…")
    try:
        token_dict = calendar_service.exchange_code_for_token(code)
    except Exception as e:
        await update.message.reply_text(
            f"❌ That code didn't work: `{e}`\n\nPlease try again — paste just the code.",
            parse_mode="Markdown",
        )
        return ASK_GOOGLE_CODE

    # Save everything to DB
    chat_id = str(update.effective_chat.id)
    db = get_db()
    user = get_or_create_user(db, chat_id)
    user.name           = ctx.user_data.get("name", "")
    user.timezone       = ctx.user_data.get("timezone", USER_TIMEZONE)
    user.home_address   = ctx.user_data.get("home_address", "")
    user.home_lat       = ctx.user_data.get("home_lat")
    user.home_lon       = ctx.user_data.get("home_lon")
    user.work_address   = ctx.user_data.get("work_address", "")
    user.work_lat       = ctx.user_data.get("work_lat")
    user.work_lon       = ctx.user_data.get("work_lon")
    user.morning_time   = ctx.user_data.get("morning_time", MORNING_SUMMARY_TIME)
    user.evening_time   = ctx.user_data.get("evening_time", EVENING_RECAP_TIME)
    user.google_token   = json.dumps(token_dict)
    user.setup_complete = True
    db.commit()
    db.close()

    # Schedule daily morning and evening jobs
    from handlers.morning import schedule_morning_job
    from handlers.evening import schedule_evening_job
    schedule_morning_job(chat_id, user.morning_time, user.timezone, ctx.application)
    schedule_evening_job(chat_id, user.evening_time, user.timezone, ctx.application)

    await update.message.reply_text(
        f"🎉 *You're all set, {ctx.user_data.get('name', 'friend')}!*\n\n"
        "✅ Google Calendar connected\n"
        f"⏰ Morning briefing: {user.morning_time}\n"
        f"🌙 Evening recap: {user.evening_time}\n"
        f"🌍 Timezone: {user.timezone}\n\n"
        "I'll send your first morning briefing tomorrow. Use /today to see today's events now, "
        "or /add to create your first event!\n\n"
        "Type /help to see everything I can do. 💪",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Setup cancelled. Send /start whenever you're ready.")
    return ConversationHandler.END


def _parse_time(s: str):
    """Parse HH:MM string. Returns (hour, minute) tuple or None."""
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if not m:
        return None
    h, mn = int(m.group(1)), int(m.group(2))
    if 0 <= h <= 23 and 0 <= mn <= 59:
        return h, mn
    return None


def get_setup_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_NAME:        [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_name)],
            ASK_TZ:          [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_tz)],
            ASK_HOME:        [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_home)],
            ASK_WORK:        [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_work)],
            ASK_MORNING:     [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_morning)],
            ASK_EVENING:     [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_evening)],
            ASK_GOOGLE_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_google_code)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        name="setup",
        persistent=False,
    )
