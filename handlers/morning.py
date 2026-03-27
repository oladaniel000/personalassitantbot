"""
handlers/morning.py
Builds and sends the morning itinerary.
Scheduled daily; also callable on demand via /today.
"""

import json
import logging
from datetime import datetime, date, timedelta

import pytz

from database.db import get_db, get_or_create_user
from database.models import Event
from services import calendar_service, weather_service, commute_service
from services.reminder_service import scheduler

log = logging.getLogger(__name__)

GRAVITY_EMOJI = {"low": "🟢", "medium": "🟡", "high": "🔴"}
CAT_EMOJI     = {"meeting": "🤝", "task": "✅", "habit": "🔁"}


# ── Scheduler registration ───────────────────────────────────────────────────

def schedule_morning_job(chat_id: str, morning_time: str, timezone: str, app) -> None:
    """Register a daily APScheduler cron job for the morning briefing."""
    hour, minute = map(int, morning_time.split(":"))
    job_id = f"morning_{chat_id}"
    try:
        scheduler.add_job(
            send_morning_itinerary,
            trigger="cron",
            hour=hour,
            minute=minute,
            timezone=timezone,
            args=[chat_id, app],
            id=job_id,
            replace_existing=True,
        )
        log.info(f"Morning job scheduled for {chat_id} at {morning_time} {timezone}")
    except Exception as e:
        log.warning(f"Could not schedule morning job: {e}")


# ── Core sender ──────────────────────────────────────────────────────────────

async def send_morning_itinerary(chat_id: str, app, target_date: date = None) -> None:
    """
    Build and send the morning itinerary to chat_id.
    target_date defaults to today in the user's timezone.
    """
    db   = get_db()
    user = get_or_create_user(db, chat_id)
    db.close()

    if not user.setup_complete:
        return

    tz = pytz.timezone(user.timezone)
    if target_date is None:
        target_date = datetime.now(tz).date()

    token_dict = json.loads(user.google_token) if user.google_token else {}

    # ── Pull events ──────────────────────────────────────────────────────────
    gcal_events = calendar_service.get_events_for_date(token_dict, target_date, user.timezone)
    formatted_gcal = [
        calendar_service.format_gcal_event_for_display(e, user.timezone)
        for e in gcal_events
    ]

    # Also get local events not yet synced
    db = get_db()
    local_events = db.query(Event).filter(
        Event.user_id == user.id,
        Event.is_synced == False,
    ).all()
    db.close()

    local_formatted = []
    for ev in local_events:
        ev_tz_start = ev.start_dt.replace(tzinfo=pytz.utc).astimezone(tz)
        if ev_tz_start.date() == target_date:
            local_formatted.append({
                "id":       None,
                "title":    ev.title,
                "start_dt": ev_tz_start,
                "end_dt":   ev.end_dt.replace(tzinfo=pytz.utc).astimezone(tz),
                "start_str": ev_tz_start.strftime("%H:%M"),
                "end_str":  ev.end_dt.replace(tzinfo=pytz.utc).astimezone(tz).strftime("%H:%M"),
                "category": ev.category,
                "gravity":  ev.gravity,
                "is_priority": ev.is_priority,
                "is_all_day": False,
            })

    # Merge — prefer GCal version if google_event_id exists
    all_events = formatted_gcal + local_formatted
    all_events.sort(key=lambda e: e["start_dt"])

    # ── Weather ──────────────────────────────────────────────────────────────
    weather = None
    if user.home_lat and user.home_lon:
        weather = weather_service.get_weather(user.home_lat, user.home_lon)

    # ── Build message ────────────────────────────────────────────────────────
    date_str  = target_date.strftime("%A, %d %B %Y")
    is_today  = target_date == datetime.now(tz).date()
    day_label = "Today" if is_today else target_date.strftime("%A")

    lines = []
    lines.append(f"🌅 *Good morning, {user.name or 'friend'}! Here's {day_label}*")
    lines.append(f"📅 *{date_str}*\n")

    # Weather block
    if weather:
        lines.append("─" * 30)
        lines.append("🌤 *WEATHER*")
        lines.append(weather_service.format_weather_today(weather))
        lines.append("")

    # Priority block
    priority_events = [e for e in all_events if e.get("is_priority")]
    if priority_events:
        lines.append("─" * 30)
        lines.append("⚡ *PRIORITY EVENTS TODAY*")
        lines.append("─" * 30)
        for e in priority_events:
            g  = GRAVITY_EMOJI.get(e["gravity"], "🟡")
            c  = CAT_EMOJI.get(e["category"], "📌")
            dur = _duration_str(e["start_dt"], e["end_dt"])
            lines.append(f"⚡{g} *{e['start_str']} — {e['title']}*  {c}  _{dur}_")
        lines.append("_These are flagged as top priority — stay sharp!_\n")

    # Full schedule block
    lines.append("─" * 30)
    if all_events:
        lines.append("📅 *FULL SCHEDULE*")
        lines.append("─" * 30)
        for e in all_events:
            g   = GRAVITY_EMOJI.get(e["gravity"], "🟡")
            c   = CAT_EMOJI.get(e["category"], "📌")
            pri = "⚡" if e.get("is_priority") else ""
            dur = _duration_str(e["start_dt"], e["end_dt"])
            lines.append(
                f"{g}{pri} {e['start_str']}–{e['end_str']}  {c} *{e['title']}*  _{dur}_"
            )
    else:
        lines.append("📭 *No events scheduled today.*")
        lines.append("Use /add to schedule something!")

    # Summary line
    n_meet   = sum(1 for e in all_events if e.get("category") == "meeting")
    n_task   = sum(1 for e in all_events if e.get("category") == "task")
    n_habit  = sum(1 for e in all_events if e.get("category") == "habit")
    lines.append("")
    lines.append("─" * 30)
    lines.append(
        f"📊 *{len(all_events)} events* — {n_meet} meeting(s), {n_task} task(s), {n_habit} habit(s)"
    )
    lines.append("\nHave a focused, productive day! 💪")

    message = "\n".join(lines)

    try:
        await app.bot.send_message(
            chat_id=chat_id,
            text=message,
            parse_mode="Markdown",
        )
    except Exception as e:
        log.warning(f"Failed to send morning itinerary: {e}")


# ── /today command handler ───────────────────────────────────────────────────

async def cmd_today(update, ctx):
    chat_id = str(update.effective_chat.id)
    await update.message.reply_text("📅 Pulling today's schedule…")
    await send_morning_itinerary(chat_id, ctx.application)


async def cmd_tomorrow(update, ctx):
    import pytz
    from datetime import timedelta
    chat_id = str(update.effective_chat.id)
    db   = get_db()
    user = get_or_create_user(db, chat_id)
    db.close()

    tz   = pytz.timezone(user.timezone)
    tomorrow = (datetime.now(tz) + timedelta(days=1)).date()

    await update.message.reply_text("📅 Pulling tomorrow's schedule…")
    await send_morning_itinerary(chat_id, ctx.application, target_date=tomorrow)

    # Commute estimate
    if user.home_lat and user.work_lat:
        commute = commute_service.get_commute_estimate(
            user.home_lat, user.home_lon,
            user.work_lat, user.work_lon,
        )
        msg = commute_service.format_commute(commute)
        await ctx.application.bot.send_message(chat_id=chat_id, text=msg)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _duration_str(start_dt, end_dt) -> str:
    try:
        mins = int((end_dt - start_dt).total_seconds() / 60)
        if mins < 60:
            return f"{mins}m"
        h = mins // 60
        m = mins % 60
        return f"{h}h {m}m" if m else f"{h}h"
    except Exception:
        return ""
