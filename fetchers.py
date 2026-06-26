"""
fetchers.py — Samuga AI News Fetcher Module
Extracted from bot.py v7.0

Contains:
  - RSS_FEEDS / LOCAL_FEEDS / SPORTS_FEEDS / WORLD_FEEDS / LIFESTYLE_FEEDS
  - fetch_mvcrisis()         MvCrisis Telegram scraper (breaking news #1 source)
  - fetch_dv_telegram()      Single Dhivehi Telegram channel scraper
  - fetch_all_dv_channels()  All 5 Dhivehi channels in parallel
  - fetch_news()             Master fetch — all sources combined
  - get_local_headlines()    Quick headline list for briefs/AI context
  - rewrite_news()           Claude rewrite for Telegram caption
  - gemini_translate()       Dhivehi → English via Gemini
  - _looks_like_ad()         Ad/promo filter
  - _feed_source_name()      URL → clean source name

Dependencies injected by bot.py at startup:
  _gemini_post, GEMINI_API_KEY, ANTHROPIC_API_KEY, ai (anthropic client)
"""

import os, hashlib, logging, threading, feedparser, requests, re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

log = logging.getLogger(__name__)

# ── Injected by bot.py after import ──────────────────────────────────────────
_gemini_post     = None   # bot.py: fetchers._gemini_post = _gemini_post
ai               = None   # bot.py: fetchers.ai = ai
GEMINI_API_KEY   = os.environ.get("GEMINI_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


# ═══════════════════════════════════════════════════════════════════════════════
# RSS Feed lists
# ═══════════════════════════════════════════════════════════════════════════════

LOCAL_FEEDS = [
    # Tier 1 — Breaking/Crisis
    {"url": "https://news.google.com/rss/search?q=maldives+breaking+incident+accident+arrest&hl=en-MV&gl=MV&ceid=MV:en", "cat": "DISASTER", "lang": "en"},
    # Tier 2 — English sources
    {"url": "https://see.mv/feed",                  "cat": "LOCAL", "lang": "en"},
    {"url": "https://english.sun.mv/feed",           "cat": "LOCAL", "lang": "en"},
    {"url": "https://edition.mv/feed",               "cat": "LOCAL", "lang": "en"},
    {"url": "https://maldivesindependent.com/feed",  "cat": "LOCAL", "lang": "en"},
    {"url": "https://oneonline.mv/en/feed",          "cat": "LOCAL", "lang": "en"},
    {"url": "https://psmnews.mv/en/feed",            "cat": "LOCAL", "lang": "en"},
    {"url": "https://maldivesvoice.com/feed",        "cat": "LOCAL", "lang": "en"},
    {"url": "https://presidency.gov.mv/feed",        "cat": "LOCAL", "lang": "en"},
    # Tier 3 — Dhivehi sources
    {"url": "https://sunonline.mv/feed",             "cat": "LOCAL", "lang": "dv"},
    {"url": "https://mihaaru.com/rss",               "cat": "LOCAL", "lang": "dv"},
    {"url": "https://avas.mv/feed",                  "cat": "LOCAL", "lang": "dv"},
    {"url": "https://news.google.com/rss/search?q=maldives+politics+parliament+government&hl=en-MV&gl=MV&ceid=MV:en", "cat": "LOCAL", "lang": "en"},
    {"url": "https://news.google.com/rss/search?q=maldives+economy+finance+business&hl=en-MV&gl=MV&ceid=MV:en",       "cat": "LOCAL", "lang": "en"},
]

SPORTS_FEEDS = [
    {"url": "https://news.google.com/rss/search?q=maldives+football+sports&hl=en-MV&gl=MV&ceid=MV:en", "cat": "SPORTS", "lang": "en"},
    {"url": "https://news.google.com/rss/search?q=world+cup+2026+results&hl=en&gl=US&ceid=US:en",       "cat": "SPORTS", "lang": "en"},
]

WORLD_FEEDS = [
    {"url": "https://news.google.com/rss/search?q=war+conflict+crisis+2026&hl=en&gl=US&ceid=US:en",    "cat": "WORLD",   "lang": "en"},
    {"url": "https://news.google.com/rss/search?q=earthquake+tsunami+disaster&hl=en&gl=US&ceid=US:en", "cat": "DISASTER", "lang": "en"},
]

LIFESTYLE_FEEDS = [
    {"url": "https://visitmaldives.com/feed",                                                                    "cat": "TOURISM", "lang": "en"},
    {"url": "https://news.google.com/rss/search?q=maldives+tourism+travel+resort&hl=en-MV&gl=MV&ceid=MV:en",  "cat": "TOURISM", "lang": "en"},
    {"url": "https://news.google.com/rss/search?q=maldives+weather+storm&hl=en-MV&gl=MV&ceid=MV:en",          "cat": "WEATHER", "lang": "en"},
]

RSS_FEEDS = LOCAL_FEEDS + SPORTS_FEEDS + WORLD_FEEDS + LIFESTYLE_FEEDS

# ── Default image keywords per category ──────────────────────────────────────
DEFAULT_KEYWORDS = {
    "LOCAL":     "maldives government",
    "FOOTBALL":  "football stadium",
    "WORLD":     "world politics",
    "DISASTER":  "emergency rescue",
    "WEATHER":   "tropical weather",
    "TOURISM":   "maldives resort beach",
}

# ── Dhivehi Telegram channel list ────────────────────────────────────────────
DV_TELEGRAM_CHANNELS = [
    {"handle": "mihaarulive",   "source": "Mihaaru",  "reliability": 95},
    {"handle": "avasonline",    "source": "Avas",     "reliability": 88},
    {"handle": "raajjemvlive",  "source": "Raajje",   "reliability": 85},
    {"handle": "voicemaldives", "source": "VoiceMV",  "reliability": 80},
    {"handle": "mvplusmedia",   "source": "MV+",      "reliability": 82},
]

# ── Ad/promo filter keywords ──────────────────────────────────────────────────
MVCRISIS_AD_MARKERS = [
    "hire", "rent", "for sale", "available", "booking", "book now", "contact",
    "call now", "whatsapp", "viber", "discount", "offer", "promo", "cheap",
    "price", "mvr ", "rufiyaa ", "delivery", "order now", "dm ", "inbox",
    "trip", "package", "tour", "charter", "ferry service", "speedboat",
    "submarine", "diving", "snorkeling", "fishing trip", "safari", "liveaboard",
    "accommodation", "guesthouse", "room available", "bed available",
    "apply now", "vacancy", "hiring", "wanted", "looking for", "job opening",
]


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)

def _looks_like_ad(text):
    """Return True if text looks like an ad, promo, or job listing."""
    t = text.lower()
    return any(marker in t for marker in MVCRISIS_AD_MARKERS)

def is_fresh(entry, hours=24):
    """Return True if feed entry was published within the last N hours."""
    try:
        pub = entry.get("published", "")
        if pub:
            dt = parsedate_to_datetime(pub)
            if dt.tzinfo:
                dt = dt.replace(tzinfo=None)
            return _utcnow() - dt < timedelta(hours=hours)
    except Exception as e:
        log.debug(f"is_fresh parse: {e}")
    return True

def _feed_source_name(url):
    """Map a feed URL to a clean source name for reliability scoring + display."""
    u = url.lower()
    if "news.google.com"       in u: return "Google News"
    if "mihaaru"               in u: return "Mihaaru"
    if "sunonline"             in u: return "SunOnline"
    if "sun.mv"                in u: return "Sun"
    if "psmnews"               in u: return "PSM News"
    if "presidency"            in u: return "Presidency"
    if "edition"               in u: return "Edition"
    if "avas"                  in u: return "Avas"
    if "see.mv"                in u: return "See"
    if "maldivesindependent"   in u: return "Maldives Independent"
    if "oneonline"             in u: return "One Online"
    if "maldivesvoice"         in u: return "Maldives Voice"
    if "visitmaldives"         in u: return "Visit Maldives"
    if "vnewsmv"               in u: return "VNews"
    if "raajjemv"              in u: return "Raajje"
    if "thepress_mv"           in u: return "ThePress"
    return ""


# ═══════════════════════════════════════════════════════════════════════════════
# Gemini translate (Dhivehi RSS → English so scoring works)
# ═══════════════════════════════════════════════════════════════════════════════

def gemini_translate(text):
    """Translate Dhivehi text to English using Gemini (with model fallback)."""
    if not GEMINI_API_KEY or not _gemini_post:
        return text
    result = _gemini_post(
        f"Translate this Dhivehi text to English. Return ONLY the English translation:\n\n{text}"
    )
    return result if result else text


# ═══════════════════════════════════════════════════════════════════════════════
# MvCrisis scraper — #1 Maldives breaking news Telegram channel
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_mvcrisis():
    """
    Scrape the public MvCrisis Telegram channel.
    Filters out ads/promos. Returns list of article dicts.
    """
    try:
        resp = requests.get(
            "https://t.me/s/mvcrisis",
            timeout=12,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        if resp.status_code != 200:
            log.warning(f"MvCrisis HTTP {resp.status_code}")
            return []

        texts = re.findall(
            r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
            resp.text, re.DOTALL
        )
        articles    = []
        skipped_ads = 0
        for raw in texts[:20]:
            text = re.sub(r"<[^>]+>", "", raw).strip()
            text = (text.replace("&amp;", "&").replace("&#39;", "'")
                    .replace("&quot;", '"').replace("&lt;", "<").replace("&gt;", ">"))
            if len(text) < 20:
                continue
            if _looks_like_ad(text):
                skipped_ads += 1
                continue
            art_id = "mvc_" + hashlib.md5(text[:60].encode()).hexdigest()[:8]
            lang   = "dv" if any("ހ" <= ch <= "޿" for ch in text) else "en"
            articles.append({
                "id":        art_id,
                "title":     text[:150],
                "summary":   text,
                "link":      "https://t.me/mvcrisis",
                "source":    "MvCrisis",
                "cat":       "LOCAL",
                "lang":      lang,
                "published": _utcnow(),
            })
        log.info(f"📡 MvCrisis: {len(articles)} news kept, {skipped_ads} ads skipped")
        return articles
    except Exception as e:
        log.error(f"MvCrisis fetch: {e}")
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# Dhivehi Telegram channel scrapers
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_dv_telegram(handle, source, reliability=80):
    """
    Scrape a single public Dhivehi Telegram channel.
    Returns articles with lang='dv' where Thaana is detected.
    """
    try:
        url  = f"https://t.me/s/{handle}"
        resp = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            log.debug(f"[FETCH] {source} Telegram: HTTP {resp.status_code}")
            return []

        texts = re.findall(
            r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
            resp.text, re.DOTALL
        )
        articles = []
        for raw in texts[:12]:
            text = re.sub(r"<[^>]+>", "", raw).strip()
            text = (text.replace("&amp;", "&").replace("&#39;", "'")
                    .replace("&quot;", '"').replace("&lt;", "<").replace("&gt;", ">"))
            if len(text) < 20:
                continue
            if _looks_like_ad(text):
                continue
            dv_chars = sum(1 for ch in text if "ހ" <= ch <= "޿")
            lang     = "dv" if dv_chars >= 1 else "en"
            art_id   = f"tg_{handle}_" + hashlib.md5(text[:60].encode()).hexdigest()[:8]
            articles.append({
                "id":          art_id,
                "title":       text[:150],
                "summary":     text,
                "link":        f"https://t.me/{handle}",
                "source":      source,
                "cat":         "LOCAL",
                "lang":        lang,
                "reliability": reliability,
                "published":   _utcnow(),
            })
        log.info(f"[FETCH] {source} Telegram: {len(articles)} items")
        return articles
    except Exception as e:
        log.error(f"[FETCH] {source} Telegram: {e}")
        return []


def fetch_all_dv_channels():
    """Fetch all Dhivehi Telegram channels in parallel threads."""
    results = []
    lock    = threading.Lock()

    def _fetch(ch):
        arts = fetch_dv_telegram(ch["handle"], ch["source"], ch["reliability"])
        with lock:
            results.extend(arts)

    threads = [
        threading.Thread(target=_fetch, args=(ch,), daemon=True)
        for ch in DV_TELEGRAM_CHANNELS
    ]
    for t in threads: t.start()
    for t in threads: t.join(timeout=15)
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# Master fetch — combines all sources
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_news():
    """
    Fetch from all sources:
      1. MvCrisis (breaking, highest priority)
      2. Dhivehi Telegram channels (Mihaaru, Avas, Raajje, VoiceMV, MV+)
      3. RSS feeds (English + Dhivehi outlets)
    Returns deduplicated list of article dicts.
    """
    articles, seen_titles = [], set()

    # 1. MvCrisis first — #1 Maldives breaking news source
    for a in fetch_mvcrisis():
        if a["title"] not in seen_titles:
            seen_titles.add(a["title"])
            articles.append(a)

    # 2. Dhivehi Telegram channels — native Dhivehi content
    for a in fetch_all_dv_channels():
        if a["title"] not in seen_titles:
            seen_titles.add(a["title"])
            articles.append(a)

    # 3. RSS feeds
    for fc in RSS_FEEDS:
        try:
            feed = feedparser.parse(fc["url"])
            for entry in feed.entries[:10]:
                title   = entry.get("title", "")
                summary = entry.get("summary", title)

                # Translate Dhivehi RSS titles to English for scoring
                if fc["lang"] == "dv":
                    title   = gemini_translate(title)
                    summary = gemini_translate(summary[:300])

                key = title.lower()[:50]
                if key in seen_titles or not is_fresh(entry):
                    continue
                seen_titles.add(key)

                # Derive clean source name
                entry_src = (entry.get("source", {}).get("title", "")
                             if isinstance(entry.get("source"), dict) else "")
                feed_src  = _feed_source_name(fc["url"])
                src_name  = entry_src or feed_src or fc["cat"]

                articles.append({
                    "id":      hashlib.md5(entry.get("link", title).encode()).hexdigest(),
                    "title":   title,
                    "summary": summary,
                    "link":    entry.get("link", ""),
                    "cat":     fc["cat"],
                    "lang":    fc["lang"],
                    "source":  src_name,
                })
        except Exception as e:
            log.error(f"Feed error: {e}")

    log.info(f"[FETCH] Found {len(articles)} fresh articles from all sources")
    return articles


# ═══════════════════════════════════════════════════════════════════════════════
# Quick headline list — used by AI briefs and chat context
# ═══════════════════════════════════════════════════════════════════════════════

def get_local_headlines():
    """
    Pull the top 10 recent headlines from the first 5 RSS feeds.
    Used for morning brief, night summary, and AI chat context injection.
    """
    headlines = []
    try:
        for fc in RSS_FEEDS[:5]:
            feed = feedparser.parse(fc["url"])
            for entry in feed.entries[:3]:
                title = entry.get("title", "")
                if title and is_fresh(entry, hours=12):
                    headlines.append(f"• [{fc['cat']}] {title}")
            if len(headlines) >= 10:
                break
    except Exception as e:
        log.debug(f"get_local_headlines: {e}")
    return headlines[:10]


# ═══════════════════════════════════════════════════════════════════════════════
# Claude rewrite — formats article into punchy Telegram caption
# ═══════════════════════════════════════════════════════════════════════════════

def rewrite_news(title, summary, cat):
    """
    Use Claude Haiku to rewrite a news article into a punchy Telegram caption.
    Also returns a Pexels image keyword for the background image.
    Returns (rewritten_text, image_keyword).
    """
    if not ai:
        return summary or title, DEFAULT_KEYWORDS.get(cat, "maldives news")

    cat_ctx = {
        "LOCAL":    "local Maldivian news",
        "FOOTBALL": "football news",
        "WORLD":    "world news",
        "DISASTER": "disaster/emergency",
        "WEATHER":  "weather news",
        "TOURISM":  "tourism news",
    }.get(cat, "news")

    extra = (
        "Note: Only headline available. Expand with relevant context."
        if not summary or summary.strip() == title.strip() or len(summary) < 30
        else ""
    )

    prompt = f"""You are a news writer for Samuga Media, a Maldivian digital media outlet.
Rewrite this {cat_ctx} into a short punchy engaging English Telegram post.
- Max 3 sentences, clear and direct, no hashtags, no emojis, professional
- IMPORTANT: Use gender-neutral terms (they/their, "the accused", "the suspect", "the individual") unless the original text explicitly states gender. Do not assume gender from names.
{extra}
Also give a specific 2-3 word Pexels image keyword for this topic.

Title: {title}
Summary: {summary}

Respond EXACTLY:
TEXT: [rewritten news]
IMAGE: [specific keyword]"""

    try:
        msg = ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        text = msg.content[0].text.strip()
        rewritten, keyword = "", DEFAULT_KEYWORDS.get(cat, "maldives news")
        for line in text.split("\n"):
            if line.startswith("TEXT:"):
                rewritten = line[5:].strip()
            elif line.startswith("IMAGE:"):
                kw = line[6:].strip()
                if kw:
                    keyword = kw
        return rewritten or summary or title, keyword
    except Exception as e:
        log.error(f"rewrite_news: {e}")
        return summary or title, DEFAULT_KEYWORDS.get(cat, "maldives news")
