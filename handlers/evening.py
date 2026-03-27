"""
handlers/evening.py
Builds and sends the evening recap.
Includes completion stats, reflective note, tomorrow's preview, and commute estimate.
"""

import json
import logging
from datetime import datetime, timedelta

import pytz

from database.db import get_db, get_or_create_user
from database.models import Event, Checkin
from services import calendar_service, weather_service, commute_service
from services.reminder_service import scheduler

log = logging.getLogger(__name__)


def schedule_evening_job(chat_id: str, evening_time: str, timezone: str, app) -> None:
    hour, minute = map(int, evening_time.split(":"))
    job_id = f"evening_{chat_id}"
    try:
        scheduler.add_job(
            send_evening_recap,
            trigger="cron",
            hour=hour,
            minute=minute,
            timezone=timezone,
            args=[chat_id, app],
            id=job_id,
            replace_existing=True,
        )
        log.info(f"Evening job scheduled for {chat_id} at {evening_time} {timezone}")
    except Exception as e:
        log.warning(f"Could not schedule evening job: {e}")


async def send_evening_recap(chat_id: str, app) -> None:
    db   = get_db()
    user = get_or_create_user(db, chat_id)

    if not user.setup_complete:
        db.close()
        return

    tz = pytz.timezone(user.timezone)
    today = datetime.now(tz).date()

    # ── Today's events ───────────────────────────────────────────────────────
    token_dict = json.loads(user.google_token) if user.google_token else {}
    gcal_today = calendar_service.get_events_for_date(token_dict, today, user.timezone)
    fmt_today  = [calendar_service.format_gcal_event_for_display(e, user.timezone) for e in gcal_today]

    # Local events
    local_today = []
    for ev in db.query(Event).filter(Event.user_id == user.id).all():
        ev_local = ev.start_dt.replace(tzinfo=pytz.utc).astimezone(tz)
        if ev_local.date() == today:
            local_today.append({
                "title":     ev.title,
                "start_str": ev_local.strftime("%H:%M"),
                "category":  ev.category,
                "completed": ev.completed,
                "note":      ev.completion_note,
            })

    # Checkins recorded today
    checkins = db.query(Checkin).filter(
        Checkin.user_id == user.id,
        Checkin.asked_at >= datetime(today.year, today.month, today.day),
    ).all()
    checkin_map = {c.event_id: c.response for c in checkins}

    # ── Tomorrow's preview ───────────────────────────────────────────────────
    tomorrow = today + timedelta(days=1)
    gcal_tmr = calendar_service.get_events_for_date(token_dict, tomorrow, user.timezone)
    fmt_tmr  = [calendar_service.format_gcal_event_for_display(e, user.timezone) for e in gcal_tmr]
    first_tmr = fmt_tmr[0] if fmt_tmr else None

    # ── Weather ──────────────────────────────────────────────────────────────
    weather = None
    if user.home_lat and user.home_lon:
        weather = weather_service.get_weather(user.home_lat, user.home_lon)

    # ── Commute ──────────────────────────────────────────────────────────────
    commute = None
    if user.home_lat and user.work_lat:
        commute = commute_service.get_commute_estimate(
            user.home_lat, user.home_lon,
            user.work_lat, user.work_lon,
        )

    db.close()

    # ── Build message ────────────────────────────────────────────────────────
    all_today = fmt_today + [
        e for e in local_today
        if not any(g["title"] == e["title"] for g in fmt_today)
    ]
    total    = len(all_today)
    done     = sum(1 for e in local_today if e.get("completed"))
    missed   = total - done
    pct      = (done / total * 100) if total > 0 else 0

    date_str = today.strftime("%A, %d %B %Y")

    lines = [f"🌙 *Evening Wrap-Up — {date_str}*\n"]

    # Wake time
    if user.wake_time_today:
        wake_local = user.wake_time_today.replace(tzinfo=pytz.utc).astimezone(tz)
        lines.append(f"⏰ You started your day at *{wake_local.strftime('%H:%M')}*.\n")

    # Completed events
    done_list = [e for e in local_today if e.get("completed")]
    if done_list:
        lines.append("✅ *Completed:*")
        for e in done_list:
            lines.append(f"  • {e['title']} _{e.get('start_str', '')}_")
        lines.append("")

    # Missed / rescheduled
    if missed > 0:
        lines.append("❌ *Missed / Not completed:*")
        for e in local_today:
            if not e.get("completed"):
                note = f" _(rescheduled)_" if e.get("note") == "rescheduled" else ""
                lines.append(f"  • {e['title']}{note}")
        lines.append("")

    # Reflective note
    lines.append("─" * 30)
    lines.append(_reflective_note(pct, user.name))
    lines.append("─" * 30 + "\n")

    # Tomorrow preview
    lines.append("☀️ *Tomorrow's Preview:*")
    if weather:
        lines.append(weather_service.format_weather_tomorrow(weather))
    if fmt_tmr:
        lines.append(f"\n📅 *{len(fmt_tmr)} event(s) scheduled:*")
        for e in fmt_tmr[:5]:  # Show first 5
            c = {"meeting": "🤝", "task": "✅", "habit": "🔁"}.get(e["category"], "📌")
            lines.append(f"  {c} {e['start_str']} — {e['title']}")
        if len(fmt_tmr) > 5:
            lines.append(f"  _...and {len(fmt_tmr) - 5} more_")
    else:
        lines.append("📭 No events scheduled for tomorrow yet.")

    # Commute
    if first_tmr:
        lines.append("")
        lines.append(commute_service.format_commute(commute, first_tmr["start_str"]))

    lines.append("\n🌟 Rest well! Tomorrow is another opportunity to win.")

    message = "\n".join(lines)
    try:
        await app.bot.send_message(
            chat_id=chat_id,
            text=message,
            parse_mode="Markdown",
        )
    except Exception as e:
        log.warning(f"Failed to send evening recap: {e}")


def _reflective_note(pct: float, name: str) -> str:
    n = name or "friend"
    if pct == 100:
        return f"🏆 *Outstanding, {n}!* You crushed every single item today. Seriously impressive."
    elif pct >= 75:
        return f"💪 *Solid day, {n}!* You got through almost everything — you're on a roll."
    elif pct >= 50:
        return f"📈 *Decent progress, {n}.* More than half done is still winning. Keep the momentum tomorrow."
    elif pct >= 25:
        return f"🌱 *Tough day, {n}, but you showed up.* Tomorrow is a fresh start — let's plan it well."
    else:
        return f"☁️ *Looks like today threw you some curveballs, {n}.* Rest up — tomorrow's a new page."


async def cmd_recap(update, ctx):
    chat_id = str(update.effective_chat.id)
    await update.message.reply_text("🌙 Preparing your evening recap…")
    await send_evening_recap(chat_id, ctx.application)
