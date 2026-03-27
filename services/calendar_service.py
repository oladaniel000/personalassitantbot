"""
services/calendar_service.py
Full Google Calendar API v3 integration.
Handles OAuth flow, reading events, writing events, and offline sync queue.
"""

import json
import logging
from datetime import datetime, timedelta, date
from typing import Optional, List

import pytz
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI, GOOGLE_SCOPES

log = logging.getLogger(__name__)

# Gravity → Google Calendar colorId mapping
GRAVITY_COLOR = {"low": "2", "medium": "5", "high": "11"}
# Google Calendar: 2=Sage (green), 5=Banana (yellow), 11=Tomato (red)

CATEGORY_EMOJI = {"meeting": "🤝", "task": "✅", "habit": "🔁"}


def build_oauth_url() -> str:
    """
    Generate the Google OAuth authorisation URL.
    The user opens this URL, approves, then pastes the code back into the bot.
    """
    flow = _make_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return auth_url


def exchange_code_for_token(code: str) -> dict:
    """
    Exchange the user-pasted auth code for access + refresh tokens.
    Returns the token dict (JSON-serialisable).
    Raises ValueError if exchange fails.
    """
    flow = _make_flow()
    flow.fetch_token(code=code)
    creds = flow.credentials
    return _creds_to_dict(creds)


def get_credentials(token_dict: dict) -> Optional[Credentials]:
    """
    Build a Credentials object from the stored token dict.
    Automatically refreshes if expired.
    Returns None if token is invalid/missing.
    """
    if not token_dict:
        return None
    try:
        creds = Credentials(
            token=token_dict.get("token"),
            refresh_token=token_dict.get("refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            scopes=GOOGLE_SCOPES,
        )
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return creds
    except Exception as e:
        log.warning(f"Credential refresh failed: {e}")
        return None


def get_events_for_date(token_dict: dict, target_date: date, timezone: str) -> List[dict]:
    """
    Pull all calendar events for target_date.
    Returns a list of raw Google Calendar event dicts, sorted by start time.
    Returns [] on error.
    """
    creds = get_credentials(token_dict)
    if not creds:
        return []
    try:
        tz = pytz.timezone(timezone)
        day_start = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0)
        day_end   = datetime(target_date.year, target_date.month, target_date.day, 23, 59, 59)
        day_start = tz.localize(day_start).isoformat()
        day_end   = tz.localize(day_end).isoformat()

        service = build("calendar", "v3", credentials=creds)
        result = service.events().list(
            calendarId="primary",
            timeMin=day_start,
            timeMax=day_end,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        return result.get("items", [])
    except HttpError as e:
        log.warning(f"Google Calendar fetch error: {e}")
        return []


def push_event(token_dict: dict, event, timezone: str) -> Optional[str]:
    """
    Create or update an Event (ORM object) on Google Calendar.
    Returns the google_event_id on success, None on failure.
    """
    creds = get_credentials(token_dict)
    if not creds:
        return None
    try:
        service = build("calendar", "v3", credentials=creds)
        body = {
            "summary": event.title,
            "description": f"category:{event.category} gravity:{event.gravity}",
            "start": {"dateTime": event.start_dt.isoformat(), "timeZone": timezone},
            "end":   {"dateTime": event.end_dt.isoformat(),   "timeZone": timezone},
            "colorId": GRAVITY_COLOR.get(event.gravity, "5"),
        }
        if event.recur_rule:
            body["recurrence"] = [event.recur_rule]

        if event.google_event_id:
            service.events().update(
                calendarId="primary",
                eventId=event.google_event_id,
                body=body,
            ).execute()
            return event.google_event_id
        else:
            created = service.events().insert(
                calendarId="primary", body=body
            ).execute()
            return created["id"]
    except HttpError as e:
        log.warning(f"Google Calendar push error: {e}")
        return None


def delete_event(token_dict: dict, google_event_id: str) -> bool:
    """Delete an event from Google Calendar. Returns True on success."""
    creds = get_credentials(token_dict)
    if not creds or not google_event_id:
        return False
    try:
        service = build("calendar", "v3", credentials=creds)
        service.events().delete(calendarId="primary", eventId=google_event_id).execute()
        return True
    except HttpError:
        return False


def sync_pending_events(db, token_dict: dict, timezone: str) -> int:
    """
    Push all local events where is_synced=False to Google Calendar.
    Returns count of successfully synced events.
    Called every 5 minutes by the scheduler.
    """
    from database.models import Event
    unsynced = db.query(Event).filter(Event.is_synced == False).all()
    count = 0
    for ev in unsynced:
        gid = push_event(token_dict, ev, timezone)
        if gid:
            ev.google_event_id = gid
            ev.is_synced = True
            count += 1
    if count:
        db.commit()
    return count


def format_gcal_event_for_display(gcal_event: dict, timezone: str) -> dict:
    """
    Convert a raw Google Calendar event dict to a clean display dict.
    """
    tz = pytz.timezone(timezone)

    start_raw = gcal_event.get("start", {})
    if "dateTime" in start_raw:
        start_dt = datetime.fromisoformat(start_raw["dateTime"])
    else:
        # All-day event
        d = date.fromisoformat(start_raw["date"])
        start_dt = datetime(d.year, d.month, d.day, 0, 0, tzinfo=tz)

    end_raw = gcal_event.get("end", {})
    if "dateTime" in end_raw:
        end_dt = datetime.fromisoformat(end_raw["dateTime"])
    else:
        d = date.fromisoformat(end_raw["date"])
        end_dt = datetime(d.year, d.month, d.day, 23, 59, tzinfo=tz)

    # Localise to user timezone for display
    if start_dt.tzinfo:
        start_local = start_dt.astimezone(tz)
        end_local   = end_dt.astimezone(tz)
    else:
        start_local = tz.localize(start_dt)
        end_local   = tz.localize(end_dt)

    # Try to extract category/gravity from description field
    desc = gcal_event.get("description", "")
    category = "meeting"
    gravity  = "medium"
    for part in desc.split():
        if part.startswith("category:"):
            category = part.split(":")[1]
        if part.startswith("gravity:"):
            gravity = part.split(":")[1]

    return {
        "id":         gcal_event.get("id"),
        "title":      gcal_event.get("summary", "(No title)"),
        "start_dt":   start_local,
        "end_dt":     end_local,
        "start_str":  start_local.strftime("%H:%M"),
        "end_str":    end_local.strftime("%H:%M"),
        "category":   category,
        "gravity":    gravity,
        "is_all_day": "dateTime" not in start_raw,
    }


# ── Internal helpers ─────────────────────────────────────────────────────────

def _make_flow():
    client_config = {
        "installed": {
            "client_id":     GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uris": [GOOGLE_REDIRECT_URI],
            "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
            "token_uri":     "https://oauth2.googleapis.com/token",
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, scopes=GOOGLE_SCOPES)
    flow.redirect_uri = GOOGLE_REDIRECT_URI
    return flow


def _creds_to_dict(creds: Credentials) -> dict:
    return {
        "token":         creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri":     creds.token_uri,
        "client_id":     creds.client_id,
        "client_secret": creds.client_secret,
        "scopes":        creds.scopes,
    }
