"""
weather.py — Samuga AI Weather Module
Extracted from bot.py v7.0

Contains:
  - get_weather_data()          Tomorrow.io + Open-Meteo fallback
  - get_prayer_times()          Aladhan API prayer times for Malé
  - get_island_forecasts()      5 Maldivian island forecasts
  - generate_weather_card()     Full 2500x3050 Pillow weather card
  - weather_code_to_info()      WMO code → emoji + description
  - draw_weather_icon()         Already in cards.py — imported here
  - detect_weather_alert()      MMS alert level detection
  - send_weather_alert()        Post alert card to community
  - send_weather_update()       Main 3x daily weather post scheduler
  - Constants: ISLAND_LOCATIONS, HIJRI_SPECIAL_DAYS, ISLAMIC_REMINDERS, MMS_ALERT_LEVELS

Dependencies from bot.py (imported at bottom of bot.py's import block):
  utcnow, mvt_now, send_photo, send_text, queue_for_social,
  TELEGRAM_CHANNEL_ID, CORE_TEAM_CHAT_ID, ALERT_THREAD_ID, TOMORROW_API_KEY
"""

import os, io, logging, requests
from datetime import datetime, timedelta
from PIL import Image, ImageDraw, ImageFont, ImageFilter

log = logging.getLogger(__name__)

# ── Env vars this module needs ────────────────────────────────────────────────
TOMORROW_API_KEY  = os.environ.get("TOMORROW_API_KEY", "")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "@samugacommunity")
CORE_TEAM_CHAT_ID = os.environ.get("CORE_TEAM_CHAT_ID", "-1002829230299")
ALERT_THREAD_ID   = int(os.environ.get("ALERT_THREAD_ID", "10169"))

# ── These are injected by bot.py after import ─────────────────────────────────
# bot.py does: import weather; weather.send_photo = send_photo; etc.
# We declare them None here so the module loads without error.
send_photo      = None
send_text       = None
queue_for_social = None
utcnow          = None
mvt_now         = None

def posting_paused():
    return os.environ.get("POSTING_PAUSED", "false").lower() == "true"

def social_paused():
    return os.environ.get("SOCIAL_PAUSED", "false").lower() == "true" or posting_paused()


# ═══════════════════════════════════════════════════════════════════════════════
# Island locations
# ═══════════════════════════════════════════════════════════════════════════════
ISLAND_LOCATIONS = [
    {"name": "Malé",           "lat": 4.1755,  "lon": 73.5093},
    {"name": "Addu",           "lat": 0.6167,  "lon": 73.1000},
    {"name": "Kulhudhuffushi", "lat": 6.6226,  "lon": 73.0700},
    {"name": "Fuvahmulah",     "lat": -0.2985, "lon": 73.4236},
    {"name": "Dhidhdhoo",      "lat": 6.8833,  "lon": 73.1167},
]

# ═══════════════════════════════════════════════════════════════════════════════
# Hijri special days + Islamic reminders
# ═══════════════════════════════════════════════════════════════════════════════
HIJRI_SPECIAL_DAYS = {
    (1,  1):  ("Islamic New Year",     "Marks the Prophet ﷺ migration from Makkah to Madinah, the start of the Hijri calendar."),
    (1, 10):  ("Ashura",               "The day Allah saved Prophet Musa and his people from Pharaoh. Fasting today is a Sunnah that expiates the past year's minor sins."),
    (3, 12):  ("Mawlid al-Nabi",       "Commemorates the birth of Prophet Muhammad ﷺ, the mercy to all creation."),
    (7, 27):  ("Isra & Mi'raj",        "The miraculous night journey of the Prophet ﷺ from Makkah to Jerusalem and his ascension to the heavens."),
    (8, 15):  ("Shab-e-Barat",         "The night of forgiveness, when Allah descends and forgives those who seek His mercy."),
    (9,  1):  ("First of Ramadan",     "The blessed month of fasting begins — a time of mercy, forgiveness and closeness to Allah."),
    (9, 27):  ("Laylat al-Qadr",       "The Night of Power, better than a thousand months. The Quran was first revealed on this night."),
    (10, 1):  ("Eid al-Fitr",          "The festival of breaking the fast, celebrating the completion of Ramadan."),
    (12, 9):  ("Day of Arafah",        "The greatest day of Hajj. Fasting today expiates the sins of two years for non-pilgrims."),
    (12,10):  ("Eid al-Adha",          "The festival of sacrifice, honouring Prophet Ibrahim's devotion to Allah."),
    (12,18):  ("Eid al-Ghadir",        "A day of remembrance and reflection in the Islamic tradition."),
}

SPECIAL_DAY_DETAILS = {
    "Ashura":           "The day Allah saved Prophet Musa from Pharaoh. Fasting today is a Sunnah that expiates the past year's minor sins.",
    "Day of Arafah":    "The greatest day of Hajj. Fasting today expiates the sins of two years for non-pilgrims.",
    "Arafa":            "The greatest day of Hajj. Fasting today expiates the sins of two years for non-pilgrims.",
    "Lailat-ul-Qadr":   "The Night of Power, better than a thousand months. The Quran was first revealed tonight.",
    "Laylat al-Qadr":   "The Night of Power, better than a thousand months. The Quran was first revealed tonight.",
    "Ramadan":          "The blessed month of fasting — mercy, forgiveness and closeness to Allah.",
    "Eid-ul-Fitr":      "The festival of breaking the fast, celebrating the completion of Ramadan.",
    "Eid-ul-Adha":      "The festival of sacrifice, honouring Prophet Ibrahim's devotion to Allah.",
    "Mawlid al-Nabi ﷺ": "Commemorates the birth of Prophet Muhammad ﷺ, the mercy to all creation.",
    "Isra and Mi'raj":  "The night journey of the Prophet ﷺ and his ascension to the heavens.",
}

ISLAMIC_REMINDERS = [
    ("\"Indeed, Allah is with the patient.\"", "Quran 2:153"),
    ("\"So remember Me; I will remember you.\"", "Quran 2:152"),
    ("\"Verily, with hardship comes ease.\"", "Quran 94:6"),
    ("\"And He is with you wherever you are.\"", "Quran 57:4"),
    ("\"Allah does not burden a soul beyond what it can bear.\"", "Quran 2:286"),
    ("\"And whoever relies upon Allah — He is sufficient for him.\"", "Quran 65:3"),
    ("\"Do not despair of the mercy of Allah.\"", "Quran 39:53"),
    ("\"The best among you are those who learn the Quran and teach it.\"", "Bukhari"),
    ("\"None of you truly believes until he loves for his brother what he loves for himself.\"", "Bukhari & Muslim"),
    ("\"The strong believer is better and more beloved to Allah than the weak believer.\"", "Muslim"),
    ("\"Whoever believes in Allah and the Last Day should speak good or remain silent.\"", "Bukhari & Muslim"),
    ("\"Allah is beautiful and He loves beauty.\"", "Muslim"),
    ("\"A kind word is charity.\"", "Bukhari & Muslim"),
    ("\"The most beloved deeds to Allah are those done consistently, even if small.\"", "Bukhari & Muslim"),
    ("\"He who does not thank people has not thanked Allah.\"", "Abu Dawud, Tirmidhi"),
    ("\"Smiling at your brother is charity.\"", "Tirmidhi"),
    ("\"Make things easy, do not make things difficult.\"", "Bukhari & Muslim"),
    ("\"Whoever treads a path seeking knowledge, Allah eases his way to Paradise.\"", "Muslim"),
    ("\"The believer is not one who eats his fill while his neighbour is hungry.\"", "Al-Adab Al-Mufrad"),
    ("\"Fear Allah wherever you are, and follow a bad deed with a good one.\"", "Tirmidhi"),
    ("\"And speak to people good words.\"", "Quran 2:83"),
    ("\"Indeed, the patient will be given their reward without measure.\"", "Quran 39:10"),
    ("\"Call upon Me; I will respond to you.\"", "Quran 40:60"),
    ("\"Whoever is grateful — his gratitude is for his own good.\"", "Quran 31:12"),
    ("\"Cleanliness is half of faith.\"", "Muslim"),
    ("\"Richness is not having many possessions, but richness is contentment of the soul.\"", "Bukhari & Muslim"),
    ("\"Be in this world as if you were a stranger or a traveller.\"", "Bukhari"),
]

# ═══════════════════════════════════════════════════════════════════════════════
# WMO code helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _tomorrow_code_to_wmo(code):
    """Map Tomorrow.io weatherCode to nearest WMO code."""
    mapping = {
        1000: 0, 1100: 1, 1101: 2, 1102: 3, 1001: 3,
        2000: 45, 2100: 48,
        4000: 51, 4001: 61, 4200: 61, 4201: 65,
        6000: 51, 6001: 61, 6200: 51, 6201: 65,
        7000: 71, 7101: 77, 7102: 71,
        5000: 71, 5001: 73, 5100: 71, 5101: 75,
        8000: 95,
    }
    return mapping.get(code, 3)

def _wmo_to_tomorrow(wmo):
    """Reverse map WMO code to Tomorrow.io weatherCode (for island forecasts)."""
    if wmo == 0:            return 1000
    if wmo in [1, 2]:       return 1101
    if wmo == 3:            return 1001
    if wmo in [45, 48]:     return 2000
    if wmo in [51,53,55]:   return 4000
    if wmo in [61,63,65]:   return 4001
    if wmo in [71,73,75,77]: return 5000
    if wmo in [80,81,82]:   return 4001
    if wmo in [95,96,99]:   return 8000
    return 1000

def weather_code_to_info(code):
    """Convert WMO weather code to emoji + description."""
    if code == 0:              return "☀️", "Clear Sky"
    if code in [1, 2]:         return "🌤️", "Partly Cloudy"
    if code == 3:              return "☁️", "Overcast"
    if code in [45, 48]:       return "🌫️", "Foggy"
    if code in [51, 53, 55]:   return "🌦️", "Drizzle"
    if code in [61, 63, 65]:   return "🌧️", "Rain"
    if code in [71, 73, 75]:   return "🌨️", "Snow"
    if code in [80, 81, 82]:   return "🌧️", "Rain Showers"
    if code in [95, 96, 99]:   return "⛈️", "Thunderstorm"
    return "🌡️", "Unknown"


# ═══════════════════════════════════════════════════════════════════════════════
# Prayer times
# ═══════════════════════════════════════════════════════════════════════════════

def get_prayer_times():
    """Fetch prayer times for Malé via Aladhan API. Returns dict or None."""
    try:
        from datetime import timezone as _tz
        now_mvt = datetime.now(_tz.utc) + timedelta(hours=5)
        url = (f"https://api.aladhan.com/v1/timingsByCity"
               f"?city=Male&country=MV&method=4"
               f"&date={now_mvt.strftime('%d-%m-%Y')}")
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            timings = data.get("data", {}).get("timings", {})
            hijri  = data.get("data", {}).get("date", {}).get("hijri", {})
            return {
                "fajr":    timings.get("Fajr", ""),
                "dhuhr":   timings.get("Dhuhr", ""),
                "asr":     timings.get("Asr", ""),
                "maghrib": timings.get("Maghrib", ""),
                "isha":    timings.get("Isha", ""),
                "hijri":   hijri,
            }
    except Exception as e:
        log.error(f"Prayer times: {e}")
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Weather data — Tomorrow.io primary, Open-Meteo fallback
# ═══════════════════════════════════════════════════════════════════════════════

def get_weather_data():
    """
    Fetch current weather for Malé.
    Primary: Tomorrow.io (richer data — UV, wind gusts, visibility).
    Fallback: Open-Meteo (free, no key needed).
    Returns normalised dict or None.
    """
    if TOMORROW_API_KEY:
        try:
            lat, lon = 4.1755, 73.5093
            fields_rt = ["temperature","weatherCode","windSpeed","windGust",
                         "humidity","apparentTemperature","uvIndex",
                         "visibility","dewPoint","pressureSurfaceLevel",
                         "precipitationProbability"]
            fields_fc = ["temperature","weatherCode","windSpeed"]

            rt = requests.get(
                "https://api.tomorrow.io/v4/weather/realtime",
                params={"location": f"{lat},{lon}",
                        "fields": ",".join(fields_rt),
                        "units": "metric",
                        "apikey": TOMORROW_API_KEY},
                timeout=12)
            fc = requests.get(
                "https://api.tomorrow.io/v4/weather/forecast",
                params={"location": f"{lat},{lon}",
                        "fields": ",".join(fields_fc),
                        "timesteps": "1h,1d",
                        "units": "metric",
                        "apikey": TOMORROW_API_KEY},
                timeout=12)

            if rt.status_code == 200 and fc.status_code == 200:
                rv = rt.json().get("data", {}).get("values", {})
                fd = fc.json()
                wmo = _tomorrow_code_to_wmo(rv.get("weatherCode", 1000))

                current = {
                    "temperature_2m":       rv.get("temperature", 29),
                    "apparent_temperature": rv.get("apparentTemperature", 29),
                    "weathercode":          wmo,
                    "windspeed_10m":        rv.get("windSpeed", 0),
                    "windgust_10m":         rv.get("windGust", 0),
                    "relativehumidity_2m":  rv.get("humidity", 80),
                    "uv_index":             rv.get("uvIndex", 0),
                    "visibility":           rv.get("visibility", 10),
                    "dewpoint_2m":          rv.get("dewPoint", 25),
                    "pressure_msl":         rv.get("pressureSurfaceLevel", 1010),
                    "precipitation_prob":   rv.get("precipitationProbability", 0),
                    "_source":              "Tomorrow.io",
                }

                # Hourly forecast
                hourly_times, hourly_t, hourly_wmo, hourly_precip = [], [], [], []
                for h in fd.get("timelines", {}).get("hourly", [])[:12]:
                    hv = h.get("values", {})
                    hourly_times.append(h.get("time", ""))
                    hourly_t.append(hv.get("temperature", 29))
                    hourly_wmo.append(_tomorrow_code_to_wmo(hv.get("weatherCode", 1000)))
                    hourly_precip.append(hv.get("precipitationProbability", 0))

                hourly = {
                    "time":                     hourly_times,
                    "temperature_2m":           hourly_t,
                    "weathercode":              hourly_wmo,
                    "precipitation_probability": hourly_precip,
                }

                # Daily H/L + sunrise/sunset
                daily_max, daily_min, daily_wmo = [], [], []
                sunrise_str, sunset_str = "06:00", "18:00"
                for day in fd.get("timelines", {}).get("daily", [])[:1]:
                    v = day.get("values", {})
                    daily_max.append(v.get("temperatureMax", 32))
                    daily_min.append(v.get("temperatureMin", 26))
                    daily_wmo.append(_tomorrow_code_to_wmo(v.get("weatherCodeMax", 1000)))
                    for key, default, out in [
                        ("sunriseTime", "06:00", "sunrise_str"),
                        ("sunsetTime",  "18:00", "sunset_str"),
                    ]:
                        val = v.get(key, "")
                        if val:
                            try:
                                dt_utc = datetime.fromisoformat(val.replace("Z", "+00:00"))
                                result = (dt_utc + timedelta(hours=5)).strftime("%H:%M")
                            except Exception:
                                result = val[11:16]
                            if key == "sunriseTime": sunrise_str = result
                            else:                    sunset_str  = result

                daily = {
                    "temperature_2m_max": daily_max or [32],
                    "temperature_2m_min": daily_min or [26],
                    "weathercode":        daily_wmo or [wmo],
                    "sunrise":            [f"2026-01-01T{sunrise_str}"],
                    "sunset":             [f"2026-01-01T{sunset_str}"],
                }

                log.info(f"🌤️ Tomorrow.io: {current['temperature_2m']:.1f}°C, "
                         f"UV={current['uv_index']}, wind={current['windspeed_10m']}km/h")
                return {"current": current, "hourly": hourly, "daily": daily,
                        "_source": "Tomorrow.io"}
            else:
                log.warning(f"Tomorrow.io HTTP rt={rt.status_code} fc={fc.status_code} — falling back")
        except Exception as e:
            log.error(f"Tomorrow.io weather: {e} — falling back to Open-Meteo")

    # ── Fallback: Open-Meteo ──────────────────────────────────────────────────
    try:
        url = ("https://api.open-meteo.com/v1/forecast"
               "?latitude=4.1755&longitude=73.5093"
               "&current=temperature_2m,weathercode,windspeed_10m,relativehumidity_2m,apparent_temperature"
               "&hourly=temperature_2m,weathercode,precipitation_probability"
               "&daily=temperature_2m_max,temperature_2m_min,sunrise,sunset,weathercode"
               "&timezone=Indian%2FMaldives&forecast_days=1")
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            data["_source"] = "Open-Meteo"
            c = data.get("current", {})
            c["uv_index"]           = 0
            c["visibility"]         = 10
            c["windgust_10m"]       = 0
            c["dewpoint_2m"]        = 25
            c["pressure_msl"]       = 1010
            c["precipitation_prob"] = 0
            c["_source"]            = "Open-Meteo"
            log.info(f"🌤️ Open-Meteo fallback: {c.get('temperature_2m', 29):.1f}°C")
            return data
    except Exception as e:
        log.error(f"Open-Meteo fallback: {e}")
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Island forecasts
# ═══════════════════════════════════════════════════════════════════════════════


def generate_outlook(hourly_slots, mvt_now_dt):
    """
    Convert the next 12 hours into a short island outlook line.
    Supports Tomorrow.io hourly slots and simple normalized fallback slots.
    """
    SEVERITY = {
        95:5, 96:5, 99:5,
        65:4, 82:4,
        63:3, 61:3, 80:3, 81:3,
        51:2, 53:2, 55:2,
        45:1, 48:1,
        3:0, 2:0, 1:0, 0:0,
    }

    def sev(code): return SEVERITY.get(int(code or 0), 0)

    def label(code):
        code = int(code or 0)
        if code in [95,96,99]: return "Thunderstorms"
        if code in [65,82]: return "Heavy showers"
        if code in [63,61,80,81]: return "Rain showers"
        if code in [51,53,55]: return "Light rain"
        if code in [45,48]: return "Foggy conditions"
        if code == 3: return "Cloudy skies"
        if code in [1,2]: return "Partly cloudy"
        return "Sunny"

    entries = []
    for slot in (hourly_slots or [])[:12]:
        try:
            if isinstance(slot, dict) and "values" in slot:
                dt = datetime.fromisoformat(str(slot.get("time","")).replace("Z","+00:00")) + timedelta(hours=5)
                vals = slot.get("values", {})
                code = _tomorrow_code_to_wmo(vals.get("weatherCode", 1000))
                precip = int(vals.get("precipitationProbability", 0) or 0)
            else:
                dt = slot.get("dt")
                code = int(slot.get("code", 0) or 0)
                precip = int(slot.get("precip", 0) or 0)
            hour = dt.hour if hasattr(dt, "hour") else mvt_now_dt.hour
            entries.append((hour, code, precip))
        except Exception:
            continue

    if not entries:
        return "Weather data unavailable"

    worst_h, worst_code, worst_precip = max(entries, key=lambda e: (sev(e[1]), e[2]))
    if sev(worst_code) == 0:
        codes = [e[1] for e in entries]
        if all(c == 0 for c in codes): return "Sunny all day"
        if all(c in [0,1,2] for c in codes): return "Sunny with some clouds"
        if all(c in [0,1,2,3] for c in codes): return "Mostly cloudy"
        return "Partly cloudy"

    desc = label(worst_code)
    if worst_h < 6: time_hint = "overnight"
    elif worst_h < 9: time_hint = "early morning"
    elif worst_h < 12: time_hint = "this morning"
    elif worst_h < 15: time_hint = "this afternoon"
    elif worst_h < 18: time_hint = f"after {worst_h - 12} PM"
    elif worst_h < 21: time_hint = "this evening"
    else: time_hint = "tonight"
    return f"{desc} {time_hint}"


def _island_openmeteo_fallback(island, mvt_now_dt):
    """Open-Meteo fallback for a single island with simple 12-hour outlook."""
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={island['lat']}&longitude={island['lon']}"
            f"&current=temperature_2m,weathercode,windspeed_10m"
            f"&hourly=weathercode,precipitation_probability"
            f"&timezone=Indian%2FMaldives&forecast_days=1"
        )
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            j = resp.json()
            c = j.get("current", {})
            hourly = j.get("hourly", {})
            times = hourly.get("time", [])[:12]
            codes = hourly.get("weathercode", [])[:12]
            precips = hourly.get("precipitation_probability", [])[:12]
            slots = []
            for t, code, precip in zip(times, codes, precips):
                try:
                    dt = datetime.fromisoformat(str(t))
                except Exception:
                    dt = mvt_now_dt
                slots.append({"dt": dt, "code": int(code or 0), "precip": int(precip or 0)})
            return {
                "name": island["name"],
                "temp": round(c.get("temperature_2m", 29)),
                "wmo": int(c.get("weathercode", 3) or 3),
                "wind": round(c.get("windspeed_10m", 0)),
                "source": "Open-Meteo",
                "outlook": generate_outlook(slots, mvt_now_dt),
            }
    except Exception as e:
        log.debug(f"Island fallback {island['name']}: {e}")
    return {
        "name": island["name"],
        "temp": 29,
        "wmo": 3,
        "wind": 0,
        "source": "Fallback",
        "outlook": "Weather data unavailable",
    }

def get_island_forecasts():
    """
    Fetch current conditions for all 5 islands.
    Primary: Tomorrow.io realtime + forecast.
    Fallback: Open-Meteo with hourly outlook.
    Returns list of dicts with keys: name, temp, wmo, wind, source, outlook
    """
    results = []
    from datetime import timezone as _tz
    mvt_now_dt = datetime.now(_tz.utc) + timedelta(hours=5)

    for island in ISLAND_LOCATIONS:
        if not TOMORROW_API_KEY:
            results.append(_island_openmeteo_fallback(island, mvt_now_dt))
            continue
        try:
            base = "https://api.tomorrow.io/v4/weather"
            params = {"location": f"{island['lat']},{island['lon']}", "units": "metric", "apikey": TOMORROW_API_KEY}

            rt = requests.get(
                f"{base}/realtime",
                params={**params, "fields": "temperature,weatherCode,windSpeed"},
                timeout=12
            )
            fc = requests.get(
                f"{base}/forecast",
                params={**params, "fields": "temperature,weatherCode,windSpeed,precipitationProbability", "timesteps": "1h"},
                timeout=12
            )

            if rt.status_code != 200 or fc.status_code != 200:
                log.warning(f"Island {island['name']}: Tomorrow.io {rt.status_code}/{fc.status_code} — Open-Meteo fallback")
                results.append(_island_openmeteo_fallback(island, mvt_now_dt))
                continue

            rv = rt.json().get("data", {}).get("values", {})
            fd = fc.json()
            outlook = generate_outlook((fd.get("timelines", {}) or {}).get("hourly", [])[:12], mvt_now_dt)

            results.append({
                "name": island["name"],
                "temp": round(rv.get("temperature", 29)),
                "wmo": _tomorrow_code_to_wmo(rv.get("weatherCode", 1000)),
                "wind": round(rv.get("windSpeed", 0)),
                "source": "Tomorrow.io",
                "outlook": outlook,
            })
            log.info(f"🏝️ {island['name']}: {round(rv.get('temperature',29))}°C — {outlook}")
        except Exception as e:
            log.warning(f"Island forecast {island['name']}: {e} — Open-Meteo fallback")
            results.append(_island_openmeteo_fallback(island, mvt_now_dt))

    ordered = []
    by_name = {r["name"]: r for r in results if r}
    for isl in ISLAND_LOCATIONS:
        if isl["name"] in by_name:
            ordered.append(by_name[isl["name"]])
    log.info(f"🏝️ Island forecasts: {len(ordered)}/{len(ISLAND_LOCATIONS)} fetched")
    return ordered if ordered else None


# ═══════════════════════════════════════════════════════════════════════════════
# Weather card generator — full Samuga branded card

# ═══════════════════════════════════════════════════════════════════════════════

def generate_weather_card(weather_data, alert_mode=False, alert_text="",
                          island_data=None, prayer_data=None, alert_level=None):
    """Samuga branded weather card — 2500x3000, cinematic, sea conditions, prayer times, Hijri, MMS alerts."""
    from cards import draw_weather_icon

    W, H = 2500, (3050 if island_data else 2300)
    img  = Image.new("RGB", (W, H), (0, 0, 0))
    draw = ImageDraw.Draw(img, "RGBA")

    # MMS alert level colors
    ALERT_COLORS = {
        "white":  (220, 220, 220),
        "yellow": (255, 200, 0),
        "orange": (255, 120, 0),
        "red":    (220, 40, 40),
    }
    accent = ALERT_COLORS.get(alert_level, (41, 171, 226)) if alert_mode else (41, 171, 226)

    # ── Background gradient ───────────────────────────────────────────────────
    for y in range(H):
        t   = y / H
        r   = int(5  + (15  - 5)  * t)
        g   = int(20 + (40  - 20) * t)
        b   = int(60 + (100 - 60) * t)
        draw.line([(0, y), (W, y)], fill=(r, g, b))

    # Top accent bar
    draw.rectangle([(0, 0), (W, 8)], fill=accent)

    # ── Font loading ──────────────────────────────────────────────────────────
    def _font(path, size):
        try:    return ImageFont.truetype(path, size)
        except: return ImageFont.load_default()

    BOLD   = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    REG    = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    f_huge  = _font(BOLD, 220)
    f_large = _font(BOLD, 90)
    f_med   = _font(BOLD, 70)
    f_reg   = _font(REG,  60)
    f_sm    = _font(REG,  50)
    f_xs    = _font(REG,  42)
    f_tag   = _font(BOLD, 55)

    # ── Logo ──────────────────────────────────────────────────────────────────
    try:
        logo = Image.open("logo.png").convert("RGBA")
        lh   = 130; lw = int(logo.width * lh / logo.height)
        logo = logo.resize((lw, lh), Image.LANCZOS)
        img.paste(logo, (80, 60), logo)
    except Exception as e:
        log.debug(f"Weather card logo: {e}")

    draw.text((W - 600, 80), "t.me/samugacommunity", font=f_sm, fill=(180, 210, 255))

    # ── Main weather data ─────────────────────────────────────────────────────
    current   = weather_data.get("current", {})
    daily     = weather_data.get("daily", {})
    hourly    = weather_data.get("hourly", {})
    source    = weather_data.get("_source", "Open-Meteo")

    temp      = round(current.get("temperature_2m", 29))
    feels     = round(current.get("apparent_temperature", temp))
    wcode     = current.get("weathercode", 0)
    wind      = round(current.get("windspeed_10m", 0))
    gusts     = round(current.get("windgust_10m", 0))
    humidity  = round(current.get("relativehumidity_2m", 80))
    uv        = round(current.get("uv_index", 0))
    precip_p  = round(current.get("precipitation_prob", 0))
    vis       = round(current.get("visibility", 10))

    temp_max  = round(daily.get("temperature_2m_max", [32])[0])
    temp_min  = round(daily.get("temperature_2m_min", [26])[0])

    emoji, condition = weather_code_to_info(wcode)

    # Sunrise / sunset
    sunrise_str, sunset_str = "06:00", "18:00"
    try:
        sr = daily.get("sunrise", [""])[0]
        ss = daily.get("sunset",  [""])[0]
        if "T" in str(sr): sunrise_str = str(sr).split("T")[1][:5]
        if "T" in str(ss): sunset_str  = str(ss).split("T")[1][:5]
    except Exception:
        pass

    from datetime import timezone as _tz
    mvt_now_dt  = datetime.now(_tz.utc) + timedelta(hours=5)
    date_str    = mvt_now_dt.strftime("%A, %d %B %Y")
    time_str    = mvt_now_dt.strftime("%H:%M") + " MVT"

    # ── Date / time header ────────────────────────────────────────────────────
    y_pos = 230
    draw.text((80, y_pos), date_str, font=f_med, fill=(180, 210, 255))
    y_pos += 85
    draw.text((80, y_pos), time_str, font=f_reg, fill=(140, 180, 230))
    y_pos += 100

    # ── Hijri date + Islamic reminder / special day ───────────────────────────
    if prayer_data and prayer_data.get("hijri"):
        hijri   = prayer_data["hijri"]
        h_day   = int(hijri.get("day", 0))
        h_month = int(hijri.get("month", {}).get("number", 0))
        h_year  = hijri.get("year", "")
        h_month_name = hijri.get("month", {}).get("en", "")
        hijri_str    = f"{h_day} {h_month_name} {h_year} AH"
        draw.text((80, y_pos), hijri_str, font=f_xs, fill=(160, 200, 255, 200))
        y_pos += 65

        special = HIJRI_SPECIAL_DAYS.get((h_month, h_day))
        if not special:
            # Check API-returned holiday name
            holidays = hijri.get("holidays", [])
            if holidays:
                hname = holidays[0]
                detail = SPECIAL_DAY_DETAILS.get(hname, "")
                special = (hname, detail)
        if special:
            sname, sdesc = special
            draw.text((80, y_pos), f"✨ {sname}", font=f_sm, fill=(255, 215, 80))
            y_pos += 60
            if sdesc:
                # Wrap description
                words = sdesc.split()
                line, lines = "", []
                for w in words:
                    test = (line + " " + w).strip()
                    if draw.textbbox((0,0), test, font=f_xs)[2] < W - 200:
                        line = test
                    else:
                        if line: lines.append(line)
                        line = w
                if line: lines.append(line)
                for dl in lines[:2]:
                    draw.text((80, y_pos), dl, font=f_xs, fill=(220, 200, 140))
                    y_pos += 52
        else:
            # Rotating daily Islamic reminder
            day_idx = mvt_now_dt.timetuple().tm_yday % len(ISLAMIC_REMINDERS)
            reminder, source_ref = ISLAMIC_REMINDERS[day_idx]
            # Wrap reminder text
            words = reminder.split()
            line, lines = "", []
            for w in words:
                test = (line + " " + w).strip()
                if draw.textbbox((0,0), test, font=f_xs)[2] < W - 200:
                    line = test
                else:
                    if line: lines.append(line)
                    line = w
            if line: lines.append(line)
            for rl in lines[:2]:
                draw.text((80, y_pos), rl, font=f_xs, fill=(200, 185, 140))
                y_pos += 52
            draw.text((80, y_pos), f"— {source_ref}", font=f_xs, fill=(160, 150, 110))
            y_pos += 55

    y_pos += 30

    # ── Divider ───────────────────────────────────────────────────────────────
    draw.rectangle([(80, y_pos), (W - 80, y_pos + 2)], fill=(255, 255, 255, 40))
    y_pos += 40

    # ── MMS Alert banner ──────────────────────────────────────────────────────
    if alert_mode and alert_text:
        alert_cfg = MMS_ALERT_LEVELS.get(alert_level or "white", MMS_ALERT_LEVELS["white"])
        banner_color = ALERT_COLORS.get(alert_level, (220, 220, 220))
        text_color   = (0, 0, 0) if alert_level in ("white", "yellow") else (255, 255, 255)
        draw.rectangle([(80, y_pos), (W - 80, y_pos + 90)], fill=banner_color)
        draw.text((110, y_pos + 18),
                  f"{alert_cfg['emoji']} {alert_cfg['label'].upper()} — {alert_cfg['headline']}",
                  font=f_tag, fill=text_color)
        y_pos += 110
        words = alert_text.split()
        line, lines = "", []
        for w in words:
            test = (line + " " + w).strip()
            if draw.textbbox((0,0), test, font=f_reg)[2] < W - 200:
                line = test
            else:
                if line: lines.append(line)
                line = w
        if line: lines.append(line)
        for al in lines[:4]:
            draw.text((80, y_pos), al, font=f_reg, fill=(255, 230, 180))
            y_pos += 70
        y_pos += 20

    # ── Weather icon + big temp ───────────────────────────────────────────────
    draw_weather_icon(draw, wcode, 280, y_pos + 160, size=220)
    draw.text((560, y_pos + 50), f"{temp}°C", font=f_huge, fill=(255, 255, 255))
    draw.text((560, y_pos + 280), condition, font=f_large, fill=(180, 215, 255))
    draw.text((560, y_pos + 380), f"Feels like {feels}°C", font=f_reg, fill=(140, 180, 230))
    y_pos += 500

    # H/L
    draw.text((80, y_pos), f"H: {temp_max}°  L: {temp_min}°", font=f_med, fill=(200, 225, 255))
    y_pos += 110

    # ── Stats row ─────────────────────────────────────────────────────────────
    draw.rectangle([(80, y_pos), (W - 80, y_pos + 2)], fill=(255, 255, 255, 40))
    y_pos += 30

    stats = [
        (f"💧 {humidity}%",   "Humidity"),
        (f"💨 {wind} km/h",   "Wind"),
        (f"💥 {gusts} km/h",  "Gusts"),
        (f"☀️ UV {uv}",       "UV Index"),
        (f"☔ {precip_p}%",   "Rain Chance"),
        (f"👁 {vis} km",      "Visibility"),
    ]
    col_w = (W - 160) // 3
    for i, (val, label) in enumerate(stats):
        cx = 80 + (i % 3) * col_w
        cy = y_pos + (i // 3) * 130
        draw.text((cx, cy),      val,   font=f_med, fill=(255, 255, 255))
        draw.text((cx, cy + 75), label, font=f_xs,  fill=(140, 180, 230))
    y_pos += 300

    # ── Sunrise / Sunset ──────────────────────────────────────────────────────
    draw.rectangle([(80, y_pos), (W - 80, y_pos + 2)], fill=(255, 255, 255, 40))
    y_pos += 30
    draw.text((80,      y_pos), f"🌅 Sunrise  {sunrise_str}", font=f_reg, fill=(255, 200, 100))
    draw.text((W // 2,  y_pos), f"🌇 Sunset  {sunset_str}",  font=f_reg, fill=(255, 160, 80))
    y_pos += 100

    # ── Prayer times ──────────────────────────────────────────────────────────
    if prayer_data:
        draw.rectangle([(80, y_pos), (W - 80, y_pos + 2)], fill=(255, 255, 255, 40))
        y_pos += 30
        draw.text((80, y_pos), "🕌 Prayer Times — Malé", font=f_med, fill=(200, 225, 255))
        y_pos += 90
        prayers = [
            ("Fajr",    prayer_data.get("fajr",    "")),
            ("Dhuhr",   prayer_data.get("dhuhr",   "")),
            ("Asr",     prayer_data.get("asr",     "")),
            ("Maghrib", prayer_data.get("maghrib", "")),
            ("Isha",    prayer_data.get("isha",    "")),
        ]
        col_w2 = (W - 160) // 3
        for i, (name, time_val) in enumerate(prayers):
            cx = 80 + (i % 3) * col_w2
            cy = y_pos + (i // 3) * 120
            draw.text((cx, cy),       name,     font=f_sm,  fill=(160, 200, 255))
            draw.text((cx, cy + 55),  time_val, font=f_med, fill=(255, 255, 255))
        y_pos += 280

    # ── Island watch ─────────────────────────────────────────────────────────
    if island_data:
        draw.rectangle([(80, y_pos), (W - 80, y_pos + 2)], fill=(255, 255, 255, 40))
        y_pos += 30
        draw.text((80, y_pos), "🏝️ Island Watch", font=f_med, fill=(200, 225, 255))
        y_pos += 90
        col_w3 = (W - 160) // 3
        for i, isl in enumerate(island_data[:6]):
            cx  = 80 + (i % 3) * col_w3
            cy  = y_pos + (i // 3) * 160
            ie, _ = weather_code_to_info(isl.get("wmo", 0))
            draw.text((cx, cy),        isl["name"],                    font=f_sm,  fill=(160, 200, 255))
            draw.text((cx, cy + 55),   f"{ie} {isl.get('temp',29)}°C", font=f_med, fill=(255, 255, 255))
            draw.text((cx, cy + 115),  f"💨 {isl.get('wind',0)} km/h", font=f_xs,  fill=(140, 180, 230))
        y_pos += 360

    # ── Footer ────────────────────────────────────────────────────────────────
    draw.rectangle([(0, H - 100), (W, H)], fill=(3, 12, 30))
    draw.rectangle([(0, H - 100), (W, H - 97)], fill=accent)
    draw.text((80, H - 72), "📡 Samuga Media  |  @samugacommunity", font=f_sm, fill=(180, 210, 255))
    src_tag = f"Data: {source}"
    draw.text((W - 500, H - 72), src_tag, font=f_xs, fill=(120, 160, 210))

    buf = io.BytesIO()
    img.save(buf, format="PNG", quality=95)
    buf.seek(0)
    return buf


# ═══════════════════════════════════════════════════════════════════════════════
# MMS Alert system
# ═══════════════════════════════════════════════════════════════════════════════

weather_alerts_today = {"date": None, "count": 0}

MMS_ALERT_LEVELS = {
    "white": {
        "emoji": "⚪",
        "label": "White Alert",
        "headline": "Strong Winds & Rough Seas",
        "wind_min": 28, "gust_min": 45,
        "color": (220, 220, 220),
    },
    "yellow": {
        "emoji": "🟡",
        "label": "Yellow Alert",
        "headline": "Thunderstorms & Rough Seas",
        "wind_min": 38, "gust_min": 60,
        "color": (255, 200, 0),
    },
    "orange": {
        "emoji": "🟠",
        "label": "Orange Alert",
        "headline": "Severe Winds & Very Rough Seas",
        "wind_min": 50, "gust_min": 75,
        "color": (255, 120, 0),
    },
    "red": {
        "emoji": "🔴",
        "label": "Red Alert",
        "headline": "Dangerous Storm Conditions",
        "wind_min": 70, "gust_min": 95,
        "color": (220, 40, 40),
    },
}

def can_send_weather_alert():
    """Max 2 MMS alerts per day."""
    from datetime import timezone as _tz
    global weather_alerts_today
    today = (datetime.now(_tz.utc) + timedelta(hours=5)).strftime("%Y-%m-%d")
    if weather_alerts_today["date"] != today:
        weather_alerts_today = {"date": today, "count": 0}
    return weather_alerts_today["count"] < 2

def detect_weather_alert(weather_data):
    """
    Check current weather against MMS thresholds.
    Returns (should_alert: bool, level_key: str, alert_text: str).
    """
    if not weather_data:
        return False, None, ""
    current = weather_data.get("current", {})
    wind    = current.get("windspeed_10m", 0)
    gusts   = current.get("windgust_10m", 0)
    wcode   = current.get("weathercode", 0)

    level_key  = None
    for key in ["red", "orange", "yellow", "white"]:
        cfg = MMS_ALERT_LEVELS[key]
        if wind >= cfg["wind_min"] or gusts >= cfg["gust_min"]:
            level_key = key
            break

    if not level_key:
        return False, None, ""

    cfg        = MMS_ALERT_LEVELS[level_key]
    storm_word = "thunderstorms, " if wcode in [95, 96, 99] else ""
    alert_text = (
        f"{storm_word.capitalize()}Strong winds and rough seas expected over Malé. "
        f"Wind {round(wind)} km/h, gusts {round(gusts)} km/h. "
        f"{cfg['headline']}."
    )
    return True, level_key, alert_text

def send_weather_alert(weather_data, level_key, alert_text):
    """Post an MMS alert card to Telegram community + notify core team."""
    global weather_alerts_today
    if not can_send_weather_alert():
        log.info("⚠️ Weather alert limit reached (2/day)")
        return
    try:
        islands     = get_island_forecasts()
        prayer_info = get_prayer_times()
        card = generate_weather_card(
            weather_data,
            alert_mode=True,
            alert_text=alert_text,
            alert_level=level_key,
            island_data=islands,
            prayer_data=prayer_info,
        )
        cfg     = MMS_ALERT_LEVELS[level_key]
        caption = (
            f"{cfg['emoji']} <b>{cfg['label'].upper()} — {cfg['headline']}</b>\n\n"
            f"{alert_text}\n\n"
            f"📡 <b>Samuga Media</b> | @samugacommunity"
        )
        if send_photo and not posting_paused():
            send_photo(TELEGRAM_CHANNEL_ID, card, caption)
        elif posting_paused():
            log.warning("🛑 Weather alert public post blocked — POSTING_PAUSED=true")

        from datetime import timezone as _tz
        today = (datetime.now(_tz.utc) + timedelta(hours=5)).strftime("%Y-%m-%d")
        if weather_alerts_today["date"] != today:
            weather_alerts_today = {"date": today, "count": 0}
        weather_alerts_today["count"] += 1

        if send_text:
            send_text(CORE_TEAM_CHAT_ID,
                f"{cfg['emoji']} <b>MMS Alert sent to community</b>\n"
                f"Level: {cfg['label']} | {alert_text[:120]}",
                thread_id=ALERT_THREAD_ID)
        log.info(f"⚠️ MMS {level_key} alert posted")
    except Exception as e:
        log.error(f"send_weather_alert: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# Main weather update — called 3x daily by scheduler
# ═══════════════════════════════════════════════════════════════════════════════

def send_weather_update(time_of_day="morning"):
    """Send weather card to all platforms. Called at 8AM, 2PM, 10:30PM MVT."""
    log.info(f"🌤️ Weather update ({time_of_day})...")
    try:
        data = get_weather_data()
        if not data:
            log.error("Weather: no data available")
            return

        islands     = get_island_forecasts()
        prayer_info = get_prayer_times()

        greetings = {
            "morning":   "🌅 Good Morning Maldives!",
            "afternoon": "☀️ Afternoon Weather Update",
            "evening":   "🌙 Evening Weather — Malé",
        }
        greeting = greetings.get(time_of_day, "🌤️ Weather Update")

        card = generate_weather_card(
            data,
            island_data=islands,
            prayer_data=prayer_info,
        )

        current  = data.get("current", {})
        daily    = data.get("daily", {})
        source   = data.get("_source", "Open-Meteo")

        temp      = round(current.get("temperature_2m", 29))
        feels     = round(current.get("apparent_temperature", temp))
        temp_max  = round(daily.get("temperature_2m_max", [32])[0])
        temp_min  = round(daily.get("temperature_2m_min", [26])[0])
        wind      = round(current.get("windspeed_10m", 0))
        humidity  = round(current.get("relativehumidity_2m", 80))
        uv        = round(current.get("uv_index", 0))
        precip_p  = round(current.get("precipitation_prob", 0))
        wcode     = current.get("weathercode", 0)
        emoji, condition = weather_code_to_info(wcode)

        # Sea conditions
        if wind >= 40:   sea_line = "🌊 Sea: Very Rough — Avoid sea travel\n\n"
        elif wind >= 25: sea_line = "🌊 Sea: Rough — Exercise caution\n\n"
        elif wind >= 15: sea_line = "🌊 Sea: Moderate\n\n"
        else:            sea_line = "🌊 Sea: Calm\n\n"

        sunrise_str, sunset_str = "06:00", "18:00"
        try:
            sr = daily.get("sunrise", [""])[0]
            ss = daily.get("sunset",  [""])[0]
            if "T" in str(sr): sunrise_str = str(sr).split("T")[1][:5]
            if "T" in str(ss): sunset_str  = str(ss).split("T")[1][:5]
        except Exception:
            pass

        # Island lines
        island_lines = ""
        if islands:
            island_lines = "🏝️ Island Watch:\n"
            for isl in islands[:5]:
                ie, _ = weather_code_to_info(isl.get("wmo", 0))
                island_lines += f"  {isl['name']}: {ie} {isl.get('temp',29)}°C  💨{isl.get('wind',0)}km/h\n"

        src_tag = f"\n\n<i>Data: {source}</i>"

        caption = (
            f"{greeting}\n\n"
            f"{emoji} <b>{condition} — Malé, Maldives</b>\n"
            f"🌡 <b>{temp}°C</b>  (Feels {feels}°C)  •  H:{temp_max}° L:{temp_min}°\n"
            f"💧 Humidity {humidity}%  •  ☔ Rain {precip_p}%  •  ☀️ UV {uv}\n"
            f"💨 Wind {wind} km/h"
            + (f" (gusts {round(current.get('windgust_10m',0))} km/h)"
               if current.get('windgust_10m', 0) > wind else "") + "\n"
            f"🌅 Sunrise {sunrise_str}  •  🌇 Sunset {sunset_str}\n\n"
            f"{sea_line}"
            f"{island_lines}\n"
            f"📡 <b>Samuga Media</b> | @samugacommunity"
            f"{src_tag}"
        )

        if send_photo and not posting_paused():
            send_photo(TELEGRAM_CHANNEL_ID, card, caption)
            log.info(f"✅ Weather card sent to Telegram ({time_of_day}) via {source}")
        elif posting_paused():
            log.warning("🛑 Weather update Telegram post blocked — POSTING_PAUSED=true")

        # Post to social (FB + IG + X)
        try:
            card.seek(0)
            if queue_for_social and not social_paused():
                queue_for_social(io.BytesIO(card.getvalue()), caption)
                log.info(f"📲 Weather card queued for FB + IG + X ({time_of_day})")
            elif social_paused():
                log.warning("🛑 Weather social queue blocked — SOCIAL/POSTING paused")
        except Exception as e:
            log.error(f"Weather social queue: {e}")

        # Auto-alert check
        should_alert, alert_type, alert_text = detect_weather_alert(data)
        if should_alert:
            log.info(f"⚠️ Alert detected: {alert_type}")
            send_weather_alert(data, alert_type, alert_text)
        else:
            log.info("✅ No alert conditions detected")

    except Exception as e:
        log.error(f"Weather update: {e}")
