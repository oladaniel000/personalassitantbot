"""
services/weather_service.py
Fetches weather from Open-Meteo (free, no API key required).
Also handles geocoding via Nominatim (OpenStreetMap, free).
"""

import requests
from datetime import datetime
from typing import Optional

# ── WMO weather code descriptions ────────────────────────────────────────────
WMO_CODES = {
    0: "Clear sky ☀️",
    1: "Mainly clear 🌤",
    2: "Partly cloudy ⛅",
    3: "Overcast ☁️",
    45: "Foggy 🌫",
    48: "Icy fog 🌫",
    51: "Light drizzle 🌦",
    53: "Moderate drizzle 🌦",
    55: "Dense drizzle 🌧",
    61: "Slight rain 🌧",
    63: "Moderate rain 🌧",
    65: "Heavy rain 🌧",
    71: "Slight snow 🌨",
    73: "Moderate snow 🌨",
    75: "Heavy snow ❄️",
    80: "Rain showers 🌦",
    81: "Moderate showers 🌧",
    82: "Violent showers ⛈",
    95: "Thunderstorm ⛈",
    96: "Thunderstorm with hail ⛈",
    99: "Thunderstorm with heavy hail ⛈",
}


def describe_weather(code: int) -> str:
    return WMO_CODES.get(code, "Mixed conditions 🌤")


def geocode_address(address: str) -> Optional[tuple]:
    """
    Convert a human-readable address to (lat, lon) using Nominatim.
    Returns None if geocoding fails.
    """
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": address, "format": "json", "limit": 1},
            headers={"User-Agent": "TelegramDailyAssistantBot/1.0"},
            timeout=10,
        )
        data = r.json()
        if not data:
            return None
        return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        return None


def get_weather(lat: float, lon: float) -> Optional[dict]:
    """
    Fetch a 2-day weather forecast from Open-Meteo.
    Returns a dict with keys: today, tomorrow — each containing weather detail.
    Returns None on network error.
    """
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": (
                    "temperature_2m_max,temperature_2m_min,"
                    "precipitation_sum,weathercode,windspeed_10m_max,"
                    "precipitation_probability_max"
                ),
                "current_weather": True,
                "timezone": "auto",
                "forecast_days": 2,
            },
            timeout=10,
        )
        data = r.json()
        daily = data.get("daily", {})
        current = data.get("current_weather", {})

        def day_summary(idx: int) -> dict:
            return {
                "max_temp":    daily["temperature_2m_max"][idx],
                "min_temp":    daily["temperature_2m_min"][idx],
                "rain_mm":     daily["precipitation_sum"][idx],
                "rain_chance": daily["precipitation_probability_max"][idx],
                "wind_max":    daily["windspeed_10m_max"][idx],
                "code":        daily["weathercode"][idx],
                "description": describe_weather(daily["weathercode"][idx]),
                "current_temp": current.get("temperature"),
                "current_wind": current.get("windspeed"),
            }

        return {
            "today":    day_summary(0),
            "tomorrow": day_summary(1) if len(daily.get("weathercode", [])) > 1 else None,
        }
    except Exception:
        return None


def format_weather_today(w: dict) -> str:
    d = w["today"]
    return (
        f"🌡 {d['max_temp']}°C / {d['min_temp']}°C  •  {d['description']}\n"
        f"   💧 Rain: {d['rain_chance']}% chance ({d['rain_mm']} mm)  •  💨 Wind: {d['wind_max']} km/h"
    )


def format_weather_tomorrow(w: dict) -> str:
    if not w.get("tomorrow"):
        return "Tomorrow's forecast unavailable."
    d = w["tomorrow"]
    return (
        f"🌡 {d['max_temp']}°C / {d['min_temp']}°C  •  {d['description']}\n"
        f"   💧 Rain: {d['rain_chance']}% chance ({d['rain_mm']} mm)  •  💨 Wind: {d['wind_max']} km/h"
    )
