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
    {"url": "https://news.google.com/rss/search?q=major+world+news+war+crisis+ceasefire+earthquake+election&hl=en&gl=US&ceid=US:en", "cat": "WORLD", "lang": "en"},
    {"url": "https://news.google.com/rss/search?q=Iran+US+Israel+Gaza+Russia+Ukraine+latest&hl=en&gl=US&ceid=US:en", "cat": "WORLD", "lang": "en"},
    {"url": "https://news.google.com/rss/search?q=South+Asia+India+Sri+Lanka+Bangladesh+Maldives&hl=en-MV&gl=MV&ceid=MV:en", "cat": "WORLD", "lang": "en"},
    {"url": "https://news.google.com/rss/search?q=global+economy+oil+prices+dollar+shipping+Indian+Ocean&hl=en&gl=US&ceid=US:en", "cat": "WORLD", "lang": "en"},
    {"url": "https://news.google.com/rss/search?q=earthquake+tsunami+disaster+flood+cyclone+latest&hl=en&gl=US&ceid=US:en", "cat": "DISASTER", "lang": "en"},
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

# ── Source ladder configs ────────────────────────────────────────────────────
RSS_CANDIDATE_PATHS = ["/feed", "/rss", "/rss.xml", "/feed.xml", "/index.xml", "/en/feed", "/rss/news"]

LOCAL_RSS_RECOVERY_SOURCES = [
    {"domain": "https://mihaaru.com",        "source": "Mihaaru", "cat": "LOCAL", "lang": "dv"},
    {"domain": "https://avas.mv",            "source": "Avas",    "cat": "LOCAL", "lang": "dv"},
    {"domain": "https://sun.mv",             "source": "Sun",     "cat": "LOCAL", "lang": "dv"},
    {"domain": "https://sunonline.mv",       "source": "Sun",     "cat": "LOCAL", "lang": "dv"},
    {"domain": "https://english.sun.mv",     "source": "Sun",     "cat": "LOCAL", "lang": "en"},
    {"domain": "https://psmnews.mv",         "source": "PSM News","cat": "LOCAL", "lang": "en"},
    {"domain": "https://edition.mv",         "source": "Edition", "cat": "LOCAL", "lang": "en"},
    {"domain": "https://raajje.mv",          "source": "Raajje",  "cat": "LOCAL", "lang": "dv"},
    {"domain": "https://voice.mv",           "source": "VoiceMV", "cat": "LOCAL", "lang": "dv"},
]

LOCAL_GOOGLE_BACKUP_FEEDS = [
    {"url": "https://news.google.com/rss/search?q=site:mihaaru.com+Maldives&hl=dv-MV&gl=MV&ceid=MV:dv", "cat": "LOCAL", "lang": "dv", "source": "Mihaaru via Google"},
    {"url": "https://news.google.com/rss/search?q=site:avas.mv+Maldives&hl=dv-MV&gl=MV&ceid=MV:dv", "cat": "LOCAL", "lang": "dv", "source": "Avas via Google"},
    {"url": "https://news.google.com/rss/search?q=site:sun.mv+Maldives+OR+site:english.sun.mv+Maldives&hl=en-MV&gl=MV&ceid=MV:en", "cat": "LOCAL", "lang": "en", "source": "Sun via Google"},
    {"url": "https://news.google.com/rss/search?q=site:psmnews.mv+Maldives&hl=en-MV&gl=MV&ceid=MV:en", "cat": "LOCAL", "lang": "en", "source": "PSM via Google"},
    {"url": "https://news.google.com/rss/search?q=site:raajje.mv+Maldives&hl=en-MV&gl=MV&ceid=MV:en", "cat": "LOCAL", "lang": "en", "source": "Raajje via Google"},
]

WEB_LATEST_SOURCES = [
    {"url": "https://mihaaru.com/",          "source": "Mihaaru",    "cat": "LOCAL", "lang": "dv"},
    {"url": "https://avas.mv/",              "source": "Avas",       "cat": "LOCAL", "lang": "dv"},
    {"url": "https://sun.mv/",               "source": "Sun",        "cat": "LOCAL", "lang": "dv"},
    {"url": "https://english.sun.mv/",       "source": "Sun",        "cat": "LOCAL", "lang": "en"},
    {"url": "https://psmnews.mv/en",         "source": "PSM News",   "cat": "LOCAL", "lang": "en"},
    {"url": "https://raajje.mv/",            "source": "Raajje",     "cat": "LOCAL", "lang": "dv"},
    {"url": "https://voice.mv/",             "source": "VoiceMV",    "cat": "LOCAL", "lang": "dv"},
    {"url": "https://edition.mv/",           "source": "Edition",    "cat": "LOCAL", "lang": "en"},
    {"url": "https://maldivesvoice.com/",    "source": "Maldives Voice", "cat": "LOCAL", "lang": "en"},
    {"url": "https://thepress.mv/",          "source": "ThePress",   "cat": "LOCAL", "lang": "dv"},
    {"url": "https://www.mndf.gov.mv/",      "source": "MNDF",       "cat": "LOCAL", "lang": "en"},
    {"url": "https://www.police.gov.mv/",    "source": "Police",     "cat": "LOCAL", "lang": "en"},
    {"url": "https://presidency.gov.mv/",    "source": "Presidency", "cat": "LOCAL", "lang": "en"},
]

WORLD_MAJOR_KEYWORDS = [
    "war", "ceasefire", "airstrike", "missile", "attack", "invasion", "conflict",
    "earthquake", "tsunami", "cyclone", "flood", "volcano", "disaster",
    "election", "president", "prime minister", "government collapsed", "coup",
    "oil price", "dollar", "global economy", "shipping", "red sea", "indian ocean",
    "iran", "israel", "gaza", "palestine", "ukraine", "russia", "india", "sri lanka",
    "china", "us", "united states", "uk", "un", "world bank", "imf",
    "travel ban", "airport closed", "flight cancelled", "visa", "pandemic", "outbreak"
]

def is_major_world_news_text(text):
    t = (text or "").lower()
    if any(k in t for k in ["world cup", "premier league", "champions league", "celebrity", "movie", "concert"]):
        return False
    return any(k in t for k in WORLD_MAJOR_KEYWORDS)

def parse_extra_telegram_sources():
    out = []
    raw = os.environ.get("SAMUGA_EXTRA_TG_CHANNELS", "").strip()
    if not raw:
        return out
    for part in raw.split(","):
        bits = [b.strip() for b in part.split(":")]
        if len(bits) >= 2 and bits[0] and bits[1]:
            out.append({
                "handle": bits[0].lstrip("@"),
                "source": bits[1],
                "lang_hint": bits[2] if len(bits) >= 3 and bits[2] else "auto",
                "reliability": int(bits[3]) if len(bits) >= 4 and bits[3].isdigit() else 80,
            })
    return out


# ── Dhivehi Telegram channel list ────────────────────────────────────────────
DV_TELEGRAM_CHANNELS = [
    {"handle": "mihaarulive",   "source": "Mihaaru",  "reliability": 95},
    {"handle": "avasonline",    "source": "Avas",     "reliability": 88},
    {"handle": "sunonlinemv",   "source": "Sun",      "reliability": 92},
    {"handle": "raajjemvlive",  "source": "Raajje",   "reliability": 85},
    {"handle": "voicemaldives", "source": "VoiceMV",  "reliability": 80},
    {"handle": "mvplusmedia",   "source": "MV+",      "reliability": 82},
    {"handle": "mvcrisis",      "source": "MvCrisis", "reliability": 70},
] + parse_extra_telegram_sources()

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


def is_dhivehi(text):
    return any('\u0780' <= c <= '\u07BF' for c in str(text or ""))

def strip_source_links(text):
    s = str(text or "")
    s = re.sub(r"<a\s+[^>]*href=[\"'][^\"']+[\"'][^>]*>(.*?)</a>", r"\1", s, flags=re.I|re.S)
    s = re.sub(r"https?://\S+|www\.\S+", "", s, flags=re.I)
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()

def _caption_match_key(text):
    if not text:
        return ""
    import unicodedata as _ud
    t = str(text).lower()
    for junk in ["samuga media", "samuga creative", "@samugacommunity", "ސަމުގާ މީޑިއާ", "📡", "🇲🇻"]:
        t = t.replace(junk.lower(), " ")
    out = []
    for ch in t:
        if "\u0780" <= ch <= "\u07bf":
            out.append(ch)
        else:
            out.append(_ud.normalize("NFKD", ch).encode("ascii", "ignore").decode("ascii"))
    t = "".join(out)
    t = re.sub(r"[^a-z0-9\u0780-\u07bf ]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:60]

def _parse_any_datetime(value):
    if not value:
        return None
    try:
        dt = value
        if isinstance(dt, str):
            s = dt.strip()
            if not s:
                return None
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            try:
                dt = datetime.fromisoformat(s)
            except Exception:
                dt = parsedate_to_datetime(s)
        if getattr(dt, "tzinfo", None):
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return None

def entry_published_dt(entry):
    for key in ("published", "updated", "created"):
        dt = _parse_any_datetime(entry.get(key, ""))
        if dt:
            return dt
    return None

def _html_unescape(text):
    try:
        import html
        return html.unescape(text or "")
    except Exception:
        return str(text or "")

def _looks_like_ad(text):
    """Return True if text looks like an ad, promo, or job listing."""
    t = text.lower()
    return any(marker in t for marker in MVCRISIS_AD_MARKERS)

def is_fresh(entry, hours=3):
    try:
        dt = entry_published_dt(entry)
        if dt:
            return _utcnow() - dt < timedelta(hours=hours)
    except Exception as e:
        log.debug(f"is_fresh parse: {e}")
    return True

def _feed_source_name(url):
    u = url.lower()
    if "news.google.com" in u: return "Google News"
    if "mihaaru" in u:         return "Mihaaru"
    if "sunonline" in u:       return "SunOnline"
    if "sun.mv" in u:          return "Sun"
    if "psmnews" in u:         return "PSM News"
    if "presidency" in u:      return "Presidency"
    if "edition" in u:         return "Edition"
    if "avas" in u:            return "Avas"
    if "see.mv" in u:          return "See"
    if "maldivesindependent" in u: return "Maldives Independent"
    if "oneonline" in u:       return "One Online"
    if "maldivesvoice" in u:   return "Maldives Voice"
    if "visitmaldives" in u:   return "Visit Maldives"
    if "vnewsmv" in u:         return "VNews"
    if "raajjemv" in u:        return "Raajje"
    if "thepress_mv" in u or "thepress.mv" in u: return "ThePress"
    if "police.gov.mv" in u:   return "Police"
    if "mndf.gov.mv" in u:     return "MNDF"
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


def _extract_telegram_messages(html_text, limit=12):
    blocks = re.findall(r'<div class="tgme_widget_message[^>]*?>(.*?)(?=<div class="tgme_widget_message|</section>|\Z)', html_text, re.DOTALL)
    out = []
    for block in blocks:
        txt_m = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.DOTALL)
        if not txt_m:
            continue
        raw = txt_m.group(1)
        clean = re.sub(r"<br\s*/?>", "\n", raw)
        clean = re.sub(r"<[^>]+>", "", clean).strip()
        clean = _html_unescape(clean)
        dt = None
        time_m = re.search(r'<time[^>]+datetime="([^"]+)"', block)
        if time_m:
            dt = _parse_any_datetime(time_m.group(1))
        out.append((clean, dt or _utcnow()))
        if len(out) >= limit:
            break
    if not out:
        texts = re.findall(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', html_text, re.DOTALL)
        dates = [_parse_any_datetime(x) for x in re.findall(r'<time[^>]+datetime="([^"]+)"', html_text)]
        for i, raw in enumerate(texts[:limit]):
            clean = re.sub(r"<br\s*/?>", "\n", raw)
            clean = re.sub(r"<[^>]+>", "", clean).strip()
            clean = _html_unescape(clean)
            out.append((clean, dates[i] if i < len(dates) and dates[i] else _utcnow()))
    return out[:limit]

def fetch_mvcrisis():
    try:
        resp = requests.get("https://t.me/s/mvcrisis", timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return []
        articles, skipped_ads = [], 0
        for text, published in _extract_telegram_messages(resp.text, limit=15):
            if len(text) < 25:
                continue
            if _looks_like_ad(text):
                skipped_ads += 1
                continue
            art_id = "mvc_" + hashlib.md5((text[:80] + str(published)).encode()).hexdigest()[:10]
            lang = "dv" if any("ހ" <= ch <= "޿" for ch in text) else "en"
            articles.append({
                "id": art_id, "title": text[:150], "summary": text,
                "link": "https://t.me/mvcrisis", "source": "MvCrisis",
                "cat": "LOCAL", "lang": lang, "published": published
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
    try:
        url = f"https://t.me/s/{handle}"
        resp = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            log.debug(f"[FETCH] {source} Telegram: HTTP {resp.status_code}")
            return []
        articles = []
        for text, published in _extract_telegram_messages(resp.text, limit=12):
            if len(text) < 20:
                continue
            if _looks_like_ad(text):
                continue
            if (_utcnow() - published).total_seconds() > 12 * 3600:
                continue
            dv_chars = sum(1 for ch in text if "ހ" <= ch <= "޿")
            lang = "dv" if dv_chars >= 1 else "en"
            art_id = f"tg_{handle}_" + hashlib.md5((text[:80] + str(published)).encode()).hexdigest()[:10]
            articles.append({
                "id": art_id, "title": text[:150], "summary": text,
                "link": f"https://t.me/{handle}", "source": source,
                "cat": "LOCAL", "lang": lang, "reliability": reliability,
                "published": published
            })
        log.info(f"[FETCH] {source} Telegram: {len(articles)} items")
        return articles
    except Exception as e:
        log.error(f"[FETCH] {source} Telegram: {e}")
        return []


def fetch_all_dv_channels():
    """Fetch selected Telegram channels in parallel and dedupe same-source duplicates."""
    results = []
    lock    = threading.Lock()

    def _fetch(ch):
        arts = fetch_dv_telegram(ch["handle"], ch["source"], ch["reliability"])
        with lock:
            results.extend(arts)

    threads = [threading.Thread(target=_fetch, args=(ch,), daemon=True) for ch in DV_TELEGRAM_CHANNELS]
    for t in threads: t.start()
    for t in threads: t.join(timeout=15)

    deduped, seen = [], set()
    for a in results:
        key = f"{a.get('source','')}::{_caption_match_key(a.get('title',''))}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(a)
    return deduped



def _build_article_from_feed_entry(entry, fc, fallback_source=""):
    title = entry.get("title", "") or ""
    summary = entry.get("summary", title) or title
    lang = fc.get("lang", "en")
    if lang == "dv" and not is_dhivehi(title + " " + summary):
        lang = "en"
    entry_src = entry.get("source", {}).get("title", "") if isinstance(entry.get("source"), dict) else ""
    src_name = fc.get("source") or entry_src or fallback_source or _feed_source_name(fc.get("url", "")) or fc.get("cat", "LOCAL")
    return {
        "id": hashlib.md5((entry.get("link", "") or title).encode()).hexdigest(),
        "title": strip_source_links(title)[:180],
        "summary": strip_source_links(summary)[:1500],
        "link": entry.get("link", ""),
        "cat": fc.get("cat", "LOCAL"),
        "lang": lang,
        "source": src_name,
        "published": entry_published_dt(entry) or _utcnow(),
    }

def fetch_rss_feed_items(fc, limit=8, max_age_hours=3):
    out = []
    url = fc.get("url", "")
    if not url:
        return out
    try:
        feed = feedparser.parse(url)
        entries = getattr(feed, "entries", []) or []
        for entry in entries[:limit]:
            if not is_fresh(entry, hours=max_age_hours):
                continue
            a = _build_article_from_feed_entry(entry, fc)
            if a.get("title"):
                out.append(a)
    except Exception as e:
        log.debug(f"[RSS] feed failed {url}: {e}")
    return out

def fetch_local_rss_recovery(limit_per_source=4):
    articles, seen_keys = [], set()
    for src in LOCAL_RSS_RECOVERY_SOURCES:
        found_for_source = 0
        base = src["domain"].rstrip("/")
        for path in RSS_CANDIDATE_PATHS:
            fc = {"url": base + path, "cat": src.get("cat", "LOCAL"), "lang": src.get("lang", "en"), "source": src.get("source", "LOCAL")}
            items = fetch_rss_feed_items(fc, limit=limit_per_source, max_age_hours=3)
            for a in items:
                key = _caption_match_key(a.get("title", ""))
                if not key or key in seen_keys:
                    continue
                seen_keys.add(key)
                articles.append(a)
                found_for_source += 1
                if found_for_source >= limit_per_source:
                    break
            if found_for_source:
                log.info(f"[RSS] {src['source']} recovery working via {path}: {found_for_source} item(s)")
                break
    for fc in LOCAL_GOOGLE_BACKUP_FEEDS:
        kept = 0
        for a in fetch_rss_feed_items(fc, limit=limit_per_source, max_age_hours=3):
            key = _caption_match_key(a.get("title", ""))
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            articles.append(a)
            kept += 1
        if kept:
            log.info(f"[RSS] Google local backup {fc.get('source','')}: {kept} item(s)")
    return articles

def fetch_world_updates(limit=8):
    articles, seen_keys = [], set()
    for fc in WORLD_FEEDS:
        for a in fetch_rss_feed_items(fc, limit=limit, max_age_hours=3):
            text = a.get("title", "") + " " + a.get("summary", "")
            if not is_major_world_news_text(text):
                continue
            key = _caption_match_key(a.get("title", ""))
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            a["cat"] = "DISASTER" if a.get("cat") == "DISASTER" else "WORLD"
            a["source"] = a.get("source") or "World News"
            articles.append(a)
    if articles:
        log.info(f"[FETCH] World updates: {len(articles)} major item(s)")
    return articles

def fetch_latest_web_pages(limit_per_source=6):
    articles = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,dv;q=0.8",
        "Cache-Control": "no-cache",
    }
    try:
        from urllib.parse import urljoin, urlparse
    except Exception:
        return articles
    for src in WEB_LATEST_SOURCES:
        try:
            resp = requests.get(src["url"], timeout=12, headers=headers)
            if resp.status_code != 200 or not resp.text:
                log.debug(f"[FETCH] {src['source']} latest page HTTP {resp.status_code}")
                continue
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(resp.text, "html.parser")
                anchors = soup.find_all("a", href=True)
                candidates = []
                for a in anchors:
                    title = " ".join(a.get_text(" ", strip=True).split())
                    if len(title) < 18 or len(title) > 180:
                        continue
                    href = urljoin(src["url"], a.get("href", ""))
                    host = urlparse(src["url"]).netloc.replace("www.", "")
                    if host not in urlparse(href).netloc:
                        continue
                    candidates.append((title, href))
            except Exception:
                candidates = []
                for href, title in re.findall(r"""<a[^>]+href=['"]([^'"]+)['"][^>]*>(.*?)</a>""", resp.text, re.I|re.S):
                    title = re.sub(r"<[^>]+>", " ", title)
                    title = " ".join(_html_unescape(title).split())
                    if 18 <= len(title) <= 180:
                        candidates.append((title, urljoin(src["url"], href)))
            seen_local, kept = set(), 0
            for title, href in candidates:
                key = re.sub(r"\W+", " ", title.lower()).strip()[:70]
                if key in seen_local:
                    continue
                seen_local.add(key)
                if _looks_like_ad(title):
                    continue
                lang = "dv" if is_dhivehi(title) else src.get("lang","en")
                art_id = "web_" + hashlib.md5((href or title).encode()).hexdigest()[:12]
                articles.append({"id": art_id, "title": title[:150], "summary": title, "link": href, "source": src["source"], "cat": src.get("cat","LOCAL"), "lang": lang, "published": _utcnow()})
                kept += 1
                if kept >= limit_per_source:
                    break
            if kept:
                log.info(f"[FETCH] {src['source']} latest page: {kept} headline(s)")
        except Exception as e:
            log.debug(f"[FETCH] latest page {src.get('source')}: {e}")
    return articles

# ═══════════════════════════════════════════════════════════════════════════════
# Master fetch — combines all sources
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_news():
    articles, seen_titles = [], set()

    def _add_article(a):
        if not a or not a.get("title"):
            return
        key = _caption_match_key(a.get("title", "")) or re.sub(r"\W+", " ", a["title"].lower()).strip()[:70]
        if key and key not in seen_titles:
            seen_titles.add(key)
            articles.append(a)

    for a in fetch_mvcrisis():
        _add_article(a)
    for a in fetch_all_dv_channels():
        _add_article(a)
    for a in fetch_latest_web_pages():
        _add_article(a)
    for a in fetch_local_rss_recovery():
        _add_article(a)
    for fc in RSS_FEEDS:
        for a in fetch_rss_feed_items(fc, limit=10, max_age_hours=3):
            _add_article(a)
    for a in fetch_world_updates(limit=6):
        _add_article(a)

    log.info(f"Found {len(articles)} fresh articles from source ladder")
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
                if title and is_fresh(entry, hours=3):
                    headlines.append(f"• [{fc['cat']}] {title}")
            if len(headlines) >= 10:
                break
    except Exception as e:
        log.debug(f"get_local_headlines: {e}")
    return headlines[:10]



# ── Public output safety gate ────────────────────────────────────────────────
# Prevent AI instruction/template placeholders from reaching cards, Telegram,
# Buffer/socials, or website text.
_PLACEHOLDER_RE = re.compile(
    r"(\[[^\]\n]{0,80}(rewritten|punchy|specific keyword|image keyword|caption|post|news|summary|text)[^\]\n]{0,80}\]"
    r"|<[^>\n]{0,80}(rewritten|caption|post|news|summary|image|keyword)[^>\n]{0,80}>"
    r"|\b(TEXT|IMAGE)\s*:\s*(\[[^\]\n]+\]|<[^>\n]+>)"
    r"|\bplaceholder\b"
    r"|3-sentence punchy post"
    r"|specific 2-3 word pexels image keyword)",
    re.IGNORECASE,
)

def has_public_placeholder(text):
    """True if text contains AI prompt/template placeholder residue."""
    s = str(text or "").strip()
    if not s:
        return False
    return bool(_PLACEHOLDER_RE.search(s))

def public_text_is_safe(text, min_chars=6):
    """Basic safety check for text that can go public."""
    s = str(text or "").strip()
    if len(s) < min_chars:
        return False
    if has_public_placeholder(s):
        return False
    return True

def fallback_rewritten_news(title="", summary="", max_len=420):
    """Safe non-AI fallback for cards/captions when model output is bad."""
    title = re.sub(r"\s+", " ", str(title or "")).strip()
    summary = re.sub(r"\s+", " ", str(summary or "")).strip()
    if summary and summary.lower() != title.lower():
        text = summary
        if title and not text.lower().startswith(title.lower()[:40]):
            text = f"{title}. {summary}"
    else:
        text = title
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0].rstrip(".,;:") + "..."
    return text or "Samuga Media is following this developing story."

def clean_ai_line(text):
    """Remove wrapper quotes/code fences and obvious labels from AI line output."""
    s = str(text or "").strip()
    s = s.strip("`").strip()
    s = re.sub(r"^\s*(TEXT|POST|CAPTION|SUMMARY)\s*:\s*", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"^\s*(IMAGE|KEYWORD|PEXELS)\s*:\s*", "", s, flags=re.IGNORECASE).strip()
    return s.strip().strip('"').strip("'").strip()

def safe_image_keyword(keyword, cat="LOCAL"):
    """Return a safe Pexels keyword, never a model placeholder."""
    kw = clean_ai_line(keyword)
    if not public_text_is_safe(kw, min_chars=3) or len(kw.split()) > 6:
        return DEFAULT_KEYWORDS.get(cat, "maldives news")
    return kw[:80]

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

Return exactly two lines, with no square brackets and no placeholders:
TEXT: write the final news post in natural English
IMAGE: write only a real 2-3 word Pexels search keyword"""

    try:
        msg = ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        text = msg.content[0].text.strip()
        rewritten, keyword = "", DEFAULT_KEYWORDS.get(cat, "maldives news")
        for line in text.split("\n"):
            line = line.strip()
            if line.upper().startswith("TEXT:"):
                rewritten = clean_ai_line(line[5:])
            elif line.upper().startswith("IMAGE:"):
                keyword = safe_image_keyword(line[6:], cat=cat)

        if not public_text_is_safe(rewritten):
            log.warning(f"rewrite_news blocked unsafe AI output: {rewritten[:90]}")
            rewritten = fallback_rewritten_news(title, summary)
        keyword = safe_image_keyword(keyword, cat=cat)
        return rewritten, keyword
    except Exception as e:
        log.error(f"rewrite_news: {e}")
        return fallback_rewritten_news(title, summary), DEFAULT_KEYWORDS.get(cat, "maldives news")
