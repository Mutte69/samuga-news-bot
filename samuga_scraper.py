"""
samuga_scraper.py - Samuga Semantic Scraper v2.0
Matches fetchers.py v7.0 injection pattern exactly.

PIPELINE:
  FETCH → CLEAN → AI EXTRACT → QUALITY GATE → LANG FIX → DEDUP → MAP → OUTPUT

WHAT'S NEW IN v2:
  ✅ Ad/junk detection (Gemini flags is_ad in extraction)
  ✅ Fingerprint dedup at scraper level (before hitting scoring layer)
  ✅ Hard Thaana detection override (never trust Gemini on lang)
  ✅ Gemini retry (2 attempts before giving up)
  ✅ Confidence score (Gemini rates itself 0-100, reject < 60)
  ✅ Semantic layer (breaking/normal/opinion + importance 0-100)
  ✅ Tighter token cap (25K chars)
  ✅ WordPress-aware body detection + full-page fallback
  ✅ Real browser headers
  ✅ Source health wired

Dependencies injected by bot.py at startup (same pattern as fetchers.py):
  _gemini_post         = bot.py's Gemini fallback-chain function
  GEMINI_API_KEY       = bot.py's key
  record_source_health = fetchers.record_source_health
"""

import os, hashlib, logging, json, re, time
import requests
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# ── Injected by bot.py after import ──────────────────────────────────────────
_gemini_post         = None
GEMINI_API_KEY       = os.environ.get("GEMINI_API_KEY", "")
record_source_health = None

# ── Config ────────────────────────────────────────────────────────────────────
SCRAPER_UA        = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
SCRAPER_TIMEOUT   = 15
SCRAPER_MAX_CHARS = 25000    # tighter cap - enough for any article, saves tokens
SCRAPER_MIN_BODY  = 200      # body shorter than this = extraction failed
SCRAPER_MIN_CONF  = 60       # confidence below this = reject
VALID_CATS        = {"BREAKING", "LOCAL", "POLITICAL", "BUSINESS",
                     "SPORTS", "WORLD", "LIFESTYLE", "DISASTER", "TOURISM"}

# ── Short-term dedup memory ───────────────────────────────────────────────────
_scrape_cache        = {}    # content_hash -> {_t, data}
_recent_fingerprints = set() # title+body fingerprints seen this session
_CACHE_TTL           = 3600  # 1 hour


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _health(source, **kw):
    if record_source_health:
        try:
            record_source_health(source, **kw)
        except Exception:
            pass


# ─── LANG DETECTION - hard override, never trust Gemini alone ────────────────
def _detect_lang(text):
    """Check for actual Thaana codepoints. If any found → dv. Else → en."""
    if any("\u0780" <= c <= "\u07BF" for c in (text or "")):
        return "dv"
    return "en"


# ─── FINGERPRINT DEDUP ───────────────────────────────────────────────────────
def _make_fingerprint(title, summary):
    raw = (title + summary[:300]).encode("utf-8", errors="ignore")
    return hashlib.md5(raw).hexdigest()


def _is_duplicate(title, summary):
    fp = _make_fingerprint(title, summary)
    if fp in _recent_fingerprints:
        return True
    _recent_fingerprints.add(fp)
    # cap memory - keep last 500 fingerprints only
    if len(_recent_fingerprints) > 500:
        _recent_fingerprints.pop()
    return False


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
        resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text, None, 200
    except Exception as e:
        return None, f"fetch failed: {e}", None


# ─── NODE 2: CLEAN ───────────────────────────────────────────────────────────
def _clean_html(html):
    """WordPress-aware extraction with full-page fallback for Gemini."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        log.error("[SCRAPER] beautifulsoup4 not installed")
        return ""

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header",
                    "aside", "form", "iframe", "noscript", "svg", "button"]):
        tag.decompose()

    # Try known containers - covers most WP themes used by Maldivian media
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

    # Full-page fallback - WP is server-rendered, body is always in HTML
    if len(best) < SCRAPER_MIN_BODY and soup.body:
        best = soup.body.get_text(separator="\n", strip=True)

    return best[:SCRAPER_MAX_CHARS]


# ─── NODE 3: GEMINI EXTRACT ──────────────────────────────────────────────────
_EXTRACT_PROMPT = """You are a precise news data extractor for Samuga, a Maldivian newsroom.
Return ONLY a valid JSON object. No markdown, no backticks, no preamble.

CRITICAL: Preserve all Thaana/Dhivehi script (U+0780-U+07BF) EXACTLY as written.
Never transliterate, romanize, or translate Dhivehi.

The text below is from a news web page and may contain menu items, ads, or related
article links mixed in. IGNORE that noise. Find the ONE main article and extract it.

Return these keys:
  "title"      : main article headline (string)
  "summary"    : main article body only - clean prose, no menus/links/ads (string)
  "category"   : one of BREAKING, LOCAL, POLITICAL, BUSINESS, SPORTS, WORLD, LIFESTYLE
  "lang"       : "dv" if body is mainly Dhivehi Thaana, else "en"
  "is_ad"      : true if this page is an advertisement, sponsored content, or promo - else false
  "confidence" : integer 0-100 - how confident you are this is a real news article with full body

If no real article found, set title and summary to empty strings.
Output the JSON object only.

Web page content:
{content}
"""

_SEMANTIC_PROMPT = """You are a senior Maldivian news editor analyzing an article for Samuga AI.
Return ONLY a valid JSON object. No markdown, no backticks.

Article title: {title}
Article body (first 500 chars): {body}

Return these keys:
  "article_type" : one of "breaking", "normal", "opinion", "advertisement", "feature"
  "importance"   : integer 0-100 (100 = major breaking story, 0 = trivial/irrelevant)
  "is_breaking"  : true if this is urgent breaking news else false
  "reason"       : one sentence explaining the importance score
"""


def _gemini_extract(clean_text):
    """Extract article data. Retries once on failure."""
    if not _gemini_post:
        return {"error": "gemini not injected"}

    prompt = _EXTRACT_PROMPT.format(content=clean_text)

    for attempt in range(2):
        raw = _gemini_post(prompt, timeout=20)
        if not raw:
            log.warning(f"[SCRAPER] Gemini empty response (attempt {attempt+1})")
            continue
        raw = raw.strip().replace("```json", "").replace("```", "").strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # try to grab first {...} block
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group(0))
                except Exception:
                    pass
            log.warning(f"[SCRAPER] Gemini JSON parse failed (attempt {attempt+1})")

    return {"error": "gemini extract failed after 2 attempts"}


def _gemini_semantic(title, summary):
    """Semantic layer - article type + importance score. Best-effort, never blocks."""
    if not _gemini_post or not title:
        return {}
    try:
        prompt = _SEMANTIC_PROMPT.format(title=title, body=summary[:500])
        raw = _gemini_post(prompt, timeout=15)
        if not raw:
            return {}
        raw = raw.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(raw)
    except Exception:
        return {}


# ─── QUALITY GATE ────────────────────────────────────────────────────────────
def _passes_quality(d):
    """Full quality gate: shape + content + ad check + confidence."""
    if not isinstance(d, dict) or d.get("error"):
        return False, "extraction error"
    if d.get("is_ad") is True:
        return False, "advertisement detected"
    if not d.get("title"):
        return False, "no title"
    if len(str(d.get("summary", ""))) < SCRAPER_MIN_BODY:
        return False, f"body too short ({len(str(d.get('summary','')))} chars)"
    conf = d.get("confidence", 100)
    if isinstance(conf, (int, float)) and conf < SCRAPER_MIN_CONF:
        return False, f"low confidence ({conf})"
    return True, "ok"


# ─── MAP TO ARTICLE DICT ─────────────────────────────────────────────────────
def _to_article_dict(d, url, source, semantic=None):
    """Map extracted JSON to exact Article.from_dict() shape bot.py expects."""
    cat = str(d.get("category", "LOCAL")).upper().strip()
    if cat not in VALID_CATS:
        cat = "LOCAL"

    title   = str(d.get("title", "")).strip()
    summary = str(d.get("summary", "")).strip()

    # Hard lang override - check actual Thaana codepoints, don't trust Gemini alone
    lang = _detect_lang(summary) or _detect_lang(title)
    if lang == "en" and d.get("lang") == "dv":
        lang = "dv"  # trust Gemini's dv call even if body slipped through as latin

    art_id = "scrape_" + hashlib.md5((url or title).encode()).hexdigest()[:12]

    article = {
        "id":         art_id,
        "title":      title[:150],
        "summary":    summary,
        "link":       url,
        "source":     source,
        "cat":        cat,
        "lang":       lang,
        "published":  _utcnow(),
        "_confidence": d.get("confidence", 100),
    }

    # Attach semantic layer if available
    if semantic:
        article["_importance"]    = semantic.get("importance", 50)
        article["_article_type"]  = semantic.get("article_type", "normal")
        article["_is_breaking"]   = semantic.get("is_breaking", False)
        article["_semantic_note"] = semantic.get("reason", "")
        # upgrade category to BREAKING if semantic layer says so
        if semantic.get("is_breaking") and cat not in ("BREAKING", "DISASTER"):
            article["cat"] = "BREAKING"
            log.info(f"[SCRAPER] semantic upgraded '{title[:40]}' → BREAKING")

    return article


# ─── MAIN ENTRY: SEMANTIC SCRAPE ─────────────────────────────────────────────
def semantic_scrape(url, source="web", run_semantic=False):
    """
    Full pipeline: FETCH → CLEAN → EXTRACT → QUALITY → LANG FIX → DEDUP → MAP
    run_semantic: if True, fires a second Gemini call for importance/type scoring.
                  Leave False for bulk fetching (saves tokens). Use True for
                  individual /scrapetest calls or high-value sources.
    Returns an Article.from_dict-ready dict or {"error": ...}.
    """
    html, err, http = _fetch_html(url)
    if err:
        _health(source, ok=False, http_status=http, reason=err)
        log.warning(f"[SCRAPER] {url} → {err}")
        return {"error": err, "link": url}

    clean = _clean_html(html)
    if len(clean) < SCRAPER_MIN_BODY:
        _health(source, ok=False, items=0, reason="no content after clean")
        return {"error": "no extractable content", "link": url}

    # Cache check - same content within TTL costs zero tokens
    chash = hashlib.sha256(clean.encode("utf-8")).hexdigest()[:16]
    hit = _scrape_cache.get(chash)
    if hit and (time.time() - hit["_t"]) < _CACHE_TTL:
        log.info(f"[SCRAPER] cache hit {url}")
        return dict(hit["data"])

    # Extract
    extracted = _gemini_extract(clean)
    ok, reason = _passes_quality(extracted)
    if not ok:
        _health(source, ok=False, items=0, reason=reason)
        log.warning(f"[SCRAPER] quality gate: {reason} - {url}")
        return {"error": reason, "link": url}

    title   = str(extracted.get("title", "")).strip()
    summary = str(extracted.get("summary", "")).strip()

    # Fingerprint dedup
    if _is_duplicate(title, summary):
        log.info(f"[SCRAPER] duplicate fingerprint: {title[:50]}")
        return {"error": "duplicate", "link": url}

    # Semantic layer (optional second Gemini call)
    semantic = _gemini_semantic(title, summary) if run_semantic else {}

    article = _to_article_dict(extracted, url, source, semantic)
    _scrape_cache[chash] = {"_t": time.time(), "data": dict(article)}
    _health(source, ok=True, items=1)

    sem_note = f" | type={semantic.get('article_type')} importance={semantic.get('importance')}" if semantic else ""
    log.info(f"[SCRAPER] ✅ '{article['title'][:50]}' ({article['cat']}/{article['lang']}){sem_note}")
    return article


# ─── HYBRID ENTRY: FREE SELECTOR FIRST, GEMINI FALLBACK ─────────────────────
def fetch_article(url, source="web", selector_fn=None, run_semantic=False):
    """
    Tier 1: free selector (instant, no tokens).
    Tier 2: Gemini semantic_scrape (only when Tier 1 fails quality gate).
    """
    if selector_fn:
        try:
            data = selector_fn(url)
            if data:
                ok, _ = _passes_quality(data)
                if ok:
                    data.setdefault("link", url)
                    data.setdefault("source", source)
                    data["_source_tier"] = "selector"
                    _health(source, ok=True, items=1)
                    return data
            log.info(f"[SCRAPER] selector weak for {url}, escalating to Gemini")
        except Exception as e:
            log.info(f"[SCRAPER] selector error {url}: {e}, escalating")

    data = semantic_scrape(url, source=source, run_semantic=run_semantic)
    data["_source_tier"] = "gemini"
    return data


# ─── BATCH HELPER ────────────────────────────────────────────────────────────
def scrape_many(urls, source="web", selector_fn=None, limit=10, run_semantic=False):
    """Scrape a list of article URLs. Returns valid Article dicts only."""
    out = []
    for u in urls[:limit]:
        art = fetch_article(u, source=source, selector_fn=selector_fn,
                            run_semantic=run_semantic)
        if not art.get("error"):
            out.append(art)
        time.sleep(0.5)
    log.info(f"[SCRAPER] scrape_many: {len(out)}/{min(len(urls),limit)} ok from {source}")
    return out
