"""
samuga_scraper.py — Samuga Semantic Scraper Module
Extracted style: matches fetchers.py v7.0 conventions

WHAT IT DOES (ScrapeGraphAI pattern, Gemini-native):
  Fetch → Clean HTML → Gemini semantic extract → JSON → Article dict
  Hybrid: free selector path first, Gemini fallback only when that fails.

  This turns a raw article URL into a clean {id,title,summary,link,source,cat,lang}
  dict that drops straight into Article.from_dict() and your existing
  scoring → stories → Content Lab pipeline. No downstream changes needed.

WHY A SCRAPER LAYER:
  Your RSS + Telegram fetchers are free and instant — keep them.
  This is the SAFETY NET: when a source has no feed, or a site changes
  layout and a selector breaks, Gemini reads the messy HTML and extracts
  structured data instead of you re-writing selectors.

Dependencies injected by bot.py at startup (same pattern as fetchers.py):
  _gemini_post   = bot.py's Gemini fallback-chain function
  GEMINI_API_KEY = bot.py's key
  record_source_health = fetchers.record_source_health (optional, for /sources)
"""

import os, hashlib, logging, json, re, time
import requests
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# ── Injected by bot.py after import (mirror of fetchers.py) ──────────────────
_gemini_post         = None   # bot.py: samuga_scraper._gemini_post = _gemini_post
GEMINI_API_KEY       = os.environ.get("GEMINI_API_KEY", "")
record_source_health = None   # bot.py: samuga_scraper.record_source_health = _ft.record_source_health

# ── Config ───────────────────────────────────────────────────────────────────
SCRAPER_UA        = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
SCRAPER_TIMEOUT   = 15
SCRAPER_MAX_CHARS = 40000   # cap content sent to Gemini — raised for WordPress pages
SCRAPER_MIN_BODY  = 200     # body shorter than this = extraction failed
VALID_CATS        = {"BREAKING", "LOCAL", "POLITICAL", "BUSINESS",
                     "SPORTS", "WORLD", "LIFESTYLE", "DISASTER", "TOURISM"}

# Simple in-memory hash cache so the same page never costs tokens twice
_scrape_cache = {}          # content_hash -> article dict
_CACHE_TTL    = 3600        # 1 hour


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _health(source, **kw):
    """Safe wrapper — only records if bot.py injected the health tracker."""
    if record_source_health:
        try:
            record_source_health(source, **kw)
        except Exception:
            pass


# ─── NODE 1: FETCH ───────────────────────────────────────────────────────────
def _fetch_html(url):
    try:
        resp = requests.get(url, timeout=SCRAPER_TIMEOUT, headers={
            "User-Agent": SCRAPER_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        if resp.status_code != 200:
            return None, f"http {resp.status_code}", resp.status_code
        resp.encoding = resp.apparent_encoding or "utf-8"  # protects Thaana UTF-8
        return resp.text, None, 200
    except Exception as e:
        return None, f"fetch failed: {e}", None


# ─── NODE 2: CLEAN ───────────────────────────────────────────────────────────
def _clean_html(html):
    """Strip noise, keep main content. Cuts tokens before Gemini sees it.
    WordPress-aware: tries common WP article containers, then falls back to
    the full cleaned page so Gemini can find the body itself."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        log.error("[SCRAPER] beautifulsoup4 not installed")
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header",
                    "aside", "form", "iframe", "noscript", "svg",
                    "button", "input"]):
        tag.decompose()

    # Try specific containers first (covers most WordPress themes + others)
    candidates = [
        soup.find("article"),
        soup.find("main"),
        soup.find("div", class_=re.compile(
            r"(entry-content|post-content|article-content|td-post-content|"
            r"single-content|content-area|story-?body|article-?body|post-?body)", re.I)),
        soup.find("div", class_=re.compile(r"(content|article|post|story)", re.I)),
        soup.find("div", id=re.compile(r"(content|article|post|story|main)", re.I)),
    ]
    best = ""
    for c in candidates:
        if c:
            t = c.get_text(separator="\n", strip=True)
            if len(t) > len(best):
                best = t

    # Fallback: if no container gave us a real body, send the whole page text.
    # Server-rendered (WordPress) pages always have the body in the HTML.
    if len(best) < SCRAPER_MIN_BODY and soup.body:
        best = soup.body.get_text(separator="\n", strip=True)

    return best[:SCRAPER_MAX_CHARS]


# ─── NODE 3: GEMINI SEMANTIC EXTRACT ─────────────────────────────────────────
_EXTRACT_PROMPT = """You are a precise news data extractor for Samuga, a Maldivian newsroom.
Return ONLY a valid JSON object. No markdown, no backticks, no preamble.

CRITICAL: Preserve all Thaana/Dhivehi script (U+0780-U+07BF) EXACTLY as written.
Never transliterate, romanize, or translate Dhivehi unless asked.

The content below is raw text from a news web page. It may contain leftover
menu items, category links, "related articles", ads, or footer text mixed in.
IGNORE that noise. Find the ONE main news article on the page and extract only
its real headline and body. If there is no actual news article (e.g. it's just
a homepage list of links), set "title" and "summary" to empty strings.

Extract these keys:
  "title"    : the main article headline (string)
  "summary"  : the main article body only, clean prose, no menus/links/ads (string)
  "category" : one of BREAKING, LOCAL, POLITICAL, BUSINESS, SPORTS, WORLD, LIFESTYLE
  "lang"     : "dv" if the body is mainly Dhivehi Thaana, else "en"

Output the JSON object only.

Web page content:
{content}
"""


def _gemini_extract(clean_text):
    if not _gemini_post:
        return {"error": "gemini not injected"}
    prompt = _EXTRACT_PROMPT.format(content=clean_text)
    raw = _gemini_post(prompt, timeout=20)
    if not raw:
        return {"error": "empty model response"}
    raw = raw.strip().replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Last-ditch: grab the first {...} block
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
        return {"error": "invalid JSON", "raw": raw[:300]}


# ─── QUALITY GATE ────────────────────────────────────────────────────────────
def _passes_quality(d):
    if not isinstance(d, dict) or d.get("error"):
        return False
    if not d.get("title"):
        return False
    if len(str(d.get("summary", ""))) < SCRAPER_MIN_BODY:
        return False
    return True


def _to_article_dict(d, url, source):
    """Map extracted JSON to the EXACT Article.from_dict() shape bot.py expects."""
    cat = str(d.get("category", "LOCAL")).upper().strip()
    if cat not in VALID_CATS:
        cat = "LOCAL"
    title = str(d.get("title", "")).strip()
    art_id = "scrape_" + hashlib.md5((url or title).encode()).hexdigest()[:12]
    return {
        "id": art_id,
        "title": title[:150],
        "summary": str(d.get("summary", "")).strip(),
        "link": url,
        "source": source,
        "cat": cat,
        "lang": d.get("lang", "en") if d.get("lang") in ("en", "dv") else "en",
        "published": _utcnow(),
    }


# ─── MAIN ENTRY: SEMANTIC SCRAPE (Gemini path) ───────────────────────────────
def semantic_scrape(url, source="web"):
    """
    Turn one article URL into a clean Article dict via Gemini semantic extraction.
    Returns an article dict (Article.from_dict-ready) or {"error": ...}.
    """
    html, err, http = _fetch_html(url)
    if err:
        _health(source, ok=False, http_status=http, reason=err)
        log.warning(f"[SCRAPER] {url} → {err}")
        return {"error": err, "link": url}

    clean = _clean_html(html)
    if len(clean) < SCRAPER_MIN_BODY:
        _health(source, ok=False, items=0, reason="no content")
        return {"error": "no extractable content", "link": url}

    # Cache check — same page within TTL costs zero tokens
    chash = hashlib.sha256(clean.encode("utf-8")).hexdigest()[:16]
    hit = _scrape_cache.get(chash)
    if hit and (time.time() - hit["_t"]) < _CACHE_TTL:
        log.info(f"[SCRAPER] cache hit {url}")
        return dict(hit["data"])

    extracted = _gemini_extract(clean)
    if not _passes_quality(extracted):
        _health(source, ok=False, items=0, reason="quality gate")
        log.warning(f"[SCRAPER] quality gate failed: {url}")
        return {"error": "quality gate failed", "link": url,
                "_raw": extracted.get("raw", "")}

    article = _to_article_dict(extracted, url, source)
    _scrape_cache[chash] = {"_t": time.time(), "data": dict(article)}
    _health(source, ok=True, items=1)
    log.info(f"[SCRAPER] extracted '{article['title'][:50]}' ({article['cat']}/{article['lang']})")
    return article


# ─── HYBRID ENTRY: FREE FIRST, GEMINI FALLBACK ───────────────────────────────
def fetch_article(url, source="web", selector_fn=None):
    """
    The function your fetchers actually call.

    selector_fn(url) -> dict|None : your existing free scraper for this source.
                                    Must return an Article-shaped dict, or None
                                    / {} if it fails or the source is unmapped.

    Tier 1: free selector (instant, no tokens).
    Tier 2: Gemini semantic_scrape — only fires when Tier 1 fails the quality gate.
    """
    if selector_fn:
        try:
            data = selector_fn(url)
            if data and _passes_quality(data):
                data.setdefault("link", url)
                data.setdefault("source", source)
                data["_source_tier"] = "selector"
                _health(source, ok=True, items=1)
                return data
            log.info(f"[SCRAPER] selector weak for {url}, escalating to Gemini")
        except Exception as e:
            log.info(f"[SCRAPER] selector error {url}: {e}, escalating to Gemini")

    data = semantic_scrape(url, source=source)
    data["_source_tier"] = "gemini"
    return data


# ─── BATCH HELPER ────────────────────────────────────────────────────────────
def scrape_many(urls, source="web", selector_fn=None, limit=10):
    """Scrape a list of article URLs. Returns list of valid Article dicts only."""
    out = []
    for u in urls[:limit]:
        art = fetch_article(u, source=source, selector_fn=selector_fn)
        if not art.get("error"):
            out.append(art)
        time.sleep(0.5)  # be polite to the source server
    log.info(f"[SCRAPER] scrape_many: {len(out)}/{min(len(urls),limit)} ok from {source}")
    return out
