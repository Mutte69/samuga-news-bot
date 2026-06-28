"""
story_builder.py — Samuga Story Builder v1.0
Full article generation from Telegram headlines.

WHAT IT DOES:
  When multiple Telegram channels report the same headline (corroborated),
  this module:
    1. Searches for the full story (RSS feeds first, then web search)
    2. Uses Gemini to write a complete EN article (3-4 paragraphs)
    3. Uses Gemini to write a complete DV article (proper Thaana)
    4. Returns both for Content Lab + website publishing

  Also handles:
    - Latin Thaana detection and proper Thaana output
    - USD/USDT rate parsing from exchange channels
    - Website article link generation for card captions

Injection pattern: same as fetchers.py
  _gemini_post   = bot.py's Gemini function
  kv_get/kv_set  = db functions
  SAMUGA_WEBSITE = "https://samugamedia.com"
"""

import hashlib, logging, json, re, time, requests
from datetime import datetime, timezone
from urllib.parse import quote_plus

log = logging.getLogger(__name__)

# ── Injected by bot.py ────────────────────────────────────────────────────────
_gemini_post   = None
kv_get         = None
kv_set         = None
SAMUGA_WEBSITE = "https://samugamedia.com"
GEMINI_API_KEY = ""

# ── Config ────────────────────────────────────────────────────────────────────
MIN_SOURCES_FOR_FULL_ARTICLE = 2   # corroboration threshold
ARTICLE_CACHE_TTL = 7200           # don't regenerate same story for 2h
_article_cache = {}                # headline_hash -> {_t, en_article, dv_article}

# ── Latin Thaana detection ────────────────────────────────────────────────────
LATIN_THAANA_PATTERNS = [
    r'\braajje\b', r'\bmale\b', r'\bdhivehi\b', r'\bsarukaar\b',
    r'\bminister\b', r'\brayyithun\b', r'\bhukumeh\b', r'\bfenaka\b',
    r'\bmagaameh\b', r'\bvazeefaa\b', r'\bahuge\b', r'\bkuri\b',
]

def _is_latin_thaana(text):
    """Detect if text is Dhivehi written in Latin script (romanized)."""
    if not text:
        return False
    t = text.lower()
    hits = sum(1 for p in LATIN_THAANA_PATTERNS if re.search(p, t))
    return hits >= 2

def _has_thaana(text):
    """Detect actual Thaana Unicode script."""
    return any('\u0780' <= c <= '\u07BF' for c in (text or ''))


# ── USD/USDT Rate Parser ──────────────────────────────────────────────────────
def parse_rate_update(text, source=""):
    """
    Parse USD/USDT rate from exchange channel posts.
    Returns dict with rate info or None if not a rate post.
    """
    if not text:
        return None
    t = text.lower()
    patterns = [
        r'(?:usd|dollar|$)\s*[:\-]?\s*([\d]+(?:\.\d+)?)',
        r'([\d]+(?:\.\d+)?)\s*(?:mvr|rf|rufiyaa)',
        r'buying\s*[:\-]?\s*([\d]+(?:\.\d+)?)',
        r'selling\s*[:\-]?\s*([\d]+(?:\.\d+)?)',
        r'usdt\s*[:\-]?\s*([\d]+(?:\.\d+)?)',
        r'(?:rate|exchange)\s*[:\-]?\s*([\d]+(?:\.\d+)?)',
    ]
    rates = []
    for p in patterns:
        m = re.search(p, t)
        if m:
            try:
                val = float(m.group(1))
                if 15.0 <= val <= 25.0:  # sane MVR range for USD
                    rates.append(val)
            except Exception:
                pass
    if not rates:
        return None
    is_usdt = 'usdt' in t or 'tether' in t
    return {
        "rate": rates[0],
        "currency": "USDT" if is_usdt else "USD",
        "source": source,
        "raw": text[:200],
        "type": "rate_update",
    }


def format_rate_card(rate_data):
    """Format a rate update into a card-ready article dict."""
    rate = rate_data["rate"]
    currency = rate_data["currency"]
    source = rate_data["source"]
    title = f"{currency}/MVR Black Market Rate: {rate:.2f}"
    summary = (
        f"The current {currency} to MVR exchange rate in the informal market "
        f"is reported at MVR {rate:.2f}, according to {source}. "
        f"This reflects the black market rate, which may differ from the official "
        f"Maldives Monetary Authority (MMA) rate."
    )
    return {
        "title": title,
        "summary": summary,
        "cat": "BUSINESS",
        "lang": "en",
        "_rate_data": rate_data,
        "_is_rate_update": True,
    }


# ── Full Story Search ─────────────────────────────────────────────────────────
def _search_google_news_for_story(headline):
    """Search Google News RSS for a specific headline to find full story."""
    try:
        query = quote_plus(f"{headline} Maldives")
        url = f"https://news.google.com/rss/search?q={query}&hl=en&gl=MV&ceid=MV:en"
        import feedparser
        feed = feedparser.parse(url)
        results = []
        for e in feed.entries[:5]:
            results.append({
                "title": e.get("title", ""),
                "link": e.get("link", ""),
                "summary": e.get("summary", ""),
            })
        return results
    except Exception as ex:
        log.debug(f"[STORY] Google News search failed: {ex}")
        return []


def _fetch_article_body(url):
    """Try to scrape full article body from a URL."""
    try:
        from samuga_scraper import semantic_scrape
        result = semantic_scrape(url, source="story_builder")
        if not result.get("error") and len(result.get("summary", "")) > 300:
            return result.get("summary", "")
    except Exception:
        pass
    return ""


def find_full_story(headline, existing_summary=""):
    """
    Try to find the full story for a headline.
    Returns the best body text available.
    Priority: existing long summary > Google News > scraped article > None
    """
    # If we already have a decent body, use it
    if existing_summary and len(existing_summary) > 400:
        return existing_summary

    # Search Google News
    results = _search_google_news_for_story(headline)
    for r in results:
        # Check if title matches reasonably
        h_words = set(headline.lower().split())
        r_words = set(r["title"].lower().split())
        overlap = len(h_words & r_words) / max(len(h_words), 1)
        if overlap >= 0.4:
            # Try to scrape the full article
            if r.get("link"):
                body = _fetch_article_body(r["link"])
                if body and len(body) > 300:
                    log.info(f"[STORY] Found full body via Google News: {len(body)} chars")
                    return body
            # Use the summary snippet if available
            if r.get("summary") and len(r["summary"]) > 100:
                return r["summary"]

    log.info(f"[STORY] No full body found for: {headline[:60]}")
    return existing_summary or ""


# ── Gemini Article Writer ─────────────────────────────────────────────────────
_EN_ARTICLE_PROMPT = """You are a professional journalist at Samuga Media, a Maldivian news outlet.
Write a complete news article in English based on the information below.

Requirements:
- 3-4 clear paragraphs
- Professional journalistic style
- Include the key facts: who, what, when, where, why
- Do NOT make up specific numbers, names, or quotes not mentioned in the source
- If information is limited, write what is confirmed and note that details are developing
- End with context or background if relevant to Maldives audience
- Maximum 300 words

Headline: {headline}
Source information: {body}
Number of sources reporting this: {source_count}

Write the full article now (no headline, just the body paragraphs):"""

_DV_ARTICLE_PROMPT = """ތިބާ އަކީ ދިވެހި ނޫހެއް ކަމަށްވާ ސަމޫގާ މީޑިއާގެ ޕްރޮފެޝަނަލް ނޫސްވެރިއެކެވެ.
ތިރިއަށް ދީފައިވާ ޚަބަރުގެ ތަފްސީލުތައް ބޭނުންކޮށްގެން ދިވެހި ބަހުން ފުރިހަމަ ޚަބަރެއް ލިޔެ ދޭށެވެ.

ލިޔުމުގެ ތަރުތީބު:
- 3 ނުވަތަ 4 ޕެރެގްރާފް
- ރަސްމީ ދިވެހި ލިޔުމުގެ ވައްތަރު (ތަދުވީ / ތާނަ ސްކްރިޕްޓް)
- ޚަބަރުގެ މުހިންމު ތަފްސީލުތައް ހިމެނޭ ގޮތަށް
- ދިވެހި ރައްޔިތުންނަށް ގުޅޭ ބަހުރުވަ ބޭނުން ކުރޭ
- ލިޔުން ވާންވާނީ ތާނަ ސްކްރިޕްޓްގައި، ލެޓިން ބަހުރުވައިން ނޫން
- ގިނަ ވެގެން 250 ބަސް

ސުރުޚީ: {headline}
ޚަބަރު: {body}

ޚަބަރު ލިޔެ ދޭށެވެ (ސުރުޚީ ނެތި، ހަމައެކަނި ލިޔުން):"""

_LATIN_TO_THAANA_PROMPT = """Convert this Dhivehi text written in Latin script to proper Dhivehi Thaana script.
Return ONLY the Thaana script text, nothing else.
Preserve all meaning exactly. Use standard Dhivehi spelling.

Latin Dhivehi text:
{text}

Thaana script:"""


def _gemini_write_en_article(headline, body, source_count=1):
    """Use Gemini to write a full English article."""
    if not _gemini_post:
        return ""
    prompt = _EN_ARTICLE_PROMPT.format(
        headline=headline,
        body=body[:3000] if body else "No additional details available yet.",
        source_count=source_count
    )
    result = _gemini_post(prompt, timeout=25)
    return (result or "").strip()


def _gemini_write_dv_article(headline, body, source_count=1):
    """Use Gemini to write a full Dhivehi Thaana article."""
    if not _gemini_post:
        return ""
    prompt = _DV_ARTICLE_PROMPT.format(
        headline=headline[:200],
        body=body[:2000] if body else "ތަފްސީލު ލިބިފައި ނެތް."
    )
    result = _gemini_post(prompt, timeout=25)
    return (result or "").strip()


def _gemini_latin_to_thaana(latin_text):
    """Convert Latin Thaana to proper Thaana script."""
    if not _gemini_post or not latin_text:
        return latin_text
    prompt = _LATIN_TO_THAANA_PROMPT.format(text=latin_text[:500])
    result = _gemini_post(prompt, timeout=15)
    return (result or latin_text).strip()


# ── Main Entry: Build Full Article ────────────────────────────────────────────
def build_full_article(headline, existing_summary="", sources=None, cluster_size=1):
    """
    Main function — takes a headline + optional summary and builds:
    - Full EN article text
    - Full DV article text (proper Thaana)
    - Website-ready content

    Returns dict:
    {
        "headline_en": "...",       # cleaned English headline
        "headline_dv": "...",       # Dhivehi headline (Thaana)
        "article_en": "...",        # full English article body
        "article_dv": "...",        # full Dhivehi article body (Thaana)
        "source_count": N,
        "sources": [...],
        "_from_cache": bool,
    }
    """
    sources = sources or []

    # Normalize headline
    headline_en = headline.strip()

    # Handle Latin Thaana headlines — convert to proper Thaana
    headline_dv = ""
    if _is_latin_thaana(headline_en):
        log.info(f"[STORY] Latin Thaana detected — converting to Thaana script")
        headline_dv = _gemini_latin_to_thaana(headline_en)
    elif _has_thaana(headline_en):
        headline_dv = headline_en
        headline_en = ""  # don't use Thaana as EN headline

    # Cache check
    cache_key = hashlib.md5(headline_en.encode() if headline_en else headline_dv.encode()).hexdigest()[:12]
    cached = _article_cache.get(cache_key)
    if cached and (time.time() - cached["_t"]) < ARTICLE_CACHE_TTL:
        log.info(f"[STORY] Cache hit for: {(headline_en or headline_dv)[:50]}")
        return {**cached["data"], "_from_cache": True}

    # Find full story body
    body = find_full_story(headline_en or headline_dv, existing_summary)

    # Write EN article
    article_en = ""
    if headline_en or body:
        article_en = _gemini_write_en_article(headline_en or headline_dv, body, cluster_size)
        log.info(f"[STORY] EN article written: {len(article_en)} chars")

    # Write DV article
    article_dv = _gemini_write_dv_article(headline_dv or headline_en, body or article_en, cluster_size)
    log.info(f"[STORY] DV article written: {len(article_dv)} chars")

    result = {
        "headline_en": headline_en,
        "headline_dv": headline_dv,
        "article_en": article_en,
        "article_dv": article_dv,
        "source_count": cluster_size,
        "sources": sources,
        "_from_cache": False,
    }

    _article_cache[cache_key] = {"_t": time.time(), "data": dict(result)}
    return result


# ── Website Caption Helper ────────────────────────────────────────────────────
def make_website_caption(card_caption, article_url):
    """
    Add 'Read full story' link to a card caption.
    article_url: the samugamedia.com article URL after publishing.
    """
    if not article_url:
        return card_caption
    return f"{card_caption}\n\n📖 <a href=\"{article_url}\">Read full story</a>"


def make_dv_website_caption(dv_caption, article_url):
    """Add Dhivehi 'Read full story' link."""
    if not article_url:
        return dv_caption
    return f"{dv_caption}\n\n📖 <a href=\"{article_url}\">މުޅި ޚަބަރު ކިޔާލާ</a>"
