"""Weather, prayer/Hijri placeholders and MMS alert hooks."""
import requests
from config import TOMORROW_API_KEY, log, mvt_now, CORE_TEAM_CHAT_ID, ALERT_THREAD_ID
from publishing import send_text


def get_weather_summary(location="Male, Maldives"):
    if not TOMORROW_API_KEY:
        return "Weather API not configured."
    try:
        r = requests.get("https://api.tomorrow.io/v4/weather/realtime", params={"location": location, "apikey": TOMORROW_API_KEY}, timeout=20)
        if r.status_code != 200:
            return "Weather data unavailable."
        v = r.json().get("data", {}).get("values", {})
        return f"{location}: {v.get('temperature','?')}°C, wind {v.get('windSpeed','?')} m/s, humidity {v.get('humidity','?')}%."
    except Exception as e:
        log.warning(f"weather failed: {e}")
        return "Weather data unavailable."


def send_weather_update(kind="daily"):
    text = f"🌤️ <b>Samuga Weather Update</b>\n{get_weather_summary()}\n\n{mvt_now().strftime('%d %b %Y • %H:%M')}"
    send_text(CORE_TEAM_CHAT_ID, text, thread_id=ALERT_THREAD_ID)


def check_mms_alerts():
    # Placeholder hook: can be replaced with MMS scraping/API later.
    log.info("🌦 MMS alert check completed")
