"""
db.py — Samuga AI Database Module
Extracted from bot.py v7.0

Contains:
  - init_database()                PostgreSQL connection + schema creation
  - db_execute()                   Safe pooled query runner
  - db_record_article()            Insert/update article in archive
  - db_mark_status()               Update article lifecycle status
  - db_publish_article_for_website() Publish article to public website feed
  - make_article_slug()            URL slug generator
  - make_article_excerpt()         Short homepage excerpt
  - generate_website_article_body() Full article body via Claude
  - kv_get() / kv_set()           Key-value store (bot_kv table)
  - mem_add/list/clear/delete()   Team memory (team_memory table)
  - db_log_learning()             Record team approval/rejection actions
  - db_set_article_message()      Store Telegram message_id for articles
  - db_set_article_matchkey()     Store caption match key
  - _caption_match_key()          Normalize headline for cross-platform matching
  - TREND_THEMES / detect_trends() Trend detection from article archive
  - is_trending_topic()            Quick trending check for score boost
  - find_or_create_story()        Story Intelligence — timeline threading
  - get_story_timeline()          Get full story update history
  - search_stories()              Find stories by keyword
  - get_active_stories()          List currently developing stories
  - canonical_category()          Resolve raw cat → display category
  - strip_source_links()          Remove external URLs from public text
  - samuga_public_summary()       Clean article summary for website
  - normalize_article_language_for_public()  Latin Thaana quality gate

Dependencies injected by bot.py at startup:
  utcnow, ai, _gemini_post, send_text, GEMINI_API_KEY,
  CORE_TEAM_CHAT_ID, ALERT_THREAD_ID
"""

import os, re, json, logging, hashlib
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

# ── Env vars ──────────────────────────────────────────────────────────────────
DATABASE_URL      = os.environ.get("DATABASE_URL", "")
SAMUGA_PUBLIC_LINK   = os.environ.get("SAMUGA_PUBLIC_LINK", "https://t.me/samugacommunity")
SAMUGA_PUBLIC_SOURCE = os.environ.get("SAMUGA_PUBLIC_SOURCE", "Samuga Media")
GEMINI_API_KEY    = os.environ.get("GEMINI_API_KEY", "")
CORE_TEAM_CHAT_ID = os.environ.get("CORE_TEAM_CHAT_ID", "-1002829230299")
ALERT_THREAD_ID   = int(os.environ.get("ALERT_THREAD_ID", "10169"))

# ── Injected by bot.py at startup ─────────────────────────────────────────────
utcnow      = None
ai          = None
_gemini_post = None
send_text   = None

# ── DB state ──────────────────────────────────────────────────────────────────
_db_pool   = None
DB_ENABLED = False

# ── URL stripping helpers ─────────────────────────────────────────────────────
_URL_RE    = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_HTML_A_RE = re.compile(r"<a\s+[^>]*href=[\"'][^\"']+[\"'][^>]*>(.*?)</a>",
                         re.IGNORECASE | re.DOTALL)

# ── Category maps ─────────────────────────────────────────────────────────────
CATEGORY_MAP = {
    "BREAKING": "BREAKING", "DISASTER": "BREAKING",
    "LOCAL":    "LOCAL",
    "POLITICAL":"POLITICAL",
    "LIFESTYLE":"LIFESTYLE", "TOURISM": "LIFESTYLE", "WEATHER": "LIFESTYLE",
    "SPORTS":   "SPORTS",   "FOOTBALL":"SPORTS",
    "WORLD":    "LOCAL",
}

POLITICAL_KEYWORDS = [
    "parliament","majlis","president","minister","ministry","government","cabinet",
    "mp ","ruling party","opposition","mdp","pnc","ppm","election","vote","policy",
    "bill","law","court","supreme court","judge","attorney general","ag office",
    "council","mayor","governor","resign","appointed","reshuffle","summit","diplomatic",
    "ambassador","foreign ministry","budget","parliamentary","constitution","impeach"
]

# ── Latin Thaana word list ────────────────────────────────────────────────────
_LATIN_THAANA_WORDS = [
    "raajje","mihaaru","avas","dhuvas","dhivehi","vaguthu","gothun","medhu",
    "hurihaa","dhathuru","furusathu","baakee","haftaa","majlis","sarukaaru",
    "rayyithun","tharaggee","qanoon","fuluhun","khabaru","miadhu","airport",
    "guriathulun","govaalaifi","kuri","mahchah","mifaharu","dhaaira","kanthah"
]


# ═══════════════════════════════════════════════════════════════════════════════
# Text helpers
# ═══════════════════════════════════════════════════════════════════════════════

def strip_source_links(text):
    """Remove external source URLs/html links from public Samuga copy."""
    s = str(text or "")
    s = _HTML_A_RE.sub(r"\1", s)
    s = _URL_RE.sub("", s)
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()

def samuga_public_summary(title="", summary="", rewritten=""):
    """Create cleaner website article text."""
    primary = rewritten or summary or title or ""
    primary = strip_source_links(primary)
    primary = re.sub(r"\s+", " ", primary).strip()
    if primary and len(primary.split()) < 18 and title:
        primary = f"{strip_source_links(title)}. {primary}"
    return primary[:2600]

def canonical_category(cat, title="", summary=""):
    """Resolve raw category + content into one of 5 display categories."""
    base = CATEGORY_MAP.get(cat, "LOCAL")
    if base == "LOCAL":
        text = (title + " " + summary).lower()
        if any(kw in text for kw in POLITICAL_KEYWORDS):
            return "POLITICAL"
    return base

def is_dhivehi(text):
    """Check if text contains Thaana script."""
    return any('\u0780' <= c <= '\u07BF' for c in text)

def looks_latin_thaana(text):
    """Detect romanized Dhivehi so it doesn't leak into website as English."""
    s = str(text or "").lower()
    if not s or is_dhivehi(s):
        return False
    hits = sum(1 for w in _LATIN_THAANA_WORDS if w in s)
    pattern_hits = len(re.findall(r"\b[a-z]*(?:dh|th|aa|ee|oo|vv|lh|sh)[a-z]*\b", s))
    return hits >= 2 or (hits >= 1 and pattern_hits >= 3)

def _caption_match_key(text):
    """Normalize headline for matching across Telegram/Facebook/Instagram."""
    if not text:
        return ""
    import unicodedata as _ud
    t = text.lower()
    for junk in ["samuga media", "samuga creative", "@samugacommunity",
                 "ސަމުގާ މީޑިއާ", "📡", "🇲🇻"]:
        t = t.replace(junk.lower(), " ")
    out = []
    for ch in t:
        if "\u0780" <= ch <= "\u07bf":
            out.append(ch)
        else:
            folded = _ud.normalize("NFKD", ch).encode("ascii", "ignore").decode("ascii")
            out.append(folded)
    t = "".join(out)
    t = re.sub(r"[^a-z0-9\u0780-\u07bf ]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:60]


# ═══════════════════════════════════════════════════════════════════════════════
# Gemini language helpers (used by normalize_article_language_for_public)
# ═══════════════════════════════════════════════════════════════════════════════

def gemini_latin_thaana_to_thaana(text):
    """Rewrite Latin Thaana into proper Dhivehi Thaana script."""
    if not GEMINI_API_KEY or not _gemini_post:
        return None
    prompt = f"""Rewrite the following romanized Dhivehi / Latin Thaana into proper Dhivehi Thaana script.

Rules:
- Output ONLY Dhivehi Thaana.
- Clean newsroom style.
- Do not add new facts.
- Do not use Latin letters unless it is a brand name that cannot be translated.
- Keep it concise.

Text:
{text}
"""
    out = _gemini_post(prompt, timeout=20)
    if out and is_dhivehi(out):
        return strip_source_links(out).strip()
    return None

def gemini_latin_thaana_to_english(text):
    """Translate romanized Dhivehi to clean English."""
    if not GEMINI_API_KEY or not _gemini_post:
        return None
    prompt = f"""Translate this romanized Dhivehi / Latin Thaana news text into clean English.

Rules:
- Output ONLY English.
- Do not add new facts.
- Keep names and places accurate.
- Clean news style.

Text:
{text}
"""
    out = _gemini_post(prompt, timeout=20)
    if out and not is_dhivehi(out):
        return strip_source_links(out).strip()
    return None

def normalize_article_language_for_public(title="", summary="", lang="en", prefer="auto"):
    """
    Public quality gate — ensures Dhivehi is real Thaana, not Latin Thaana.
    Returns (title, summary, lang, ok).
    """
    title   = strip_source_links(title or "")
    summary = strip_source_links(summary or "")
    combined    = f"{title} {summary}".strip()
    current_lang = (lang or "en").lower()
    has_thaana  = is_dhivehi(combined)
    latin_dv    = looks_latin_thaana(combined)

    if current_lang in ("dv", "dhivehi") or has_thaana:
        if has_thaana:
            return title, summary, "dv", True
        if latin_dv:
            converted = gemini_latin_thaana_to_thaana(combined)
            if converted:
                return converted[:500], converted[:2600], "dv", True
            return title, summary, "dv", False
        return title, summary, "en", True

    if latin_dv:
        converted = gemini_latin_thaana_to_english(combined)
        if converted:
            sent = re.split(r"(?<=[.!?])\s+", converted.strip(), maxsplit=1)
            new_title = sent[0][:500] if sent else converted[:500]
            return new_title, converted[:2600], "en", True
        return title, summary, "en", False

    return title, summary, "en", True


# ═══════════════════════════════════════════════════════════════════════════════
# PostgreSQL connection + schema
# ═══════════════════════════════════════════════════════════════════════════════

def init_database():
    """Connect to Postgres and create all tables. Sets DB_ENABLED on success."""
    global _db_pool, DB_ENABLED
    if not DATABASE_URL:
        log.info("🗄️ No DATABASE_URL — running in JSON-only mode")
        return
    try:
        from psycopg2 import pool as _pgpool
        url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        _db_pool = _pgpool.SimpleConnectionPool(1, 5, dsn=url)
        conn = _db_pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS articles (
                        id              TEXT PRIMARY KEY,
                        title           TEXT NOT NULL,
                        summary         TEXT,
                        link            TEXT,
                        source          TEXT,
                        category        TEXT,
                        lang            TEXT,
                        score           INTEGER DEFAULT 0,
                        reliability     INTEGER DEFAULT 0,
                        is_breaking     BOOLEAN DEFAULT FALSE,
                        cluster_id      TEXT,
                        status          TEXT DEFAULT 'seen',
                        found_at        TIMESTAMPTZ DEFAULT NOW(),
                        posted_at       TIMESTAMPTZ,
                        tg_message_id   BIGINT,
                        tg_views        INTEGER DEFAULT 0,
                        meta_engagement INTEGER DEFAULT 0,
                        match_key       TEXT,
                        article_slug    TEXT,
                        article_excerpt TEXT,
                        article_body    TEXT,
                        article_generated_at TIMESTAMPTZ
                    );
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_articles_found_at ON articles(found_at);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_articles_status   ON articles(status);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_articles_category ON articles(category);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_articles_cluster  ON articles(cluster_id);")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS bot_kv (
                        key        TEXT PRIMARY KEY,
                        value      JSONB,
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS learning (
                        id              SERIAL PRIMARY KEY,
                        article_id      TEXT,
                        action          TEXT,
                        member          TEXT,
                        category        TEXT,
                        source          TEXT,
                        score           INTEGER,
                        theme           TEXT,
                        original_caption TEXT,
                        final_caption    TEXT,
                        lang            TEXT,
                        created_at      TIMESTAMPTZ DEFAULT NOW()
                    );
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_learning_action ON learning(action);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_learning_theme  ON learning(theme);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_articles_msgid  ON articles(tg_message_id);")
                # Safe ALTER TABLE additions
                for col in [
                    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS meta_engagement INTEGER DEFAULT 0;",
                    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS match_key TEXT;",
                    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS article_slug TEXT;",
                    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS article_excerpt TEXT;",
                    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS article_body TEXT;",
                    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS article_generated_at TIMESTAMPTZ;",
                ]:
                    cur.execute(col)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_articles_matchkey ON articles(match_key);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_articles_slug ON articles(article_slug);")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS team_memory (
                        id          SERIAL PRIMARY KEY,
                        category    TEXT,
                        content     TEXT NOT NULL,
                        added_by    TEXT,
                        created_at  TIMESTAMPTZ DEFAULT NOW()
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS public_chat_messages (
                        id            SERIAL PRIMARY KEY,
                        platform      TEXT,
                        session_id    TEXT,
                        user_key      TEXT,
                        user_message  TEXT,
                        bot_reply     TEXT,
                        lang          TEXT,
                        intent        TEXT,
                        topics        TEXT[],
                        used_search   BOOLEAN DEFAULT FALSE,
                        created_at    TIMESTAMPTZ DEFAULT NOW()
                    );
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_public_chat_created  ON public_chat_messages(created_at);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_public_chat_platform ON public_chat_messages(platform);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_public_chat_intent   ON public_chat_messages(intent);")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS public_interest_daily (
                        day           DATE,
                        topic         TEXT,
                        platform      TEXT,
                        count         INTEGER DEFAULT 0,
                        updated_at    TIMESTAMPTZ DEFAULT NOW(),
                        PRIMARY KEY (day, topic, platform)
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS stories (
                        id            SERIAL PRIMARY KEY,
                        title         TEXT NOT NULL,
                        slug          TEXT,
                        category      TEXT,
                        status        TEXT DEFAULT 'active',
                        place         TEXT,
                        event_type    TEXT,
                        update_count  INTEGER DEFAULT 0,
                        first_seen    TIMESTAMPTZ DEFAULT NOW(),
                        last_update   TIMESTAMPTZ DEFAULT NOW()
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS story_updates (
                        id          SERIAL PRIMARY KEY,
                        story_id    INTEGER REFERENCES stories(id),
                        article_id  TEXT,
                        headline    TEXT NOT NULL,
                        summary     TEXT,
                        source      TEXT,
                        link        TEXT,
                        created_at  TIMESTAMPTZ DEFAULT NOW()
                    );
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_stories_status ON stories(status);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_stories_slug   ON stories(slug);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_stupd_story    ON story_updates(story_id);")
            conn.commit()
        finally:
            _db_pool.putconn(conn)
        DB_ENABLED = True
        log.info("🗄️ ✅ PostgreSQL connected — article archive active")
    except Exception as e:
        log.error(f"🗄️ Database init failed (falling back to JSON): {e}")
        DB_ENABLED = False


# ═══════════════════════════════════════════════════════════════════════════════
# Core query runner
# ═══════════════════════════════════════════════════════════════════════════════

def db_execute(query, params=None, fetch=None):
    """
    Run a query safely with pooled connection.
    fetch: None | 'one' | 'all'
    """
    if not DB_ENABLED or not _db_pool:
        return None
    conn = None
    try:
        conn = _db_pool.getconn()
        with conn.cursor() as cur:
            cur.execute(query, params or ())
            result = None
            if fetch == "one":
                result = cur.fetchone()
            elif fetch == "all":
                result = cur.fetchall()
        conn.commit()
        return result
    except Exception as e:
        log.error(f"🗄️ db_execute: {e}")
        if conn:
            try: conn.rollback()
            except Exception: pass
        return None
    finally:
        if conn and _db_pool:
            _db_pool.putconn(conn)


# ═══════════════════════════════════════════════════════════════════════════════
# Article lifecycle
# ═══════════════════════════════════════════════════════════════════════════════

def db_record_article(article, score=0, reliability=0, status="seen", is_breaking=False):
    """Insert or update an article. Never downgrades a posted article."""
    if not DB_ENABLED:
        return
    db_execute("""
        INSERT INTO articles (id, title, summary, link, source, category, lang, score, reliability, is_breaking, status)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (id) DO UPDATE SET
            title=COALESCE(NULLIF(EXCLUDED.title,''), articles.title),
            summary=COALESCE(NULLIF(EXCLUDED.summary,''), articles.summary),
            link=COALESCE(NULLIF(EXCLUDED.link,''), articles.link),
            source=COALESCE(NULLIF(EXCLUDED.source,''), articles.source),
            category=COALESCE(NULLIF(EXCLUDED.category,''), articles.category),
            lang=COALESCE(NULLIF(EXCLUDED.lang,''), articles.lang),
            score=GREATEST(COALESCE(articles.score,0), COALESCE(EXCLUDED.score,0)),
            reliability=GREATEST(COALESCE(articles.reliability,0), COALESCE(EXCLUDED.reliability,0)),
            is_breaking=COALESCE(EXCLUDED.is_breaking, articles.is_breaking),
            status=CASE
                WHEN articles.status IN ('posted','published','social_posted') THEN articles.status
                ELSE EXCLUDED.status
            END
    """, (
        article.get("id"), article.get("title","")[:500], article.get("summary","")[:2000],
        article.get("link",""), article.get("source",""),
        canonical_category(article.get("cat","LOCAL"), article.get("title",""), article.get("summary","")),
        article.get("lang","en"), score, reliability, is_breaking, status
    ))

def db_mark_status(article_id, status, posted=False):
    """Update article lifecycle status without downgrading posted articles."""
    if not DB_ENABLED or not article_id:
        return
    if posted:
        db_execute("UPDATE articles SET status=%s, posted_at=COALESCE(posted_at, NOW()) WHERE id=%s",
                   (status, article_id))
    else:
        db_execute("""
            UPDATE articles
            SET status = CASE
                WHEN status IN ('posted','published','social_posted') AND %s IN ('queued','seen')
                    THEN status
                ELSE %s
            END
            WHERE id=%s
        """, (status, status, article_id))

def db_set_article_message(article_id, message_id):
    """Store Telegram message_id for view tracking."""
    if not DB_ENABLED or not article_id or not message_id:
        return
    db_execute("UPDATE articles SET tg_message_id=%s WHERE id=%s", (message_id, article_id))

def db_set_article_matchkey(article_id, title):
    """Store normalized match key for FB/IG caption matching."""
    if not DB_ENABLED or not article_id:
        return
    mk = _caption_match_key(title)
    if mk:
        db_execute("UPDATE articles SET match_key=%s WHERE id=%s", (mk, article_id))


# ═══════════════════════════════════════════════════════════════════════════════
# Website article engine
# ═══════════════════════════════════════════════════════════════════════════════

def make_article_slug(title, article_id=""):
    """Create a clean URL slug for website article pages."""
    try:
        import unicodedata
        t = strip_source_links(title or "").lower()
        t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode("ascii")
        t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
        t = re.sub(r"-{2,}", "-", t)
        if not t:
            t = "samuga-story"
        suffix = str(article_id or "")[:10].replace("_", "-")
        return f"{t[:70]}-{suffix}" if suffix else t[:80]
    except Exception:
        return f"samuga-story-{str(article_id or '')[:10]}"

def make_article_excerpt(title="", summary="", lang="en"):
    """Short homepage excerpt separate from full article body."""
    text = strip_source_links(summary or title or "")
    text = re.sub(r"\s+", " ", text).strip()
    if lang == "dv":
        return text[:300]
    sentences = re.split(r"(?<=[.!?])\s+", text)
    excerpt = ""
    for s in sentences:
        if len(excerpt) + len(s) < 280:
            excerpt = (excerpt + " " + s).strip()
        else:
            break
    return excerpt or text[:280]

def generate_website_article_body(title="", summary="", category="LOCAL",
                                   source="Samuga Media", is_breaking=False):
    """Generate a full website article body using Claude Haiku."""
    if not ai:
        return summary or title or ""
    try:
        breaking_note = "This is breaking news — keep it tight and urgent." if is_breaking else ""
        prompt = f"""You are a news writer for Samuga Media, a Maldivian digital news outlet.

Write a clean, professional news article body for the Samuga website.

Title: {title}
Summary: {summary}
Category: {category}
Source: {source}
{breaking_note}

Rules:
- 3 to 5 short paragraphs
- Clear, journalistic English
- No hashtags, no emojis
- Do not invent facts not in the summary
- Do not include the title in the body
- Do not include source URLs
- End with context or background if relevant

Write the article body only:"""

        msg = ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        return strip_source_links(msg.content[0].text.strip())
    except Exception as e:
        log.error(f"generate_website_article_body: {e}")
        return summary or title or ""

def db_publish_article_for_website(article_id, title="", summary="", category="LOCAL",
                                    source="Samuga Media", link="", lang="en",
                                    score=0, reliability=0, is_breaking=False):
    """Make a public story visible on the Samuga website."""
    if not DB_ENABLED or not article_id:
        return

    lang = (lang or "en").lower()
    article_id = str(article_id)
    if lang in ("dv", "dhivehi") and not article_id.endswith("_dv"):
        article_id = f"{article_id}_dv"

    public_title   = strip_source_links(title or "")[:500]
    public_summary = samuga_public_summary(public_title, summary or "")[:2600]

    public_title, public_summary, lang, lang_ok = normalize_article_language_for_public(
        public_title, public_summary, lang=lang
    )
    if not lang_ok:
        log.warning(f"🌐 Website publish held — language cleanup failed: {public_title[:70]}")
        return

    safe_cat       = canonical_category(category or "LOCAL", public_title, public_summary)
    slug           = make_article_slug(public_title, article_id)
    article_excerpt = make_article_excerpt(public_title, public_summary, lang=lang)
    article_body    = ""
    if lang == "en":
        article_body = generate_website_article_body(
            title=public_title, summary=public_summary,
            category=safe_cat, source=SAMUGA_PUBLIC_SOURCE, is_breaking=is_breaking
        )
    else:
        article_body = public_summary or public_title

    db_execute("""
        INSERT INTO articles
            (id, title, summary, link, source, category, lang, score, reliability,
             is_breaking, status, posted_at, match_key, article_slug, article_excerpt,
             article_body, article_generated_at)
        VALUES
            (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'posted',NOW(),%s,%s,%s,%s,NOW())
        ON CONFLICT (id) DO UPDATE SET
            title=COALESCE(NULLIF(EXCLUDED.title,''), articles.title),
            summary=COALESCE(NULLIF(EXCLUDED.summary,''), articles.summary),
            link=EXCLUDED.link, source=EXCLUDED.source,
            category=COALESCE(NULLIF(EXCLUDED.category,''), articles.category),
            lang=COALESCE(NULLIF(EXCLUDED.lang,''), articles.lang),
            score=GREATEST(COALESCE(articles.score,0), COALESCE(EXCLUDED.score,0)),
            reliability=GREATEST(COALESCE(articles.reliability,0), COALESCE(EXCLUDED.reliability,0)),
            is_breaking=COALESCE(EXCLUDED.is_breaking, articles.is_breaking),
            status='posted',
            posted_at=COALESCE(articles.posted_at, NOW()),
            match_key=COALESCE(NULLIF(EXCLUDED.match_key,''), articles.match_key),
            article_slug=COALESCE(NULLIF(EXCLUDED.article_slug,''), articles.article_slug),
            article_excerpt=COALESCE(NULLIF(EXCLUDED.article_excerpt,''), articles.article_excerpt),
            article_body=COALESCE(NULLIF(EXCLUDED.article_body,''), articles.article_body),
            article_generated_at=COALESCE(articles.article_generated_at, EXCLUDED.article_generated_at)
    """, (
        article_id, public_title, public_summary, SAMUGA_PUBLIC_LINK,
        SAMUGA_PUBLIC_SOURCE, safe_cat, lang or "en", score or 0,
        reliability or 0, bool(is_breaking), _caption_match_key(public_title or ""),
        slug, article_excerpt, article_body
    ))


# ═══════════════════════════════════════════════════════════════════════════════
# Key-value store
# ═══════════════════════════════════════════════════════════════════════════════

def kv_get(key, default=None):
    """Read a JSON value from bot_kv."""
    if not DB_ENABLED:
        return default
    row = db_execute("SELECT value FROM bot_kv WHERE key=%s", (key,), fetch="one")
    if row and row[0] is not None:
        return row[0]
    return default

def kv_set(key, value):
    """Write a JSON value to bot_kv (upsert)."""
    if not DB_ENABLED:
        return
    db_execute("""
        INSERT INTO bot_kv (key, value, updated_at)
        VALUES (%s, %s::jsonb, NOW())
        ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
    """, (key, json.dumps(value)))


# ═══════════════════════════════════════════════════════════════════════════════
# Team memory
# ═══════════════════════════════════════════════════════════════════════════════

def mem_add(content, category="fact", added_by="team"):
    db_execute(
        "INSERT INTO team_memory (category, content, added_by) VALUES (%s, %s, %s)",
        (category, content.strip(), added_by)
    )

def mem_list(limit=30):
    rows = db_execute(
        "SELECT category, content, added_by FROM team_memory ORDER BY created_at DESC LIMIT %s",
        (limit,), fetch="all"
    )
    if not rows:
        return []
    return [f"[{r[0]}] {r[1]} (by {r[2]})" for r in rows]

def mem_clear_all():
    db_execute("DELETE FROM team_memory")

def mem_delete_last(n=1):
    db_execute("""
        DELETE FROM team_memory WHERE id IN (
            SELECT id FROM team_memory ORDER BY created_at DESC LIMIT %s
        )
    """, (n,))


# ═══════════════════════════════════════════════════════════════════════════════
# Learning log
# ═══════════════════════════════════════════════════════════════════════════════

def db_log_learning(article_id, action, member="", category="", source="",
                    score=0, theme="", original_caption="", final_caption="", lang="en"):
    """Record a team action so the bot can learn from approvals/rejections."""
    if not DB_ENABLED:
        return
    db_execute("""
        INSERT INTO learning (article_id, action, member, category, source, score,
                              theme, original_caption, final_caption, lang)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (
        article_id, action, (member or "")[:60], (category or "")[:40],
        (source or "")[:80], score, (theme or "")[:40],
        (original_caption or "")[:1000], (final_caption or "")[:1000], lang
    ))


# ═══════════════════════════════════════════════════════════════════════════════
# Trend detector
# ═══════════════════════════════════════════════════════════════════════════════

TREND_THEMES = {
    "Cost of Living":   ["cost of living","price","prices","inflation","expensive","rufiyaa","dollar rate","import","grocery","staple"],
    "Housing":          ["housing","flat","flats","land","plot","gedhoru","apartment","rent","hiya","vinares","social housing"],
    "Corruption":       ["corruption","bribe","embezzle","graft","acc ","anti-corruption","scandal","misuse","fraud","laundering"],
    "Drugs":            ["drug","drugs","narcotic","trafficking","heroin","cannabis","addict","rehab"],
    "Politics":         ["parliament","majlis","president","minister","cabinet","mp ","party","election","vote","impeach","no-confidence"],
    "Tourism":          ["tourism","resort","arrival","occupancy","tourist","visitor","bed night","travel"],
    "Fishing":          ["fishing","fisheries","fishermen","tuna","catch","mifco","masveriya"],
    "Crime":            ["murder","stabbing","assault","robbery","theft","arrested","police","gang","violence"],
    "Economy":          ["economy","gdp","budget","debt","loan","reserve","imf","world bank","deficit","sovereign"],
    "Weather/Disaster": ["storm","flood","rain","swell","udha","fire","accident","sinking","capsize","rescue"],
    "Health":           ["hospital","health","disease","dengue","outbreak","aasandha","medical","clinic","doctor"],
    "Infrastructure":   ["bridge","harbour","airport","road","construction","project","development","sewerage","water"],
    "Education":        ["school","education","student","university","exam","teacher","scholarship"],
    "India/Foreign":    ["india","china","indian","chinese","foreign","diplomatic","embassy","bilateral","agreement"],
}

def _detect_themes(text):
    t = text.lower()
    return {theme for theme, kws in TREND_THEMES.items() if any(kw in t for kw in kws)}

def detect_trends(hours=24, min_mentions=3):
    """Analyze article archive for trending themes. Returns sorted list."""
    if not DB_ENABLED:
        return []
    rows = db_execute(
        "SELECT title, summary FROM articles WHERE found_at > NOW() - INTERVAL %s",
        (f"{hours} hours",), fetch="all"
    )
    if not rows:
        return []
    theme_counts, theme_titles = {}, {}
    for title, summary in rows:
        text = f"{title or ''} {summary or ''}"
        for theme in _detect_themes(text):
            theme_counts[theme] = theme_counts.get(theme, 0) + 1
            theme_titles.setdefault(theme, [])
            if title and len(theme_titles[theme]) < 3:
                theme_titles[theme].append(title[:70])
    trends = [(th, c, theme_titles.get(th, []))
              for th, c in theme_counts.items() if c >= min_mentions]
    trends.sort(key=lambda x: x[1], reverse=True)
    return trends

def is_trending_topic(title, summary="", min_mentions=4):
    """Quick check: does this article belong to a currently-trending theme?"""
    if not DB_ENABLED:
        return (False, None, 0)
    themes = _detect_themes(f"{title} {summary}")
    if not themes:
        return (False, None, 0)
    trends = {t[0]: t[1] for t in detect_trends(hours=24, min_mentions=min_mentions)}
    for theme in themes:
        if theme in trends:
            return (True, theme, trends[theme])
    return (False, None, 0)


# ═══════════════════════════════════════════════════════════════════════════════
# Story Intelligence — timeline threading
# ═══════════════════════════════════════════════════════════════════════════════

def _story_cluster_key(title):
    import re as _re
    t = title.lower()
    t = _re.sub(r"[^a-z0-9\u0780-\u07bf ]", " ", t)
    stopwords = {"a","an","the","in","on","at","to","of","for","and","or","is","are",
                 "was","were","has","have","maldives","maldivian","male"}
    words = [w for w in t.split() if w not in stopwords and len(w) > 2]
    return " ".join(sorted(words[:6]))

def _story_similarity(a, b):
    def kws(t):
        t = t.lower()
        t = re.sub(r"[^a-z0-9\u0780-\u07bf ]", " ", t)
        return set(w for w in t.split() if len(w) > 2)
    ka, kb = kws(a), kws(b)
    if not ka or not kb: return 0.0
    return len(ka & kb) / len(ka | kb)

_MV_PLACES_SI = [
    "male","malé","hulhumale","addu","fuvahmulah","kulhudhuffushi","thinadhoo",
    "naifaru","ungoofaaru","eydhafushi","dhidhdhoo","velidhoo","mahibadhoo",
    "hithadhoo","fonadhoo","vilingili",
]
_EVENT_TYPES_SI = {
    "fire":     ["fire","blaze","burning","burned","arson"],
    "accident": ["crash","collision","accident","capsize","sinking","overturned"],
    "death":    ["dead","died","killed","murder","death","deceased"],
    "missing":  ["missing","search","rescue","disappeared"],
    "arrest":   ["arrested","detained","remanded","charged","sentenced"],
    "flood":    ["flood","flooding","inundated","submerged"],
    "assault":  ["assault","stabbed","attacked","injured","wounded"],
}

def _si_detect_place(title):
    t = title.lower()
    for p in _MV_PLACES_SI:
        if p in t: return p
    return None

def _si_detect_event(title):
    t = title.lower()
    for etype, kws in _EVENT_TYPES_SI.items():
        if any(k in t for k in kws): return etype
    return None

def _notify_developing_story(story_id, title, source_count, source_list):
    """Alert core team when a story hits 3 updates."""
    if not send_text:
        return
    try:
        send_text(
            CORE_TEAM_CHAT_ID,
            f"📚 <b>Developing Story #{story_id}</b>\n\n"
            f"<b>{title[:90]}</b>\n\n"
            f"Now confirmed by <b>{source_count} sources</b>: {source_list}\n\n"
            f"Use <code>/story {story_id}</code> to see the full timeline.",
            thread_id=ALERT_THREAD_ID
        )
    except Exception as e:
        log.debug(f"Notify developing story: {e}")

def find_or_create_story(title, category, article_id, summary, source, link):
    """
    Find existing active story this article belongs to, or create a new one.
    Returns (story_id, is_new, update_number) or (None, False, 0) if DB off.
    """
    if not DB_ENABLED:
        return (None, False, 0)
    try:
        place = _si_detect_place(title)
        event = _si_detect_event(title)
        slug  = _story_cluster_key(title)

        candidates = db_execute("""
            SELECT id, title, place, event_type, update_count
            FROM stories
            WHERE status IN ('active','developing')
              AND last_update > NOW() - INTERVAL '72 hours'
            ORDER BY last_update DESC
            LIMIT 40
        """, fetch="all") or []

        matched_id, best_score = None, 0
        for sid, stitle, splace, sevent, ucount in candidates:
            score = 0
            if place and event and splace == place and sevent == event:
                score = 100
            elif place and splace == place and _story_similarity(title, stitle) >= 0.30:
                score = 70
            elif _story_similarity(title, stitle) >= 0.60:
                score = 60
            elif place and splace == place:
                from scoring import _dup_keywords
                shared = set(_dup_keywords(title)) & set(_dup_keywords(stitle))
                if len(shared) >= 2:
                    score = 55
            if score > best_score:
                best_score = score
                matched_id = sid
        if best_score < 50:
            matched_id = None

        if matched_id:
            db_execute("""
                INSERT INTO story_updates (story_id, article_id, headline, summary, source, link)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (matched_id, article_id, title, summary, source, link))
            db_execute("""
                UPDATE stories
                SET update_count = update_count + 1,
                    last_update = NOW(),
                    status = CASE WHEN update_count + 1 >= 3 THEN 'developing' ELSE status END
                WHERE id = %s
            """, (matched_id,))
            cnt = db_execute("SELECT update_count FROM stories WHERE id=%s",
                             (matched_id,), fetch="one")
            update_num = cnt[0] if cnt else 1
            log.info(f"📚 Story #{matched_id} updated (update #{update_num}): {title[:50]}")
            if update_num == 3:
                try:
                    src_rows = db_execute("""
                        SELECT DISTINCT source FROM story_updates
                        WHERE story_id=%s AND source IS NOT NULL
                    """, (matched_id,), fetch="all") or []
                    sources  = [s[0] for s in src_rows if s[0]]
                    src_list = ", ".join(sources[:5]) if sources else "multiple outlets"
                    _notify_developing_story(matched_id, title, len(sources), src_list)
                except Exception as e:
                    log.debug(f"Proactive alert: {e}")
            return (matched_id, False, update_num)
        else:
            new_id = db_execute("""
                INSERT INTO stories (title, slug, category, place, event_type, update_count)
                VALUES (%s, %s, %s, %s, %s, 1)
                RETURNING id
            """, (title, slug, category, place, event), fetch="one")
            story_id = new_id[0] if new_id else None
            if story_id:
                db_execute("""
                    INSERT INTO story_updates (story_id, article_id, headline, summary, source, link)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (story_id, article_id, title, summary, source, link))
                log.info(f"📚 New Story #{story_id} created: {title[:50]}")
            return (story_id, True, 1)
    except Exception as e:
        log.error(f"Story intelligence: {e}")
        return (None, False, 0)

def get_story_timeline(story_id):
    """Return full timeline of a story as a dict."""
    if not DB_ENABLED:
        return None
    story = db_execute("""
        SELECT id, title, category, status, place, event_type, update_count, first_seen, last_update
        FROM stories WHERE id=%s
    """, (story_id,), fetch="one")
    if not story:
        return None
    updates = db_execute("""
        SELECT headline, summary, source, created_at
        FROM story_updates WHERE story_id=%s ORDER BY created_at ASC
    """, (story_id,), fetch="all") or []
    return {
        "id": story[0], "title": story[1], "category": story[2], "status": story[3],
        "place": story[4], "event_type": story[5], "update_count": story[6],
        "first_seen": story[7], "last_update": story[8],
        "updates": [{"headline": u[0], "summary": u[1], "source": u[2], "time": u[3]}
                    for u in updates]
    }

def search_stories(query, limit=5):
    """Find stories matching a free-text query."""
    if not DB_ENABLED:
        return []
    rows = db_execute("""
        SELECT id, title, status, update_count, last_update, place, event_type
        FROM stories ORDER BY last_update DESC LIMIT 100
    """, fetch="all") or []
    from scoring import _dup_keywords
    q_words = set(_dup_keywords(query))
    scored  = []
    for sid, title, status, ucount, last_up, place, event in rows:
        t_words = set(_dup_keywords(title))
        overlap = len(q_words & t_words)
        ql = query.lower()
        if place and place.lower() in ql: overlap += 2
        if event and event in ql:         overlap += 2
        if overlap > 0:
            scored.append((overlap, sid, title, status, ucount, last_up))
    scored.sort(reverse=True)
    return [{"id": s[1], "title": s[2], "status": s[3],
             "update_count": s[4], "last_update": s[5]} for s in scored[:limit]]

def get_active_stories(limit=10):
    """List currently developing/active stories."""
    if not DB_ENABLED:
        return []
    rows = db_execute("""
        SELECT id, title, status, update_count, last_update, place, event_type
        FROM stories
        WHERE status IN ('active','developing')
          AND last_update > NOW() - INTERVAL '72 hours'
          AND update_count >= 2
        ORDER BY update_count DESC, last_update DESC
        LIMIT %s
    """, (limit,), fetch="all") or []
    return [{"id": r[0], "title": r[1], "status": r[2], "update_count": r[3],
             "last_update": r[4], "place": r[5], "event_type": r[6]} for r in rows]
