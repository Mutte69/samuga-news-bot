"""Samuga AI configuration, constants, source lists, and logging."""
import os
import logging
from datetime import datetime, timedelta, timezone

SAMUGA_VERSION = "8.0-modular"

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger("samuga")

# API keys / destinations
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "@samugacommunity")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "SamugaNewsBot")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")
IMGBB_API_KEY = os.environ.get("IMGBB_API_KEY", "")
TOMORROW_API_KEY = os.environ.get("TOMORROW_API_KEY", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

BUFFER_TOKEN = os.environ.get("BUFFER_ACCESS_TOKEN", "")
BUFFER_FB_ID = os.environ.get("BUFFER_FACEBOOK_ID", "")
BUFFER_IG_ID = os.environ.get("BUFFER_INSTAGRAM_ID", "")
BUFFER_TW_ID = os.environ.get("BUFFER_TWITTER_ID", "")
BUFFER_PROFILE_IDS = [x for x in [BUFFER_FB_ID, BUFFER_IG_ID, BUFFER_TW_ID] if x]

META_PAGE_TOKEN = os.environ.get("META_PAGE_TOKEN", "")
META_PAGE_ID = os.environ.get("META_PAGE_ID", "")
META_API_VER = os.environ.get("META_API_VER", "v21.0")

SAMUGA_PUBLIC_LINK = os.environ.get("SAMUGA_PUBLIC_LINK", "https://t.me/samugacommunity")
SAMUGA_PUBLIC_SOURCE = os.environ.get("SAMUGA_PUBLIC_SOURCE", "Samuga Media")

CORE_TEAM_CHAT_ID = os.environ.get("CORE_TEAM_CHAT_ID", "-1002829230299")
CONTENT_LAB_THREAD_ID = int(os.environ.get("CONTENT_LAB_THREAD_ID", "9061"))
ALERT_THREAD_ID = int(os.environ.get("ALERT_THREAD_ID", "10169"))

# Timing / selection
SOCIAL_POST_GAP_SECONDS = int(os.environ.get("SOCIAL_POST_GAP_SECONDS", "120"))
BREAKING_SCAN_MINUTES = int(os.environ.get("BREAKING_SCAN_MINUTES", "5"))
NORMAL_SCAN_MINUTES = int(os.environ.get("NORMAL_SCAN_MINUTES", "15"))
DAILY_PUBLIC_POST_MAX = int(os.environ.get("DAILY_PUBLIC_POST_MAX", "30"))
DHIVEHI_EXPIRY_SECONDS = int(os.environ.get("DHIVEHI_EXPIRY_SECONDS", "7200"))
HIGH_CONF_AUTOPOST_SECONDS = int(os.environ.get("HIGH_CONF_AUTOPOST_SECONDS", "0"))
MEDIUM_CONF_AUTOPOST_SECONDS = int(os.environ.get("MEDIUM_CONF_AUTOPOST_SECONDS", "300"))
LOW_CONF_REVIEW_SECONDS = int(os.environ.get("LOW_CONF_REVIEW_SECONDS", "1800"))

# Content Lab flood guard for previews only, not website publishing
CONTENT_LAB_NORMAL_MAX_PER_HOUR = int(os.environ.get("CONTENT_LAB_NORMAL_MAX_PER_HOUR", "4"))
CONTENT_LAB_HIGH_MAX_PER_HOUR = int(os.environ.get("CONTENT_LAB_HIGH_MAX_PER_HOUR", "6"))

DATA_DIR = os.environ.get("DATA_DIR", "/data")
SEEN_FILE = os.path.join(DATA_DIR, "seen_articles.json")
STATE_FILE = os.path.join(DATA_DIR, "bot_state.json")
os.makedirs(DATA_DIR, exist_ok=True)

CAT_CONFIG = {
    "BREAKING": {"label": "🚨 BREAKING NEWS", "color": (220, 50, 50)},
    "LOCAL": {"label": "🇲🇻 LOCAL NEWS", "color": (41, 171, 226)},
    "POLITICAL": {"label": "🏛️ POLITICAL", "color": (180, 140, 40)},
    "LIFESTYLE": {"label": "🌴 LIFESTYLE", "color": (160, 80, 220)},
    "SPORTS": {"label": "🏅 SPORTS", "color": (34, 180, 80)},
    "WORLD": {"label": "🌍 WORLD NEWS", "color": (220, 80, 60)},
    "WEATHER": {"label": "🌦 WEATHER", "color": (30, 150, 210)},
}

CATEGORY_MAP = {
    "BREAKING": "BREAKING", "DISASTER": "BREAKING", "ACCIDENT": "BREAKING",
    "LOCAL": "LOCAL", "POLITICAL": "POLITICAL", "POLITICS": "POLITICAL",
    "LIFESTYLE": "LIFESTYLE", "TOURISM": "LIFESTYLE", "BUSINESS": "LOCAL",
    "SPORTS": "SPORTS", "FOOTBALL": "SPORTS", "WORLD": "WORLD", "WEATHER": "WEATHER",
}

POLITICAL_KEYWORDS = [
    "parliament", "majlis", "president", "minister", "ministry", "government",
    "cabinet", "mp ", "opposition", "mdp", "pnc", "ppm", "election", "vote",
    "policy", "bill", "law", "court", "judge", "council", "mayor", "budget",
]

BREAKING_KEYWORDS = [
    "killed", "dead", "dies", "death", "murder", "shot", "stabbed", "explosion",
    "bomb", "attack", "tsunami", "earthquake", "flood", "disaster", "sinking",
    "collapsed", "missing person", "fire", "emergency", "gas leak", "capsized",
    "rescue", "accident", "crash", "alert", "warning",
    "މަރު", "ހަމަލާ", "އަނިޔާ", "ފައިރ", "އެލާޓ", "ގެއްލި",
]
BREAKING_BLACKLIST = [
    "world cup", "football", "cricket", "sports", "resort", "award", "ranking",
    "luxury", "launch", "event", "promotion", "sale", "discount",
]

LOW_VALUE_KEYWORDS = [
    "sponsored", "advertisement", "promo", "promotion", "sale", "discount", "offer",
    "launches menu", "grand opening", "coupon", "giveaway", "win a", "brand ambassador",
]

LOCAL_ENTITY_KEYWORDS = [
    "maldives", "maldivian", "male'", "malé", "raajje", "ރާއްޖެ", "މާލެ",
    "president office", "police", "mndf", "mms", "hpa", "mma", "bml", "mifco",
    "majlis", "ministry", "atoll", "island", "resort", "tourism",
]

# Direct source ladder. RSS is tried where possible, but latest-page/Telegram/Google backup keeps speed.
RSS_FEEDS = [
    {"url": "https://news.google.com/rss/search?q=maldives+breaking+incident+accident&hl=en-MV&gl=MV&ceid=MV:en", "source": "Google News MV", "cat": "BREAKING", "lang": "en"},
    {"url": "https://english.sun.mv/feed", "source": "Sun English", "cat": "LOCAL", "lang": "en"},
    {"url": "https://edition.mv/feed", "source": "Edition", "cat": "LOCAL", "lang": "en"},
    {"url": "https://psmnews.mv/en/feed", "source": "PSM News", "cat": "LOCAL", "lang": "en"},
    {"url": "https://presidency.gov.mv/feed", "source": "Presidency", "cat": "LOCAL", "lang": "en"},
    {"url": "https://sunonline.mv/feed", "source": "Sun Dhivehi", "cat": "LOCAL", "lang": "dv"},
    {"url": "https://mihaaru.com/rss", "source": "Mihaaru", "cat": "LOCAL", "lang": "dv"},
    {"url": "https://avas.mv/feed", "source": "Avas", "cat": "LOCAL", "lang": "dv"},
    {"url": "https://news.google.com/rss/search?q=maldives+politics+government+parliament&hl=en-MV&gl=MV&ceid=MV:en", "source": "Google News Politics", "cat": "POLITICAL", "lang": "en"},
    {"url": "https://news.google.com/rss/search?q=maldives+economy+tourism+business&hl=en-MV&gl=MV&ceid=MV:en", "source": "Google News Economy", "cat": "LOCAL", "lang": "en"},
    {"url": "https://news.google.com/rss/search?q=Iran+US+Israel+Gaza+Russia+Ukraine+earthquake+tsunami&hl=en&gl=US&ceid=US:en", "source": "Google News World", "cat": "WORLD", "lang": "en"},
]

LATEST_PAGE_SOURCES = [
    {"name": "Mihaaru", "url": "https://mihaaru.com/", "cat": "LOCAL", "lang": "dv", "priority": 95},
    {"name": "Avas", "url": "https://avas.mv/", "cat": "LOCAL", "lang": "dv", "priority": 90},
    {"name": "Sun", "url": "https://sun.mv/", "cat": "LOCAL", "lang": "dv", "priority": 90},
    {"name": "Sun English", "url": "https://english.sun.mv/", "cat": "LOCAL", "lang": "en", "priority": 90},
    {"name": "PSM News", "url": "https://psmnews.mv/en", "cat": "LOCAL", "lang": "en", "priority": 86},
    {"name": "Raajje", "url": "https://raajje.mv/", "cat": "LOCAL", "lang": "dv", "priority": 85},
    {"name": "VoiceMV", "url": "https://voice.mv/", "cat": "LOCAL", "lang": "dv", "priority": 82},
    {"name": "Edition", "url": "https://edition.mv/", "cat": "LOCAL", "lang": "en", "priority": 83},
    {"name": "ThePress", "url": "https://thepress.mv/", "cat": "LOCAL", "lang": "dv", "priority": 80},
    {"name": "Police", "url": "https://police.gov.mv/", "cat": "BREAKING", "lang": "dv", "priority": 95},
    {"name": "MNDF", "url": "https://mndf.gov.mv/", "cat": "BREAKING", "lang": "dv", "priority": 94},
    {"name": "Presidency", "url": "https://presidency.gov.mv/", "cat": "POLITICAL", "lang": "en", "priority": 92},
]

TELEGRAM_SOURCE_CHANNELS = [
    # handle should not include @. Public t.me/s pages only; private channels need Bot API admin access.
    {"handle": "mihaarunews", "source": "Mihaaru", "lang": "dv", "priority": 95},
    {"handle": "avasmv", "source": "Avas", "lang": "dv", "priority": 90},
    {"handle": "raajjemv", "source": "Raajje", "lang": "dv", "priority": 85},
    {"handle": "sunonlinemv", "source": "Sun", "lang": "dv", "priority": 90},
    {"handle": "mvcrisis", "source": "MvCrisis", "lang": "dv", "priority": 88},
]

# Add extra channels from env: handle:Source:lang:priority,handle2:Source2:en:80
_extra = os.environ.get("SAMUGA_EXTRA_TG_CHANNELS", "").strip()
if _extra:
    for item in _extra.split(","):
        parts = [p.strip() for p in item.split(":")]
        if len(parts) >= 2:
            TELEGRAM_SOURCE_CHANNELS.append({
                "handle": parts[0].lstrip("@"),
                "source": parts[1],
                "lang": parts[2] if len(parts) > 2 else "dv",
                "priority": int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 80,
            })

WORLD_SEARCH_QUERIES = [
    "major breaking news world conflict disaster economy",
    "Iran US Israel Gaza latest",
    "South Asia India Sri Lanka Maldives regional news",
    "oil prices dollar global economy breaking",
]

USER_AGENT = os.environ.get("SAMUGA_USER_AGENT", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36 SamugaBot/8.0")
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "15"))


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def mvt_now():
    return utcnow() + timedelta(hours=5)


def get_mvt_hour():
    return mvt_now().hour


def is_day_mode():
    h = get_mvt_hour()
    return 6 <= h < 22


def canonical_category(cat: str, title: str = "", summary: str = "") -> str:
    base = CATEGORY_MAP.get((cat or "LOCAL").upper(), "LOCAL")
    if base == "LOCAL":
        text = f"{title} {summary}".lower()
        if any(k in text for k in POLITICAL_KEYWORDS):
            return "POLITICAL"
    return base


def log_startup():
    log.info(f"🚀 Samuga AI v{SAMUGA_VERSION} starting (modular newsroom engine)...")
    log.info("📅 Website updates 24/7 | Breaking scan every 5min | Normal scan every 15min")
    log.info(f"📲 Social queue worker: {SOCIAL_POST_GAP_SECONDS}s gap between posts")
    log.info("🧠 Public Samuga AI: Website + Telegram + future WhatsApp shared brain")
    log.info("🪜 Source ladder: RSS → latest-page → Telegram → Google/Tavily backup")
