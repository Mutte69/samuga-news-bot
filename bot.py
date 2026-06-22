import os, threading, time, logging, hashlib, json, feedparser, requests, anthropic, re, html
from PIL import Image, ImageDraw, ImageFont, ImageEnhance
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from apscheduler.schedulers.blocking import BlockingScheduler
from io import BytesIO

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "@samugacommunity")
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
PEXELS_API_KEY      = os.environ.get("PEXELS_API_KEY", "")
GEMINI_API_KEY      = os.environ.get("GEMINI_API_KEY", "")
TAVILY_API_KEY      = os.environ.get("TAVILY_API_KEY", "")
BOT_USERNAME        = os.environ.get("BOT_USERNAME", "SamugaNewsBot")
IMGBB_API_KEY       = os.environ.get("IMGBB_API_KEY", "")
BUFFER_TOKEN        = os.environ.get("BUFFER_ACCESS_TOKEN", "")
BUFFER_FB_ID        = os.environ.get("BUFFER_FACEBOOK_ID", "")
BUFFER_IG_ID        = os.environ.get("BUFFER_INSTAGRAM_ID", "")
BUFFER_TW_ID        = os.environ.get("BUFFER_TWITTER_ID", "")
BUFFER_POST_MODE    = os.environ.get("BUFFER_POST_MODE", "addToQueue")

ai = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── RSS Feeds ─────────────────────────────────────────────────────────────────
RSS_FEEDS = [
    {"url": "https://news.google.com/rss/search?q=maldives&hl=en-MV&gl=MV&ceid=MV:en", "cat": "LOCAL", "lang": "en"},
    {"url": "https://see.mv/feed", "cat": "LOCAL", "lang": "en"},
    {"url": "https://english.sun.mv/feed", "cat": "LOCAL", "lang": "en"},
    {"url": "https://edition.mv/feed", "cat": "LOCAL", "lang": "en"},
    {"url": "https://maldivesindependent.com/feed", "cat": "LOCAL", "lang": "en"},
    {"url": "https://oneonline.mv/en/feed", "cat": "LOCAL", "lang": "en"},
    {"url": "https://psmnews.mv/en/feed", "cat": "LOCAL", "lang": "en"},
    {"url": "https://mihaaru.com/rss", "cat": "LOCAL", "lang": "dv"},
    {"url": "https://avas.mv/feed", "cat": "LOCAL", "lang": "dv"},
    {"url": "https://news.google.com/rss/search?q=world+cup+2026+football&hl=en&gl=US&ceid=US:en", "cat": "FOOTBALL", "lang": "en"},
    {"url": "https://news.google.com/rss/search?q=champions+league+football&hl=en&gl=US&ceid=US:en", "cat": "FOOTBALL", "lang": "en"},
    {"url": "https://news.google.com/rss/search?q=war+conflict+breaking&hl=en&gl=US&ceid=US:en", "cat": "WORLD", "lang": "en"},
    {"url": "https://news.google.com/rss/search?q=earthquake+tsunami+volcanic+eruption&hl=en&gl=US&ceid=US:en", "cat": "DISASTER", "lang": "en"},
    {"url": "https://news.google.com/rss/search?q=maldives+accident+incident+breaking&hl=en&gl=MV&ceid=MV:en", "cat": "DISASTER", "lang": "en"},
    {"url": "https://news.google.com/rss/search?q=maldives+weather+storm&hl=en&gl=US&ceid=US:en", "cat": "WEATHER", "lang": "en"},
    {"url": "https://news.google.com/rss/search?q=maldives+tourism+travel&hl=en&gl=US&ceid=US:en", "cat": "TOURISM", "lang": "en"},
]

CAT_CONFIG = {
    "LOCAL":   {"label": "🇲🇻  LOCAL NEWS",   "color": (41,171,226)},
    "FOOTBALL":{"label": "⚽  FOOTBALL",       "color": (34,180,80)},
    "WORLD":   {"label": "🌍  WORLD NEWS",     "color": (220,80,60)},
    "DISASTER":{"label": "🚨  DISASTER ALERT", "color": (220,120,0)},
    "WEATHER": {"label": "🌤️  WEATHER",        "color": (100,180,240)},
    "TOURISM": {"label": "✈️  TOURISM",        "color": (160,80,220)},
}

BREAKING_KEYWORDS = [
    "breaking","urgent","alert","killed","dead","dies","explosion","crash","attack",
    "arrested","emergency","disaster","flood","fire","missing","tsunami","earthquake",
    "accident","murder","bomb","resign","crisis","leaked","scandal","raid","collapse",
    "shot","war","strike","invasion","hostage","trapped","sinking"
]

# ── Storage ───────────────────────────────────────────────────────────────────
DATA_DIR  = "/data"
os.makedirs(DATA_DIR, exist_ok=True)
SEEN_FILE = os.path.join(DATA_DIR, "seen_articles.json")

def load_seen():
    try:
        if os.path.exists(SEEN_FILE):
            with open(SEEN_FILE) as f: return set(json.load(f))
    except: pass
    return set()

def save_seen(seen):
    try:
        with open(SEEN_FILE,"w") as f: json.dump(list(seen)[-1000:], f)
    except: pass

# ── Memory ────────────────────────────────────────────────────────────────────
recent_posts = []
user_conversations = {}

def remember_post(title, cat, timestamp):
    recent_posts.append({"title":title,"cat":cat,"time":timestamp})
    if len(recent_posts) > 50: recent_posts.pop(0)

def get_conversation(uid):
    if uid not in user_conversations: user_conversations[uid] = []
    return user_conversations[uid]

def add_to_conversation(uid, role, content):
    conv = get_conversation(uid)
    conv.append({"role":role,"content":content})
    if len(conv) > 10: user_conversations[uid] = conv[-10:]

# ── Helpers ───────────────────────────────────────────────────────────────────
def get_mvt_hour(): return (datetime.utcnow().hour + 5) % 24
def is_day_mode(): return 7 <= get_mvt_hour() < 18
