"""
services/commute_service.py
Estimates commute duration using OpenRouteService free API.
Free tier: 2,000 requests/day — more than sufficient.
Get your free key at: https://openrouteservice.org/dev/#/signup
"""

import requests
from typing import Optional
from config import ORS_API_KEY


def get_commute_estimate(
    home_lat: float, home_lon: float,
    work_lat: float, work_lon: float,
) -> Optional[dict]:
    """
    Returns dict with keys:
      duration_min — estimated drive time in minutes (integer)
      distance_km  — distance in kilometres (float)
    Returns None if ORS_API_KEY is not set or request fails.
    """
    if not ORS_API_KEY:
        return None

    try:
        r = requests.post(
            "https://api.openrouteservice.org/v2/directions/driving-car",
            headers={
                "Authorization": ORS_API_KEY,
                "Content-Type": "application/json",
            },
            json={
                "coordinates": [
                    [home_lon, home_lat],   # ORS uses [lon, lat] order
                    [work_lon, work_lat],
                ]
            },
            timeout=12,
        )
        data = r.json()
        summary = data["routes"][0]["summary"]
        return {
            "duration_min": round(summary["duration"] / 60),
            "distance_km":  round(summary["distance"] / 1000, 1),
        }
    except Exception:
        return None


def format_commute(commute: Optional[dict], first_event_time_str: str = "") -> str:
    if not commute:
        return "🚗 Commute estimate unavailable (set ORS_API_KEY to enable)."
    msg = (
        f"🚗 Commute: ~{commute['duration_min']} min  •  {commute['distance_km']} km"
    )
    if first_event_time_str:
        msg += f"\n   📍 Leave early enough to arrive before {first_event_time_str}"
    return msg
