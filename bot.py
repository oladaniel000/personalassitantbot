"""
bot.py — Main entry point for the Telegram Daily Personal Assistant.
Registers all handlers, initialises the scheduler, starts the bot.
"""

import json
import logging
import asyncio
from datetime import datetime

from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters
)

from config import TELEGRAM_BOT_TOKEN, DB_URL
from database.db import init_db, get_db, get_or_create_user
from services.reminder_service import init_scheduler
from services import calendar_service

# ── Handlers ─────────────────────────────────────────────────────────────────
from handlers.setup import get_setup_handler
from handlers.event_add import get_add_handler
from handlers.event_checkin import get_checkin_handler
from handlers.morning import cmd_today, cmd_tomorrow
from handlers.evening import cmd_recap
from handlers.misc import cmd_help, cmd_sync, cmd_done, cmd_snooze, cmd_delete, cmd_woke

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)


async def post_init(app: Application) -> None:
    """
    Called once after the Application is fully initialised.
    - Initialises the scheduler
    - Re-registers daily morning/evening jobs for all existing users
    - Starts the 5-minute Google Calendar sync job
    """
    scheduler = init_scheduler()

    db = get_db()
    from database.models import UserState
    users = db.query(UserState).filter(UserState.setup_complete == True).all()

    from handlers.morning import schedule_morning_job
    from handlers.evening import schedule_evening_job

    for user in users:
        chat_id = user.telegram_chat_id
        schedule_morning_job(chat_id, user.morning_time or "07:00", user.timezone or "Africa/Lagos", app)
        schedule_evening_job(chat_id, user.evening_time or "21:00", user.timezone or "Africa/Lagos", app)

    db.close()

    # Periodic Google Calendar sync — every 5 minutes
    scheduler.add_job(
        _sync_all_users,
        trigger="interval",
        minutes=5,
        args=[app],
        id="gcal_sync_all",
        replace_existing=True,
    )

    log.info(f"Bot initialised. {len(users)} user(s) loaded. Scheduler running.")


async def _sync_all_users(app: Application) -> None:
    """Push any unsynced local events to Google Calendar for all users."""
    db = get_db()
    from database.models import UserState
    users = db.query(UserState).filter(
        UserState.setup_complete == True,
        UserState.google_token != None,
    ).all()
    db.close()

    for user in users:
        try:
            db2 = get_db()
            token_dict = json.loads(user.google_token)
            count = calendar_service.sync_pending_events(db2, token_dict, user.timezone)
            if count:
                log.info(f"Synced {count} event(s) for user {user.telegram_chat_id}")
            db2.close()
        except Exception as e:
            log.warning(f"Sync failed for {user.telegram_chat_id}: {e}")


def main() -> None:
    # 1. Initialise database (creates tables if missing)
    init_db()
    log.info("Database initialised.")

    # 2. Build the Application
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # 3. Register conversation handlers (order matters — most specific first)
    app.add_handler(get_setup_handler())
    app.add_handler(get_add_handler())
    app.add_handler(get_checkin_handler())

    # 4. Register simple command handlers
    app.add_handler(CommandHandler("today",    cmd_today))
    app.add_handler(CommandHandler("tomorrow", cmd_tomorrow))
    app.add_handler(CommandHandler("recap",    cmd_recap))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("sync",     cmd_sync))
    app.add_handler(CommandHandler("done",     cmd_done))
    app.add_handler(CommandHandler("snooze",   cmd_snooze))
    app.add_handler(CommandHandler("delete",   cmd_delete))
    app.add_handler(CommandHandler("woke",     cmd_woke))

    # 5. Start polling
    log.info("Starting bot polling…")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
