"""
weather.py — Samuga AI Weather Module
Extracted from bot.py v7.0

Contains:
  - get_weather_data()          Tomorrow.io + Open-Meteo fallback
  - get_prayer_times()          Aladhan API prayer times for Malé
  - get_island_forecasts()      5 Maldivian island forecasts
  - generate_weather_card()     Full 2500x3050 Pillow weather card
  - weather_code_to_info()      WMO code → emoji + description
  - draw_weather_icon()         Vector weather icon renderer
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

def get_daily_islamic_reminder(mvt_now):
    """Pick a reminder that rotates by day — different each day, stable within a day."""
    day_index = mvt_now.timetuple().tm_yday
    text, source = ISLAMIC_REMINDERS[day_index % len(ISLAMIC_REMINDERS)]
    return {"text": text, "source": source}

def get_prayer_times():
    """
    Fetch today's prayer times + Hijri date for Malé, Maldives.
    Uses AlAdhan API with exact Malé coordinates and Maldives-style calculation.
    Returns dict or None on failure.
    """
    try:
        from datetime import timezone as _tz
        mvt_now = datetime.now(_tz.utc) + timedelta(hours=5)
        date_str = mvt_now.strftime("%d-%m-%Y")

        # Exact Malé coordinates + Maldives Islamic Ministry style calculation.
        # Maldives uses Shafi'i Asr, Fajr 19.5°, Isha 78 min after Maghrib.
        # tune order: Imsak,Fajr,Sunrise,Dhuhr,Asr,Sunset,Maghrib,Isha,Midnight
        MALE_LAT, MALE_LON = 4.1755, 73.5093
        url = (f"https://api.aladhan.com/v1/timings/{date_str}"
               f"?latitude={MALE_LAT}&longitude={MALE_LON}"
               f"&method=99&methodSettings=19.5,null,78%20min"
               f"&school=0"
               f"&timezonestring=Indian/Maldives"
               f"&tune=0,0,0,1,-3,0,-1,0,0")
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            log.warning(f"Prayer times API: HTTP {resp.status_code} — trying fallback")
            url2 = (f"https://api.aladhan.com/v1/timingsByCity/{date_str}"
                    f"?city=Male&country=Maldives&method=4")
            resp = requests.get(url2, timeout=10)
            if resp.status_code != 200:
                return None

        d = resp.json().get("data", {})
        timings = d.get("timings", {})
        hijri   = d.get("date", {}).get("hijri", {})

        def clean_t(t):
            return t[:5] if t else "--:--"

        prayers = {
            "Fajr":    clean_t(timings.get("Fajr", "")),
            "Dhuhr":   clean_t(timings.get("Dhuhr", "")),
            "Asr":     clean_t(timings.get("Asr", "")),
            "Maghrib": clean_t(timings.get("Maghrib", "")),
            "Isha":    clean_t(timings.get("Isha", "")),
        }

        h_day = int(hijri.get("day", 0) or 0)
        h_month_num = hijri.get("month", {}).get("number", 0)
        h_month_name = hijri.get("month", {}).get("en", "")
        h_year = hijri.get("year", "")

        api_holidays = hijri.get("holidays", [])
        special_name = api_holidays[0] if api_holidays else None
        special_desc = ""

        if special_name:
            special_desc = SPECIAL_DAY_DETAILS.get(special_name, "")
            if not special_desc:
                key = (h_month_num, h_day)
                if key in HIJRI_SPECIAL_DAYS:
                    _, special_desc = HIJRI_SPECIAL_DAYS[key]
        else:
            key = (h_month_num, h_day)
            if key in HIJRI_SPECIAL_DAYS:
                special_name, special_desc = HIJRI_SPECIAL_DAYS[key]

        reminder = None
        if not special_name:
            reminder = get_daily_islamic_reminder(mvt_now)

        log.info(
            f"🕌 Prayer times — Fajr {prayers['Fajr']} Dhuhr {prayers['Dhuhr']} "
            f"Asr {prayers['Asr']} Maghrib {prayers['Maghrib']} Isha {prayers['Isha']}"
            + (f" | {special_name}" if special_name else "")
        )

        return {
            "prayers": prayers,
            "hijri_day": h_day,
            "hijri_month": h_month_name,
            "hijri_year": h_year,
            "special_name": special_name,
            "special_desc": special_desc,
            "reminder": reminder,
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

def draw_weather_icon(draw, code, x, y, size=40):
    """Draw vector weather icon — scales cleanly at any size (line widths proportional)."""
    import math
    cx, cy = x, y
    s = size
    lw = max(2, s // 18)   # proportional line width

    if code == 0:  # Sun
        draw.ellipse([cx-s//3, cy-s//3, cx+s//3, cy+s//3], fill=(255,210,40,255))
        for angle in range(0, 360, 30):
            rad = math.radians(angle)
            x1 = cx + int((s//3+s//12)*math.cos(rad))
            y1 = cy + int((s//3+s//12)*math.sin(rad))
            x2 = cx + int((s//2+s//10)*math.cos(rad))
            y2 = cy + int((s//2+s//10)*math.sin(rad))
            draw.line([x1,y1,x2,y2], fill=(255,210,40,230), width=lw)
    elif code in [1,2]:  # Partly cloudy — sun behind cloud
        draw.ellipse([cx-s//6, cy-s//2, cx+s//2, cy+s//8], fill=(255,210,40,235))
        draw.ellipse([cx-s//2, cy-s//8, cx+s//6, cy+s//2], fill=(225,235,250,255))
        draw.ellipse([cx-s//8, cy-s//5, cx+s//2, cy+s//3], fill=(225,235,250,255))
        draw.ellipse([cx-s//2, cy, cx+s//4, cy+s//2], fill=(225,235,250,255))
    elif code == 3:  # Cloud
        draw.ellipse([cx-s//2, cy-s//8, cx+s//2, cy+s//2], fill=(210,220,245,255))
        draw.ellipse([cx-s//3, cy-s//3, cx+s//6, cy+s//4], fill=(210,220,245,255))
        draw.ellipse([cx-s//12, cy-s//4, cx+s//2, cy+s//3], fill=(210,220,245,255))
    elif code in [51,53,55,61,63,65,80,81,82]:  # Rain
        draw.ellipse([cx-s//2, cy-s//5, cx+s//2, cy+s//3], fill=(175,190,225,255))
        draw.ellipse([cx-s//3, cy-s//3, cx+s//6, cy+s//5], fill=(175,190,225,255))
        draw.ellipse([cx-s//12, cy-s//4, cx+s//2, cy+s//4], fill=(175,190,225,255))
        for rx in [-s//3, 0, s//3]:
            draw.line([cx+rx, cy+s//3, cx+rx-s//12, cy+s//2+s//8],
                      fill=(90,160,255,235), width=lw)
    elif code in [95,96,99]:  # Thunderstorm
        draw.ellipse([cx-s//2, cy-s//5, cx+s//2, cy+s//3], fill=(90,90,115,255))
        draw.ellipse([cx-s//3, cy-s//3, cx+s//6, cy+s//5], fill=(90,90,115,255))
        draw.ellipse([cx-s//12, cy-s//4, cx+s//2, cy+s//4], fill=(90,90,115,255))
        bolt = [cx+s//12, cy+s//4, cx-s//12, cy+s//4, cx, cy+s//2,
                cx-s//6, cy+s//2, cx+s//5, cy+s*3//4]
        draw.line(bolt, fill=(255,215,0,255), width=lw+1)
    else:  # Default cloud
        draw.ellipse([cx-s//2, cy-s//8, cx+s//2, cy+s//2], fill=(190,200,230,255))
        draw.ellipse([cx-s//3, cy-s//3, cx+s//6, cy+s//4], fill=(190,200,230,255))

def generate_weather_card(weather_data, alert_mode=False, alert_text="", island_data=None, prayer_data=None, alert_level=None):
    """Samuga branded weather card v3 — 2500x3000, cinematic, sea conditions, prayer times, Hijri, MMS alerts."""
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    import io

    W, H = 2500, (3050 if island_data else 2300)
    img = Image.new("RGB", (W, H), (0, 0, 0))
    draw = ImageDraw.Draw(img, "RGBA")

    current    = weather_data.get("current", {})
    hourly_d   = weather_data.get("hourly", {})
    daily_d    = weather_data.get("daily", {})
    source     = weather_data.get("_source", "")

    temp     = round(current.get("temperature_2m", 29))
    feels    = round(current.get("apparent_temperature", 29))
    humidity = current.get("relativehumidity_2m", 80)
    wind     = round(current.get("windspeed_10m", 10))
    gusts    = round(current.get("windgust_10m", 0))
    uv       = current.get("uv_index", 0)
    vis      = round(current.get("visibility", 10))
    dew      = round(current.get("dewpoint_2m", 25))
    pressure = round(current.get("pressure_msl", 1010))
    precip_p = current.get("precipitation_prob", 0)
    code     = current.get("weathercode", 0)
    _, condition = weather_code_to_info(code)

    temp_max = round(daily_d.get("temperature_2m_max", [temp])[0])
    temp_min = round(daily_d.get("temperature_2m_min", [temp])[0])
    sunrise_raw = daily_d.get("sunrise", [""])[0]
    sunset_raw  = daily_d.get("sunset",  [""])[0]
    sunrise_str = sunrise_raw.split("T")[1][:5] if "T" in sunrise_raw else "06:00"
    sunset_str  = sunset_raw.split("T")[1][:5]  if "T" in sunset_raw  else "18:19"

    hours  = hourly_d.get("time", [])
    temps  = hourly_d.get("temperature_2m", [])
    codes  = hourly_d.get("weathercode", [])
    precip = hourly_d.get("precipitation_probability", [])

    from datetime import timezone
    mvt = datetime.now(timezone.utc) + timedelta(hours=5)

    # ── Sea condition assessment (Maldives-specific) ──────────────────────────
    def sea_condition(wind_kmh, gust_kmh, precip_pct, wcode):
        if wind_kmh >= 50 or gust_kmh >= 65 or wcode in [95,96,99]:
            return "⛔", "Very Rough Sea", "Avoid all sea travel"
        if wind_kmh >= 35 or gust_kmh >= 45:
            return "🟠", "Rough Sea", "Caution — small craft warning"
        if wind_kmh >= 20 or gust_kmh >= 30:
            return "🟡", "Moderate Sea", "Speedboats with care"
        return "🟢", "Calm Sea", "Good conditions for travel"

    sea_icon, sea_label, sea_advice = sea_condition(wind, gusts, precip_p, code)

    # ── Background — deep layered atmospheric ─────────────────────────────────
    if alert_mode and alert_level:
        # Each MMS level gets its own tinted background
        if alert_level == "white":
            TOP, BOT = (30, 45, 70), (12, 22, 42)      # light steel blue
        elif alert_level == "yellow":
            TOP, BOT = (60, 50, 8), (28, 22, 4)        # dark yellowish
        elif alert_level == "orange":
            TOP, BOT = (70, 38, 6), (32, 16, 3)        # dark orangish
        elif alert_level == "red":
            TOP, BOT = (55, 6, 6), (18, 2, 2)          # deep red (serious)
        else:
            TOP, BOT = (45, 5, 5), (15, 2, 2)
    elif alert_mode:
        TOP, BOT = (45, 5, 5), (15, 2, 2)
    elif code in [95,96,99]:
        TOP, BOT = (18, 10, 45), (6, 4, 22)
    elif code in [61,63,65,80,81,82,51,53,55]:
        TOP, BOT = (8, 18, 52), (4, 8, 28)
    elif code == 0:
        TOP, BOT = (5, 22, 80), (3, 10, 42)
    else:
        TOP, BOT = (8, 18, 55), (4, 8, 32)

    # Three-stop gradient: top → mid → bottom
    MID = tuple(int((TOP[i]+BOT[i])//2 + 8) for i in range(3))
    for y in range(H):
        t = y / H
        if t < 0.45:
            f = t / 0.45
            r = int(TOP[0] + (MID[0]-TOP[0])*f)
            g = int(TOP[1] + (MID[1]-TOP[1])*f)
            b = int(TOP[2] + (MID[2]-TOP[2])*f)
        else:
            f = (t-0.45) / 0.55
            r = int(MID[0] + (BOT[0]-MID[0])*f)
            g = int(MID[1] + (BOT[1]-MID[1])*f)
            b = int(MID[2] + (BOT[2]-MID[2])*f)
        draw.line([(0,y),(W,y)], fill=(max(0,min(255,r)), max(0,min(255,g)), max(0,min(255,b)), 255))

    # Atmospheric glow layers — large soft blobs of colour for depth
    glow = Image.new("RGBA", (W, H), (0,0,0,0))
    gd   = ImageDraw.Draw(glow)

    # Primary glow — centre-top (SKY blue)
    for r in range(700, 0, -1):
        a = int(28 * (1 - r/700))
        gd.ellipse([(W//2-r, 180-r), (W//2+r, 180+r)], fill=(41,171,226,a))

    # Secondary glow — lower left (deeper blue)
    for r in range(500, 0, -1):
        a = int(18 * (1 - r/500))
        gd.ellipse([(200-r, H-400-r), (200+r, H-400+r)], fill=(20,60,160,a))

    # Accent glow — lower right (hint of teal)
    for r in range(400, 0, -1):
        a = int(14 * (1 - r/400))
        gd.ellipse([(W-300-r, H-500-r), (W-300+r, H-500+r)], fill=(20,120,140,a))

    glow = glow.filter(ImageFilter.GaussianBlur(60))
    img_rgba = img.convert("RGBA")
    img_rgba = Image.alpha_composite(img_rgba, glow)

    # Noise/grain overlay for depth (subtle)
    import random
    grain = Image.new("RGBA", (W, H), (0,0,0,0))
    gpx  = grain.load()
    for yy in range(0, H, 3):
        for xx in range(0, W, 3):
            v = random.randint(0, 12)
            gpx[xx, yy] = (v, v, v+4, 6)
    img_rgba = Image.alpha_composite(img_rgba, grain)

    img  = img_rgba.convert("RGB")
    draw = ImageDraw.Draw(img, "RGBA")

    # Alert tint overlay — coloured by level
    if alert_mode:
        tint_map = {
            "white":  (60, 90, 130, 30),
            "yellow": (120, 100, 10, 35),
            "orange": (140, 70, 5, 35),
            "red":    (90, 0, 0, 45),
        }
        tc = tint_map.get(alert_level, (80, 0, 0, 40))
        overlay = Image.new("RGBA", (W,H), tc)
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        draw = ImageDraw.Draw(img, "RGBA")

    # ── Fonts ─────────────────────────────────────────────────────────────────
    def F(sz, bold=False):
        try:
            path = f"/usr/share/fonts/truetype/dejavu/DejaVuSans{chr(45)+'Bold' if bold else ''}.ttf"
            return ImageFont.truetype(path, sz)
        except:
            return ImageFont.load_default()

    f_giant  = F(420, True)   # temperature
    f_huge   = F(110, True)   # condition
    f_large  = F(80,  True)   # location, section headers
    f_med    = F(64)           # H/L, details
    f_small  = F(52,  True)   # island names, sea label
    f_body   = F(46)           # outlook text
    f_tiny   = F(38)           # hourly labels
    f_xs     = F(32)           # sub-labels
    f_xxs    = F(26)           # footer, source

    # ── MMS Alert banner FIRST (so logo sits below it, not under it) ──────────
    banner_h = 0
    if alert_mode and alert_level:
        level_cfg = MMS_ALERT_LEVELS.get(alert_level, MMS_ALERT_LEVELS["white"])
        acolor = level_cfg["color"]
        banner_h = 130
        draw.rectangle([(0, 0), (W, banner_h)], fill=(acolor[0], acolor[1], acolor[2], 235))
        btext = f"{level_cfg['emoji']}  {level_cfg['label']}  —  {level_cfg['headline'].upper()}"
        btw = draw.textlength(btext, font=f_small)
        txt_color = (20,20,20,255) if alert_level in ["white","yellow"] else (255,255,255,255)
        draw.text(((W-btw)//2, 38), btext, font=f_small, fill=txt_color)
    elif alert_mode:
        banner_h = 110
        draw.rectangle([(0, 0), (W, banner_h)], fill=(200, 40, 40, 235))
        btext = "⚠  WEATHER ALERT  ⚠"
        btw = draw.textlength(btext, font=f_small)
        draw.text(((W-btw)//2, 30), btext, font=f_small, fill=(255,255,255,255))

    # ── SAMUGA LOGO — top left (below banner if in alert mode) ────────────────
    logo_y = (banner_h + 25) if alert_mode else 55
    try:
        logo = Image.open("logo.png").convert("RGBA")
        lh = 120; lw2 = int(logo.width * lh / logo.height)
        logo = logo.resize((lw2, lh), Image.LANCZOS)
        ir = img.convert("RGBA"); ir.paste(logo, (70, logo_y), logo)
        img = ir.convert("RGB"); draw = ImageDraw.Draw(img, "RGBA")
    except Exception as e:
        log.debug(f"weather logo: {e}")
        draw.text((70, logo_y), "SAMUGA MEDIA", font=f_xs, fill=(255,255,255,200))

    # Channel tag — top right (below banner if in alert mode)
    tag = "t.me/samugacommunity"
    ttw = draw.textlength(tag, font=f_xs)
    tag_y = (banner_h + 45) if alert_mode else 78
    draw.text((W-ttw-70, tag_y), tag, font=f_xs, fill=(255,255,255,130))

    # ── LOCATION ──────────────────────────────────────────────────────────────
    loc = "Malé, Maldives"
    loc_y = (banner_h + 180) if alert_mode else 240
    lcw = draw.textlength(loc, font=f_large)
    draw.text(((W-lcw)//2, loc_y), loc, font=f_large, fill=(255,255,255,230))

    # ── WEATHER ICON — dead centre between location and temperature ────────────
    icon_y = loc_y + 175
    draw_weather_icon(draw, code, W//2, icon_y, size=175)

    # ── TEMPERATURE ───────────────────────────────────────────────────────────
    temp_str = f"{temp}°"
    ttw2 = draw.textlength(temp_str, font=f_giant)
    temp_y = icon_y + 175
    draw.text(((W-ttw2)//2, temp_y), temp_str, font=f_giant, fill=(255,255,255,255))

    # ── PRAYER TIMES (left of temp) + HIJRI (right of temp) ───────────────────
    if prayer_data:
        prayers  = prayer_data.get("prayers", {})
        h_day    = prayer_data.get("hijri_day", "")
        h_month  = prayer_data.get("hijri_month", "")
        h_year   = prayer_data.get("hijri_year", "")
        sp_name  = prayer_data.get("special_name", "")
        sp_desc  = prayer_data.get("special_desc", "")
        reminder = prayer_data.get("reminder", None)

        flank_y = temp_y + 30   # align with top of big temperature

        # ── LEFT: Prayer times ────────────────────────────────────────────────
        px = 90
        py = flank_y
        draw.text((px, py), "PRAYER TIMES", font=f_xs, fill=(255,220,100,210))
        py += 60
        prayer_order = ["Fajr","Dhuhr","Asr","Maghrib","Isha"]
        for name in prayer_order:
            draw.text((px, py), name, font=f_small, fill=(255,255,255,220))
            t_val = prayers.get(name, "--:--")
            tw_p = int(draw.textlength(t_val, font=f_small))
            draw.text((px + 430 - tw_p, py), t_val, font=f_small, fill=(160,215,255,235))
            py += 78

        # ── RIGHT: Hijri calendar ─────────────────────────────────────────────
        rx = W - 90 - 540   # right block left edge (room for long month names)
        ry = flank_y
        draw.text((rx, ry), "HIJRI CALENDAR", font=f_xs, fill=(255,220,100,210))
        ry += 60
        # Big day number
        h_day_str = str(h_day)
        draw.text((rx, ry), h_day_str, font=F(150, True), fill=(255,255,255,245))
        ry += 165
        # Month + year below the number
        draw.text((rx, ry), h_month, font=f_med, fill=(255,255,255,215))
        ry += 66
        draw.text((rx, ry), f"{h_year} AH", font=f_body, fill=(200,225,255,165))
        ry += 70

        # Special day box (gold) OR Islamic reminder box (subtle teal)
        if sp_name:
            box_left  = rx
            box_right = W - 80
            box_w_px  = box_right - box_left
            desc_lines = []
            if sp_desc:
                words = sp_desc.split()
                cur = ""
                for w in words:
                    test = (cur + " " + w).strip()
                    if draw.textlength(test, font=F(28)) <= box_w_px - 40:
                        cur = test
                    else:
                        desc_lines.append(cur); cur = w
                if cur: desc_lines.append(cur)
            box_h = 56 + len(desc_lines)*38 + 30
            draw.rounded_rectangle([(box_left, ry),(box_right, ry+box_h)],
                                   radius=18, fill=(58,44,4,180))
            draw.text((box_left+24, ry+18), sp_name, font=F(38,True), fill=(255,220,80,255))
            dyy = ry + 70
            for dl in desc_lines:
                draw.text((box_left+24, dyy), dl, font=F(28), fill=(255,205,90,210))
                dyy += 38
        elif reminder:
            # Islamic reminder box — subtle teal/blue accent
            box_left  = rx
            box_right = W - 80
            box_w_px  = box_right - box_left
            r_text = reminder.get("text", "")
            r_src  = reminder.get("source", "")
            # Wrap the reminder text
            words = r_text.split()
            lines = []
            cur = ""
            for w in words:
                test = (cur + " " + w).strip()
                if draw.textlength(test, font=F(30)) <= box_w_px - 40:
                    cur = test
                else:
                    lines.append(cur); cur = w
            if cur: lines.append(cur)
            box_h = 50 + len(lines)*40 + 50
            draw.rounded_rectangle([(box_left, ry),(box_right, ry+box_h)],
                                   radius=18, fill=(10,40,55,170))
            # Small header
            draw.text((box_left+24, ry+16), "✦ DAILY REMINDER", font=F(24,True), fill=(120,200,220,220))
            dyy = ry + 56
            for ln in lines:
                draw.text((box_left+24, dyy), ln, font=F(30), fill=(220,240,250,225))
                dyy += 40
            # Source
            draw.text((box_left+24, dyy+4), f"— {r_src}", font=F(26), fill=(150,200,220,190))

    # ── CONDITION ─────────────────────────────────────────────────────────────
    cond_y = temp_y + 440
    ccw = draw.textlength(condition, font=f_huge)
    draw.text(((W-ccw)//2, cond_y), condition, font=f_huge, fill=(255,255,255,200))

    # ── H / L ─────────────────────────────────────────────────────────────────
    hl_y = cond_y + 130
    hl_str = f"H:{temp_max}°   L:{temp_min}°"
    hlw = draw.textlength(hl_str, font=f_med)
    draw.text(((W-hlw)//2, hl_y), hl_str, font=f_med, fill=(255,255,255,180))

    # Alert text — wrapped, below H/L (full detail is also in the caption)
    if alert_mode and alert_text:
        acol = (255,140,140,255)
        if alert_level in MMS_ALERT_LEVELS:
            c = MMS_ALERT_LEVELS[alert_level]["color"]
            acol = (min(255,c[0]+40), min(255,c[1]+40), min(255,c[2]+40), 255)
        # Wrap alert text
        words = alert_text.split()
        lines = []; cur = ""
        for w in words:
            test = (cur + " " + w).strip()
            if draw.textlength(test, font=f_body) <= W - 200:
                cur = test
            else:
                lines.append(cur); cur = w
        if cur: lines.append(cur)
        ay = hl_y + 80
        for ln in lines[:3]:
            lw_a = draw.textlength(ln, font=f_body)
            draw.text(((W-lw_a)//2, ay), ln, font=f_body, fill=acol)
            ay += 56
        hl_y = ay - 80  # push details down below the alert text

    # ── DETAILS — 3 rows ──────────────────────────────────────────────────────
    dy = hl_y + 90
    def centred(text, font, color, y):
        w = draw.textlength(text, font=font)
        draw.text(((W-w)//2, y), text, font=font, fill=color)

    row1 = f"Feels {feels}°   Humidity {humidity}%   Wind {wind} km/h"
    if gusts and gusts > wind: row1 += f" (gusts {gusts})"
    centred(row1, f_med, (255,255,255,175), dy); dy += 70

    row2_parts = []
    if uv:       row2_parts.append(f"UV {uv}")
    if vis:      row2_parts.append(f"Visibility {vis} km")
    if dew:      row2_parts.append(f"Dew {dew}°")
    if pressure: row2_parts.append(f"Pressure {pressure} hPa")
    if row2_parts:
        centred("   ".join(row2_parts), f_body, (255,255,255,145), dy); dy += 60

    sun_str = f"Sunrise {sunrise_str}   Sunset {sunset_str}"
    if precip_p: sun_str += f"   Rain {precip_p}%"
    centred(sun_str, f_body, (255,220,100,200), dy); dy += 50

    # ── THIN DIVIDER ──────────────────────────────────────────────────────────
    div1 = dy + 20
    draw.line([(80,div1),(W-80,div1)], fill=(255,255,255,35), width=2)

    # ── SEA & WIND CONDITION SECTION (Maldives-specific) ─────────────────────
    sea_y = div1 + 50
    # Section label
    sea_hdr = "SEA & WIND CONDITIONS"
    shw = draw.textlength(sea_hdr, font=f_small)
    draw.text(((W-shw)//2, sea_y), sea_hdr, font=f_small, fill=(255,220,100,220))
    sea_y += 80

    # Three columns: wind | sea state | advice
    col_w = W // 3
    # Wind column
    draw.text((col_w*0 + 80, sea_y), "WIND", font=f_xs, fill=(255,255,255,120))
    wind_val = f"{wind} km/h"
    draw.text((col_w*0 + 80, sea_y+40), wind_val, font=f_small, fill=(255,255,255,230))
    if gusts > wind:
        draw.text((col_w*0 + 80, sea_y+100), f"Gusts {gusts} km/h", font=f_body, fill=(255,200,100,180))

    # Sea state column — centred
    sl_w = draw.textlength(sea_label, font=f_small)
    draw.text(((W-sl_w)//2, sea_y+40), sea_label, font=f_small, fill=(255,255,255,230))
    adv_w = draw.textlength(sea_advice, font=f_body)
    draw.text(((W-adv_w)//2, sea_y+100), sea_advice, font=f_body, fill=(200,230,255,170))

    # UV + Visibility column — right
    draw.text((col_w*2 + 80, sea_y), "UV INDEX", font=f_xs, fill=(255,255,255,120))
    uv_col = "Low" if uv<=2 else "Moderate" if uv<=5 else "High" if uv<=7 else "Very High"
    draw.text((col_w*2 + 80, sea_y+40), f"{uv} — {uv_col}", font=f_small, fill=(255,255,255,230))
    draw.text((col_w*2 + 80, sea_y+100), f"Vis {vis} km", font=f_body, fill=(200,230,255,170))

    sea_y += 160
    div2 = sea_y + 10
    draw.line([(80,div2),(W-80,div2)], fill=(255,255,255,35), width=2)
    div3 = div2

    # ── HOURLY STRIP — next 8 hours ───────────────────────────────────────────
    hourly_y = div3 + 40
    now_hour = mvt.hour
    slot_w = (W - 160) // 8
    displayed = 0

    for h_str, ht, hc, hp in zip(hours, temps, codes, precip):
        try:
            h_hour = int(h_str.split("T")[1][:2])
        except:
            continue
        if h_hour < now_hour: continue
        if displayed >= 8: break

        hx = 80 + displayed * slot_w + slot_w // 2

        # Hour label
        h_label = "Now" if displayed == 0 else f"{h_hour:02d}:00"
        hlw2 = draw.textlength(h_label, font=f_tiny)
        draw.text((hx-hlw2//2, hourly_y), h_label, font=f_tiny, fill=(255,255,255,160))

        # Icon
        draw_weather_icon(draw, hc, hx, hourly_y+75, size=78)

        # Temp
        ht_str = f"{round(ht)}°"
        htw = draw.textlength(ht_str, font=f_small)
        draw.text((hx-htw//2, hourly_y+140), ht_str, font=f_small, fill=(255,255,255,255))

        # Rain %
        if hp and hp > 0:
            hp_str = f"{hp}%"
            hpw = draw.textlength(hp_str, font=f_tiny)
            draw.text((hx-hpw//2, hourly_y+200), hp_str, font=f_tiny, fill=(120,200,255,200))

        displayed += 1

    div3 = hourly_y + 260
    draw.line([(80,div3),(W-80,div3)], fill=(255,255,255,35), width=2)

    # ── ISLAND WATCH STRIP ────────────────────────────────────────────────────
    if island_data:
        iw_y = div3 + 50

        iw_hdr = "WEATHER WATCH — MALDIVES"
        ihw = draw.textlength(iw_hdr, font=f_small)
        draw.text(((W-ihw)//2, iw_y), iw_hdr, font=f_small, fill=(255,220,100,225))
        iw_y += 90

        for isl in island_data:
            iname = isl["name"]
            iout  = isl["outlook"]
            itemp = isl.get("temp", 29)

            # Name left, temp right
            draw.text((90, iw_y), iname, font=f_small, fill=(255,255,255,230))
            ts2 = f"{itemp}°C"
            tw3 = int(draw.textlength(ts2, font=f_small))
            draw.text((W-90-tw3, iw_y), ts2, font=f_small, fill=(160,215,255,210))
            # Outlook below
            draw.text((90, iw_y+58), iout, font=f_body, fill=(200,225,255,165))

            # Subtle row separator
            draw.line([(90, iw_y+108),(W-90, iw_y+108)], fill=(255,255,255,18), width=1)
            iw_y += 118

        div4 = iw_y + 20
        draw.line([(80,div4),(W-80,div4)], fill=(255,255,255,30), width=2)
        bottom_start = div4
    else:
        bottom_start = div3

    # ── BOTTOM BAR ────────────────────────────────────────────────────────────
    bar_y = H - 160
    # Semi-transparent dark strip
    bar_overlay = Image.new("RGBA", (W, 160), (0,0,0,80))
    img.paste(Image.new("RGB",(W,160),(0,0,0)), (0,bar_y),
              Image.new("L",(W,160), 80))
    draw = ImageDraw.Draw(img, "RGBA")

    time_str = mvt.strftime("%A, %d %B %Y  •  %H:%M MVT")
    tfw = draw.textlength(time_str, font=f_xs)
    draw.text(((W-tfw)//2, bar_y+18), time_str, font=f_xs, fill=(255,255,255,120))

    brand = "Samuga Media  |  @samugacommunity"
    bw3 = draw.textlength(brand, font=f_small)
    draw.text(((W-bw3)//2, bar_y+62), brand, font=f_small, fill=(255,255,255,210))

    if source:
        src_txt = f"Data: {source}"
        stw2 = draw.textlength(src_txt, font=f_xxs)
        draw.text((W-stw2-80, bar_y+128), src_txt, font=f_xxs, fill=(255,255,255,80))

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
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