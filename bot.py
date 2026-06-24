import os, io, threading, time, logging, hashlib, json, feedparser, requests, anthropic, re
from PIL import Image, ImageDraw, ImageFont, ImageEnhance
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from apscheduler.schedulers.blocking import BlockingScheduler
from io import BytesIO

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ── Timezone-aware UTC helper (replaces deprecated utcnow()) ─────────
from datetime import timezone as _tz
def utcnow():
    """Naive UTC datetime — same value as the old utcnow() but not deprecated."""
    return datetime.now(_tz.utc).replace(tzinfo=None)

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
# Meta Graph API (Phase 2.5) — reads FB + IG engagement off your own page
META_PAGE_TOKEN     = os.environ.get("META_PAGE_TOKEN", "")
META_PAGE_ID        = os.environ.get("META_PAGE_ID", "")
META_APP_SECRET     = os.environ.get("META_APP_SECRET", "")
META_IG_ID          = os.environ.get("META_IG_ID", "")  # optional; auto-resolved if blank
META_API_VER        = os.environ.get("META_API_VER", "v21.0")

ai = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── RSS Feeds ─────────────────────────────────────────────────────────────────
# ── RSS Feeds (v4 Strategy) ───────────────────────────────────────────────────
# LOCAL (70%) — Maldivian sources, priority order
LOCAL_FEEDS = [
    # Tier 1 - Breaking/Crisis
    {"url": "https://news.google.com/rss/search?q=maldives+breaking+incident+accident+arrest&hl=en-MV&gl=MV&ceid=MV:en", "cat": "DISASTER", "lang": "en"},
    # Tier 2 - English sources
    {"url": "https://see.mv/feed",                   "cat": "LOCAL",   "lang": "en"},
    {"url": "https://english.sun.mv/feed",            "cat": "LOCAL",   "lang": "en"},  # already present
    {"url": "https://edition.mv/feed",                "cat": "LOCAL",   "lang": "en"},  # already present (editon)
    {"url": "https://maldivesindependent.com/feed",   "cat": "LOCAL",   "lang": "en"},
    {"url": "https://oneonline.mv/en/feed",           "cat": "LOCAL",   "lang": "en"},
    {"url": "https://psmnews.mv/en/feed",             "cat": "LOCAL",   "lang": "en"},  # already present (PSM News)
    {"url": "https://maldivesvoice.com/feed",         "cat": "LOCAL",   "lang": "en"},  # NEW: Maldives Voice
    {"url": "https://presidency.gov.mv/feed",         "cat": "LOCAL",   "lang": "en"},  # NEW: Presidency
    # Tier 3 - Dhivehi sources
    {"url": "https://sunonline.mv/feed",              "cat": "LOCAL",   "lang": "dv"},  # NEW: SunOnline (Dhivehi)
    {"url": "https://mihaaru.com/rss",                "cat": "LOCAL",   "lang": "dv"},
    {"url": "https://avas.mv/feed",                   "cat": "LOCAL",   "lang": "dv"},
    {"url": "https://news.google.com/rss/search?q=maldives+politics+parliament+government&hl=en-MV&gl=MV&ceid=MV:en", "cat": "LOCAL", "lang": "en"},
    {"url": "https://news.google.com/rss/search?q=maldives+economy+finance+business&hl=en-MV&gl=MV&ceid=MV:en",       "cat": "LOCAL", "lang": "en"},
]

# SPORTS (10%) — Maldives sports first, then major international
SPORTS_FEEDS = [
    {"url": "https://news.google.com/rss/search?q=maldives+football+sports&hl=en-MV&gl=MV&ceid=MV:en", "cat": "SPORTS", "lang": "en"},
    {"url": "https://news.google.com/rss/search?q=world+cup+2026+results&hl=en&gl=US&ceid=US:en",       "cat": "SPORTS", "lang": "en"},
]

# WORLD (10%) — Only major international that affects Maldives or region
WORLD_FEEDS = [
    {"url": "https://news.google.com/rss/search?q=war+conflict+crisis+2026&hl=en&gl=US&ceid=US:en",     "cat": "WORLD", "lang": "en"},
    {"url": "https://news.google.com/rss/search?q=earthquake+tsunami+disaster&hl=en&gl=US&ceid=US:en",  "cat": "DISASTER", "lang": "en"},
]

# LIFESTYLE (10%)
LIFESTYLE_FEEDS = [
    {"url": "https://visitmaldives.com/feed",                                                                   "cat": "TOURISM", "lang": "en"},  # NEW: Visit Maldives official
    {"url": "https://news.google.com/rss/search?q=maldives+tourism+travel+resort&hl=en-MV&gl=MV&ceid=MV:en", "cat": "TOURISM", "lang": "en"},
    {"url": "https://news.google.com/rss/search?q=maldives+weather+storm&hl=en-MV&gl=MV&ceid=MV:en",         "cat": "WEATHER", "lang": "en"},
    # AccuWeather RSS removed — weather handled by 8AM/8PM weather cards instead
]

RSS_FEEDS = LOCAL_FEEDS + SPORTS_FEEDS + WORLD_FEEDS + LIFESTYLE_FEEDS

CAT_CONFIG = {
    "BREAKING":  {"label": "🚨  BREAKING NEWS", "color": (220,50,50)},
    "LOCAL":     {"label": "🇲🇻  LOCAL NEWS",    "color": (41,171,226)},
    "POLITICAL": {"label": "🏛️  POLITICAL",      "color": (180,140,40)},
    "LIFESTYLE": {"label": "🌴  LIFESTYLE",      "color": (160,80,220)},
    "SPORTS":    {"label": "🏅  SPORTS",         "color": (34,180,80)},
    # Legacy aliases — mapped so old code/feeds keep working
    "DISASTER":  {"label": "🚨  BREAKING NEWS", "color": (220,50,50)},
    "WORLD":     {"label": "🌍  WORLD NEWS",     "color": (220,80,60)},
    "WEATHER":   {"label": "🌴  LIFESTYLE",      "color": (160,80,220)},
    "TOURISM":   {"label": "🌴  LIFESTYLE",      "color": (160,80,220)},
    "FOOTBALL":  {"label": "🏅  SPORTS",         "color": (34,180,80)},
}

# Maps any legacy/raw category to one of the 5 canonical display categories
CATEGORY_MAP = {
    "BREAKING":"BREAKING", "DISASTER":"BREAKING",
    "LOCAL":"LOCAL",
    "POLITICAL":"POLITICAL",
    "LIFESTYLE":"LIFESTYLE", "TOURISM":"LIFESTYLE", "WEATHER":"LIFESTYLE",
    "SPORTS":"SPORTS", "FOOTBALL":"SPORTS",
    "WORLD":"LOCAL",  # world news folded into local (only Maldives-relevant posts anyway)
}

# Keywords that mark a story as POLITICAL (split out from general LOCAL)
POLITICAL_KEYWORDS = [
    "parliament","majlis","president","minister","ministry","government","cabinet",
    "mp ","ruling party","opposition","mdp","pnc","ppm","election","vote","policy",
    "bill","law","court","supreme court","judge","attorney general","ag office",
    "council","mayor","governor","resign","appointed","reshuffle","summit","diplomatic",
    "ambassador","foreign ministry","budget","parliamentary","constitution","impeach"
]

def canonical_category(cat, title="", summary=""):
    """Resolve raw category + content into one of the 5 display categories."""
    base = CATEGORY_MAP.get(cat, "LOCAL")
    # If it's LOCAL, check whether it's actually political
    if base == "LOCAL":
        text = (title + " " + summary).lower()
        if any(kw in text for kw in POLITICAL_KEYWORDS):
            return "POLITICAL"
    return base

# ── Core Team Session Context (in-memory only, clears on restart) ────────────
core_team_session_context = {}  # user_id -> stored context

# Pending manual card waiting for /confirm before firing to all platforms
# Only one at a time — new "create card and post" replaces the previous pending one.
_pending_manual_post = {}  # {card_bytes, full_caption, chat_id, thread_id, first_name}

# ── Universal Approval Queue (in-memory) ─────────────────────────────────────
# Every card (English + Dhivehi) waits here for Content Lab approval before posting.
# Cards expire after 2 hours if not approved.
ENGLISH_AUTOPOST_SECONDS = 900    # English: auto-post after 15 min if not approved
DHIVEHI_EXPIRY_SECONDS   = 7200   # Dhivehi: expire (delete) after 2h if not approved

approval_queue = {}  # key -> {card_bytes, caption, title, link, cat, lang, dv_text, created_at, ...}
_approval_counter = [0]

def store_pending_approval(card_bytes, caption, title, link, cat="LOCAL", lang="en",
                           dv_text=None, keyword="maldives news", source="LOCAL",
                           is_breaking=False, allow_social=True):
    """Store a fully-built card awaiting approval. Returns the key."""
    _approval_counter[0] += 1
    prefix = "dv" if lang == "dv" else "en"
    key = f"{prefix}{_approval_counter[0]}"
    approval_queue[key] = {
        "card_bytes": card_bytes,   # PNG bytes of the finished card (None for dv until approved)
        "caption": caption,          # full telegram caption
        "title": title,
        "link": link,
        "cat": cat,
        "lang": lang,
        "dv_text": dv_text,          # Dhivehi text (for dv cards, editable)
        "keyword": keyword,
        "source": source,
        "is_breaking": is_breaking,
        "allow_social": allow_social,
        "created_at": utcnow(),
    }
    # Cap queue size
    if len(approval_queue) > 40:
        oldest = list(approval_queue.keys())[0]
        del approval_queue[oldest]
    persist_state()
    return key

def expire_old_approvals():
    """
    Runs every few minutes:
      - English cards not approved within 15 min → AUTO-POST to community + social
      - Dhivehi cards not approved within 2 hours → DELETE (never auto-post unreviewed Dhivehi)
    """
    now = utcnow()

    # English auto-post after 15 min
    en_due = [k for k, v in approval_queue.items()
              if v["lang"] == "en" and (now - v["created_at"]).total_seconds() > ENGLISH_AUTOPOST_SECONDS]
    for k in en_due:
        item = approval_queue.pop(k)
        title = item.get("title","")[:50]
        log.info(f"⏰ English {k} not approved in 15min — auto-posting: {title}")
        try:
            ok = _publish_now(
                item["card_bytes"], item["caption"], item["cat"],
                item["title"], item["link"],
                is_breaking_flag=item.get("is_breaking", False),
                allow_social=item.get("allow_social", True),
                rewritten=item.get("rewritten",""),
                summary=item.get("summary","")
            )
            if ok:
                send_text(CORE_TEAM_CHAT_ID,
                          f"⏰ <b>{k.upper()} Auto-posted</b> (no approval in 15min):\n📰 {item['title'][:80]}",
                          thread_id=CONTENT_LAB_THREAD_ID)
        except Exception as e:
            log.error(f"Auto-post {k} failed: {e}")

    # Dhivehi expire (delete) after 2h
    dv_due = [k for k, v in approval_queue.items()
              if v["lang"] == "dv" and (now - v["created_at"]).total_seconds() > DHIVEHI_EXPIRY_SECONDS]
    for k in dv_due:
        title = approval_queue[k].get("title","")[:40]
        del approval_queue[k]
        log.info(f"⏰ Dhivehi {k} expired (2h, not approved, deleted): {title}")

    if en_due or dv_due:
        persist_state()

# Backwards-compat alias (old code references dhivehi_pending)
dhivehi_pending = approval_queue

# ── Core Team Config ──────────────────────────────────────────────────────────
CORE_TEAM_CHAT_ID = "-1002829230299"
CONTENT_LAB_THREAD_ID = 9061  # "Content Lab" topic — Dhivehi approvals + manual card creation

CORE_TEAM_MEMBERS = {
    "manchii": {"name": "Manchii", "full": "Abdul Muhsin", "role": "Founder & MD", "notes": "Big ideas, entrepreneur, boss, loves to push boundaries"},
    "mutte":   {"name": "Manchii", "full": "Abdul Muhsin", "role": "Founder & MD", "notes": "Big ideas, entrepreneur, boss, loves to push boundaries"},
    "uly":     {"name": "Uly", "full": "Mariyam Ulya", "role": "Co-Founder & Editor-in-Chief", "notes": "Journalist brain, editorial standards, keeps content sharp"},
    "ulya":    {"name": "Uly", "full": "Mariyam Ulya", "role": "Co-Founder & Editor-in-Chief", "notes": "Journalist brain, editorial standards, keeps content sharp"},
    "thooma":  {"name": "Thooma", "full": "Aminath Thooma", "role": "Presenter & Marketing Assistant", "notes": "Content face, presenter energy, needs confidence boosts sometimes"},
    "kit":     {"name": "Kity", "full": "Kit", "role": "Manchii's wife & idea contributor", "notes": "Creative, boosts team morale, great at boosting Thooma, shares fresh ideas"},
    "kity":    {"name": "Kity", "full": "Kit", "role": "Manchii's wife & idea contributor", "notes": "Creative, boosts team morale, great at boosting Thooma, shares fresh ideas"},
}

CORE_TEAM_PROACTIVE_TRIGGERS = [
    "?", "idea", "what do you think", "thoughts", "suggest", "brainstorm",
    "samuga", "content", "post", "story", "plan", "strategy", "marketing",
    "tiktok", "instagram", "facebook", "caption", "script", "video", "reel",
    "haha", "lol", "😂", "anyone", "guys", "let's", "lets", "what if", "how about"
]

# ── Rejection humor responses ────────────────────────────────────────────────
REJECT_RESPONSES = [
    "Okay okay, deleted. The article didn't make the cut. Just like my invite to your last outing. 💔",
    "Gone. Rejected. Just like that one pitch Manchii had at 2am. We don't talk about it. 🗑️",
    "Poof. Vanished. The article felt it too. 😭",
    "Rejected faster than a loan application. Card deleted bro. 🚮",
    "Understood. We move. The article does not. 👋",
    "That article just got voted off the island. Maldivian style. 🏝️",
    "Fine fine, I'll delete it. But between us — I thought it was good. Just saying. 🤷",
    "Deleted! The article is now in a better place. (The bin.) 🗑️✨",
]

BREAKING_KEYWORDS = [
    "killed","dead","dies","murder","shot","stabbed","explosion","bomb","attack",
    "tsunami","earthquake","flood","disaster","sinking","collapsed","hostage",
    "missing person","fire broke","crash landed","emergency landing","gas leak",
    "capsized","swept away","search and rescue"
]
# Note: "arrested" and "raided" removed — those go through normal news flow

# Keywords that should NEVER be breaking news
BREAKING_BLACKLIST = [
    "world cup","football","cricket","sports","fifa","champions league","premier league","tourism","resort","hotel","travel",
    "award","ranking","luxury","boutique","hospitality","destination","lagoon",
    "civil war","squad","team","player","match","game","season","transfer",
    "economy","business","market","price","investment","opening","launch","event"
]

# ── PostgreSQL Database Layer (v6) ────────────────────────────────────────────
# Railway auto-injects DATABASE_URL when Postgres is in the project.
# The bot uses Postgres for the article archive + intelligence, but ALWAYS falls
# back to JSON files if the DB is unavailable, so it never breaks.
DATABASE_URL = os.environ.get("DATABASE_URL", "")
_db_pool = None
DB_ENABLED = False

def init_database():
    """Connect to Postgres and create tables. Sets DB_ENABLED on success."""
    global _db_pool, DB_ENABLED
    if not DATABASE_URL:
        log.info("🗄️ No DATABASE_URL — running in JSON-only mode")
        return
    try:
        import psycopg2
        from psycopg2 import pool as _pgpool
        # Railway sometimes gives postgres:// — psycopg2 wants postgresql://
        url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        _db_pool = _pgpool.SimpleConnectionPool(1, 5, dsn=url)
        # Create schema
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
                        status          TEXT DEFAULT 'seen',   -- seen|queued|posted|rejected|duplicate
                        found_at        TIMESTAMPTZ DEFAULT NOW(),
                        posted_at       TIMESTAMPTZ,
                        tg_message_id   BIGINT,
                        tg_views        INTEGER DEFAULT 0,
                        meta_engagement INTEGER DEFAULT 0,   -- FB+IG reactions/comments/shares/likes
                        match_key       TEXT                 -- normalized headline for caption matching
                    );
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_articles_found_at ON articles(found_at);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_articles_status   ON articles(status);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_articles_category ON articles(category);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_articles_cluster  ON articles(cluster_id);")
                # Key-value store for bot state (replaces bot_state.json eventually)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS bot_kv (
                        key        TEXT PRIMARY KEY,
                        value      JSONB,
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    );
                """)
                # Phase 2: learning table — records every team action so the bot
                # can learn from approvals/rejections over time.
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS learning (
                        id              SERIAL PRIMARY KEY,
                        article_id      TEXT,
                        action          TEXT,          -- approved | rejected | edited | auto_posted
                        member          TEXT,          -- who did it (first_name)
                        category        TEXT,
                        source          TEXT,
                        score           INTEGER,
                        theme           TEXT,          -- trend theme if any
                        original_caption TEXT,
                        final_caption    TEXT,
                        lang            TEXT,
                        created_at      TIMESTAMPTZ DEFAULT NOW()
                    );
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_learning_action ON learning(action);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_learning_theme  ON learning(theme);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_articles_msgid  ON articles(tg_message_id);")
                # Phase 2.5: add Meta columns to an already-existing articles table (no-op if present)
                cur.execute("ALTER TABLE articles ADD COLUMN IF NOT EXISTS meta_engagement INTEGER DEFAULT 0;")
                cur.execute("ALTER TABLE articles ADD COLUMN IF NOT EXISTS match_key TEXT;")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_articles_matchkey ON articles(match_key);")
            conn.commit()
        finally:
            _db_pool.putconn(conn)
        DB_ENABLED = True
        log.info("🗄️ ✅ PostgreSQL connected — article archive active")
    except Exception as e:
        log.error(f"🗄️ Database init failed (falling back to JSON): {e}")
        DB_ENABLED = False

def db_execute(query, params=None, fetch=None):
    """
    Run a query safely with pooled connection.
    fetch: None (no result), 'one', or 'all'. Returns result or None on failure.
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

def db_record_article(article, score=0, reliability=0, status="seen", is_breaking=False):
    """Insert or update an article in the archive. Safe no-op if DB disabled."""
    if not DB_ENABLED:
        return
    db_execute("""
        INSERT INTO articles (id, title, summary, link, source, category, lang, score, reliability, is_breaking, status)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (id) DO UPDATE SET
            score=EXCLUDED.score, reliability=EXCLUDED.reliability, status=EXCLUDED.status
    """, (
        article.get("id"), article.get("title","")[:500], article.get("summary","")[:2000],
        article.get("link",""), article.get("source",""), article.get("cat",""),
        article.get("lang","en"), score, reliability, is_breaking, status
    ))

def db_mark_status(article_id, status, posted=False):
    """Update an article's lifecycle status (queued/posted/rejected/duplicate)."""
    if not DB_ENABLED:
        return
    if posted:
        db_execute("UPDATE articles SET status=%s, posted_at=NOW() WHERE id=%s", (status, article_id))
    else:
        db_execute("UPDATE articles SET status=%s WHERE id=%s", (status, article_id))

# ── Phase 2: bot_kv helpers + learning logger ────────────────────────────────
def kv_get(key, default=None):
    """Read a JSON value from bot_kv. Returns default if missing or DB off."""
    if not DB_ENABLED:
        return default
    row = db_execute("SELECT value FROM bot_kv WHERE key=%s", (key,), fetch="one")
    if row and row[0] is not None:
        return row[0]   # psycopg2 returns JSONB already parsed to dict/list
    return default

def kv_set(key, value):
    """Write a JSON value to bot_kv (upsert). No-op if DB off."""
    if not DB_ENABLED:
        return
    db_execute("""
        INSERT INTO bot_kv (key, value, updated_at)
        VALUES (%s, %s::jsonb, NOW())
        ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
    """, (key, json.dumps(value)))

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

def db_set_article_message(article_id, message_id):
    """Store the Telegram message_id for an article so we can fetch its views later."""
    if not DB_ENABLED or not article_id or not message_id:
        return
    db_execute("UPDATE articles SET tg_message_id=%s WHERE id=%s", (message_id, article_id))

def _caption_match_key(text):
    """
    Normalize a headline/caption to a stable key for matching the same story
    across Telegram, Facebook and Instagram. Lowercase, strip punctuation/emoji,
    collapse whitespace, take the first ~60 chars of meaningful words.
    """
    if not text:
        return ""
    import re as _re, unicodedata as _ud
    t = text.lower()
    # Drop the boilerplate Samuga tagline so it doesn't dominate the key
    for junk in ["samuga media", "samuga creative", "@samugacommunity",
                 "ސަމޫގާ މީޑިއާ", "📡", "🇲🇻"]:
        t = t.replace(junk.lower(), " ")
    # Fold accents to plain ASCII per-character so "Malé"->"male", but keep
    # thaana characters exactly in place (NFKD on thaana would corrupt them).
    out = []
    for ch in t:
        if "\u0780" <= ch <= "\u07bf":
            out.append(ch)                       # thaana — keep as-is
        else:
            folded = _ud.normalize("NFKD", ch).encode("ascii", "ignore").decode("ascii")
            out.append(folded)
    t = "".join(out)
    # Keep latin letters, digits, thaana; drop everything else
    t = _re.sub(r"[^a-z0-9\u0780-\u07bf ]", " ", t)
    t = _re.sub(r"\s+", " ", t).strip()
    return t[:60]

def db_set_article_matchkey(article_id, title):
    """Store the normalized match key for an article (for FB/IG caption matching)."""
    if not DB_ENABLED or not article_id:
        return
    mk = _caption_match_key(title)
    if mk:
        db_execute("UPDATE articles SET match_key=%s WHERE id=%s", (mk, article_id))

# ── TREND DETECTOR (v6 Intelligence) ─────────────────────────────────────────
# Reads the Postgres archive, extracts topics from article titles, counts how
# often each topic appears in 24h. 5+ mentions = a trending story.
# This is the "Understand + Rank" layer — the bot starts to see patterns.

# Topic keywords grouped by theme — what Maldives actually talks about.
# Each theme has trigger words; an article counts toward a theme if any match.
TREND_THEMES = {
    "Cost of Living":     ["cost of living","price","prices","inflation","expensive","rufiyaa","dollar rate","import","grocery","staple"],
    "Housing":            ["housing","flat","flats","land","plot","gedhoru","apartment","rent","hiya","vinares","social housing"],
    "Corruption":         ["corruption","bribe","embezzle","graft","acc ","anti-corruption","scandal","misuse","fraud","laundering"],
    "Drugs":              ["drug","drugs","narcotic","trafficking","heroin","cannabis","addict","rehab"],
    "Politics":           ["parliament","majlis","president","minister","cabinet","mp ","party","election","vote","impeach","no-confidence"],
    "Tourism":            ["tourism","resort","arrival","occupancy","tourist","visitor","bed night","travel"],
    "Fishing":            ["fishing","fisheries","fishermen","tuna","catch","mifco","masveriya"],
    "Crime":              ["murder","stabbing","assault","robbery","theft","arrested","police","gang","violence"],
    "Economy":            ["economy","gdp","budget","debt","loan","reserve","imf","world bank","deficit","sovereign"],
    "Weather/Disaster":   ["storm","flood","rain","swell","udha","fire","accident","sinking","capsize","rescue"],
    "Health":             ["hospital","health","disease","dengue","outbreak","aasandha","medical","clinic","doctor"],
    "Infrastructure":     ["bridge","harbour","airport","road","construction","project","development","sewerage","water"],
    "Education":          ["school","education","student","university","exam","teacher","scholarship"],
    "India/Foreign":      ["india","china","indian","chinese","foreign","diplomatic","embassy","bilateral","agreement"],
}

def _detect_themes(text):
    """Return the set of themes an article touches based on its text."""
    t = text.lower()
    hits = set()
    for theme, kws in TREND_THEMES.items():
        if any(kw in t for kw in kws):
            hits.add(theme)
    return hits

def detect_trends(hours=24, min_mentions=3):
    """
    Analyze the article archive for trending themes.
    Returns a sorted list of (theme, count, sample_titles) for themes with
    >= min_mentions in the time window. DB-only — returns [] if no Postgres.
    """
    if not DB_ENABLED:
        return []
    rows = db_execute(
        "SELECT title, summary FROM articles WHERE found_at > NOW() - INTERVAL %s",
        (f"{hours} hours",), fetch="all")
    if not rows:
        return []
    theme_counts = {}
    theme_titles = {}
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
    """
    Quick check: does this article belong to a currently-trending theme?
    Used to BOOST scoring for stories about hot topics.
    Returns (is_trending, theme_name, mention_count) or (False, None, 0).
    """
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


# ── Storage ───────────────────────────────────────────────────────────────────
DATA_DIR  = "/data"
os.makedirs(DATA_DIR, exist_ok=True)
SEEN_FILE  = os.path.join(DATA_DIR, "seen_articles.json")
STATE_FILE = os.path.join(DATA_DIR, "bot_state.json")

def load_seen():
    try:
        if os.path.exists(SEEN_FILE):
            with open(SEEN_FILE) as f: return set(json.load(f))
    except Exception as e:
        log.error(f"load_seen: {e}")
    return set()

def save_seen(seen):
    try:
        with open(SEEN_FILE,"w") as f: json.dump(list(seen)[-1000:], f)
    except Exception as e:
        log.error(f"save_seen: {e}")

# ── Generic State Persistence (survives Railway restarts) ─────────────────────
# Saves volatile state (dedup memory, daily counters, analytics, throttle timer,
# approval queue metadata) to /data so a restart doesn't wipe everything.
_state_lock = threading.Lock()

def _load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                return json.load(f)
    except Exception as e:
        log.error(f"load_state: {e}")
    return {}

def _save_state(state):
    try:
        with _state_lock:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, STATE_FILE)  # atomic write — no half-written files
    except Exception as e:
        log.error(f"save_state: {e}")

_poll_offset = [0]  # Telegram update offset — persisted so bot never misses messages on restart

def persist_state():
    """Snapshot all volatile state to disk. Called after any meaningful change."""
    try:
        state = {
            "recent_story_titles": [(t, ts.isoformat()) for (t, ts) in recent_story_titles],
            "recent_posts": recent_posts[-50:],
            "analytics": analytics,
            "daily_sports_count": daily_sports_count,
            "daily_world_count": daily_world_count,
            "daily_tourism_count": daily_tourism_count,
            "social_post_counts": _serialize_social_counts(),
            "polls_today": polls_today,
            "last_regular_post_time": last_regular_post_time.isoformat() if last_regular_post_time else None,
            "approval_counter": _approval_counter[0],
            "approval_queue": _serialize_approval_queue(),
            "poll_offset": _poll_offset[0],
        }
        _save_state(state)
    except Exception as e:
        log.error(f"persist_state: {e}")

def _serialize_social_counts():
    sc = dict(social_post_counts)
    if sc.get("date") and not isinstance(sc["date"], str):
        sc["date"] = sc["date"].isoformat()
    return sc

def _serialize_approval_queue():
    """Approval queue minus the heavy card image bytes (those are rebuilt on approval)."""
    import base64
    out = {}
    for k, v in approval_queue.items():
        item = dict(v)
        # Encode card bytes as base64 so English cards survive restart with their image
        if item.get("card_bytes"):
            try:
                item["card_bytes"] = base64.b64encode(item["card_bytes"]).decode()
                item["_card_b64"] = True
            except Exception:
                item["card_bytes"] = None
                item["_card_b64"] = False
        item["created_at"] = item["created_at"].isoformat() if item.get("created_at") else None
        out[k] = item
    return out

def restore_state():
    """Load persisted state back into memory on startup."""
    global recent_story_titles, recent_posts, analytics
    global daily_sports_count, daily_world_count, daily_tourism_count
    global social_post_counts, polls_today, last_regular_post_time
    state = _load_state()
    if not state:
        log.info("📦 No saved state — starting fresh")
        return
    import base64
    try:
        # Dedup memory
        recent_story_titles.clear()
        for (t, ts) in state.get("recent_story_titles", []):
            try: recent_story_titles.append((t, datetime.fromisoformat(ts)))
            except Exception: pass

        recent_posts.clear()
        recent_posts.extend(state.get("recent_posts", []))

        analytics.update(state.get("analytics", {}))

        daily_sports_count.update(state.get("daily_sports_count", {}))
        daily_world_count.update(state.get("daily_world_count", {}))
        daily_tourism_count.update(state.get("daily_tourism_count", {}))
        social_post_counts.update(state.get("social_post_counts", {}))
        polls_today.update(state.get("polls_today", {}))

        lrt = state.get("last_regular_post_time")
        if lrt:
            try: last_regular_post_time = datetime.fromisoformat(lrt)
            except Exception: pass

        _approval_counter[0] = state.get("approval_counter", 0)
        _poll_offset[0] = state.get("poll_offset", 0)

        # Restore approval queue (with card images)
        for k, item in state.get("approval_queue", {}).items():
            try:
                if item.get("_card_b64") and item.get("card_bytes"):
                    item["card_bytes"] = base64.b64decode(item["card_bytes"])
                item.pop("_card_b64", None)
                item["created_at"] = datetime.fromisoformat(item["created_at"]) if item.get("created_at") else utcnow()
                approval_queue[k] = item
            except Exception as e:
                log.error(f"restore approval {k}: {e}")

        log.info(f"📦 State restored: {len(recent_story_titles)} dedup titles, "
                 f"{len(approval_queue)} pending cards, {len(recent_posts)} recent posts")
    except Exception as e:
        log.error(f"restore_state: {e}")

# ── Memory ────────────────────────────────────────────────────────────────────
recent_posts = []
user_conversations = {}

# ── Duplicate Story Detection ─────────────────────────────────────────────────
# Tracks recently posted/queued story titles so the same event from different
# sources (Mihaaru / Sun / PSM) doesn't get posted multiple times.
recent_story_titles = []  # list of (title, timestamp)
DUP_WINDOW_HOURS = 18     # consider stories within this window for dedup
DUP_THRESHOLD = 0.55      # similarity above which two stories are "the same"

_DUP_STOPWORDS = {
    "the","a","an","of","in","on","at","to","for","and","or","is","are","was","were",
    "has","have","had","with","by","from","as","that","this","it","its","their","they",
    "maldives","maldivian","male","reported","says","said","after","over","amid","new",
    "breaking","news","update","live","video","photo","watch","near","into","out","be","will"
}

# Synonym groups — different outlets use different words for the same event
_DUP_SYNONYMS = {
    "parliament":"majlis","majlis":"majlis",
    "passes":"approve","approves":"approve","approved":"approve","passed":"approve","endorses":"approve","endorsed":"approve",
    "crash":"accident","accident":"accident","collision":"accident","collide":"accident",
    "injures":"injured","injured":"injured","hurt":"injured","wounded":"injured",
    "visits":"visit","visit":"visit","arrives":"visit","arrival":"visit","trip":"visit","arrived":"visit",
    "dies":"dead","died":"dead","killed":"dead","death":"dead","dead":"dead","passes away":"dead",
    "fire":"fire","blaze":"fire",
    "boat":"boat","speedboat":"boat","vessel":"boat","dhoni":"boat","launch":"boat","ferry":"boat",
    "arrested":"arrest","arrest":"arrest","detained":"arrest","held":"arrest",
    "minister":"minister","ministry":"minister",
    "president":"president","raees":"president",
}

def _dup_canon(word):
    return _DUP_SYNONYMS.get(word, word)

def _dup_keywords(title):
    """Extract canonicalized meaningful keywords from a title."""
    import re as _re
    t = title.lower()
    t = _re.sub(r"[^a-z0-9\u0780-\u07bf ]", " ", t)  # keep latin, digits, thaana
    return set(_dup_canon(w) for w in t.split() if w not in _DUP_STOPWORDS and len(w) > 2)

def _title_similarity(a, b):
    """Similarity 0-1 using canonicalized keyword overlap + containment."""
    ka, kb = _dup_keywords(a), _dup_keywords(b)
    if not ka or not kb:
        return 0.0
    overlap = len(ka & kb) / max(1, len(ka | kb))          # Jaccard
    contain = len(ka & kb) / max(1, min(len(ka), len(kb))) # smaller-set containment
    return max(overlap, contain * 0.85)

def is_duplicate_story(title):
    """True if a very similar story was posted/queued within the dedup window."""
    global recent_story_titles
    now = utcnow()
    recent_story_titles = [(t, ts) for (t, ts) in recent_story_titles
                           if (now - ts).total_seconds() < DUP_WINDOW_HOURS * 3600]
    for (past_title, _ts) in recent_story_titles:
        sim = _title_similarity(title, past_title)
        if sim >= DUP_THRESHOLD:
            log.info(f"🔁 Duplicate ({sim:.2f}): '{title[:45]}' ≈ '{past_title[:45]}'")
            return True
    return False

def remember_story_title(title):
    """Record a title so future similar stories are flagged as duplicates."""
    recent_story_titles.append((title, utcnow()))
    if len(recent_story_titles) > 200:
        recent_story_titles.pop(0)
    persist_state()

# ── Story Clustering (v6) — group same event from multiple sources ───────────
# When 3 outlets report the same fire, the bot doesn't post 3 times. It detects
# they're the same story and posts ONCE as "🔥 Multiple sources reporting...".
# story_clusters: cluster_key -> {"sources": set, "first_title": str, "ts": datetime}
story_clusters = {}

def _cluster_key(title):
    """A stable-ish key for a story so the same event maps to the same cluster."""
    kws = sorted(_dup_keywords(title))
    return " ".join(kws[:6])  # top keywords as the signature

# Maldivian place names + event types — strong signals two stories are the same event
_CLUSTER_PLACES = ["hulhumale","male","male'","villingili","addu","fuvahmulah","kulhudhuffushi",
    "thinadhoo","gan","hithadhoo","naifaru","dharavandhoo","maafushi","guraidhoo","thulusdhoo",
    "vilimale","gulhi","dhiffushi","raa","baa","laamu","gaafu","seenu","haa","noonu","thaa"]
_CLUSTER_EVENTS = {
    "fire":["fire","blaze","burn"], "accident":["accident","crash","collision"],
    "death":["dies","died","dead","killed","death","passed away"], "arrest":["arrest","arrested","detained"],
    "drowning":["drown","drowned"], "sinking":["sink","sank","capsize","capsized"],
    "robbery":["robbery","theft","stolen","burgle"], "stabbing":["stab","stabbed","stabbing"],
    "protest":["protest","demonstration","rally"], "storm":["storm","flood","swell","udha"],
}

def _cluster_similarity(a, b):
    """Looser similarity for clustering: same PLACE + same EVENT TYPE = same story."""
    base = _title_similarity(a, b)
    ta, tb = a.lower(), b.lower()
    # Shared place?
    place_a = next((p for p in _CLUSTER_PLACES if p in ta), None)
    place_b = next((p for p in _CLUSTER_PLACES if p in tb), None)
    same_place = place_a and place_a == place_b
    # Shared event type?
    def event_of(t):
        for ev, kws in _CLUSTER_EVENTS.items():
            if any(k in t for k in kws): return ev
        return None
    ev_a, ev_b = event_of(ta), event_of(tb)
    same_event = ev_a and ev_a == ev_b
    # Same place AND same event = almost certainly the same story
    if same_place and same_event:
        return max(base, 0.75)
    # Same event + decent word overlap
    if same_event and base >= 0.25:
        return max(base, 0.60)
    return base

def register_in_cluster(title, source):
    """
    Record that `source` is reporting this story. Returns (cluster_size, sources_list).
    If multiple sources report the same event, cluster_size > 1 = a corroborated story.
    """
    now = utcnow()
    # Clean old clusters
    expired = [k for k, v in story_clusters.items()
               if (now - v["ts"]).total_seconds() > DUP_WINDOW_HOURS * 3600]
    for k in expired:
        del story_clusters[k]

    # Find an existing cluster this title belongs to (cluster-aware fuzzy match)
    matched_key = None
    for k, v in story_clusters.items():
        if _cluster_similarity(title, v["first_title"]) >= 0.58:
            matched_key = k
            break
    if matched_key is None:
        matched_key = _cluster_key(title)
        story_clusters[matched_key] = {"sources": set(), "first_title": title, "ts": now}

    story_clusters[matched_key]["sources"].add(source or "Unknown")
    srcs = sorted(story_clusters[matched_key]["sources"])
    return (len(srcs), srcs)

# ── Source Reliability Scoring ────────────────────────────────────────────────
# Higher = more trusted. Used as a tie-breaker and a scoring boost so a direct
# Mihaaru/MvCrisis story outranks a Google News scrape of the same topic.
SOURCE_RELIABILITY = {
    "mvcrisis":    70,  # fast but mixes ads — lowered, filtered separately
    "mihaaru":     95,
    "sun":         92,
    "sunonline":   92,
    "psm":         90,
    "psmnews":     90,
    "presidency":  90,  # official gov source
    "edition":     88,
    "avas":        85,
    "see":         82,
    "maldivesindependent": 82,
    "oneonline":   80,
    "maldivesvoice": 78,
    "visitmaldives": 75,
    "google news": 55,  # aggregator — least trusted, often duplicates
}
DEFAULT_RELIABILITY = 60

def source_reliability(source_name):
    """Return a 0-100 reliability score for a source string."""
    if not source_name:
        return DEFAULT_RELIABILITY
    s = source_name.lower()
    for key, val in SOURCE_RELIABILITY.items():
        if key in s:
            return val
    return DEFAULT_RELIABILITY

# Analytics counters (reset weekly)
analytics = {"posts_by_cat": {}, "breaking_count": 0, "social_success": 0, "social_fail": 0, "week_start": None}

def track_analytics(cat, is_breaking=False, social_ok=None):
    global analytics
    from datetime import timezone as _tz
    week = (datetime.now(_tz.utc) + timedelta(hours=5)).isocalendar()[1]
    if analytics["week_start"] != week:
        analytics = {"posts_by_cat": {}, "breaking_count": 0, "social_success": 0, "social_fail": 0, "week_start": week}
    if cat != "SOCIAL":
        analytics["posts_by_cat"][cat] = analytics["posts_by_cat"].get(cat, 0) + 1
    if is_breaking: analytics["breaking_count"] += 1
    if social_ok is True: analytics["social_success"] += 1
    if social_ok is False: analytics["social_fail"] += 1

def remember_post(title, cat, timestamp):
    recent_posts.append({"title":title,"cat":cat,"time":timestamp})
    if len(recent_posts) > 50: recent_posts.pop(0)
    track_analytics(cat)
    persist_state()

def get_conversation(uid):
    if uid not in user_conversations: user_conversations[uid] = []
    return user_conversations[uid]

def add_to_conversation(uid, role, content):
    conv = get_conversation(uid)
    conv.append({"role":role,"content":content})
    if len(conv) > 10: user_conversations[uid] = conv[-10:]

# ── Helpers ───────────────────────────────────────────────────────────────────
def get_mvt_hour(): return (utcnow().hour + 5) % 24
def is_day_mode(): return 6 <= get_mvt_hour() < 22

def is_fresh(entry, hours=24):
    try:
        pub = entry.get("published","")
        if pub:
            dt = parsedate_to_datetime(pub)
            if dt.tzinfo: dt = dt.replace(tzinfo=None)
            return utcnow() - dt < timedelta(hours=hours)
    except Exception as e: log.debug(f"is_fresh parse: {e}")
    return True

def is_breaking(title, summary="", cat=""):
    text = (title + " " + summary).lower()

    # Never breaking for these categories
    if cat in ["FOOTBALL", "TOURISM", "WEATHER", "SPORTS", "LIFESTYLE"]: return False

    # Never breaking if it looks like an ad/promo (submarine hire, speedboat rental, etc.)
    if _looks_like_ad(text): return False

    # Check blacklist first — if any blacklist term present, not breaking
    if any(bl in text for bl in BREAKING_BLACKLIST): return False

    # Must match a real breaking keyword
    if not any(kw in text for kw in BREAKING_KEYWORDS): return False

    # For LOCAL category — must be Maldives related
    if cat == "LOCAL":
        mv_terms = ["maldives","male","malé","dhivehi","maldivian","raajje","atoll",
                    "police","court","majlis","minister","president","island"]
        if not any(t in text for t in mv_terms): return False

    return True

last_regular_post_time = None

# ── Daily posting counters (reset at midnight MVT) ────────────────────────────
def _mvt_today():
    return (utcnow() + timedelta(hours=5)).strftime("%Y-%m-%d")

daily_sports_count  = {"date": None, "count": 0}
daily_world_count   = {"date": None, "count": 0}
daily_tourism_count = {"date": None, "count": 0}

def can_post_cat_today(counter_dict, max_count):
    today = _mvt_today()
    if counter_dict["date"] != today:
        counter_dict["date"] = today
        counter_dict["count"] = 0
    return counter_dict["count"] < max_count

def increment_cat_count(counter_dict):
    today = _mvt_today()
    if counter_dict["date"] != today:
        counter_dict["date"] = today
        counter_dict["count"] = 0
    counter_dict["count"] += 1
    persist_state()

def can_post_regular():
    global last_regular_post_time
    if not last_regular_post_time: return True
    return (utcnow()-last_regular_post_time).total_seconds() >= 5400  # 1hr 30min

# ── Social filter — only quality LOCAL/DISASTER/relevant WORLD goes to socials ─
def allowed_for_social(article):
    """Only high-value articles go to Facebook/Instagram/X."""
    cat = article["cat"]
    # Never post these to social
    if cat in ["SPORTS", "FOOTBALL", "WEATHER", "TOURISM"]:
        return False
    if cat == "WORLD":
        # Only Maldives-relevant world news
        text = (article["title"] + " " + article.get("summary","")).lower()
        mv_terms = ["maldives","indian ocean","south asia","india","china","un ","dollar","oil price","global economy"]
        return any(t in text for t in mv_terms)
    # LOCAL and DISASTER always allowed
    return True

# ── Pending article queue — best article waiting for 90min window ─────────────
# Instead of posting to social every scan, we store the best article and post
# it only when the 90min Telegram window opens.
_pending_article = None  # holds the best unseen article between scans

# ── Social post daily counter (MVT based) ─────────────────────────────────────
social_post_counts = {"date": None, "count": 0}

def mvt_now():
    """Current time in Maldives Time (UTC+5)"""
    from datetime import timezone
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=5)

def is_day_social():
    """6AM to 10PM MVT = day mode for socials"""
    h = mvt_now().hour
    return 6 <= h < 22

def can_post_social():
    """Check if social daily limit not reached: 20 posts 6AM-10PM, 5 posts night"""
    global social_post_counts
    today = mvt_now().date()
    if social_post_counts["date"] != today:
        social_post_counts = {"date": today, "count": 0}
    limit = 20 if is_day_social() else 3
    return social_post_counts["count"] < limit

def increment_social_count():
    global social_post_counts
    today = mvt_now().date()
    if social_post_counts["date"] != today:
        social_post_counts = {"date": today, "count": 0}
    social_post_counts["count"] += 1
    persist_state()
    log.info(f"📊 Social posts today: {social_post_counts['count']} ({'day' if is_day_social() else 'night'} limit: {20 if is_day_social() else 3})")

# ── Gemini Translate ──────────────────────────────────────────────────────────
def gemini_translate(text):
    if not GEMINI_API_KEY: return text
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={GEMINI_API_KEY}"
        resp = requests.post(url, json={"contents":[{"parts":[{"text":f"Translate this Dhivehi text to English. Return ONLY the English translation:\n\n{text}"}]}]}, timeout=15)
        if resp.status_code == 200:
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e: log.error(f"Gemini: {e}")
    return text

# ── Fetch News ────────────────────────────────────────────────────────────────
# Words that signal an ad/promo/spam — never treat these as news
MVCRISIS_AD_MARKERS = [
    "hire","rent","for sale","available","booking","book now","contact","call now",
    "whatsapp","viber","discount","offer","promo","cheap","price","mvr ","rufiyaa ",
    "delivery","order now","dm ","inbox","trip","package","tour","charter","ferry service",
    "submarine","speed boat hire","speedboat hire","private trips","advertise","sponsored",
    "sale!","%","https://sauvees","buy ","sell ","service available","we offer"
]

def _looks_like_ad(text):
    """Heuristic: is this MvCrisis post an ad/promo rather than news?"""
    t = text.lower()
    hits = sum(1 for m in MVCRISIS_AD_MARKERS if m in t)
    # Multiple ad markers, or a phone number pattern, or a price = likely ad
    import re as _re
    has_phone = bool(_re.search(r"\b[79]\d{6}\b", t))  # Maldivian mobile pattern
    has_price = bool(_re.search(r"\b\d+\s*(mvr|rf|rufiyaa|usd|\$)\b", t))
    return hits >= 2 or (hits >= 1 and (has_phone or has_price))

def fetch_mvcrisis():
    """Scrape MvCrisis public Telegram channel — filters ads, only keeps real news."""
    try:
        resp = requests.get("https://t.me/s/mvcrisis", timeout=10,
                           headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200: return []
        import re as _re, hashlib
        texts = _re.findall(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', resp.text, _re.DOTALL)
        articles = []
        skipped_ads = 0
        for raw in texts[:15]:
            text = _re.sub(r"<[^>]+>", "", raw).strip()
            text = text.replace("&amp;","&").replace("&#39;","'").replace("&quot;",'"')
            if len(text) < 25: continue  # too short to be real news
            # Skip ads/promos
            if _looks_like_ad(text):
                skipped_ads += 1
                continue
            art_id = "mvc_" + hashlib.md5(text[:60].encode()).hexdigest()[:8]
            lang = "dv" if any("ހ" <= ch <= "޿" for ch in text) else "en"
            # Don't force DISASTER — let is_breaking() + category logic decide.
            # MvCrisis posts default to LOCAL; only become BREAKING if keywords match.
            articles.append({
                "id": art_id,
                "title": text[:150],
                "summary": text,
                "link": "https://t.me/mvcrisis",
                "source": "MvCrisis",
                "cat": "LOCAL",   # was DISASTER — fixed: not everything is an emergency
                "lang": lang,
                "published": utcnow()
            })
        log.info(f"📡 MvCrisis: {len(articles)} news kept, {skipped_ads} ads skipped")
        return articles
    except Exception as e:
        log.error(f"MvCrisis fetch: {e}")
        return []

def _feed_source_name(url):
    """Map a feed URL to a clean source name for reliability scoring + display."""
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
    return ""

def fetch_news():
    articles, seen_titles = [], set()
    # MvCrisis first — #1 Maldives breaking news source
    for a in fetch_mvcrisis():
        if a["title"] not in seen_titles:
            seen_titles.add(a["title"])
            articles.append(a)
    for fc in RSS_FEEDS:
        try:
            feed = feedparser.parse(fc["url"])
            for entry in feed.entries[:10]:
                title   = entry.get("title","")
                summary = entry.get("summary", title)
                if fc["lang"] == "dv":
                    title   = gemini_translate(title)
                    summary = gemini_translate(summary[:300])
                key = title.lower()[:50]
                if key in seen_titles or not is_fresh(entry): continue
                seen_titles.add(key)
                # Derive a clean source name: prefer RSS entry source, else feed domain
                entry_src = entry.get("source",{}).get("title", "") if isinstance(entry.get("source"), dict) else ""
                feed_src = _feed_source_name(fc["url"])
                src_name = entry_src or feed_src or fc["cat"]
                articles.append({
                    "id": hashlib.md5(entry.get("link",title).encode()).hexdigest(),
                    "title": title, "summary": summary,
                    "link": entry.get("link",""), "cat": fc["cat"],
                    "lang": fc["lang"],
                    "source": src_name,
                })
        except Exception as e: log.error(f"Feed error: {e}")
    log.info(f"Found {len(articles)} fresh articles")
    return articles

def get_local_headlines():
    headlines = []
    try:
        for fc in RSS_FEEDS[:5]:
            feed = feedparser.parse(fc["url"])
            for entry in feed.entries[:3]:
                title = entry.get("title","")
                if title and is_fresh(entry, hours=12):
                    headlines.append(f"• [{fc['cat']}] {title}")
            if len(headlines) >= 10: break
    except Exception as e: log.debug(f"get_local_headlines: {e}")
    return headlines[:10]

# ── Rewrite with Claude ───────────────────────────────────────────────────────
DEFAULT_KEYWORDS = {"LOCAL":"maldives government","FOOTBALL":"football stadium","WORLD":"world politics","DISASTER":"emergency rescue","WEATHER":"tropical weather","TOURISM":"maldives resort beach"}

def rewrite_news(title, summary, cat):
    cat_ctx = {"LOCAL":"local Maldivian news","FOOTBALL":"football news","WORLD":"world news","DISASTER":"disaster/emergency","WEATHER":"weather news","TOURISM":"tourism news"}.get(cat,"news")
    extra = "Note: Only headline available. Expand with relevant context." if not summary or summary.strip()==title.strip() or len(summary)<30 else ""
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
        msg = ai.messages.create(model="claude-haiku-4-5-20251001", max_tokens=400, messages=[{"role":"user","content":prompt}])
        text, kw = "", DEFAULT_KEYWORDS.get(cat,"maldives")
        for line in msg.content[0].text.strip().split('\n'):
            if line.startswith("TEXT:"): text = line[5:].strip()
            elif line.startswith("IMAGE:"): kw = line[6:].strip()
        return (text or title), kw
    except Exception as e:
        log.error(f"Claude rewrite: {e}")
        return title, DEFAULT_KEYWORDS.get(cat,"maldives")

# ── Pexels ────────────────────────────────────────────────────────────────────
# Category-specific Pexels keyword pools for manual cards with no photo attached
CAT_BG_KEYWORDS = {
    "BREAKING":  ["emergency lights dark", "crisis night city", "dark dramatic sky", "police lights night", "siren emergency"],
    "LOCAL":     ["maldives aerial ocean", "male maldives cityscape", "maldives island aerial", "tropical island drone", "maldives lagoon blue"],
    "POLITICAL": ["parliament building architecture", "government building columns", "official meeting room", "flag government building", "diplomatic hall"],
    "LIFESTYLE": ["maldives resort overwater", "maldives sunset beach", "tropical luxury resort", "maldives turquoise water", "maldives bungalow ocean"],
    "SPORTS":    ["football stadium lights night", "soccer field aerial", "athlete stadium crowd", "sport arena lights", "football pitch green"],
    "DISASTER":  ["emergency lights dark", "crisis rescue night", "dark storm dramatic", "fire rescue dark", "disaster rescue"],
    "WORLD":     ["world globe dark", "city skyline night", "international airport", "global city lights", "urban skyline dramatic"],
    "TOURISM":   ["maldives resort luxury", "tropical beach aerial", "maldives overwater villa", "island paradise blue", "resort pool tropical"],
    "WEATHER":   ["storm clouds dramatic", "tropical rain dark", "monsoon ocean waves", "dark clouds sea", "storm lightning ocean"],
    "FOOTBALL":  ["football stadium lights night", "soccer field green aerial", "football match crowd", "sport arena lights", "football pitch"],
}
DEFAULT_BG_KEYWORDS = ["maldives ocean aerial", "island blue lagoon", "tropical dark dramatic", "maldives night city", "ocean waves dark"]

def fetch_background_image(keyword, cat=None):
    """Fetch background from Pexels. If cat is given, uses category keyword pool for variety."""
    if not PEXELS_API_KEY: return None
    import random as _rand
    try:
        # Use category pool for manual cards (more variety, category-appropriate)
        if cat and cat in CAT_BG_KEYWORDS:
            search_kw = _rand.choice(CAT_BG_KEYWORDS[cat])
        elif not keyword or keyword in ["maldives news", "news"]:
            search_kw = _rand.choice(DEFAULT_BG_KEYWORDS)
        else:
            search_kw = keyword
        resp = requests.get(
            f"https://api.pexels.com/v1/search?query={search_kw}&per_page=10&orientation=square",
            headers={"Authorization": PEXELS_API_KEY}, timeout=15)
        if resp.status_code == 200:
            photos = resp.json().get("photos", [])
            if photos:
                # Pick randomly from results — no more same photo every time
                photo = _rand.choice(photos)
                r = requests.get(photo["src"]["large"], timeout=20)
                if r.status_code == 200:
                    log.info(f"✅ Pexels: {search_kw}")
                    return Image.open(BytesIO(r.content)).convert("RGB")
    except Exception as e:
        log.error(f"Pexels: {e}")
    return None

# ── Generate Card ─────────────────────────────────────────────────────────────
WHITE=(255,255,255); LIGHT_GRAY=(200,215,230); BG_TOP=(10,40,75); BG_BOTTOM=(5,20,45)


# ── Dhivehi Card Generator (Pango/Cairo — proper Thaana shaping) ──────────────
def generate_dhivehi_card(text, source, timestamp, cat, bg_image=None):
    """Generate card with proper Thaana shaping using Pango/Cairo"""
    try:
        import gi
        gi.require_version("Pango", "1.0")
        gi.require_version("PangoCairo", "1.0")
        from gi.repository import Pango, PangoCairo
        import cairo
    except Exception as e:
        log.error(f"Pango not available (falling back to PIL): {e}")
        log.info("Tip: ensure python3-gi is installed in Dockerfile")
        return generate_card(text, source, timestamp, cat, bg_image, _skip_dhivehi=True)

    import numpy as np

    W, H = 1080, 1080
    DV_CAT = {
        "BREAKING": {"label": "ބްރޭކިން ނިއުސް", "color": (220, 50, 50)},
        "LOCAL":    {"label": "ލޯކަލް ނިއުސް",   "color": (0, 180, 255)},
        "POLITICAL":{"label": "ސިޔާސީ",          "color": (180, 140, 40)},
        "LIFESTYLE":{"label": "ލައިފްސްޓައިލް",  "color": (160, 80, 220)},
        "SPORTS":   {"label": "ކުޅިވަރު",        "color": (34, 180, 80)},
        # Legacy aliases
        "DISASTER": {"label": "ބްރޭކިން ނިއުސް", "color": (220, 50, 50)},
        "WORLD":    {"label": "ދުނިޔެ",          "color": (50, 180, 100)},
        "FOOTBALL": {"label": "ކުޅިވަރު",        "color": (34, 180, 80)},
        "TOURISM":  {"label": "ލައިފްސްޓައިލް",  "color": (160, 80, 220)},
        "WEATHER":  {"label": "ލައިފްސްޓައިލް",  "color": (160, 80, 220)},
    }
    cfg = DV_CAT.get(cat, DV_CAT["LOCAL"])
    accent = cfg["color"]
    label_dv = cfg["label"]

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    ctx = cairo.Context(surface)

    # Background
    if bg_image:
        try:
            bg = bg_image.copy().convert("RGB")
            r = bg.width / bg.height
            nh, nw = (H, int(H*r)) if r > 1 else (int(W/r), W)
            bg = bg.resize((nw, nh), Image.LANCZOS)
            bg = bg.crop(((nw-W)//2, (nh-H)//2, (nw-W)//2+W, (nh-H)//2+H))
            bg = ImageEnhance.Brightness(bg).enhance(0.32)
            navy = Image.new("RGB", (W, H), (8, 30, 65))
            bg = Image.blend(bg, navy, 0.45).convert("RGBA")
            bg_arr = np.array(bg)
            bg_bgra = np.ascontiguousarray(bg_arr[:, :, [2, 1, 0, 3]])
            bg_surf = cairo.ImageSurface.create_for_data(bg_bgra, cairo.FORMAT_ARGB32, W, H)
            ctx.set_source_surface(bg_surf, 0, 0)
            ctx.paint()
        except Exception as e:
            log.error(f"BG paste: {e}")
            ctx.set_source_rgb(0.008, 0.047, 0.107)
            ctx.paint()
    else:
        ctx.set_source_rgb(0.008, 0.047, 0.107)
        ctx.paint()

    # Gradients
    grad = cairo.LinearGradient(0, H//2, 0, H)
    grad.add_color_stop_rgba(0, 0.02, 0.08, 0.2, 0)
    grad.add_color_stop_rgba(1, 0.02, 0.08, 0.2, 0.85)
    ctx.set_source(grad); ctx.rectangle(0, 0, W, H); ctx.fill()

    grad2 = cairo.LinearGradient(0, 0, 0, 170)
    grad2.add_color_stop_rgba(0, 0.02, 0.08, 0.2, 0.75)
    grad2.add_color_stop_rgba(1, 0, 0, 0, 0)
    ctx.set_source(grad2); ctx.rectangle(0, 0, W, H); ctx.fill()

    # Accent bar
    ctx.set_source_rgb(accent[0]/255, accent[1]/255, accent[2]/255)
    ctx.rectangle(0, 0, W, 5); ctx.fill()

    # PIL overlay for logo + footer
    from PIL import ImageDraw as _ID, ImageFont as _IF
    ov = Image.new("RGBA", (W, H), (0,0,0,0))
    od = _ID.Draw(ov)
    try:
        logo = Image.open("logo.png").convert("RGBA")
        lh = 72; lw = int(logo.width * lh / logo.height)
        logo = logo.resize((lw, lh), Image.LANCZOS)
        ov.paste(logo, (50, 38), logo)
    except Exception as e: log.debug(f"logo overlay: {e}")
    try:
        f_sm = _IF.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 21)
        od.text((W-310, 50), "t.me/samugacommunity", font=f_sm, fill=(200,230,255,220))
        od.text((50, H-52), f"Source: {source}", font=f_sm, fill=(180,200,220,220))
        tw = od.textlength(timestamp, font=f_sm)
        od.text((W-50-int(tw), H-52), timestamp, font=f_sm, fill=(180,200,220,220))
        od.line([(0, H-65),(W, H-65)], fill=(255,255,255,50), width=1)
    except Exception as e: log.debug(f"timestamp draw: {e}")
    ov_arr = np.array(ov)
    ov_bgra = np.ascontiguousarray(ov_arr[:, :, [2, 1, 0, 3]])
    ov_surf = cairo.ImageSurface.create_for_data(ov_bgra, cairo.FORMAT_ARGB32, W, H)
    ctx.set_source_surface(ov_surf, 0, 0); ctx.paint()

    # Category label (Dhivehi Pango)
    tag_y = 580
    cat_lo = PangoCairo.create_layout(ctx)
    # Use English label if no Thaana chars, otherwise use Dhivehi label
    cat_text = label_dv if any("\u0780" <= ch <= "\u07BF" for ch in label_dv) else label_dv
    cat_lo.set_text(cat_text, -1)
    cat_lo.set_font_description(Pango.FontDescription("Noto Sans Thaana Bold 20"))
    tw, _ = cat_lo.get_pixel_size()
    ctx.set_source_rgb(accent[0]/255, accent[1]/255, accent[2]/255)
    ctx.rectangle(50, tag_y, tw+26, 36); ctx.fill()
    ctx.set_source_rgb(1,1,1)
    ctx.move_to(63, tag_y+6); PangoCairo.show_layout(ctx, cat_lo)

    # Headline
    words = text.split()
    hw, bw = [], []
    cc = 0
    for i, w in enumerate(words):
        if cc < 80: hw.append(w); cc += len(w)+1
        else: bw = words[i:]; break
    headline = " ".join(hw)
    body = " ".join(bw)

    def to_arabic_nums(t):
        """Convert Western digits to Arabic-Indic numerals for RTL Thaana rendering"""
        return t.translate(str.maketrans("0123456789", "\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669"))

    h_lo = PangoCairo.create_layout(ctx)
    h_lo.set_width(980 * Pango.SCALE)
    h_lo.set_alignment(Pango.Alignment.RIGHT)
    h_fd = Pango.FontDescription("Noto Sans Thaana 50")
    h_fd.set_weight(Pango.Weight.ULTRABOLD)
    h_lo.set_font_description(h_fd)
    h_lo.set_text(to_arabic_nums(headline), -1)
    ctx.set_source_rgb(1,1,1)
    ctx.move_to(50, tag_y+44); PangoCairo.show_layout(ctx, h_lo)

    if body:
        _, hh = h_lo.get_pixel_size()
        b_lo = PangoCairo.create_layout(ctx)
        b_lo.set_width(980 * Pango.SCALE)
        b_lo.set_alignment(Pango.Alignment.RIGHT)
        b_lo.set_font_description(Pango.FontDescription("Noto Sans Thaana 26"))
        b_lo.set_text(to_arabic_nums(body), -1)
        ctx.set_source_rgba(0.78, 0.86, 1, 0.85)
        ctx.move_to(50, tag_y+44+hh+8); PangoCairo.show_layout(ctx, b_lo)

    # Export
    png_buf = io.BytesIO()
    surface.write_to_png(png_buf)
    png_buf.seek(0)
    return png_buf

def generate_card(text, source, timestamp, cat, bg_image=None, morning=False, _skip_dhivehi=False):
    # Route Dhivehi text to Pango-based card generator
    if not morning and not _skip_dhivehi and any('\u0780' <= ch <= '\u07BF' for ch in text):
        return generate_dhivehi_card(text, source, timestamp, cat, bg_image)

    W, H = 1080, 1080
    accent = (255,180,0) if morning else CAT_CONFIG.get(cat,CAT_CONFIG["LOCAL"])["color"]
    label  = "🌅  MORNING BRIEF" if morning else CAT_CONFIG.get(cat,CAT_CONFIG["LOCAL"])["label"]

    img = Image.new("RGB",(W,H),BG_TOP)
    if bg_image:
        bg = bg_image.copy()
        r = bg.width/bg.height
        nh,nw = (H,int(H*r)) if r>1 else (int(W/r),W)
        bg = bg.resize((nw,nh),Image.LANCZOS).crop(((nw-W)//2,(nh-H)//2,(nw-W)//2+W,(nh-H)//2+H))
        bg = ImageEnhance.Brightness(bg).enhance(0.32)
        img = Image.blend(bg, Image.new("RGB",(W,H),(8,30,65)), 0.45)
    else:
        d = ImageDraw.Draw(img)
        for y in range(H):
            t=y/H
            d.line([(0,y),(W,y)],fill=(int(BG_TOP[0]+(BG_BOTTOM[0]-BG_TOP[0])*t),int(BG_TOP[1]+(BG_BOTTOM[1]-BG_TOP[1])*t),int(BG_TOP[2]+(BG_BOTTOM[2]-BG_TOP[2])*t)))

    ov=Image.new("RGBA",(W,H),(0,0,0,0)); od=ImageDraw.Draw(ov)
    for y in range(H//2,H):
        t=(y-H//2)/(H//2); od.line([(0,y),(W,y)],fill=(5,20,50,int(185*t)))
    img=Image.alpha_composite(img.convert("RGBA"),ov).convert("RGB")

    ov2=Image.new("RGBA",(W,H),(0,0,0,0)); od2=ImageDraw.Draw(ov2)
    for y in range(0,170):
        t=1-y/170; od2.line([(0,y),(W,y)],fill=(5,20,50,int(190*t)))
    img=Image.alpha_composite(img.convert("RGBA"),ov2).convert("RGB")

    draw=ImageDraw.Draw(img)
    draw.rectangle([(0,0),(W,5)],fill=accent)

    try:
        logo=Image.open("logo.png").convert("RGBA")
        lh=72; lw=int(logo.width*lh/logo.height)
        logo=logo.resize((lw,lh),Image.LANCZOS)
        img.paste(logo,(50,38),logo)
    except Exception as e: log.debug(f"logo paste: {e}")

    # Detect Thaana script and use Noto Sans Thaana font for Dhivehi
    has_thaana = any('\u0780' <= ch <= '\u07BF' for ch in text)
    # Look for font in: /app (repo), /data (volume), system
    def find_thaana_font(name):
        for path in [f"/app/{name}", f"/data/{name}", f"/usr/share/fonts/truetype/noto/{name}"]:
            if os.path.exists(path): return path
        return None
    THAANA_BOLD = find_thaana_font("NotoSansThaana-Bold.ttf")
    THAANA_REG  = find_thaana_font("NotoSansThaana-Regular.ttf")
    try:
        if has_thaana and THAANA_BOLD:
            f_tag  = ImageFont.truetype(THAANA_BOLD, 22)
            f_title= ImageFont.truetype(THAANA_BOLD, 46)
            f_body = ImageFont.truetype(THAANA_REG or THAANA_BOLD, 27)
            f_sm   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 21)
            log.info(f"🇲🇻 Thaana font loaded: {THAANA_BOLD}")
        else:
            f_tag  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22)
            f_title= ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 46)
            f_body = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 27)
            f_sm   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 21)
    except Exception as e:
        log.debug(f"font load fallback: {e}")
        f_tag=f_title=f_body=f_sm=ImageFont.load_default()

    draw.text((W-310,50),"t.me/samugacommunity",font=f_sm,fill=(200,230,255))
    # For Thaana cards use plain English label (no emoji, DejaVu renders it)
    tag_label = {"BREAKING":"BREAKING NEWS","LOCAL":"LOCAL NEWS","POLITICAL":"POLITICAL",
                 "LIFESTYLE":"LIFESTYLE","SPORTS":"SPORTS",
                 "DISASTER":"BREAKING NEWS","WORLD":"WORLD NEWS","WEATHER":"LIFESTYLE",
                 "TOURISM":"LIFESTYLE","FOOTBALL":"SPORTS"}.get(cat, cat) if has_thaana else label
    f_tag_en = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22)
    tag_y=590; tw=draw.textbbox((0,0),tag_label,font=f_tag_en)[2]+26
    draw.rectangle([(50,tag_y),(50+tw,tag_y+34)],fill=accent)
    draw.text((63,tag_y+6),tag_label,font=f_tag_en,fill=WHITE if not morning else (0,0,0))

    def wrap(t,f,mw):
        words=t.split(); lines,cur=[],""
        for w in words:
            test=(cur+" "+w).strip()
            if draw.textbbox((0,0),test,font=f)[2]<=mw: cur=test
            else:
                if cur: lines.append(cur)
                cur=w
        if cur: lines.append(cur)
        return lines

    # Convert Western digits to Arabic-Indic for RTL Thaana rendering
    if has_thaana:
        text = text.translate(str.maketrans("0123456789", "\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669"))

    # For Thaana text — don't split on '. ' as it breaks Dhivehi sentences
    if has_thaana:
        words = text.split()
        headline_words = []
        body_words = []
        char_count = 0
        for i, w in enumerate(words):
            if char_count < 80:
                headline_words.append(w)
                char_count += len(w) + 1
            else:
                body_words = words[i:]
                break
        headline = ' '.join(headline_words)
        body = ' '.join(body_words)
    else:
        sentences=text.split('. ')
        headline=sentences[0]+('.' if len(sentences)>1 else '')
        body='. '.join(sentences[1:]) if len(sentences)>1 else ''

    y=tag_y+48
    for line in wrap(headline,f_title,W-100)[:4]:
        draw.text((50,y),line,font=f_title,fill=WHITE); y+=56
    if body:
        y+=4
        for line in wrap(body,f_body,W-100)[:3]:
            draw.text((50,y),line,font=f_body,fill=LIGHT_GRAY); y+=36

    draw.rectangle([(0,H-78),(W,H)],fill=(3,12,30))
    draw.rectangle([(0,H-78),(W,H-75)],fill=accent)
    draw.text((50,H-53),f"Source: {source}",font=f_sm,fill=LIGHT_GRAY)
    draw.text((W-260,H-53),timestamp,font=f_sm,fill=LIGHT_GRAY)

    buf=BytesIO(); img.save(buf,format="PNG",quality=95); buf.seek(0)
    return buf

# ── Telegram ──────────────────────────────────────────────────────────────────
def send_to_telegram(buf, caption):
    """Post a photo to the community channel. Returns message_id (int) or False."""
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
            data={"chat_id": TELEGRAM_CHANNEL_ID, "caption": caption, "parse_mode": "HTML"},
            files={"photo": ("card.png", buf, "image/png")}, timeout=30)
        resp.raise_for_status()
        mid = resp.json().get("result", {}).get("message_id")
        log.info(f"✅ Posted to Telegram (msg {mid})")
        return mid or True
    except Exception as e:
        log.error(f"Telegram: {e}")
        return False

def download_telegram_photo(photo_list):
    """Download the highest quality photo from a Telegram photo array"""
    try:
        # Get largest photo (last in list)
        file_id = photo_list[-1]["file_id"]
        # Get file path
        resp = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile",
            params={"file_id": file_id}, timeout=10
        )
        file_path = resp.json()["result"]["file_path"]
        # Download the file
        img_resp = requests.get(
            f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}",
            timeout=20
        )
        from PIL import Image
        img = Image.open(io.BytesIO(img_resp.content)).convert("RGB")
        log.info("✅ Telegram photo downloaded — returning PIL Image for card generation")
        return img  # generate_card expects PIL Image, not BytesIO
    except Exception as e:
        log.error(f"Photo download: {e}")
        return None

def send_photo(chat_id, buf, caption, thread_id=None):
    """Send a photo to any Telegram chat/channel, optionally to a topic thread"""
    try:
        buf.seek(0)
        data = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
        if thread_id: data["message_thread_id"] = thread_id
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
            data=data,
            files={"photo": ("card.png", buf, "image/png")},
            timeout=30
        )
        resp.raise_for_status()
        log.info("✅ Photo sent to Telegram")
        return True
    except Exception as e:
        log.error(f"send_photo: {e}")
        return False

def send_text(chat_id, text, reply_to=None, thread_id=None):
    payload={"chat_id":chat_id,"text":text,"parse_mode":"HTML"}
    if reply_to: payload["reply_to_message_id"]=reply_to
    if thread_id: payload["message_thread_id"]=thread_id
    try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",json=payload,timeout=15)
    except Exception as e: log.error(f"Send text: {e}")

# ── Gemini Dhivehi Caption ────────────────────────────────────────────────────
def make_dhivehi_caption(english_text, title):
    """Convert English news caption to Dhivehi using Gemini"""
    if not GEMINI_API_KEY:
        return None
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={GEMINI_API_KEY}"
        prompt = f"""You are a Maldivian news writer. Write a short news caption in Dhivehi (Thaana script) for this news.

English title: {title}
English summary: {english_text}

Write 2-3 sentences in natural Dhivehi as it would appear in a Maldivian news channel.
Return ONLY the Dhivehi text, nothing else."""

        resp = requests.post(url, json={"contents":[{"parts":[{"text":prompt}]}]}, timeout=15)
        if resp.status_code == 200:
            dv_text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            log.info(f"✅ Gemini Dhivehi caption done")
            return dv_text
    except Exception as e:
        log.error(f"Gemini Dhivehi caption: {e}")
    return None

# ── Auto Poll ─────────────────────────────────────────────────────────────────
POLL_KEYWORDS = [
    "government","president","parliament","minister","policy","law","vote","election",
    "decision","budget","tax","fee","regulation","announce","reform","appointed",
    "resign","fired","arrested","court","judge","sentence","verdict","accused",
    "protest","rally","strike","ban","approve","reject","pass","failed"
]

# Poll daily counter (max 3/day MVT)
polls_today = {"date": None, "count": 0}

def can_post_poll():
    global polls_today
    today = (utcnow() + timedelta(hours=5)).strftime("%Y-%m-%d")
    if polls_today["date"] != today:
        polls_today = {"date": today, "count": 0}
    return polls_today["count"] < 3

def increment_poll_count():
    global polls_today
    today = (utcnow() + timedelta(hours=5)).strftime("%Y-%m-%d")
    if polls_today["date"] != today:
        polls_today = {"date": today, "count": 0}
    polls_today["count"] += 1
    persist_state()
    log.info(f"🗳️ Polls today: {polls_today['count']}/3")

def should_create_poll(title, summary, cat):
    """Check if news warrants a poll (max 3/day)"""
    if cat not in ["LOCAL", "WORLD"]: return False
    if not can_post_poll(): return False
    text = (title + " " + summary).lower()
    return any(kw in text for kw in POLL_KEYWORDS)

def generate_poll_question(title, rewritten):
    """Use Claude to generate a relevant poll question"""
    try:
        msg = ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role":"user","content":f"""Based on this news, create a simple Telegram poll.

News: {title}
Summary: {rewritten}

Return EXACTLY in this format (nothing else):
QUESTION: [one short poll question in English]
OPT1: [option 1, max 4 words]
OPT2: [option 2, max 4 words]
OPT3: [option 3, max 4 words]

Keep it simple, neutral and relevant to the news."""}]
        )
        text = msg.content[0].text.strip()
        question, options = "", []
        for line in text.split('\n'):
            if line.startswith("QUESTION:"): question = line[9:].strip()
            elif line.startswith("OPT"): options.append(line.split(":",1)[1].strip())
        return question, options[:3]
    except Exception as e:
        log.error(f"Poll generation: {e}")
        return None, []

def send_poll(question, options):
    """Send a Telegram poll to the channel"""
    if not question or len(options) < 2:
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPoll",
            json={
                "chat_id": TELEGRAM_CHANNEL_ID,
                "question": f"🗳️ {question}",
                "options": options,
                "is_anonymous": True,
            },
            timeout=15
        )
        if resp.status_code == 200:
            log.info(f"✅ Poll sent: {question[:50]}")
        else:
            log.error(f"Poll error: {resp.status_code}")
    except Exception as e:
        log.error(f"Poll send: {e}")

# ── Buffer / Social ───────────────────────────────────────────────────────────
def upload_to_imgbb(img_bytes):
    if not IMGBB_API_KEY: return None
    try:
        import base64
        resp=requests.post("https://api.imgbb.com/1/upload",data={"key":IMGBB_API_KEY,"image":base64.b64encode(img_bytes).decode()},timeout=20)
        if resp.status_code==200:
            url=resp.json()["data"]["url"]; log.info(f"✅ imgbb: {url[:50]}"); return url
    except Exception as e: log.error(f"imgbb: {e}")
    return None

def resolve_url(url):
    """Follow redirects to get real URL (fixes Google News RSS links)"""
    if not url: return url
    try:
        if "news.google.com" in url or "feedproxy" in url:
            r = requests.get(url, allow_redirects=True, timeout=10)
            log.info(f"🔗 Resolved: {r.url[:80]}")
            return r.url
    except Exception as e:
        log.warning(f"URL resolve failed: {e}")
    return url

def post_to_buffer(image_url, caption, channel_id, metadata=None):
    """Post to a single Buffer channel. Returns True on success."""
    if not BUFFER_TOKEN or not channel_id: return False
    clean = re.sub(r'<[^>]+>', '', caption)
    clean = clean.replace('&amp;', '&').replace('&#039;', "'").replace('&quot;', '"').replace('&lt;', '<').replace('&gt;', '>').strip()

    query = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    ... on PostActionSuccess { post { id text } }
    ... on MutationError { message }
  }
}"""

    post_input = {
        "text": clean,
        "channelId": channel_id,
        "schedulingType": "automatic",
        "mode": "shareNow",
        "assets": [{"image": {"url": image_url}}],
    }
    if metadata:
        post_input["metadata"] = metadata

    try:
        resp = requests.post(
            "https://api.buffer.com",
            json={"query": query, "variables": {"input": post_input}},
            headers={"Authorization": f"Bearer {BUFFER_TOKEN}", "Content-Type": "application/json"},
            timeout=20
        )
        log.info(f"Buffer raw [{channel_id[:8]}]: {resp.status_code} | {resp.text[:400]}")
        if resp.status_code == 200:
            data = resp.json()
            if "errors" in data:
                log.error(f"Buffer GraphQL error [{channel_id[:8]}]: {data['errors']}")
                return False
            result = data.get("data", {}).get("createPost", {})
            err_msg = result.get("message", "")
            if err_msg:
                log.error(f"Buffer mutation error [{channel_id[:8]}]: {err_msg}")
                return False
            post_id = result.get("post", {}).get("id", "?")
            log.info(f"✅ Buffer posted [{channel_id[:8]}] id={post_id}")
            return True
        else:
            log.error(f"Buffer HTTP {resp.status_code}: {resp.text[:300]}")
    except Exception as e:
        log.error(f"Buffer exception [{channel_id[:8]}]: {e}")
    return False

def post_to_social(img_buf, caption):
    if not BUFFER_TOKEN:
        log.warning("Social: no BUFFER_TOKEN, skipping")
        return
    if not can_post_social():
        limit = 20 if is_day_social() else 3
        log.info(f"📵 Social limit reached ({30 if is_day_social() else 5} posts {'day' if is_day_social() else 'night'}) — skipping")
        return
    try:
        img_bytes = img_buf.getvalue()
        image_url = upload_to_imgbb(img_bytes)
        if not image_url:
            log.error("Social: imgbb upload failed, skipping")
            return

        # Strip HTML for all social platforms
        clean = re.sub(r'<[^>]+>', '', caption)
        clean = clean.replace('&amp;', '&').replace('&#039;', "'").replace('&quot;', '"').replace('&lt;', '<').replace('&gt;', '>').strip()

        # Extract and resolve article URL (fixes Google News redirects)
        link_match = re.search(r"href='([^']+)'", caption)
        raw_url = link_match.group(1) if link_match else ""
        article_url = resolve_url(raw_url) if raw_url else ""

        # FB/IG: full text + real link
        fb_ig = clean
        if article_url and article_url not in fb_ig:
            fb_ig = fb_ig + "\n\n" + article_url
        fb_ig = fb_ig[:2200]

        # Twitter: first line + link (280 char hard limit)
        lines = [l.strip() for l in clean.split('\n') if l.strip()]
        tw = (lines[0] if lines else clean)[:220]
        if article_url:
            tw = tw + "\n\n" + article_url
        tw = tw[:280]

        results = {}
        for cid, cap, name, meta in [
            (BUFFER_FB_ID, fb_ig, "Facebook",  {"facebook":  {"type": "post"}}),
            (BUFFER_IG_ID, fb_ig, "Instagram", {"instagram": {"type": "post", "shouldShareToFeed": True}}),
            (BUFFER_TW_ID, tw,    "Twitter",   None),
        ]:
            if not cid:
                log.warning(f"Social: skipping {name} — no channel ID set")
                continue
            results[name] = post_to_buffer(image_url, cap, cid, metadata=meta)
            time.sleep(2)

        ok = [k for k, v in results.items() if v]
        fail = [k for k, v in results.items() if not v]
        if ok:
            log.info(f"✅ Social posted to: {', '.join(ok)}")
            increment_social_count()
            track_analytics("SOCIAL", social_ok=True)
        if fail:
            log.error(f"❌ Social failed for: {', '.join(fail)}")
        return results  # Return per-platform results so callers can report back
    except Exception as e:
        log.error(f"Social: {e}")

# ── Post Article ──────────────────────────────────────────────────────────────
def _build_card_and_caption(article):
    """Build the card image + caption for an article. Returns (card_bytes, caption, rewritten, keyword)."""
    raw_cat = article["cat"]
    breaking = is_breaking(article["title"], article["summary"], raw_cat)
    # Resolve to one of the 5 display categories. Breaking overrides everything.
    if breaking:
        display_cat = "BREAKING"
    else:
        display_cat = canonical_category(raw_cat, article["title"], article.get("summary",""))
    rewritten, keyword = rewrite_news(article["title"], article["summary"], raw_cat)
    bg = fetch_background_image(keyword)
    ts = (utcnow() + timedelta(hours=5)).strftime("%d %b %Y • %H:%M")
    card = generate_card(rewritten, article["source"], ts, display_cat, bg)
    card_bytes = card.getvalue()

    cat_emoji = {"BREAKING":"🚨","LOCAL":"🇲🇻","POLITICAL":"🏛️","LIFESTYLE":"🌴","SPORTS":"🏅"}.get(display_cat,"📰")
    breaking_tag = "🚨 <b>BREAKING NEWS</b>\n\n" if breaking else ""

    # Clustering boosts a story's importance internally (more outlets covering it
    # = bigger story = higher score), but we NEVER credit competitors on the card.
    # Samuga sees everything, merges it, rewrites it, and posts it as its own.
    caption = (f"{breaking_tag}{cat_emoji} <b>{article['title']}</b>\n\n"
               f"{rewritten}\n\n"
               f"📡 <b>Samuga Media</b> | @samugacommunity")
    return card_bytes, caption, rewritten, keyword


def _publish_now(card_bytes, caption, cat, title, link, is_breaking_flag, allow_social,
                 rewritten="", summary="", report_to=None, article_id=None):
    """
    Post a card to Telegram + socials. Returns (tg_ok, social_results).
    report_to: optional (chat_id, thread_id) to send a per-platform status report.
    article_id: if given, the Telegram message_id is stored for later view tracking.
    """
    global last_regular_post_time
    buf = io.BytesIO(card_bytes)
    ts = (utcnow() + timedelta(hours=5)).strftime("%d %b %Y • %H:%M")
    social_results = {}

    log.info(f"📰 [{'🔴BREAKING' if is_breaking_flag else '🟡REGULAR'}][{cat}] {title[:60]}...")
    buf.seek(0)
    tg_ok = send_to_telegram(buf, caption)

    if tg_ok:
        remember_post(title, cat, ts)
        if isinstance(tg_ok, int) and article_id:        # Phase 2: store msg id
            db_set_article_message(article_id, tg_ok)
        if article_id:                                    # Phase 2.5: store match key for FB/IG
            db_set_article_matchkey(article_id, title)
        if not is_breaking_flag:
            last_regular_post_time = utcnow()
            persist_state()
            log.info("🕐 Regular timer reset — next Telegram post in 90min")
        else:
            log.info("🔴 Breaking posted!")

    # Social — run synchronously when report_to is set so we can report results
    if allow_social:
        social_buf = io.BytesIO(card_bytes)
        if report_to:
            # Synchronous — so we can tell the team what happened per platform
            social_results = post_to_social(social_buf, caption) or {}
        else:
            # Background thread for automated posts (don't block)
            threading.Thread(target=post_to_social, args=(social_buf, caption), daemon=True).start()

    # Poll
    if tg_ok and should_create_poll(title, summary, cat):
        log.info("🗳️ Generating poll...")
        question, options = generate_poll_question(title, rewritten or title)
        if question and options:
            time.sleep(3)
            send_poll(question, options)
            increment_poll_count()

    # Report per-platform status back to Content Lab
    if report_to:
        rchat, rthread = report_to
        tg_icon  = "✅" if tg_ok else "❌"
        fb_icon  = "✅" if social_results.get("Facebook")  else ("❌" if "Facebook"  in social_results else "⏭️")
        ig_icon  = "✅" if social_results.get("Instagram") else ("❌" if "Instagram" in social_results else "⏭️")
        tw_icon  = "✅" if social_results.get("Twitter")   else ("❌" if "Twitter"   in social_results else "⏭️")
        status = (
            f"{tg_icon} Telegram  {fb_icon} Facebook  {ig_icon} Instagram  {tw_icon} Twitter"
        )
        if tg_ok:
            msg = f"✅ <b>{title[:70]}</b>\n\n📡 {status}"
        else:
            msg = (f"⚠️ <b>Post had issues</b>\n\n"
                   f"📡 {status}\n\n"
                   f"Telegram failed — card not posted to community.")
        # Show retry tip if anything failed
        has_failure = not tg_ok or any(v is False for v in social_results.values())
        if has_failure:
            msg += f"\n\n💡 <i>Telegram failed? The article may have expired. Create a manual card instead.</i>"
        send_text(rchat, msg, thread_id=rthread)

    return tg_ok, social_results


def _send_approval_card(key, item):
    """Send a card preview to Content Lab with approve/reject buttons."""
    cat = item["cat"]
    lang_tag = "🇲🇻 Dhivehi" if item["lang"] == "dv" else "🇬🇧 English"
    brk = "🚨 BREAKING " if item["is_breaking"] else ""
    cat_emoji = {"BREAKING":"🚨","LOCAL":"🇲🇻","POLITICAL":"🏛️","LIFESTYLE":"🌴","SPORTS":"🏅","FOOTBALL":"⚽","WORLD":"🌍","DISASTER":"🚨","WEATHER":"🌤️","TOURISM":"✈️"}.get(cat,"📰")
    # KEY first and BIG so stacked cards are instantly identifiable
    header = (
        f"🔑 <b>{key.upper()}</b>  •  {cat_emoji} {cat}\n"
        f"{brk}<b>{lang_tag} Card — Review Needed</b>\n\n"
        f"<b>📰 {item['title']}</b>\n\n"
    )
    if item["lang"] == "dv" and item.get("dv_text"):
        header += f"<b>Bot wrote:</b>\n{item['dv_text']}\n\n"
    footer = (
        f"✅ <code>/approved {key}</code>\n"
    )
    if item["lang"] == "dv":
        footer += f"✏️ <code>/approved {key} [corrected dhivehi text]</code>\n"
    footer += f"❌ <code>/reject {key}</code>\n\n"
    # Tell the team the auto-post / expiry behaviour
    if item["lang"] == "en":
        footer += "<i>⏰ Auto-posts in 15 min if not reviewed</i>"
    else:
        footer += "<i>⏰ Expires in 2h if not approved (Dhivehi never auto-posts)</i>"
    msg = header + footer

    # If we have a finished card image, send it as a photo with the caption
    if item.get("card_bytes"):
        buf = io.BytesIO(item["card_bytes"])
        send_photo(CORE_TEAM_CHAT_ID, buf, msg, thread_id=CONTENT_LAB_THREAD_ID)
    else:
        send_text(CORE_TEAM_CHAT_ID, msg, thread_id=CONTENT_LAB_THREAD_ID)
    log.info(f"📨 Approval card sent to Content Lab: {key} ({item['lang']})")


def post_article(article, seen, social_only=False, allow_social=True):
    """
    New v5 flow:
      - English BREAKING → publish instantly (1 at a time, no approval)
      - Everything else (English regular + ALL Dhivehi) → queue for Content Lab approval
    Marks article as seen immediately so it isn't re-queued every scan.
    """
    cat = article["cat"]
    breaking = is_breaking(article["title"], article["summary"], cat)
    is_dv = article.get("lang") == "dv"

    # Mark seen now so the same article isn't re-processed on the next scan
    seen.add(article["id"]); save_seen(seen)

    # Archive every article we process (DB no-op if Postgres unavailable)
    db_record_article(article, score=score_article(article),
                      reliability=source_reliability(article.get("source","")),
                      status="seen", is_breaking=breaking)

    # ── Story clustering — track which sources report this event ──
    cluster_size, cluster_sources = register_in_cluster(article["title"], article.get("source",""))

    # ── Duplicate story check — skip if same event already posted/queued ──
    if is_duplicate_story(article["title"]):
        log.info(f"⏭️ Skipping duplicate ({cluster_size} sources): {article['title'][:55]}")
        db_mark_status(article["id"], "duplicate")
        return False
    # Record this title so later similar stories are caught
    remember_story_title(article["title"])
    # Stash cluster info on the article so the card can show "X sources reporting"
    article["_cluster_size"] = cluster_size
    article["_cluster_sources"] = cluster_sources

    # ── English BREAKING: publish instantly, no approval ──
    if breaking and not is_dv:
        card_bytes, caption, rewritten, keyword = _build_card_and_caption(article)
        tg_ok, _social = _publish_now(card_bytes, caption, cat, article["title"], article["link"],
                            is_breaking_flag=True, allow_social=allow_social,
                            rewritten=rewritten, summary=article.get("summary",""),
                            article_id=article["id"])
        db_mark_status(article["id"], "posted" if tg_ok else "seen", posted=bool(tg_ok))
        return bool(tg_ok)

    # ── Dhivehi cards: generate Dhivehi text, queue for approval (card built on approval) ──
    if is_dv:
        try:
            rewritten, keyword = rewrite_news(article["title"], article["summary"], cat)
            dv_text = make_dhivehi_caption(rewritten, article["title"])
            if not dv_text:
                log.warning(f"Dhivehi caption failed for: {article['title'][:50]}")
                return False
            key = store_pending_approval(
                None, None, article["title"], article["link"], cat=cat, lang="dv",
                dv_text=dv_text, keyword=keyword, source=article.get("source","LOCAL"),
                is_breaking=breaking, allow_social=allow_social
            )
            approval_queue[key]["article_id"] = article["id"]
            approval_queue[key]["_cluster_size"] = article.get("_cluster_size", 1)
            approval_queue[key]["_trend_theme"] = article.get("_trend_theme", "")
            approval_queue[key]["summary"] = article.get("summary", "")
            _send_approval_card(key, approval_queue[key])
            db_mark_status(article["id"], "queued")
            return True
        except Exception as e:
            log.error(f"Dhivehi approval queue: {e}")
            return False

    # ── English regular: build card, queue for approval ──
    try:
        card_bytes, caption, rewritten, keyword = _build_card_and_caption(article)
        key = store_pending_approval(
            card_bytes, caption, article["title"], article["link"], cat=cat, lang="en",
            dv_text=None, keyword=keyword, source=article.get("source","LOCAL"),
            is_breaking=breaking, allow_social=allow_social
        )
        # Stash rewritten + summary for poll generation on approval
        approval_queue[key]["rewritten"] = rewritten
        approval_queue[key]["summary"] = article.get("summary","")
        approval_queue[key]["article_id"] = article["id"]
        approval_queue[key]["_cluster_size"] = article.get("_cluster_size", 1)
        approval_queue[key]["_trend_theme"] = article.get("_trend_theme", "")
        _send_approval_card(key, approval_queue[key])
        db_mark_status(article["id"], "queued")
        return True
    except Exception as e:
        log.error(f"English approval queue: {e}")
        return False


# ── Run Job ───────────────────────────────────────────────────────────────────
def score_article(a):
    """Score article by Maldives relevance + breaking priority"""
    score = 0
    title_lower = a["title"].lower()
    summary_lower = a.get("summary","").lower()
    cat = a["cat"]
    # Category priority — LOCAL is king
    if cat == "LOCAL": score += 80
    elif cat == "DISASTER": score += 70
    elif cat == "WEATHER": score += 30
    elif cat == "TOURISM": score += 20
    elif cat == "WORLD": score += 10
    elif cat in ["FOOTBALL", "SPORTS"]: score += 2  # Very low priority
    # Maldives keywords boost
    mv_kws = ["maldives","male","dhivehi","raajje","mvr","atoll","island","gaa",
              "parliament","majlis","president","minister","police","court","malé",
              "hulhumale","addu","fuvahmulah","laamu","economy","rufiyaa"]
    for kw in mv_kws:
        if kw in title_lower or kw in summary_lower: score += 20
    # Sports penalty — only post if really relevant
    if cat in ["FOOTBALL", "SPORTS"]:
        maldives_sports = ["maldives","dhivehi","raajje","team maldives","national team"]
        if not any(kw in title_lower + summary_lower for kw in maldives_sports):
            score -= 30  # Heavy penalty for non-Maldives sports
    # World news — only if Maldives related
    if cat == "WORLD":
        if not any(kw in title_lower + summary_lower for kw in ["maldives","indian ocean","south asia","economy"]):
            score -= 20
    # Breaking boost
    if is_breaking(a["title"], a.get("summary",""), cat): score += 80
    # Source reliability — trusted sources rank higher (0-25 boost)
    rel = source_reliability(a.get("source",""))
    score += int((rel - 50) / 2)  # 100→+25, 55→+2, 60→+5
    # Trending boost — stories about hot topics rank higher (+30 if trending)
    try:
        trending, theme, count = is_trending_topic(a["title"], a.get("summary",""))
        if trending:
            score += 30
            a["_trend_theme"] = theme  # stash for display
    except Exception as e:
        log.debug(f"trend check in score: {e}")
    # Corroboration boost — if multiple outlets are covering the same event,
    # it's a bigger story. +12 per extra source (capped at +36). The bot SEES
    # the consensus and ranks accordingly — but never credits competitors publicly.
    cluster_size = a.get("_cluster_size", 1)
    if cluster_size >= 2:
        score += min((cluster_size - 1) * 12, 36)
    # ── Engagement nudge (Phase 2) — ±LEARN_CAP, INERT until /learning on ──
    try:
        eng_pts, eng_theme = topic_weight_for(a["title"], a.get("summary",""))
        if eng_pts:
            score += eng_pts
            a["_engagement_pts"] = eng_pts
            a["_engagement_theme"] = eng_theme
    except Exception as e:
        log.debug(f"engagement nudge: {e}")
    return score

def score_breakdown(a):
    """
    Return (total, [(label, points), ...]) explaining how an article scored.
    Mirrors score_article() so the team can see exactly why a story ranked.
    """
    items = []
    title_lower = a["title"].lower()
    summary_lower = a.get("summary", "").lower()
    text = title_lower + " " + summary_lower
    cat = a["cat"]

    cat_base = {"LOCAL": 80, "DISASTER": 70, "WEATHER": 30, "TOURISM": 20,
                "WORLD": 10, "FOOTBALL": 2, "SPORTS": 2}.get(cat, 0)
    if cat_base:
        items.append((f"Category ({cat})", cat_base))

    mv_kws = ["maldives","male","dhivehi","raajje","mvr","atoll","island","gaa",
              "parliament","majlis","president","minister","police","court","malé",
              "hulhumale","addu","fuvahmulah","laamu","economy","rufiyaa"]
    mv_hits = sum(1 for kw in mv_kws if kw in title_lower or kw in summary_lower)
    if mv_hits:
        items.append((f"Maldives keywords (x{mv_hits})", mv_hits * 20))

    if cat in ["FOOTBALL", "SPORTS"]:
        maldives_sports = ["maldives","dhivehi","raajje","team maldives","national team"]
        if not any(kw in text for kw in maldives_sports):
            items.append(("Non-Maldives sports penalty", -30))

    if cat == "WORLD":
        if not any(kw in text for kw in ["maldives","indian ocean","south asia","economy"]):
            items.append(("Non-relevant world penalty", -20))

    try:
        if is_breaking(a["title"], a.get("summary",""), cat):
            items.append(("Breaking news", 80))
    except Exception:
        pass

    rel = source_reliability(a.get("source", ""))
    rel_pts = int((rel - 50) / 2)
    if rel_pts:
        items.append((f"Source trust ({a.get('source','?')}, {rel}/100)", rel_pts))

    try:
        trending, theme, count = is_trending_topic(a["title"], a.get("summary",""))
        if trending:
            items.append((f"Trending topic ({theme}, {count} stories)", 30))
    except Exception:
        pass

    cluster_size = a.get("_cluster_size", 1)
    if cluster_size >= 2:
        corr = min((cluster_size - 1) * 12, 36)
        items.append((f"Corroborated ({cluster_size} sources)", corr))

    try:
        eng_pts, eng_theme = topic_weight_for(a["title"], a.get("summary",""))
        if eng_pts:
            items.append((f"Audience nudge ({eng_theme})", eng_pts))
    except Exception:
        pass

    total = sum(p for _, p in items)
    return total, items

def format_score_breakdown(a):
    """Pretty HTML block for Telegram showing the itemized score."""
    total, items = score_breakdown(a)
    lines = [f"🧮 <b>Why this scored {total}</b>", f"<i>{a['title'][:90]}</i>", ""]
    for label, pts in items:
        sign = "➕" if pts > 0 else "➖"
        lines.append(f"  {sign} {label}: <b>{pts:+d}</b>")
    if not items:
        lines.append("  <i>(no scoring signals matched)</i>")
    lines.append("")
    try:
        if is_breaking(a["title"], a.get("summary",""), a["cat"]):
            lines.append("📌 <b>Breaking</b> → posts immediately, no approval.")
        elif a.get("lang") == "dv":
            lines.append("📌 <b>Dhivehi</b> → always queued for Content Lab review.")
        else:
            lines.append("📌 <b>Regular English</b> → queued; auto-posts in 15 min if not reviewed.")
    except Exception:
        pass
    try:
        if learning_is_active():
            lines.append(f"🧠 Learning <b>ON</b> — audience data may shift score ±{LEARN_CAP}.")
        else:
            lines.append("🧠 Learning <b>off</b> — audience data not affecting scores yet.")
    except Exception:
        pass
    return "\n".join(lines)

def run_job(social_only=False, breaking_only=False):
    """
    Every 15-min scan:
      - Post 1 breaking article immediately (Telegram + social) if found
      - If 90-min regular window is OPEN: post 1 best regular article (Telegram + social)
      - If 90-min window is CLOSED: do NOT post anything — no social-only spillover
    Social media ONLY posts alongside Telegram. Never independently on a timer.
    """
    global daily_sports_count, daily_world_count, daily_tourism_count, _pending_article
    h = get_mvt_hour()
    log.info(f"🕐 MVT {h:02d}:xx | {'DAY' if is_day_mode() else 'NIGHT'}")
    seen = load_seen()
    articles = fetch_news()

    fresh = [a for a in articles if a["id"] not in seen]
    if not fresh:
        log.info("No fresh articles."); return

    # ── Pre-build story clusters: register every fresh article's source FIRST ──
    # This way, by the time we pick the best one, we already know how many
    # sources are reporting the same event (corroboration = stronger story).
    for a in fresh:
        size, srcs = register_in_cluster(a["title"], a.get("source",""))
        a["_cluster_size"] = size
        a["_cluster_sources"] = srcs

    fresh.sort(key=score_article, reverse=True)

    breaking_articles = [a for a in fresh if is_breaking(a["title"], a.get("summary",""), a["cat"])]
    regular_articles  = [] if breaking_only else [a for a in fresh if not is_breaking(a["title"], a.get("summary",""), a["cat"])]

    if breaking_only and not breaking_articles:
        log.info("🌙 Night mode: no breaking news found"); return

    log.info(f"🔴 {len(breaking_articles)} breaking | 🟡 {len(regular_articles)} regular")

    # ── 1. Breaking news: post immediately, no throttle, Telegram + social ──
    if breaking_articles:
        a = breaking_articles[0]
        log.info(f"🔴 BREAKING in run_job: {a['title'][:60]}")
        post_article(a, seen, social_only=False, allow_social=True)

    if breaking_only:
        return

    # ── 2. Regular news: ONLY post if 90-min Telegram window is open ──────
    if not can_post_regular():
        secs_left = int(5400 - (utcnow() - last_regular_post_time).total_seconds())
        log.info(f"⏳ Telegram throttled — {secs_left//60}m left. No social posting either. Waiting.")
        return  # Nothing. No social. No exceptions.

    # ── Window is open — post the BEST articles ──
    # Top story always goes. Additional stories (#2-5) post too IF they score
    # high enough AND are a different category (no two LOCAL in a row).
    # This means a big day (election + crime + economy) isn't bottlenecked to 1.
    MAX_PER_RUN = 5
    STRONG_SCORE = 120   # #2+ must clear this to ride along with the top story
    posted_count = 0
    posted_cats = set()

    for a in regular_articles:
        if posted_count >= MAX_PER_RUN:
            break
        cat = a["cat"]
        text_lower = (a["title"] + " " + a.get("summary","")).lower()
        a_score = score_article(a)

        # Sports: Maldives national team only, max 1/day
        if cat in ["SPORTS", "FOOTBALL"]:
            mv_sports = ["maldives","dhivehi","raajje","national team","team maldives"]
            if not any(kw in text_lower for kw in mv_sports):
                log.info(f"⛔ Non-Maldives sports — skipping: {a['title'][:50]}")
                continue
            if not can_post_cat_today(daily_sports_count, 1):
                log.info("⛔ Sports daily limit (1) reached")
                continue

        # World: Maldives-relevant only, max 2/day
        elif cat == "WORLD":
            mv_world = ["maldives","indian ocean","south asia","india","china","un ","dollar","oil","global economy"]
            if not any(kw in text_lower for kw in mv_world):
                log.info(f"⛔ Non-relevant world news — skipping: {a['title'][:50]}")
                continue
            if not can_post_cat_today(daily_world_count, 2):
                log.info("⛔ World daily limit (2) reached")
                continue

        # Tourism: max 2/day
        elif cat == "TOURISM":
            if not can_post_cat_today(daily_tourism_count, 2):
                log.info("⛔ Tourism daily limit (2) reached")
                continue

        # Weather: never post as regular article (weather card handles this)
        elif cat == "WEATHER":
            log.info("⛔ Weather — skipping (weather card handles this)")
            continue

        # ── Multi-post gating ──
        # First article (top story) always posts. Extras must be STRONG + new category.
        if posted_count >= 1:
            disp_cat = canonical_category(cat, a["title"], a.get("summary",""))
            if a_score < STRONG_SCORE:
                log.info(f"⏹️ Stopping extras — next best score {a_score} < {STRONG_SCORE}")
                break  # articles are sorted, so nothing below will qualify either
            if disp_cat in posted_cats:
                log.info(f"⏭️ Already posted a {disp_cat} this run — skipping for variety")
                continue

        # Post to Telegram. Social only fires if article passes social filter.
        allow_social = allowed_for_social(a)
        log.info(f"🟡 REGULAR [{cat}] score={a_score} social={'yes' if allow_social else 'no'}: {a['title'][:55]}")
        if post_article(a, seen, social_only=False, allow_social=allow_social):
            posted_count += 1
            posted_cats.add(canonical_category(cat, a["title"], a.get("summary","")))
            if cat in ["SPORTS","FOOTBALL"]: increment_cat_count(daily_sports_count)
            elif cat == "WORLD": increment_cat_count(daily_world_count)
            elif cat == "TOURISM": increment_cat_count(daily_tourism_count)

    log.info(f"✅ run_job done — {posted_count} article(s) queued/posted this run.")

# Sources scanned in the fast breaking-news check (5 min cycle)
BREAKING_SOURCES = [
    {"url": "https://sunonline.mv/feed",              "cat": "LOCAL", "lang": "dv"},
    {"url": "https://psmnews.mv/en/feed",             "cat": "LOCAL", "lang": "en"},
    # visitmaldives removed from breaking sources — tourism is never breaking news
    {"url": "https://maldivesvoice.com/feed",         "cat": "LOCAL", "lang": "en"},
    {"url": "https://english.sun.mv/feed",            "cat": "LOCAL", "lang": "en"},
    {"url": "https://edition.mv/feed",                "cat": "LOCAL", "lang": "en"},
    {"url": "https://mihaaru.com/rss",                "cat": "LOCAL", "lang": "dv"},
    {"url": "https://avas.mv/feed",                   "cat": "LOCAL", "lang": "dv"},
]

def fetch_breaking_sources():
    """Fetch only the priority breaking-news sources (used by 5-min fast check)."""
    articles, seen_titles = [], set()
    # MvCrisis always first
    for a in fetch_mvcrisis():
        if a["title"] not in seen_titles:
            seen_titles.add(a["title"])
            articles.append(a)
    for fc in BREAKING_SOURCES:
        try:
            feed = feedparser.parse(fc["url"])
            for entry in feed.entries[:5]:
                title   = entry.get("title", "")
                summary = entry.get("summary", title)
                if fc["lang"] == "dv":
                    title   = gemini_translate(title)
                    summary = gemini_translate(summary[:300])
                key = title.lower()[:50]
                if key in seen_titles or not is_fresh(entry): continue
                seen_titles.add(key)
                articles.append({
                    "id":      hashlib.md5(entry.get("link", title).encode()).hexdigest(),
                    "title":   title,
                    "summary": summary,
                    "link":    entry.get("link", ""),
                    "cat":     fc["cat"],
                    "lang":    fc["lang"],
                    "source":  entry.get("source", {}).get("title", fc["cat"]),
                })
        except Exception as e:
            log.error(f"Breaking source feed error ({fc['url']}): {e}")
    return articles

def breaking_news_check():
    """Fast check every 5 min — priority sources only, no Telegram throttle"""
    try:
        seen = load_seen()
        articles = fetch_breaking_sources()
        for a in articles:
            if a["id"] in seen: continue
            if a["cat"] not in ["LOCAL", "DISASTER"]: continue
            if not is_breaking(a["title"], a.get("summary",""), a["cat"]): continue
            # Score for Maldives relevance
            if score_article(a) < 60: continue
            log.info(f"🔴 BREAKING FAST: {a['title'][:60]}")
            post_article(a, seen, social_only=False, allow_social=True)
            break  # one at a time
    except Exception as e:
        log.error(f"Breaking check: {e}")

def scheduled_check():
    h=get_mvt_hour()
    if not is_day_mode():
        log.info(f"🌙 Night mode (MVT {h:02d}:xx) — breaking news only")
        run_job(breaking_only=True); return
    run_job()

# ── Morning Brief (7AM MVT) ───────────────────────────────────────────────────
def send_morning_brief():
    log.info("🌅 Morning brief...")
    try:
        headlines=get_local_headlines()
        if not headlines: return
        # Inject actual MVT date so Claude never hallucinates it
        from datetime import timezone, timedelta
        mvt = datetime.now(timezone.utc) + timedelta(hours=5)
        today_str = mvt.strftime("%A, %d %B %Y")
        prompt = f"""Create a warm "Good Morning Maldives 🌅" news brief for @samugacommunity.
Today's date is {today_str} (Maldives Time). Use this exact date in your greeting.
Headlines: {chr(10).join(headlines[:8])}
- Friendly greeting mentioning today's date exactly as given above
- Top 3-5 stories in 1 sentence each with emoji  
- Upbeat closing
- Max 180 words, English"""
        msg=ai.messages.create(model="claude-haiku-4-5-20251001",max_tokens=400,messages=[{"role":"user","content":prompt}])
        brief=msg.content[0].text.strip()
        caption=f"🌅 <b>Good Morning Maldives!</b>\n\n{brief}\n\n📡 <b>Samuga Media</b> | @samugacommunity"
        send_text(TELEGRAM_CHANNEL_ID, caption)
        log.info("✅ Morning brief sent!")
    except Exception as e: log.error(f"Morning brief: {e}")

# ── Tip/Story CTA ────────────────────────────────────────────────────────────
def send_tip_cta():
    """Send story tip CTA to Telegram channel (8:30AM and 8:30PM MVT)"""
    msg = (
        "🚨 <b>Have a story, tip, or news update?</b>\n\n"
        "Share it with Samuga Media privately and anonymously.\n"
        "🔒 Your identity stays confidential. 📩 Message us: @Samuga_Media\n\n"
        "Your voice matters. The people's media starts with you. 💙"
    )
    send_text(TELEGRAM_CHANNEL_ID, msg)
    log.info("📣 Tip CTA sent")

# ── Weather Card ──────────────────────────────────────────────────────────────
def _tomorrow_code_to_wmo(code):
    """
    Map Tomorrow.io weatherCode to the nearest WMO code so weather_code_to_info()
    works unchanged. Tomorrow.io codes: 1000=clear, 1100=mostly clear,
    1101=partly cloudy, 1102=mostly cloudy, 1001=cloudy, 2000=fog,
    4000=drizzle, 4001=rain, 4200=light rain, 4201=heavy rain,
    8000=thunderstorm, 5000=snow (won't happen in Maldives but handled).
    """
    mapping = {
        1000: 0,    # clear sky
        1100: 1,    # mostly clear
        1101: 2,    # partly cloudy
        1102: 3,    # mostly cloudy
        1001: 3,    # cloudy/overcast
        2000: 45,   # fog
        2100: 48,   # light fog
        4000: 51,   # drizzle
        4001: 61,   # rain
        4200: 61,   # light rain
        4201: 65,   # heavy rain
        6000: 51,   # freezing drizzle
        6001: 61,   # freezing rain
        6200: 51,   # light freezing rain
        6201: 65,   # heavy freezing rain
        7000: 71,   # ice pellets
        7101: 77,   # heavy ice pellets
        7102: 71,   # light ice pellets
        5000: 71,   # snow
        5001: 73,   # flurries
        5100: 71,   # light snow
        5101: 75,   # heavy snow
        8000: 95,   # thunderstorm
    }
    return mapping.get(code, 3)

# ── Island Watch — 5 Maldivian population centres ────────────────────────────
ISLAND_LOCATIONS = [
    {"name": "Malé",           "lat": 4.1755,   "lon": 73.5093},
    {"name": "Addu",           "lat": 0.6167,   "lon": 73.1000},
    {"name": "Kulhudhuffushi", "lat": 6.6226,   "lon": 73.0700},
    {"name": "Fuvahmulah",     "lat": -0.2985,  "lon": 73.4236},
    {"name": "Dhidhdhoo",      "lat": 6.8833,   "lon": 73.1167},
]

def generate_outlook(hourly_slots, mvt_now):
    """
    Convert next 12 hours of Tomorrow.io hourly slots into a one-line outlook.
    e.g. "Heavy showers after 4 PM", "Sunny all day", "Thunderstorms tonight"
    Uses Tomorrow.io native weatherCode (not WMO).
    """
    from datetime import datetime, timezone, timedelta as _td

    SEVERITY = {
        8000:5, 8001:5, 8002:5,           # thunderstorm
        4201:4, 6201:4,                    # heavy rain
        4001:3, 6001:3, 4200:3,            # rain
        4000:2, 6000:2, 5000:2, 7000:2,   # drizzle/snow/ice
        2000:1, 2100:1,                    # fog
        1001:0, 1102:0,                    # cloudy
        1101:0, 1100:0,                    # partly cloudy
        1000:0,                            # clear
    }

    def sev(code): return SEVERITY.get(code, 0)

    def label(code):
        if code in [8000,8001,8002]: return "thunderstorms"
        if code in [4201,6201]:      return "heavy showers"
        if code in [4001,6001,4200]: return "rain showers"
        if code in [4000,6000]:      return "light rain"
        if code in [2000,2100]:      return "foggy conditions"
        if code in [1001,1102]:      return "cloudy skies"
        if code in [1100,1101]:      return "partly cloudy"
        return "sunny"

    # Parse all slots into (mvt_hour, raw_code, precip)
    entries = []
    for slot in hourly_slots[:12]:
        try:
            t_str = slot.get("time","")
            dt_utc = datetime.fromisoformat(t_str.replace("Z","+00:00"))
            dt_mvt = dt_utc + _td(hours=5)
            v = slot.get("values",{})
            raw_code = v.get("weatherCode", 1000)
            precip   = v.get("precipitationProbability", 0)
            entries.append((dt_mvt.hour, raw_code, precip))
        except:
            continue

    if not entries:
        return "Weather data unavailable"

    now_h = mvt_now.hour

    # Find the single worst event across all upcoming hours
    worst = max(entries, key=lambda e: (sev(e[1]), e[2]))
    worst_h, worst_code, worst_precip = worst

    # If nothing severe at all — classify overall
    if sev(worst_code) == 0:
        all_codes = [e[1] for e in entries]
        if all(c == 1000 for c in all_codes):
            return "Sunny all day"
        if all(c in [1000,1100] for c in all_codes):
            return "Sunny with some clouds"
        if all(c in [1000,1100,1101,1001,1102] for c in all_codes):
            return "Mostly cloudy"
        return "Partly cloudy"

    # There IS a significant event — say when it happens
    desc = label(worst_code)

    if worst_h < 6:    time_hint = "overnight"
    elif worst_h < 9:  time_hint = "early morning"
    elif worst_h < 12: time_hint = "this morning"
    elif worst_h == 12: time_hint = "at noon"
    elif worst_h < 15: time_hint = "this afternoon"
    elif worst_h < 18: time_hint = f"after {worst_h - 12} PM"
    elif worst_h < 21: time_hint = "this evening"
    else:              time_hint = "tonight"

    # If already happening now, say "right now"
    if abs(worst_h - now_h) <= 1:
        return f"{desc.capitalize()} right now"

    return f"{desc.capitalize()} {time_hint}"

def get_island_forecasts():
    """
    Fetch 12-hour hourly forecast for all 5 islands via Tomorrow.io.
    Returns list of {name, temp, code, outlook} dicts, or [] on failure.
    """
    TOMORROW_API_KEY = os.environ.get("TOMORROW_API_KEY", "")
    if not TOMORROW_API_KEY:
        return []

    from datetime import datetime, timezone, timedelta as _td
    mvt_now = datetime.now(timezone.utc) + _td(hours=5)
    results = []

    for island in ISLAND_LOCATIONS:
        try:
            params = (f"?location={island['lat']},{island['lon']}"
                      f"&apikey={TOMORROW_API_KEY}&units=metric")
            base = "https://api.tomorrow.io/v4/weather"

            rt = requests.get(f"{base}/realtime{params}", timeout=12)
            fc = requests.get(f"{base}/forecast{params}", timeout=12)

            if rt.status_code != 200 or fc.status_code != 200:
                log.warning(f"Island forecast {island['name']}: HTTP {rt.status_code}/{fc.status_code}")
                results.append({"name": island["name"], "temp": 29,
                                 "code": 1000, "outlook": "Data unavailable"})
                continue

            rv = rt.json()["data"]["values"]
            fd = fc.json()
            temp = round(rv.get("temperature", 29))
            code = _tomorrow_code_to_wmo(rv.get("weatherCode", 1000))
            hourly_slots = fd.get("timelines", {}).get("hourly", [])[:12]
            outlook = generate_outlook(hourly_slots, mvt_now)

            results.append({"name": island["name"], "temp": temp,
                             "code": code, "outlook": outlook})
            log.info(f"🏝️ {island['name']}: {temp}°C — {outlook}")

        except Exception as e:
            log.error(f"Island forecast {island['name']}: {e}")
            results.append({"name": island["name"], "temp": 29,
                             "code": 1000, "outlook": "Data unavailable"})

    return results


    """
    Fetch real-time weather for Malé, Maldives.
    Primary: Tomorrow.io (richer data — UV, gusts, visibility, dew point).
    Fallback: Open-Meteo (free, no key, always available).
    Returns a normalised dict the card renderer understands.
    """
    TOMORROW_API_KEY = os.environ.get("TOMORROW_API_KEY", "")

    # ── Primary: Tomorrow.io ─────────────────────────────────────────────────
    if TOMORROW_API_KEY:
        try:
            # Two calls: realtime (current) + forecast (hourly + daily)
            base = "https://api.tomorrow.io/v4/weather"
            params = f"?location=4.1755,73.5093&apikey={TOMORROW_API_KEY}&units=metric"

            rt = requests.get(f"{base}/realtime{params}", timeout=15)
            fc = requests.get(f"{base}/forecast{params}", timeout=15)

            if rt.status_code == 200 and fc.status_code == 200:
                rv = rt.json()["data"]["values"]
                fd = fc.json()

                # Current conditions
                wmo = _tomorrow_code_to_wmo(rv.get("weatherCode", 1000))
                current = {
                    "temperature_2m":        rv.get("temperature", 29),
                    "apparent_temperature":   rv.get("temperatureApparent", 29),
                    "relativehumidity_2m":    rv.get("humidity", 80),
                    "windspeed_10m":          rv.get("windSpeed", 10),
                    "windgust_10m":           rv.get("windGust", 0),
                    "weathercode":            wmo,
                    "uv_index":               rv.get("uvIndex", 0),
                    "visibility":             rv.get("visibility", 10),
                    "dewpoint_2m":            rv.get("dewPoint", 25),
                    "pressure_msl":           rv.get("pressureSurfaceLevel", 1010),
                    "precipitation_prob":     rv.get("precipitationProbability", 0),
                    "_source":                "Tomorrow.io",
                }

                # Hourly forecast (next 8 hours)
                hourly_t, hourly_wmo, hourly_precip = [], [], []
                hourly_times = []
                for slot in fd.get("timelines", {}).get("hourly", [])[:12]:
                    v = slot.get("values", {})
                    hourly_times.append(slot.get("time", ""))
                    hourly_t.append(v.get("temperature", 29))
                    hourly_wmo.append(_tomorrow_code_to_wmo(v.get("weatherCode", 1000)))
                    hourly_precip.append(v.get("precipitationProbability", 0))

                hourly = {
                    "time":                     hourly_times,
                    "temperature_2m":           hourly_t,
                    "weathercode":              hourly_wmo,
                    "precipitation_probability":hourly_precip,
                }

                # Daily H/L + sunrise/sunset
                daily_max, daily_min, daily_wmo = [], [], []
                sunrise_str, sunset_str = "06:00", "18:00"
                for i, day in enumerate(fd.get("timelines", {}).get("daily", [])[:1]):
                    v = day.get("values", {})
                    daily_max.append(v.get("temperatureMax", 32))
                    daily_min.append(v.get("temperatureMin", 26))
                    daily_wmo.append(_tomorrow_code_to_wmo(v.get("weatherCodeMax", 1000)))
                    sr = v.get("sunriseTime", "")
                    ss = v.get("sunsetTime", "")
                    if sr: sunrise_str = sr[11:16]
                    if ss: sunset_str  = ss[11:16]

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

    # ── Fallback: Open-Meteo (no key needed, always free) ────────────────────
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
            # Normalise Open-Meteo current to match Tomorrow.io shape
            c = data.get("current", {})
            c["uv_index"]        = 0
            c["visibility"]      = 10
            c["windgust_10m"]    = 0
            c["dewpoint_2m"]     = 25
            c["pressure_msl"]    = 1010
            c["precipitation_prob"] = 0
            c["_source"]         = "Open-Meteo"
            log.info(f"🌤️ Open-Meteo fallback: {c.get('temperature_2m',29):.1f}°C")
            return data
    except Exception as e:
        log.error(f"Open-Meteo fallback: {e}")
    return None

def weather_code_to_info(code):
    """Convert WMO weather code to emoji + description"""
    if code == 0:   return "☀️", "Clear Sky"
    if code in [1,2]: return "🌤️", "Partly Cloudy"
    if code == 3:   return "☁️", "Overcast"
    if code in [45,48]: return "🌫️", "Foggy"
    if code in [51,53,55]: return "🌦️", "Drizzle"
    if code in [61,63,65]: return "🌧️", "Rain"
    if code in [71,73,75]: return "🌨️", "Snow"
    if code in [80,81,82]: return "🌧️", "Rain Showers"
    if code in [95,96,99]: return "⛈️", "Thunderstorm"
    return "🌡️", "Unknown"

def draw_weather_icon(draw, code, x, y, size=40):
    """Draw vector weather icon instead of emoji (fixes font rendering issues)"""
    cx, cy = x, y
    s = size
    if code == 0:  # Sun
        draw.ellipse([cx-s//3, cy-s//3, cx+s//3, cy+s//3], fill=(255,220,50,255))
        for angle in range(0, 360, 45):
            import math
            rad = math.radians(angle)
            x1 = cx + int((s//3+4)*math.cos(rad))
            y1 = cy + int((s//3+4)*math.sin(rad))
            x2 = cx + int((s//2+2)*math.cos(rad))
            y2 = cy + int((s//2+2)*math.sin(rad))
            draw.line([x1,y1,x2,y2], fill=(255,220,50,220), width=2)
    elif code in [1,2]:  # Part cloud
        draw.ellipse([cx-s//3, cy-s//3, cx+s//3, cy+s//3], fill=(255,220,50,200))
        draw.ellipse([cx-s//2, cy, cx+s//2, cy+s//2], fill=(220,230,255,240))
        draw.ellipse([cx-s//4, cy-s//6, cx+s//4+6, cy+s//3+4], fill=(220,230,255,240))
    elif code == 3:  # Cloud
        draw.ellipse([cx-s//2, cy-s//6, cx+s//2, cy+s//2], fill=(200,210,240,240))
        draw.ellipse([cx-s//4, cy-s//3, cx+s//4+4, cy+s//4], fill=(200,210,240,240))
    elif code in [51,53,55,61,63,65,80,81,82]:  # Rain
        draw.ellipse([cx-s//2, cy-s//4, cx+s//2, cy+s//3], fill=(160,180,220,240))
        draw.ellipse([cx-s//4, cy-s//2, cx+s//4+4, cy+s//6], fill=(160,180,220,240))
        for rx in [-s//3, 0, s//3]:
            draw.line([cx+rx, cy+s//3, cx+rx-4, cy+s//2+4], fill=(100,160,255,220), width=2)
    elif code in [95,96,99]:  # Thunder
        draw.ellipse([cx-s//2, cy-s//4, cx+s//2, cy+s//3], fill=(80,80,100,240))
        draw.ellipse([cx-s//4, cy-s//2, cx+s//4+4, cy+s//6], fill=(80,80,100,240))
        pts = [cx+4,cy+s//4, cx-4,cy+s//4, cx,cy+s//2, cx-8,cy+s//2, cx+12,cy+s*3//4]
        draw.line(pts, fill=(255,220,0,255), width=3)
    else:  # Default cloud
        draw.ellipse([cx-s//2, cy-s//6, cx+s//2, cy+s//2], fill=(180,190,220,240))

def generate_weather_card(weather_data, alert_mode=False, alert_text="", island_data=None):
    """Samuga branded weather card with logo, UV, gusts, dew point, pressure, data source.
    island_data = list of {name, temp, code, outlook} from get_island_forecasts().
    Card is 1080×1350 when island_data present, 1080×1080 otherwise.
    """
    from PIL import Image, ImageDraw, ImageFont
    import math

    W, H = 1080, (1350 if island_data else 1080)
    img = Image.new("RGB", (W, H), (0,0,0))
    draw = ImageDraw.Draw(img, "RGBA")

    current = weather_data.get("current", {})
    hourly  = weather_data.get("hourly", {})
    daily   = weather_data.get("daily", {})
    source  = weather_data.get("_source", "")

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

    temp_max = round(daily.get("temperature_2m_max", [temp])[0])
    temp_min = round(daily.get("temperature_2m_min", [temp])[0])

    sunrise_raw = daily.get("sunrise", [""])[0]
    sunset_raw  = daily.get("sunset",  [""])[0]
    sunrise_str = sunrise_raw.split("T")[1][:5] if "T" in sunrise_raw else "06:00"
    sunset_str  = sunset_raw.split("T")[1][:5]  if "T" in sunset_raw  else "18:19"

    hours  = hourly.get("time", [])
    temps  = hourly.get("temperature_2m", [])
    codes  = hourly.get("weathercode", [])
    precip = hourly.get("precipitation_probability", [])

    # Background gradient — weather-aware
    if alert_mode:
        for y in range(H):
            t=y/H; draw.line([(0,y),(W,y)], fill=(int(40+t*20),int(5+t*8),int(5+t*10),255))
    elif code in [95,96,99]:
        for y in range(H):
            t=y/H; draw.line([(0,y),(W,y)], fill=(int(15+t*25),int(8+t*15),int(35+t*50),255))
    elif code in [61,63,65,80,81,82,51,53,55]:
        for y in range(H):
            t=y/H; draw.line([(0,y),(W,y)], fill=(int(20+t*20),int(40+t*30),int(80+t*50),255))
    elif code == 0:
        for y in range(H):
            t=y/H; draw.line([(0,y),(W,y)], fill=(int(10+t*8),int(60+t*35),int(155+t*35),255))
    else:
        for y in range(H):
            t=y/H; draw.line([(0,y),(W,y)], fill=(int(40+t*20),int(60+t*30),int(100+t*50),255))

    # Dark vignette at top so logo is readable
    for y in range(110):
        draw.line([(0,y),(W,y)], fill=(0,0,0,int(130*(1-y/110))))

    def F(sz, bold=False):
        try:
            return ImageFont.truetype(
                f"/usr/share/fonts/truetype/dejavu/DejaVuSans{chr(45)+'Bold' if bold else ''}.ttf", sz)
        except:
            return ImageFont.load_default()

    font_huge  = F(160, True)
    font_large = F(48,  True)
    font_med   = F(34)
    font_small = F(26,  True)
    font_tiny  = F(21)
    font_xs    = F(17)
    font_xxs   = F(14)

    from datetime import timezone
    mvt = datetime.now(timezone.utc) + timedelta(hours=5)

    # Samuga logo — top left
    try:
        logo = Image.open("logo.png").convert("RGBA")
        lh = 62; lw2 = int(logo.width * lh / logo.height)
        logo = logo.resize((lw2, lh), Image.LANCZOS)
        ir = img.convert("RGBA"); ir.paste(logo, (36, 24), logo)
        img = ir.convert("RGB"); draw = ImageDraw.Draw(img, "RGBA")
    except Exception as e:
        log.debug(f"weather logo: {e}")

    # Alert accent bars
    if alert_mode:
        draw.rectangle([(0,0),(W,6)], fill=(220,50,50,255))
        draw.rectangle([(0,0),(5,H)], fill=(220,50,50,255))
        btext = "WEATHER ALERT"
        btw = draw.textlength(btext, font=font_small)
        draw.text(((W-btw)//2, 36), btext, font=font_small, fill=(255,80,80,255))

    # Channel tag top right
    tag = "t.me/samugacommunity"
    ttw = draw.textlength(tag, font=font_xxs)
    draw.text((W-ttw-36, 40), tag, font=font_xxs, fill=(255,255,255,140))

    # Location
    loc = "Male, Maldives"
    top_y = 100 if not alert_mode else 68
    lcw = draw.textlength(loc, font=font_large)
    draw.text(((W-lcw)//2, top_y), loc, font=font_large, fill=(255,255,255,230))

    # Big icon
    draw_weather_icon(draw, code, W//2, top_y+80, size=90)

    # Temperature
    temp_str = f"{temp}\u00b0"
    ttw2 = draw.textlength(temp_str, font=font_huge)
    draw.text(((W-ttw2)//2, top_y+180), temp_str, font=font_huge, fill=(255,255,255,255))

    # Condition
    ccw = draw.textlength(condition, font=font_large)
    draw.text(((W-ccw)//2, top_y+356), condition, font=font_large, fill=(255,255,255,200))

    # Alert text
    if alert_mode and alert_text:
        atw = draw.textlength(alert_text, font=font_small)
        if atw > W-80:
            words2=alert_text.split(); alines=[]; cur2=""
            for aw in words2:
                if draw.textlength(cur2+" "+aw,font=font_small)<W-80: cur2=(cur2+" "+aw).strip()
                else: alines.append(cur2); cur2=aw
            if cur2: alines.append(cur2)
            for ai,al in enumerate(alines[:2]):
                alw=draw.textlength(al,font=font_small)
                draw.text(((W-alw)//2, top_y+414+ai*34), al, font=font_small, fill=(255,120,120,255))
        else:
            draw.text(((W-atw)//2, top_y+414), alert_text, font=font_small, fill=(255,120,120,255))

    # H/L
    hl_y = top_y + 414 if not alert_mode else top_y + 480
    hl_str = f"H:{temp_max}\u00b0  L:{temp_min}\u00b0"
    hlw = draw.textlength(hl_str, font=font_med)
    draw.text(((W-hlw)//2, hl_y), hl_str, font=font_med, fill=(255,255,255,190))

    dy = hl_y + 46
    # Row 1: feels, humidity, wind
    row1 = f"Feels {feels}\u00b0   Humidity {humidity}%   Wind {wind} km/h"
    if gusts and gusts > wind: row1 += f" (gusts {gusts})"
    r1w = draw.textlength(row1, font=font_tiny)
    draw.text(((W-r1w)//2, dy), row1, font=font_tiny, fill=(255,255,255,175))
    dy += 30

    # Row 2: UV, visibility, dew point, pressure
    r2parts = []
    if uv: r2parts.append(f"UV {uv}")
    if vis: r2parts.append(f"Vis {vis} km")
    if dew: r2parts.append(f"Dew {dew}\u00b0")
    if pressure: r2parts.append(f"{pressure} hPa")
    if r2parts:
        row2 = "   ".join(r2parts)
        r2w = draw.textlength(row2, font=font_tiny)
        draw.text(((W-r2w)//2, dy), row2, font=font_tiny, fill=(255,255,255,150))
        dy += 30

    # Row 3: sunrise/sunset + precip prob
    sun_str = f"Sunrise {sunrise_str}   Sunset {sunset_str}"
    if precip_p: sun_str += f"   Rain {precip_p}%"
    ssw = draw.textlength(sun_str, font=font_tiny)
    draw.text(((W-ssw)//2, dy), sun_str, font=font_tiny, fill=(255,220,100,200))
    dy += 30

    # Divider
    div_y = dy + 8
    draw.line([(60,div_y),(W-60,div_y)], fill=(255,255,255,50), width=1)

    # Hourly strip
    now_hour = mvt.hour
    slot_w2 = (W-120)//8
    displayed = 0
    y_base = div_y + 20

    for i2,(h_str,ht,hc,hp) in enumerate(zip(hours,temps,codes,precip)):
        try: h_hour = int(h_str.split("T")[1][:2])
        except: continue
        if h_hour < now_hour: continue
        if displayed >= 8: break
        hx = 60 + displayed*slot_w2 + slot_w2//2
        h_label = "Now" if displayed==0 else f"{h_hour:02d}:00"
        hlw2 = draw.textlength(h_label, font=font_xs)
        draw.text((hx-hlw2//2, y_base), h_label, font=font_xs, fill=(255,255,255,170))
        draw_weather_icon(draw, hc, hx, y_base+40, size=26)
        ht_str = f"{round(ht)}\u00b0"
        htw = draw.textlength(ht_str, font=font_small)
        draw.text((hx-htw//2, y_base+68), ht_str, font=font_small, fill=(255,255,255,255))
        if hp and hp>0:
            hp_str=f"{hp}%"; hpw=draw.textlength(hp_str,font=font_xs)
            draw.text((hx-hpw//2, y_base+100), hp_str, font=font_xs, fill=(120,200,255,210))
        displayed += 1

    # ── ISLAND WATCH STRIP ────────────────────────────────────────────────────
    if island_data:
        iw_y = y_base + 128
        draw.line([(40,iw_y),(W-40,iw_y)], fill=(255,255,255,40), width=1)
        iw_y += 14
        hdr = "WEATHER WATCH — MALDIVES"
        hdw = draw.textlength(hdr, font=font_small)
        draw.text(((W-hdw)//2, iw_y), hdr, font=font_small, fill=(255,220,100,230))
        iw_y += 38

        for isl in island_data:
            iname = isl["name"]
            iout  = isl["outlook"]
            itemp = isl.get("temp", 29)
            icode = isl.get("code", 1000)

            # Island name — left
            draw.text((48, iw_y), iname, font=F(18, True), fill=(255,255,255,230))
            # Temperature — right
            ts2 = f"{itemp}\u00b0C"
            tw3 = int(draw.textlength(ts2, font=F(18, True)))
            draw.text((W-48-tw3, iw_y), ts2, font=F(18, True), fill=(160,215,255,210))
            # Outlook — below name, dimmer
            draw.text((48, iw_y+24), iout, font=font_xs, fill=(200,225,255,170))
            iw_y += 58

        draw.line([(40,iw_y),(W-40,iw_y)], fill=(255,255,255,30), width=1)

    # Bottom bar
    bar_y = H - 78
    draw.line([(60,bar_y),(W-60,bar_y)], fill=(255,255,255,40), width=1)
    time_str = mvt.strftime("%A, %d %B %Y  \u2022  %H:%M MVT")
    tfw = draw.textlength(time_str, font=font_xs)
    draw.text(((W-tfw)//2, bar_y+10), time_str, font=font_xs, fill=(255,255,255,120))
    brand = "Samuga Media  |  @samugacommunity"
    bw3 = draw.textlength(brand, font=font_small)
    draw.text(((W-bw3)//2, bar_y+34), brand, font=font_small, fill=(255,255,255,200))
    if source:
        src_txt = f"Data: {source}"
        stw2 = draw.textlength(src_txt, font=font_xxs)
        draw.text((W-stw2-36, bar_y+58), src_txt, font=font_xxs, fill=(255,255,255,80))

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf

# ── Weather Alert System ──────────────────────────────────────────────────────
# Checks every weather fetch for dangerous conditions.
# Max 2 alerts per day (MVT). Never spams.
weather_alerts_today = {"date": None, "count": 0}

ALERT_THRESHOLDS = {
    "heavy_rain":    {"precip_prob": 85, "rain_codes": [65,82,95,96,99]},
    "strong_wind":   {"wind_kmh": 40, "gust_kmh": 55},
    "thunderstorm":  {"codes": [95,96,99]},
}

def can_send_weather_alert():
    global weather_alerts_today
    today = (utcnow() + timedelta(hours=5)).strftime("%Y-%m-%d")
    if weather_alerts_today["date"] != today:
        weather_alerts_today = {"date": today, "count": 0}
    return weather_alerts_today["count"] < 2

def increment_alert_count():
    global weather_alerts_today
    today = (utcnow() + timedelta(hours=5)).strftime("%Y-%m-%d")
    if weather_alerts_today["date"] != today:
        weather_alerts_today = {"date": today, "count": 0}
    weather_alerts_today["count"] += 1
    log.info(f"⚠️ Weather alerts today: {weather_alerts_today['count']}/2")

def detect_weather_alert(weather_data):
    """
    Check if current conditions warrant an alert.
    Returns (should_alert, alert_type, alert_text) or (False, None, None).
    """
    current = weather_data.get("current", {})
    code    = current.get("weathercode", 0)
    wind    = current.get("windspeed_10m", 0)
    gusts   = current.get("windgust_10m", 0)
    precip  = current.get("precipitation_prob", 0)

    # Thunderstorm — highest priority
    if code in ALERT_THRESHOLDS["thunderstorm"]["codes"]:
        return (True, "thunderstorm",
                f"Thunderstorm conditions over Malé — stay safe and stay indoors.")

    # Heavy rain
    if (code in ALERT_THRESHOLDS["heavy_rain"]["rain_codes"] and
            precip >= ALERT_THRESHOLDS["heavy_rain"]["precip_prob"]):
        return (True, "heavy_rain",
                f"Heavy rain expected — {int(precip)}% precipitation probability. "
                "Expect poor visibility and flooding risk.")

    # Rough sea / strong wind
    if (wind >= ALERT_THRESHOLDS["strong_wind"]["wind_kmh"] or
            gusts >= ALERT_THRESHOLDS["strong_wind"]["gust_kmh"]):
        w_str = f"Wind {int(wind)} km/h" + (f", gusts {int(gusts)} km/h" if gusts > wind else "")
        return (True, "strong_wind",
                f"Rough sea conditions — {w_str}. "
                "Caution advised for sea travel and water activities.")

    return (False, None, None)

def send_weather_alert(weather_data, alert_type, alert_text):
    """Send a weather alert card to community + core team. Max 2/day."""
    if not can_send_weather_alert():
        log.info("⚠️ Weather alert limit (2/day) reached — skipping")
        return
    try:
        card = generate_weather_card(weather_data, alert_mode=True, alert_text=alert_text)
        current = weather_data.get("current", {})
        code = current.get("weathercode", 0)
        emoji, condition = weather_code_to_info(code)

        alert_icons = {
            "thunderstorm": "⛈️",
            "heavy_rain":   "🌧️",
            "strong_wind":  "💨",
        }
        icon = alert_icons.get(alert_type, "⚠️")

        caption = (
            f"⚠️ <b>WEATHER ALERT — Malé, Maldives</b>\n\n"
            f"{icon} {alert_text}\n\n"
            f"🌡️ Current: {round(current.get('temperature_2m',29))}°C — {condition}\n\n"
            f"📡 <b>Samuga Media</b> | @samugacommunity"
        )

        # Community channel
        card.seek(0)
        send_photo(TELEGRAM_CHANNEL_ID, card, caption)

        # Core team notification
        card.seek(0)
        team_note = (
            f"⚠️ <b>Weather alert sent to community</b>\n"
            f"Type: {alert_type}\n"
            f"{alert_text}\n"
            f"Alerts today: {weather_alerts_today['count']+1}/2"
        )
        send_text(CORE_TEAM_CHAT_ID, team_note)

        increment_alert_count()
        log.info(f"⚠️ Weather alert sent: {alert_type}")
    except Exception as e:
        log.error(f"Weather alert send: {e}")

def send_weather_update(time_of_day="morning"):
    """Send weather card to Telegram + island watch + check for alerts"""
    log.info(f"🌤️ Weather update ({time_of_day})...")
    try:
        data = get_weather_data()
        if not data:
            log.error("Weather: no data"); return

        # Fetch island forecasts (5 islands — ~10 API calls, well within 500/day limit)
        log.info("🏝️ Fetching island forecasts...")
        islands = get_island_forecasts()
        if islands:
            log.info(f"🏝️ Got {len(islands)} island forecasts")
        else:
            log.warning("🏝️ No island forecast data — card will show Malé only")

        card = generate_weather_card(data, island_data=islands if islands else None)
        current = data.get("current", {})
        temp     = round(current.get("temperature_2m", 29))
        code     = current.get("weathercode", 0)
        uv       = current.get("uv_index", 0)
        wind     = round(current.get("windspeed_10m", 10))
        precip_p = current.get("precipitation_prob", 0)
        source   = data.get("_source", "")
        emoji, condition = weather_code_to_info(code)
        greeting = "\U0001f305 Good Morning Maldives!" if time_of_day == "morning" else "\U0001f319 Good Evening Maldives!"
        src_tag = f"\n<i>Data: {source}</i>" if source else ""

        # Build island summary for caption
        island_lines = ""
        if islands:
            island_lines = "\n\n🏝 <b>Weather Watch</b>\n"
            for isl in islands:
                island_lines += f"📍 <b>{isl['name']}</b> — {isl['outlook']}\n"

        caption = (
            f"{greeting}\n\n"
            f"{emoji} <b>Current Weather \u2014 Mal\u00e9</b>\n"
            f"\U0001f321\ufe0f <b>{temp}\u00b0C</b> \u2014 {condition}\n"
            f"\U0001f4a8 Wind {wind} km/h  \u00b7  \u2614 Rain {precip_p}%  \u00b7  \u2600\ufe0f UV {uv}"
            f"{island_lines}\n\n"
            f"\U0001f4e1 <b>Samuga Media</b> | @samugacommunity"
            f"{src_tag}"
        )
        send_photo(TELEGRAM_CHANNEL_ID, card, caption)
        log.info(f"\u2705 Weather card sent ({time_of_day}) via {source}")

        # Alert check after every regular card
        should_alert, alert_type, alert_text = detect_weather_alert(data)
        if should_alert:
            log.info(f"\u26a0\ufe0f Alert detected: {alert_type}")
            send_weather_alert(data, alert_type, alert_text)
        else:
            log.info("\u2705 No alert conditions detected")

    except Exception as e:
        log.error(f"Weather update: {e}")

# ── Night Summary (12AM MVT) ──────────────────────────────────────────────────
def send_night_summary():
    log.info("🌙 Night summary...")
    try:
        if not recent_posts: log.info("No posts for summary"); return
        posts_text="\n".join([f"• [{p['cat']}] {p['title']}" for p in recent_posts[-15:]])
        prompt=f"""Create a "Tonight's Top Stories 🌙" summary for @samugacommunity.
Today's posts: {posts_text}
- Warm good evening greeting
- Top 5 stories in 1 sentence each with emoji
- Good night closing
- Max 180 words, English"""
        msg=ai.messages.create(model="claude-haiku-4-5-20251001",max_tokens=400,messages=[{"role":"user","content":prompt}])
        summary=msg.content[0].text.strip()
        caption=f"🌙 <b>Tonight's Top Stories</b>\n\n{summary}\n\n📡 <b>Samuga Media</b> | @samugacommunity"
        send_text(TELEGRAM_CHANNEL_ID, caption)
        log.info("✅ Night summary sent!")
    except Exception as e: log.error(f"Night summary: {e}")

# ── AI Nightly Journalist (v6) — the bot that THINKS ──────────────────────────
# At ~10:30PM, Claude reviews the entire day's article archive and writes a real
# editorial brief for the team: what mattered today, what it means for Maldivians,
# and a ready-to-shoot TikTok angle for Thooma. Lands in Content Lab, not public.
def send_ai_journalist_brief():
    log.info("🧠 Samuga AI brief generating...")
    try:
        # Pull today's articles from the archive (richer than recent_posts)
        articles_text = ""
        trends_text = ""
        if DB_ENABLED:
            rows = db_execute(
                """SELECT title, category, source, status FROM articles
                   WHERE found_at > NOW() - INTERVAL '18 hours'
                   ORDER BY score DESC LIMIT 40""", fetch="all")
            if rows:
                articles_text = "\n".join(
                    [f"• [{cat}] {title} ({src}) — {status}" for title, cat, src, status in rows])
            # Today's trends
            trends = detect_trends(hours=24, min_mentions=3)
            if trends:
                trends_text = "\n".join([f"• {theme}: {count} stories" for theme, count, _ in trends[:6]])
        # Fallback to recent_posts if no DB
        if not articles_text and recent_posts:
            articles_text = "\n".join([f"• [{p['cat']}] {p['title']}" for p in recent_posts[-20:]])
        if not articles_text:
            log.info("Samuga AI: no articles to review"); return

        from datetime import timezone as _tzx
        mvt = datetime.now(_tzx.utc) + timedelta(hours=5)
        today_str = mvt.strftime("%A, %d %B %Y")

        prompt = f"""You are Samuga AI, the senior editor at Samuga Media, a sharp Maldivian news outlet. It's the end of the day ({today_str}). Review today's news and write a private editorial brief for the team (Manchii, Uly, Thooma). Be insightful and specific to the Maldives — not generic.

TODAY'S ARTICLES:
{articles_text}

TRENDING THEMES TODAY:
{trends_text or "(not enough data yet)"}

Write a brief with EXACTLY these sections (use the emoji headers):

📰 TOP 3 STORIES TODAY
(The 3 most important stories, 1 line each, ranked by what matters to ordinary Maldivians — not by what's flashy.)

🇲🇻 WHAT THIS MEANS
(2-3 sentences: the real significance for everyday people in the Maldives. Connect the dots between stories if there's a pattern.)

🔮 WHAT TO WATCH TOMORROW
(1-2 things likely to develop or worth following up on.)

🎬 TIKTOK ANGLE FOR THOOMA
(One specific, punchy video idea based on today's biggest story — give a hook line she could open with.)

Keep it tight, smart, and in English. Max 280 words. Write like a real editor talking to their team, not a robot."""

        msg = ai.messages.create(model="claude-haiku-4-5-20251001", max_tokens=700,
                                 messages=[{"role": "user", "content": prompt}])
        brief = msg.content[0].text.strip()
        caption = (f"🧠 <b>SAMUGA NIGHTLY BRIEF</b>\n"
                   f"<i>{today_str}</i>\n\n"
                   f"{brief}\n\n"
                   f"━━━━━━━━━━━━━━\n"
                   f"<i>Auto-generated by Samuga AI. Not posted publicly — for the team only.</i>")
        send_text(CORE_TEAM_CHAT_ID, caption, thread_id=CONTENT_LAB_THREAD_ID)
        log.info("🧠 ✅ Samuga AI brief sent to Content Lab!")
    except Exception as e:
        log.error(f"Samuga AI brief: {e}")

# ── Phase 2: ENGAGEMENT LEARNING ENGINE (observe-only until /learning on) ─────
LEARN_MIN_POSTS        = 200   # total posted articles before activation allowed
LEARN_MIN_WEEKS        = 4     # weeks of history before activation allowed
LEARN_MIN_VALID_VIEWS  = 50    # posts that actually have view counts (real data)
LEARN_CAP              = 15    # max ± points engagement may move a score (hard cap)

_scraper_health = {"ok": 0, "fail": 0, "warned": False}

def fetch_message_views(message_id):
    """
    Scrape view count for a public-channel post. Returns int or None.
    Tracks success/failure so we can warn the team if it stops working.
    NOTE: Telegram's Bot API can't read post views — this scrapes the public
    t.me page. Works while the channel is public. Swap to a Telethon MTProto
    client later for guaranteed counts (single-function change).
    """
    if not message_id:
        return None
    try:
        chan = TELEGRAM_CHANNEL_ID.lstrip("@")
        url = f"https://t.me/{chan}/{message_id}?embed=1&mode=tme"
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            _scraper_health["fail"] += 1
            return None
        import re as _re
        m = _re.search(r'tgme_widget_message_views[^>]*>([\d.,KMkm]+)<', resp.text)
        if not m:
            _scraper_health["fail"] += 1
            return None
        raw = m.group(1).strip().upper().replace(",", "")
        if raw.endswith("K"):
            val = int(float(raw[:-1]) * 1000)
        elif raw.endswith("M"):
            val = int(float(raw[:-1]) * 1_000_000)
        else:
            val = int(float(raw))
        _scraper_health["ok"] += 1
        return val
    except Exception as e:
        log.debug(f"fetch_message_views({message_id}): {e}")
        _scraper_health["fail"] += 1
        return None

def check_scraper_health(min_attempts=20):
    """Warn Content Lab once if view-scraping is mostly failing. Resets counters."""
    ok, fail = _scraper_health["ok"], _scraper_health["fail"]
    total = ok + fail
    if total >= min_attempts and fail / total > 0.7 and not _scraper_health["warned"]:
        send_text(CORE_TEAM_CHAT_ID,
            "⚠️ <b>View tracking looks broken.</b>\n\n"
            f"View scraping failed {fail}/{total} times this run. Telegram may have "
            "changed their page format, or the channel went private.\n\n"
            "Learning will keep using old numbers until this is fixed. Engagement "
            "data won't update.\n\n"
            "<i>Nothing else is affected — posting works normally.</i>",
            thread_id=CONTENT_LAB_THREAD_ID)
        _scraper_health["warned"] = True
        log.warning(f"⚠️ Scraper health poor: {fail}/{total} failed")
    _scraper_health["ok"] = 0
    _scraper_health["fail"] = 0

# ── Phase 2.5: META GRAPH API — Facebook + Instagram engagement ──────────────
# Reads engagement off your OWN page (no scraping). FB lost reach/impressions in
# Meta's June 2026 change, so FB = reactions+comments+shares. IG = likes+comments
# (+impressions where available). Matched to articles by caption (match_key).
_meta_health = {"ok": 0, "fail": 0, "warned": False}

def _meta_get(path, params=None):
    """GET the Graph API. Returns parsed JSON dict or None."""
    if not META_PAGE_TOKEN:
        return None
    try:
        p = dict(params or {})
        p["access_token"] = META_PAGE_TOKEN
        url = f"https://graph.facebook.com/{META_API_VER}/{path}"
        resp = requests.get(url, params=p, timeout=15)
        if resp.status_code == 200:
            _meta_health["ok"] += 1
            return resp.json()
        # Surface the Graph error message to logs (token expiry, perms, etc.)
        try:
            err = resp.json().get("error", {}).get("message", resp.text[:200])
        except Exception:
            err = resp.text[:200]
        log.error(f"Meta GET {path} → {resp.status_code}: {err}")
        _meta_health["fail"] += 1
        return None
    except Exception as e:
        log.error(f"Meta GET {path}: {e}")
        _meta_health["fail"] += 1
        return None

def _resolve_ig_id():
    """Find the Instagram Business account linked to the FB page. Cached in bot_kv."""
    if META_IG_ID:
        return META_IG_ID
    cached = kv_get("meta_ig_id", {})
    if isinstance(cached, dict) and cached.get("id"):
        return cached["id"]
    if not META_PAGE_ID:
        return None
    data = _meta_get(META_PAGE_ID, {"fields": "instagram_business_account"})
    ig = (data or {}).get("instagram_business_account", {}).get("id") if data else None
    if ig:
        kv_set("meta_ig_id", {"id": ig})
        log.info(f"📷 Resolved IG business account: {ig}")
    return ig

def _fetch_fb_post_engagement(limit=50):
    """
    Return list of (caption_text, engagement_int) for recent FB page posts.
    Engagement = reactions + comments + shares (reach/impressions deprecated by Meta).
    """
    if not META_PAGE_ID:
        return []
    data = _meta_get(f"{META_PAGE_ID}/posts", {
        "fields": "message,created_time,"
                  "reactions.summary(total_count).limit(0),"
                  "comments.summary(total_count).limit(0),"
                  "shares",
        "limit": limit,
    })
    out = []
    for post in (data or {}).get("data", []):
        msg = post.get("message", "")
        if not msg:
            continue
        reacts = post.get("reactions", {}).get("summary", {}).get("total_count", 0)
        comments = post.get("comments", {}).get("summary", {}).get("total_count", 0)
        shares = post.get("shares", {}).get("count", 0)
        eng = (reacts or 0) + (comments or 0) + (shares or 0)
        out.append((msg, eng))
    log.info(f"📘 FB: {len(out)} posts with engagement")
    return out

def _fetch_ig_post_engagement(limit=50):
    """
    Return list of (caption_text, engagement_int) for recent IG media.
    Engagement = like_count + comments_count.
    """
    ig_id = _resolve_ig_id()
    if not ig_id:
        return []
    data = _meta_get(f"{ig_id}/media", {
        "fields": "caption,like_count,comments_count,timestamp",
        "limit": limit,
    })
    out = []
    for media in (data or {}).get("data", []):
        cap = media.get("caption", "")
        if not cap:
            continue
        eng = (media.get("like_count") or 0) + (media.get("comments_count") or 0)
        out.append((cap, eng))
    log.info(f"📷 IG: {len(out)} media with engagement")
    return out

def fetch_meta_insights(days=28):
    """
    Pull FB + IG engagement, match each post to an article by caption (match_key),
    and write the combined number to articles.meta_engagement. Runs weekly.
    Returns number of articles updated.
    """
    if not DB_ENABLED or not META_PAGE_TOKEN:
        return 0
    # Get candidate articles (posted recently, with a match key)
    rows = db_execute("""
        SELECT id, match_key FROM articles
        WHERE status='posted' AND match_key IS NOT NULL AND match_key <> ''
          AND posted_at > NOW() - INTERVAL %s
    """, (f"{days} days",), fetch="all")
    if not rows:
        return 0
    articles = [(aid, mk) for aid, mk in rows]

    # Gather all platform posts (caption, engagement)
    platform_posts = _fetch_fb_post_engagement() + _fetch_ig_post_engagement()
    if not platform_posts:
        check_meta_health()
        return 0

    # Pre-normalize platform captions to match keys
    norm_posts = [(_caption_match_key(cap), eng) for cap, eng in platform_posts]

    updated = 0
    for aid, mk in articles:
        if not mk:
            continue
        total_eng = 0
        matched = False
        for pmk, eng in norm_posts:
            if not pmk:
                continue
            # Match if either key contains the other's leading chunk (captions get
            # truncated differently per platform). Require a decent overlap.
            short = min(len(mk), len(pmk))
            if short >= 18 and (mk[:short] == pmk[:short] or mk in pmk or pmk in mk):
                total_eng += eng
                matched = True
        if matched:
            db_execute("UPDATE articles SET meta_engagement=%s WHERE id=%s", (total_eng, aid))
            updated += 1
    log.info(f"📊 Meta insights matched {updated}/{len(articles)} articles")
    check_meta_health()
    return updated

def check_meta_health(min_attempts=4):
    """Warn Content Lab once if Meta API calls are mostly failing (token expired etc.)."""
    ok, fail = _meta_health["ok"], _meta_health["fail"]
    total = ok + fail
    if total >= min_attempts and fail / total > 0.7 and not _meta_health["warned"]:
        send_text(CORE_TEAM_CHAT_ID,
            "⚠️ <b>Facebook/Instagram data tracking failed.</b>\n\n"
            f"Meta API calls failed {fail}/{total} times. The Page token may have "
            "expired or lost permissions.\n\n"
            "Regenerate it (Graph API Explorer → me/accounts) and update "
            "<code>META_PAGE_TOKEN</code> in Railway.\n\n"
            "<i>Posting still works — only FB/IG learning data is affected.</i>",
            thread_id=CONTENT_LAB_THREAD_ID)
        _meta_health["warned"] = True
        log.warning(f"⚠️ Meta health poor: {fail}/{total} failed")
    _meta_health["ok"] = 0
    _meta_health["fail"] = 0


def backfill_tg_views(hours=240, limit=120):
    """Update tg_views for posted articles with a message_id. Runs weekly."""
    if not DB_ENABLED:
        return 0
    rows = db_execute("""
        SELECT id, tg_message_id FROM articles
        WHERE status='posted' AND tg_message_id IS NOT NULL
          AND posted_at > NOW() - INTERVAL %s
        ORDER BY posted_at DESC LIMIT %s
    """, (f"{hours} hours", limit), fetch="all")
    if not rows:
        return 0
    updated = 0
    for art_id, mid in rows:
        views = fetch_message_views(mid)
        if views is not None and views > 0:
            db_execute("UPDATE articles SET tg_views=%s WHERE id=%s", (views, art_id))
            updated += 1
        time.sleep(0.4)
    log.info(f"📈 Backfilled views for {updated}/{len(rows)} posts")
    check_scraper_health()
    return updated

def _median(nums):
    """Median of a list of numbers. 0 if empty."""
    s = sorted(n for n in nums if n is not None)
    if not s:
        return 0
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2

def compute_topic_weights(days=28):
    """
    Rank trend themes by MEDIAN engagement (average = secondary). Combines
    Telegram views + Facebook/Instagram engagement, each normalized to its OWN
    platform baseline first (different scales), then blended. Writes to
    bot_kv['topic_weights']. Does NOT change scoring. Returns the weights dict.
    """
    if not DB_ENABLED:
        return {}
    rows = db_execute("""
        SELECT title, summary, tg_views, meta_engagement FROM articles
        WHERE status='posted'
          AND (tg_views > 0 OR meta_engagement > 0)
          AND posted_at > NOW() - INTERVAL %s
    """, (f"{days} days",), fetch="all")
    if not rows:
        return {}

    # Platform baselines (median of non-zero values) so we can normalize scales
    tg_vals   = [r[2] for r in rows if r[2] and r[2] > 0]
    meta_vals = [r[3] for r in rows if r[3] and r[3] > 0]
    tg_base   = _median(tg_vals) or 1
    meta_base = _median(meta_vals) or 1

    def _combined_signal(tg, meta):
        """Each platform normalized to ~1.0 = its own median, then averaged."""
        parts = []
        if tg and tg > 0:
            parts.append(tg / tg_base)
        if meta and meta > 0:
            parts.append(meta / meta_base)
        return sum(parts) / len(parts) if parts else 0.0

    theme_signals = {}
    for title, summary, tg, meta in rows:
        sig = _combined_signal(tg, meta)
        if sig <= 0:
            continue
        for theme in _detect_themes(f"{title or ''} {summary or ''}"):
            theme_signals.setdefault(theme, []).append(sig)
    if not theme_signals:
        return {}

    all_sig = [s for ss in theme_signals.values() for s in ss]
    baseline = _median(all_sig) or 1.0

    import math
    weights = {}
    for theme, ss in theme_signals.items():
        if len(ss) < 3:
            continue
        med = _median(ss)
        avg = sum(ss) / len(ss)
        ratio = med / baseline if baseline else 1.0
        raw = math.log2(ratio) * LEARN_CAP if ratio > 0 else 0
        weight = max(-LEARN_CAP, min(LEARN_CAP, round(raw)))
        # 'median' shown as a relative index (1.0 = typical post) for readability
        weights[theme] = {"weight": weight, "median": round(med, 2),
                          "avg": round(avg, 2), "n": len(ss)}

    kv_set("topic_weights", weights)
    kv_set("topic_weights_baseline", {"median": round(baseline, 2)})
    log.info(f"📊 Computed topic weights for {len(weights)} themes (baseline median {round(baseline)})")
    return weights

def learning_stats():
    """Return (posted_total, weeks_elapsed, valid_view_count)."""
    if not DB_ENABLED:
        return (0, 0, 0)
    posted = db_execute("SELECT COUNT(*) FROM articles WHERE status='posted'", fetch="one")
    posted = posted[0] if posted else 0
    first = db_execute("SELECT MIN(found_at) FROM articles", fetch="one")
    weeks = 0
    if first and first[0]:
        try:
            weeks = (utcnow() - first[0].replace(tzinfo=None)).days / 7.0
        except Exception:
            weeks = 0
    valid = db_execute("SELECT COUNT(*) FROM articles WHERE status='posted' AND (tg_views > 0 OR meta_engagement > 0)", fetch="one")
    valid = valid[0] if valid else 0
    return (posted, round(weeks, 1), valid)

def learning_is_active():
    """True only if a human flipped the switch."""
    flag = kv_get("learning_active", {"on": False})
    return bool(flag.get("on")) if isinstance(flag, dict) else bool(flag)

def topic_weight_for(title, summary=""):
    """Engagement nudge ±LEARN_CAP, ONLY if learning active. (points, theme) or (0,None)."""
    if not learning_is_active():
        return (0, None)
    weights = kv_get("topic_weights", {})
    if not weights:
        return (0, None)
    themes = _detect_themes(f"{title} {summary}")
    best_pts, best_theme = 0, None
    for th in themes:
        w = weights.get(th, {}).get("weight", 0)
        if abs(w) > abs(best_pts):
            best_pts, best_theme = w, th
    return (best_pts, best_theme)

def _top_gainers_losers(weights, n=4):
    """Format top +n gainers and -n losers as two text blocks."""
    if not weights:
        return ("", "")
    items = [(th, d["weight"], d["median"], d["n"]) for th, d in weights.items()]
    gain = sorted([i for i in items if i[1] > 0], key=lambda x: -x[1])[:n]
    lose = sorted([i for i in items if i[1] < 0], key=lambda x:  x[1])[:n]
    g = "\n".join([f"  • {th} +{w} <i>({med}× typical, {nn} posts)</i>" for th, w, med, nn in gain])
    l = "\n".join([f"  • {th} {w} <i>({med}× typical, {nn} posts)</i>" for th, w, med, nn in lose])
    return (g, l)

def check_learning_readiness():
    """Weekly: if gate met and not yet asked, send the ONE-TIME readiness prompt."""
    if not DB_ENABLED:
        return
    posted, weeks, valid = learning_stats()
    already = kv_get("learning_prompt_sent", {"sent": False})
    if learning_is_active() or (isinstance(already, dict) and already.get("sent")):
        return
    if posted < LEARN_MIN_POSTS or weeks < LEARN_MIN_WEEKS or valid < LEARN_MIN_VALID_VIEWS:
        log.info(f"🧪 Learning not ready: posts={posted}/{LEARN_MIN_POSTS} "
                 f"weeks={weeks}/{LEARN_MIN_WEEKS} valid_views={valid}/{LEARN_MIN_VALID_VIEWS}")
        return
    weights = compute_topic_weights()
    gainers, losers = _top_gainers_losers(weights)
    msg = (
        "🧠 <b>Learning mode ready</b>\n\n"
        f"I've banked <b>{posted}</b> posts over <b>{weeks}</b> weeks, "
        f"<b>{valid}</b> with real view counts.\n\n"
        "<b>Top performers:</b>\n" + (gainers or "  (not enough data)") + "\n\n"
        "<b>Underperformers:</b>\n" + (losers or "  (not enough data)") + "\n\n"
        "If you approve, I'll let audience data <i>nudge</i> my posting decisions — "
        f"capped at ±{LEARN_CAP} pts. It informs, it never overrides a serious story.\n\n"
        "✅ <code>/learning on</code> to activate\n"
        "📊 <code>/learning status</code> to see the numbers\n"
        "<i>Ignore to stay observe-only. I won't ask again.</i>"
    )
    send_text(CORE_TEAM_CHAT_ID, msg, thread_id=CONTENT_LAB_THREAD_ID)
    kv_set("learning_prompt_sent", {"sent": True, "at": utcnow().isoformat()})
    log.info("🧠 Readiness prompt sent to Content Lab (one-time).")

# ── Weekly Analytics Report to Core Team ─────────────────────────────────────
def send_weekly_analytics():
    log.info("📊 Weekly analytics report...")
    try:
        from datetime import timezone
        mvt = datetime.now(timezone.utc) + timedelta(hours=5)
        week_str = mvt.strftime("Week of %d %B %Y")

        total = sum(v for k, v in analytics["posts_by_cat"].items() if k != "SOCIAL")
        by_cat = analytics["posts_by_cat"]

        lines = []
        for cat in ["LOCAL","WORLD","FOOTBALL","TOURISM","WEATHER","DISASTER"]:
            if cat in by_cat:
                lines.append(f"  • {cat}: {by_cat[cat]} posts")

        cat_lines = chr(10).join([f"  - {c}: {by_cat[c]} posts" for c in ["LOCAL","WORLD","FOOTBALL","TOURISM","WEATHER","DISASTER"] if c in by_cat])
        report = (
            "<b>Samuga Media Weekly Report</b>" + chr(10)
            + week_str + chr(10) + chr(10)
            + "<b>Total Articles:</b> " + str(total) + chr(10)
            + (cat_lines if cat_lines else "  No posts yet") + chr(10) + chr(10)
            + "<b>Breaking News:</b> " + str(analytics["breaking_count"]) + chr(10) + chr(10)
            + "<b>Social Posting:</b>" + chr(10)
            + "  Success: " + str(analytics["social_success"]) + chr(10)
            + "  Failed: " + str(analytics["social_fail"]) + chr(10) + chr(10)
            + "<b>Bot:</b> Samuga News Bot v3.2" + chr(10)
            + "Samuga Media | @samugacommunity"
        )
        # ── Phase 2: weekly engagement crunch + readiness ──
        learn_block = ""
        try:
            backfill_tg_views()                      # refresh view counts (matured)
            fetch_meta_insights()                    # refresh FB + IG engagement
            weights = compute_topic_weights()        # recompute (stored, not yet acting)
            posted, weeks, valid = learning_stats()
            gainers, losers = _top_gainers_losers(weights)
            mode = "ACTIVE ✅" if learning_is_active() else "observing 👀"
            learn_block = (
                chr(10) + "<b>📈 What we learned this week</b>" + chr(10)
                + f"Mode: {mode}  ({posted} posts, {valid} with views)" + chr(10) + chr(10)
                + "<b>Top gainers:</b>" + chr(10) + (gainers or "  (gathering data)") + chr(10) + chr(10)
                + "<b>Top losers:</b>"  + chr(10) + (losers  or "  (gathering data)") + chr(10)
            )
        except Exception as e:
            log.error(f"weekly learning block: {e}")
        report = report + learn_block

        send_text(CORE_TEAM_CHAT_ID, report)
        check_learning_readiness()                  # one-time prompt if gate met
        log.info("✅ Analytics report sent to core team")
    except Exception as e:
        log.error(f"Analytics report: {e}")

# ── Weekly Digest (Friday 6PM MVT) ───────────────────────────────────────────
def send_weekly_digest():
    log.info("📊 Weekly digest...")
    try:
        if not recent_posts: return
        posts_text="\n".join([f"• [{p['cat']}] {p['title']}" for p in recent_posts])
        prompt=f"""Create a "This Week in Maldives 🇲🇻" weekly digest for @samugacommunity.
This week: {posts_text}
- Top 5 most important stories
- 2 sentences each with emoji
- Encouraging closing
- Max 280 words"""
        msg=ai.messages.create(model="claude-haiku-4-5-20251001",max_tokens=500,messages=[{"role":"user","content":prompt}])
        digest=msg.content[0].text.strip()
        caption=f"📊 <b>This Week in Maldives 🇲🇻</b>\n\n{digest}\n\n📡 <b>Samuga Media</b> | @samugacommunity"
        send_text(TELEGRAM_CHANNEL_ID, caption)
        log.info("✅ Weekly digest sent!")
    except Exception as e: log.error(f"Weekly digest: {e}")

# ── Tavily Search ─────────────────────────────────────────────────────────────
def tavily_search(query):
    if not TAVILY_API_KEY: return ""
    try:
        resp=requests.post("https://api.tavily.com/search",
            json={"api_key":TAVILY_API_KEY,"query":query,"search_depth":"basic","max_results":4,"include_answer":True},timeout=15)
        if resp.status_code==200:
            data=resp.json()
            answer=data.get("answer","")
            snippets=[r.get("content","")[:200] for r in data.get("results",[])[:3]]
            log.info(f"✅ Tavily: {query[:40]}")
            return (answer+"\n"+"\n".join(snippets)).strip()
    except Exception as e: log.error(f"Tavily: {e}")
    return ""

def needs_web_search(msg):
    # Skip search only for simple greetings / meta questions
    # Skip search for short messages or greetings
    if len(msg.strip()) <= 4: return False
    skip_kws = ["hello", "hi", "who are you", "what is samuga", "about you",
                "thank", "okay", "ok", "bye", "good morning", "good night",
                "good evening", "assalam", "hey", "sup", "wassup"]
    if any(k in msg.lower() for k in skip_kws): return False
    return True  # Default: always search for current info

# ── Smart Chat ────────────────────────────────────────────────────────────────
def is_dhivehi(text):
    """Check if text contains Thaana script (Dhivehi)"""
    return any('\u0780' <= c <= '\u07BF' for c in text)

def chat_with_gemini_dhivehi(user_message, context="", conversation_history=None):
    """Handle Dhivehi chat using actual Gemini API (native Dhivehi support)"""
    if not GEMINI_API_KEY:
        log.warning("No GEMINI_API_KEY — falling back to Claude for Dhivehi")
        return None
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={GEMINI_API_KEY}"

        # Try web search for Dhivehi queries too
        web_context = ""
        try:
            if needs_web_search(user_message) or not context:
                web_context = tavily_search(f"maldives news today 2026")
                if web_context:
                    log.info("🌐 Dhivehi path: web search done")
        except Exception as e:
            log.error(f"Dhivehi web search: {e}")

        if web_context:
            news_section = "LIVE WEB SEARCH (use this for answers, never repeat same info):\n" + web_context[:600]
        elif context:
            news_section = "LATEST NEWS CONTEXT:\n" + context
        else:
            news_section = ""

        system_prompt = (
            "You are Samuga AI, a Maldivian news assistant. Always reply in natural Dhivehi (Thaana script) only.\n\n"
            "ABOUT SAMUGA:\n"
            "- Samuga Media: Maldivian digital news outlet\n"
            "- Channel: @samugacommunity\n"
            "- Founder: Abdul Muhsin (Manchii) | Co-Founder: Mariyam Ulya (Uly)\n\n"
            + (news_section + "\n\n" if news_section else "") +
            "RULES:\n"
            "- Reply ONLY in Dhivehi Thaana script\n"
            "- Natural, conversational tone like a friendly Maldivian\n"
            "- Max 3-4 sentences\n"
            "- NEVER repeat the same news you already mentioned in this conversation\n"
            "- If asked for more — give DIFFERENT stories\n"
            "- Mention @samugacommunity when relevant\n"
            "- Never write in English or Latin script\n"
            "- Never say you cannot search or lack real-time info"
        )

        # Build contents array with history for multi-turn
        contents = []
        if conversation_history:
            for turn in conversation_history[-6:]:
                role = "user" if turn["role"] == "user" else "model"
                contents.append({"role": role, "parts": [{"text": turn["content"]}]})
        contents.append({"role": "user", "parts": [{"text": user_message}]})

        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": contents,
            "generationConfig": {"maxOutputTokens": 400, "temperature": 0.7}
        }

        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code == 200:
            reply = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            log.info("✅ Gemini Dhivehi chat reply done")
            return reply
        else:
            log.error(f"Gemini Dhivehi chat HTTP {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        log.error(f"Gemini Dhivehi chat error: {e}")
    return None

def chat_with_claude(user_message, user_id=None):
    try:
        # Run headlines + web search in parallel to cut latency
        results = {}

        def fetch_headlines():
            try: results["headlines"] = get_local_headlines()
            except Exception as e: log.debug(f"fetch_headlines: {e}"); results["headlines"] = []

        def fetch_web():
            try:
                if needs_web_search(user_message):
                    q = user_message
                    local_kws = ["weather","news","update","what happened","anything","latest","today"]
                    if any(w in user_message.lower() for w in local_kws) and "maldives" not in user_message.lower():
                        q = f"maldives {user_message} 2026"
                    elif any(w in user_message.lower() for w in ["world cup","match","score","won","win"]):
                        q = f"{user_message} 2026 latest"
                    results["web"] = tavily_search(q)
                    if results["web"]: log.info(f"🌐 Web: {results['web'][:60]}...")
            except Exception as e:
                log.error(f"Web search: {e}")
                results["web"] = ""

        t1 = threading.Thread(target=fetch_headlines)
        t2 = threading.Thread(target=fetch_web)
        t1.start(); t2.start()
        t1.join(timeout=5); t2.join(timeout=5)

        headlines = results.get("headlines", [])
        web_context = results.get("web", "") or ""
        headlines_text = "\n".join(headlines[:8]) if headlines else "No recent headlines."

        memory_text = ""
        if recent_posts:
            memory_text = "Recently posted:\n" + "".join([f"• [{p['cat']}] {p['title']}\n" for p in recent_posts[-5:]])

        if web_context:
            context = f"LIVE WEB SEARCH (use this for your answer):\n{web_context[:800]}"
            if memory_text: context += f"\n\n{memory_text}"
        else:
            context = f"LATEST NEWS:\n{headlines_text}"
            if memory_text: context += f"\n\n{memory_text}"

        system=f"""You are Samuga AI — smart friendly Maldivian news assistant for Samuga Media.

ABOUT SAMUGA:
Samuga Media delivers trusted Maldivian news. @samugacommunity is our Telegram channel.
Founder & MD: Abdul Muhsin (Manchii/Mutte) — Maldivian entrepreneur
Co-Founder & Editor: Mariyam Ulya (Uly) — journalist & editorial lead

CONTEXT:
{context}

PERSONALITY:
- Warm, friendly, like a knowledgeable Maldivian friend
- Max 4 sentences per reply
- Use context for accurate answers
- Guide to @samugacommunity for more
- If user writes Dhivehi — reply in Dhivehi
- Never say you lack real-time data"""

        messages=get_conversation(user_id).copy() if user_id else []
        messages.append({"role":"user","content":user_message})

        msg=ai.messages.create(model="claude-haiku-4-5-20251001",max_tokens=600,system=system,messages=messages)
        reply=msg.content[0].text.strip()

        if user_id:
            add_to_conversation(user_id,"user",user_message)
            add_to_conversation(user_id,"assistant",reply)
        return reply
    except Exception as e:
        log.error(f"Chat: {e}")
        return "Hey! Something went wrong 😅 Check @samugacommunity for the latest!"

# ── Core Team Smart Chat ──────────────────────────────────────────────────────
def get_sender_info(user_name, first_name):
    """Identify core team member from username or first name"""
    check = (user_name or "").lower()
    fname = (first_name or "").lower()
    for key, info in CORE_TEAM_MEMBERS.items():
        if key in check or key in fname or info["name"].lower() in fname:
            return info
    return None

def should_respond_proactively(text):
    """Check if bot should jump in without being tagged"""
    t = text.lower()
    return any(trigger in t for trigger in CORE_TEAM_PROACTIVE_TRIGGERS)

def chat_with_coreteam(message, sender_name, sender_info=None, conversation_history=None, session_ctx=""):
    """Smart core team chat — creative, funny, knows the team"""
    try:
        # Build sender context
        if sender_info:
            sender_ctx = f"{sender_info['name']} ({sender_info['role']}) — {sender_info['notes']}"
        else:
            sender_ctx = sender_name or "a team member"

        # Get recent headlines for context
        headlines = []
        try: headlines = get_local_headlines()
        except Exception as e: log.debug(f"headlines ctx: {e}")
        news_ctx = "\n".join(headlines[:5]) if headlines else ""

        news_line = ("LATEST MALDIVES NEWS:\n" + news_ctx) if news_ctx else ""
        system = (
            "You are Samuga AI — witty, sharp, creative team assistant for Samuga Media core team.\n\n"
            "ABOUT SAMUGA MEDIA:\n"
            "- Maldivian digital news & media outlet\n"
            "- Mission: unfiltered truth, real stories, people voice\n"
            "- Telegram: @samugacommunity | Website: en.samugamedia.com\n\n"
            "THE CORE TEAM (you know them personally):\n"
            "- Manchii (Abdul Muhsin) — Founder & MD. Big vision, entrepreneur, always thinking next move. Roast him gently\n"
            "- Uly (Mariyam Ulya) — Co-Founder & Editor-in-Chief. Sharp journalist brain, keeps content accurate\n"
            "- Thooma (Aminath Thooma) — Presenter & Marketing. Face of content, full of energy, needs confidence boost sometimes\n"
            "- Kity (Kit) — Manchii wife, creative contributor, team heart, great at hyping Thooma, brings fresh ideas\n\n"
            f"YOU ARE SPEAKING WITH: {sender_ctx}\n\n"
            + (news_line + "\n\n" if news_line else "")
            + (("SESSION CONTEXT (team shared this for reference):\n" + session_ctx + "\n\n") if session_ctx else "")
            + "YOUR PERSONALITY IN THIS GROUP:\n"
            "- Casual, warm, feel like a real team member not a bot\n"
            "- Funny and witty — crack jokes when vibe calls for it, especially with Manchii\n"
            "- Hype Thooma when she needs it, she has got it in her\n"
            "- Support Kity ideas, she brings great creative energy\n"
            "- Help with content ideas, scripts, captions, strategies instantly\n"
            "- When brainstorming — give 3 specific ideas not generic ones\n"
            "- Keep replies SHORT unless asked for detail — max 3-4 sentences casual\n"
            "- Use occasional emoji but do not overdo it\n"
            "- Never sound corporate or formal\n"
            "- Speak like you are part of the team, not serving the team"
        )

        messages = []
        if conversation_history:
            messages = conversation_history[-8:]  # last 4 exchanges
        messages.append({"role": "user", "content": message})

        msg = ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            system=system,
            messages=messages
        )
        return msg.content[0].text.strip()
    except Exception as e:
        log.error(f"Core team chat: {e}")
        return None

# ── Chat Handler ──────────────────────────────────────────────────────────────
def handle_updates():
    # Use persisted offset so we never miss messages across restarts
    offset = _poll_offset[0]
    bot_mention=f"@{BOT_USERNAME}".lower()
    log.info(f"💬 Chat listening for @{BOT_USERNAME}... (offset={offset})")
    while True:
        try:
            resp=requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                params={"offset":offset,"timeout":30},timeout=40)
            if resp.status_code!=200: time.sleep(5); continue
            for update in resp.json().get("result",[]):
                offset=update["update_id"]+1
                _poll_offset[0] = offset
                # Save offset every 10 updates — cheap insurance against missing messages on restart
                if offset % 10 == 0:
                    persist_state()
                msg=update.get("message",{})
                if not msg: continue
                text=msg.get("text","") or msg.get("caption","")
                photo=msg.get("photo")  # list of photo sizes if message has photo
                video=msg.get("video") or msg.get("video_note")
                if not text and not photo and not video: continue
                if not text: text=""
                # Skip videos for card creation — only photos supported
                if video and not photo: photo = None
                chat_id=msg["chat"]["id"]
                msg_id=msg["message_id"]
                thread_id=msg.get("message_thread_id")  # for forum/topic groups
                chat_type=msg["chat"]["type"]
                user_name=msg.get("from",{}).get("username","")
                first_name=msg.get("from",{}).get("first_name","there")
                display_name=user_name or first_name
                user_id=str(msg.get("from",{}).get("id",""))

                if chat_type=="private":
                    if text.startswith("/start"):
                        send_text(chat_id,
                            f"👋 Hey {first_name}! I'm <b>Samuga AI</b> — your Maldives news assistant!\n\n"
                            f"Ask me anything about Maldives news, politics, tourism, football or world news.\n\n"
                            f"ދިވެހިން ވެސް ވާހަކަ ދެއްކިދާނެ! 🇲🇻\n\n"
                            f"📡 Follow <b>@samugacommunity</b> for live news updates!",reply_to=msg_id)
                    elif text.startswith("/search "):
                        query=text[8:].strip()
                        log.info(f"🔍 Search: {query}")
                        results=tavily_search(f"{query} maldives")
                        reply=chat_with_claude(f"Tell me about: {query}. Use this info: {results[:400]}", user_id)
                        send_text(chat_id, reply, reply_to=msg_id, thread_id=thread_id)
                    else:
                        log.info(f"💬 DM {display_name}: {text[:50]}")
                        # Route Dhivehi to Gemini
                        if is_dhivehi(text):
                            log.info("🇲🇻 Dhivehi detected — using Gemini")
                            headlines = get_local_headlines()
                            context = "\n".join(headlines[:5]) if headlines else ""
                            history = get_conversation(user_id)
                            reply = chat_with_gemini_dhivehi(text, context, history)
                            if reply:
                                add_to_conversation(user_id, "user", text)
                                add_to_conversation(user_id, "assistant", reply)
                            else:
                                reply = chat_with_claude(text, user_id)
                        else:
                            reply = chat_with_claude(text, user_id)
                        send_text(chat_id, reply, reply_to=msg_id, thread_id=thread_id)

                elif chat_type in ["group","supergroup"]:
                    is_core_team = str(chat_id) == CORE_TEAM_CHAT_ID
                    tagged = bot_mention in text.lower()
                    clean = text.replace(bot_mention, "").strip() if tagged else text.strip()

                    # Core team group — smarter behavior
                    if is_core_team:
                        sender_info = get_sender_info(display_name, first_name)
                        history = get_conversation(user_id)

                        # /approved <key> [optional corrected dhivehi text]
                        if text.strip().lower().startswith("/approved "):
                            parts = text.strip()[10:].strip().split(" ", 1)
                            key = parts[0].strip().lower()
                            corrected = parts[1].strip() if len(parts) > 1 else None

                            # Always acknowledge immediately — team should NEVER get silence
                            send_text(chat_id, f"⏳ Got it {first_name}! Processing <b>{key.upper()}</b>...", reply_to=msg_id, thread_id=thread_id)

                            if key in approval_queue:
                                item = approval_queue.pop(key)
                                persist_state()
                                action = "edited" if corrected else "approved"
                                try:
                                    ok = False
                                    if item["lang"] == "dv":
                                        # Build Dhivehi card now (with optional correction)
                                        final_dv = corrected if corrected else item["dv_text"]
                                        kw = item.get("keyword", item["cat"].lower())
                                        bg = fetch_background_image(kw)
                                        ts_now = (utcnow() + timedelta(hours=5)).strftime("%d %b %Y • %H:%M")
                                        card = generate_card(final_dv, item["source"], ts_now, item["cat"], bg)
                                        full_caption = (
                                            f"🇲🇻 <b>{item['title']}</b>\n\n"
                                            f"{final_dv}\n\n"
                                            f"📡 <b>ސަމޫގާ މީޑިއާ</b> | @samugacommunity"
                                        )
                                        card.seek(0)
                                        ok = send_to_telegram(card, full_caption)
                                        if isinstance(ok, int) and item.get("article_id"):
                                            db_set_article_message(item["article_id"], ok)
                                            db_set_article_matchkey(item["article_id"], item["title"])
                                        if ok:
                                            remember_post(item["title"], item["cat"], ts_now)
                                            if item.get("allow_social"):
                                                card.seek(0)
                                                threading.Thread(target=post_to_social, args=(io.BytesIO(card.getvalue()), full_caption), daemon=True).start()
                                    else:
                                        # English — publish + report per platform back to Content Lab
                                        tg_ok, social_res = _publish_now(
                                            item["card_bytes"], item["caption"], item["cat"],
                                            item["title"], item["link"],
                                            is_breaking_flag=item.get("is_breaking", False),
                                            allow_social=item.get("allow_social", True),
                                            rewritten=item.get("rewritten",""),
                                            summary=item.get("summary",""),
                                            report_to=(chat_id, thread_id),
                                            article_id=item.get("article_id")
                                        )
                                        ok = tg_ok

                                    if ok:
                                        if item.get("article_id"):
                                            db_mark_status(item["article_id"], "posted", posted=True)
                                        db_log_learning(
                                            article_id=item.get("article_id"),
                                            action=("edited" if corrected else "approved"),
                                            member=first_name,
                                            category=item.get("cat",""),
                                            source=item.get("source",""),
                                            theme=item.get("_trend_theme",""),
                                            original_caption=item.get("dv_text") or item.get("caption",""),
                                            final_caption=(corrected or item.get("dv_text") or item.get("caption","")),
                                            lang=item.get("lang","en"))
                                        log.info(f"✅ {key} ({item['lang']}) posted by {first_name}")
                                    else:
                                        # Telegram failed — put card back so team can retry
                                        approval_queue[key] = item
                                        persist_state()
                                        send_text(chat_id,
                                            f"❌ <b>{key.upper()} — Telegram failed</b>\n\n"
                                            f"Card is still in queue. Try again:\n"
                                            f"<code>/approved {key}</code>\n"
                                            f"Or reject: <code>/reject {key}</code>",
                                            reply_to=msg_id, thread_id=thread_id)
                                        log.error(f"❌ {key} post failed for {first_name}")
                                except Exception as e:
                                    log.error(f"Approval post error: {e}")
                                    # Put card back so team can retry
                                    approval_queue[key] = item
                                    persist_state()
                                    send_text(chat_id,
                                        f"❌ <b>Error posting {key.upper()}</b>: {str(e)[:100]}\n\n"
                                        f"Card saved — try again: <code>/approved {key}</code>",
                                        reply_to=msg_id, thread_id=thread_id)
                            else:
                                # Key not found — give helpful context
                                send_text(chat_id,
                                    f"⚠️ <b>{key.upper()}</b> not found in queue\n\n"
                                    f"It may have already posted, been rejected, or expired.\n"
                                    f"Run <code>/pending</code> to see what's still waiting.",
                                    reply_to=msg_id, thread_id=thread_id)

                        # /reject <key>
                        elif text.strip().lower().startswith("/reject "):
                            key = text.strip()[8:].strip()
                            if key in approval_queue:
                                rej_item  = approval_queue[key]
                                rej_title = rej_item.get("title","")[:70]
                                if rej_item.get("article_id"):
                                    db_mark_status(rej_item["article_id"], "rejected")
                                db_log_learning(
                                    article_id=rej_item.get("article_id"),
                                    action="rejected",
                                    member=first_name,
                                    category=rej_item.get("cat",""),
                                    source=rej_item.get("source",""),
                                    theme=rej_item.get("_trend_theme",""),
                                    original_caption=rej_item.get("dv_text") or rej_item.get("caption",""),
                                    lang=rej_item.get("lang","en"))
                                del approval_queue[key]
                                persist_state()
                                import random as _r
                                send_text(chat_id, f"❌ <b>{key.upper()}</b> rejected — {rej_title}\n\n{_r.choice(REJECT_RESPONSES)}", reply_to=msg_id, thread_id=thread_id)
                                log.info(f"🗑️ {key} rejected by {first_name}")
                            else:
                                send_text(chat_id, f"Key <code>{key}</code> not found — maybe already posted or rejected.", reply_to=msg_id, thread_id=thread_id)

                        # /stats — newsroom archive overview (DB-powered)
                        elif text.strip().lower() in ["/stats", "/archive"]:
                            if not DB_ENABLED:
                                send_text(chat_id, "🗄️ Database not connected — archive stats unavailable. Running in JSON mode.", reply_to=msg_id, thread_id=thread_id)
                            else:
                                try:
                                    total = db_execute("SELECT COUNT(*) FROM articles", fetch="one")
                                    today = db_execute("SELECT COUNT(*) FROM articles WHERE found_at > NOW() - INTERVAL '24 hours'", fetch="one")
                                    posted = db_execute("SELECT COUNT(*) FROM articles WHERE status='posted' AND found_at > NOW() - INTERVAL '24 hours'", fetch="one")
                                    dupes = db_execute("SELECT COUNT(*) FROM articles WHERE status='duplicate' AND found_at > NOW() - INTERVAL '24 hours'", fetch="one")
                                    by_cat = db_execute("""
                                        SELECT category, COUNT(*) FROM articles
                                        WHERE found_at > NOW() - INTERVAL '24 hours'
                                        GROUP BY category ORDER BY COUNT(*) DESC LIMIT 6
                                    """, fetch="all")
                                    top_src = db_execute("""
                                        SELECT source, COUNT(*) FROM articles
                                        WHERE found_at > NOW() - INTERVAL '24 hours' AND source IS NOT NULL
                                        GROUP BY source ORDER BY COUNT(*) DESC LIMIT 5
                                    """, fetch="all")
                                    msg_lines = ["🗞️ <b>Samuga Newsroom — Last 24h</b>\n"]
                                    msg_lines.append(f"📥 Scanned: <b>{today[0] if today else 0}</b>")
                                    msg_lines.append(f"✅ Posted: <b>{posted[0] if posted else 0}</b>")
                                    msg_lines.append(f"🔁 Duplicates blocked: <b>{dupes[0] if dupes else 0}</b>")
                                    msg_lines.append(f"📚 Total archive: <b>{total[0] if total else 0}</b>\n")
                                    if by_cat:
                                        msg_lines.append("<b>By category:</b>")
                                        for c, n in by_cat:
                                            msg_lines.append(f"  • {c}: {n}")
                                    if top_src:
                                        msg_lines.append("\n<b>Top sources:</b>")
                                        for s, n in top_src:
                                            msg_lines.append(f"  • {s}: {n}")
                                    send_text(chat_id, "\n".join(msg_lines), reply_to=msg_id, thread_id=thread_id)
                                except Exception as e:
                                    log.error(f"/stats: {e}")
                                    send_text(chat_id, f"❌ Stats error: {e}", reply_to=msg_id, thread_id=thread_id)

                        # /trends — what Maldives is talking about right now
                        elif text.strip().lower() in ["/trends", "/trending"]:
                            if not DB_ENABLED:
                                send_text(chat_id, "🗄️ Database not connected — trends unavailable.", reply_to=msg_id, thread_id=thread_id)
                            else:
                                try:
                                    trends = detect_trends(hours=24, min_mentions=3)
                                    if not trends:
                                        send_text(chat_id, "📊 No clear trends yet — archive is still filling up. Check back after a few hours of news.", reply_to=msg_id, thread_id=thread_id)
                                    else:
                                        lines = ["🔥 <b>Trending in Maldives — Last 24h</b>\n"]
                                        medals = ["🥇","🥈","🥉"] + ["🔹"]*20
                                        for i, (theme, count, titles) in enumerate(trends[:8]):
                                            lines.append(f"{medals[i]} <b>{theme}</b> — {count} stories")
                                        lines.append("\n<i>The bot boosts stories about these hot topics automatically.</i>")
                                        send_text(chat_id, "\n".join(lines), reply_to=msg_id, thread_id=thread_id)
                                except Exception as e:
                                    log.error(f"/trends: {e}")
                                    send_text(chat_id, f"❌ Trends error: {e}", reply_to=msg_id, thread_id=thread_id)

                        # /learning on | off | status — engagement learning switch
                        elif text.strip().lower().startswith("/learning"):
                            arg = text.strip().lower().replace("/learning", "").strip()
                            if not DB_ENABLED:
                                send_text(chat_id, "🗄️ Database not connected — learning unavailable.", reply_to=msg_id, thread_id=thread_id)
                            elif arg == "on":
                                posted, weeks, valid = learning_stats()
                                if posted < LEARN_MIN_POSTS or weeks < LEARN_MIN_WEEKS or valid < LEARN_MIN_VALID_VIEWS:
                                    send_text(chat_id,
                                        f"⏳ Not ready yet:\n"
                                        f"  • Posts: {posted}/{LEARN_MIN_POSTS}\n"
                                        f"  • Weeks: {weeks}/{LEARN_MIN_WEEKS}\n"
                                        f"  • Posts with views: {valid}/{LEARN_MIN_VALID_VIEWS}\n\n"
                                        f"I'll keep collecting and tell you when the gate is met.",
                                        reply_to=msg_id, thread_id=thread_id)
                                else:
                                    kv_set("learning_active", {"on": True, "by": first_name, "at": utcnow().isoformat()})
                                    weights = compute_topic_weights()
                                    gainers, losers = _top_gainers_losers(weights)
                                    send_text(chat_id,
                                        f"✅ <b>Learning mode ON</b> (by {first_name})\n\n"
                                        f"Audience data now nudges scoring, capped at ±{LEARN_CAP} pts.\n\n"
                                        f"<b>Getting a boost:</b>\n{gainers or '  (none yet)'}\n\n"
                                        f"<b>Getting demoted:</b>\n{losers or '  (none yet)'}\n\n"
                                        f"<i>Serious news always wins — this only breaks ties.</i>\n"
                                        f"Turn off anytime: <code>/learning off</code>",
                                        reply_to=msg_id, thread_id=thread_id)
                                    log.info(f"🧠 Learning ACTIVATED by {first_name}")
                            elif arg == "off":
                                kv_set("learning_active", {"on": False, "by": first_name, "at": utcnow().isoformat()})
                                send_text(chat_id,
                                    f"🛑 <b>Learning mode OFF</b> (by {first_name})\n"
                                    f"Back to observe-only. Scoring ignores audience data again.",
                                    reply_to=msg_id, thread_id=thread_id)
                                log.info(f"🧠 Learning DEACTIVATED by {first_name}")
                            else:  # status
                                posted, weeks, valid = learning_stats()
                                active = learning_is_active()
                                weights = kv_get("topic_weights", {})
                                gainers, losers = _top_gainers_losers(weights)
                                ready = (posted >= LEARN_MIN_POSTS and weeks >= LEARN_MIN_WEEKS and valid >= LEARN_MIN_VALID_VIEWS)
                                send_text(chat_id,
                                    f"🧠 <b>Learning status</b>\n\n"
                                    f"Mode: {'ACTIVE ✅' if active else 'observing 👀'}\n"
                                    f"Gate: {'met ✅' if ready else 'not met'}\n"
                                    f"  • Posts: {posted}/{LEARN_MIN_POSTS}\n"
                                    f"  • Weeks: {weeks}/{LEARN_MIN_WEEKS}\n"
                                    f"  • Posts with views: {valid}/{LEARN_MIN_VALID_VIEWS}\n\n"
                                    f"<b>Top gainers:</b>\n{gainers or '  (gathering data)'}\n\n"
                                    f"<b>Top losers:</b>\n{losers or '  (gathering data)'}\n\n"
                                    + (f"Cap: ±{LEARN_CAP} pts. " if active else "")
                                    + ("<code>/learning on</code> to activate." if (ready and not active) else ""),
                                    reply_to=msg_id, thread_id=thread_id)

                        # /meta — test the Facebook/Instagram connection live
                        elif text.strip().lower() in ["/meta", "/facebook", "/insights"]:
                            if not META_PAGE_TOKEN:
                                send_text(chat_id,
                                    "📵 No <code>META_PAGE_TOKEN</code> set in Railway. "
                                    "FB/IG learning is off.", reply_to=msg_id, thread_id=thread_id)
                            else:
                                send_text(chat_id, "🔌 Testing Facebook + Instagram connection... ⏳",
                                          reply_to=msg_id, thread_id=thread_id)
                                try:
                                    fb = _fetch_fb_post_engagement(limit=10)
                                    ig_id = _resolve_ig_id()
                                    ig = _fetch_ig_post_engagement(limit=10) if ig_id else []
                                    lines = ["🔌 <b>Meta connection test</b>\n"]
                                    if fb:
                                        top_fb = max(e for _, e in fb)
                                        lines.append(f"📘 Facebook: ✅ {len(fb)} posts read (top engagement: {top_fb})")
                                    else:
                                        lines.append("📘 Facebook: ⚠️ no posts returned (new page, or check token perms)")
                                    if ig_id and ig:
                                        top_ig = max(e for _, e in ig)
                                        lines.append(f"📷 Instagram: ✅ {len(ig)} posts read (top engagement: {top_ig})")
                                    elif ig_id:
                                        lines.append("📷 Instagram: linked ✅ but no posts returned yet")
                                    else:
                                        lines.append("📷 Instagram: ⚠️ not linked — switch IG to Professional & link to the FB page")
                                    matched = fetch_meta_insights()
                                    lines.append(f"\n🔗 Matched to <b>{matched}</b> articles in the archive.")
                                    lines.append("<i>Runs automatically every Friday + Tuesday. Data feeds learning (still observe-only).</i>")
                                    send_text(chat_id, "\n".join(lines), reply_to=msg_id, thread_id=thread_id)
                                except Exception as e:
                                    log.error(f"/meta: {e}")
                                    send_text(chat_id, f"❌ Meta test error: {e}", reply_to=msg_id, thread_id=thread_id)

                        # /why <key> — explain how a queued card scored
                        elif text.strip().lower().startswith("/why"):
                            key = text.strip()[4:].strip()
                            if not key:
                                send_text(chat_id,
                                    "Usage: <code>/why en12</code> — explains how a card in the "
                                    "queue scored. Run <code>/pending</code> to see keys.",
                                    reply_to=msg_id, thread_id=thread_id)
                            elif key not in approval_queue:
                                send_text(chat_id,
                                    f"Key <code>{key}</code> not in the queue. "
                                    f"<code>/pending</code> shows what's waiting.",
                                    reply_to=msg_id, thread_id=thread_id)
                            else:
                                item = approval_queue[key]
                                art = {
                                    "title": item.get("title",""),
                                    "summary": item.get("summary",""),
                                    "cat": item.get("cat","LOCAL"),
                                    "source": item.get("source",""),
                                    "lang": item.get("lang","en"),
                                    "_cluster_size": item.get("_cluster_size", 1),
                                    "_trend_theme": item.get("_trend_theme",""),
                                }
                                try:
                                    send_text(chat_id, format_score_breakdown(art),
                                              reply_to=msg_id, thread_id=thread_id)
                                except Exception as e:
                                    log.error(f"/why: {e}")
                                    send_text(chat_id, f"❌ Couldn't explain {key}: {e}",
                                              reply_to=msg_id, thread_id=thread_id)

                        # /weather — force send a weather card preview to core team
                        elif text.strip().lower() in ["/weather", "/wx"]:
                            send_text(chat_id, "🌤️ Fetching weather + island data... ⏳",
                                      reply_to=msg_id, thread_id=thread_id)
                            def _send_weather_preview():
                                try:
                                    data = get_weather_data()
                                    if not data:
                                        send_text(chat_id, "❌ Weather data unavailable right now.", thread_id=thread_id)
                                        return
                                    islands = get_island_forecasts()
                                    card = generate_weather_card(data, island_data=islands if islands else None)
                                    current = data.get("current", {})
                                    temp  = round(current.get("temperature_2m", 29))
                                    code  = current.get("weathercode", 0)
                                    emoji, condition = weather_code_to_info(code)
                                    source = data.get("_source", "")
                                    island_lines = ""
                                    if islands:
                                        island_lines = "\n\n🏝 <b>Weather Watch</b>\n"
                                        for isl in islands:
                                            island_lines += f"📍 <b>{isl['name']}</b> — {isl['outlook']}\n"
                                    caption = (
                                        f"🌤️ <b>Weather Preview — Malé, Maldives</b>\n"
                                        f"{emoji} {temp}°C — {condition}"
                                        f"{island_lines}\n"
                                        f"<i>Data: {source} · Preview only, not posted to community</i>"
                                    )
                                    send_photo(chat_id, card, caption, thread_id=thread_id)
                                    log.info(f"🌤️ Weather preview sent to core team by {first_name}")
                                except Exception as e:
                                    log.error(f"/weather preview: {e}")
                                    send_text(chat_id, f"❌ Error: {e}", thread_id=thread_id)
                            threading.Thread(target=_send_weather_preview, daemon=True).start()

                        # /brief — generate the AI nightly editorial brief on demand
                        elif text.strip().lower() in ["/brief", "/journalist", "/editor"]:
                            send_text(chat_id, "🧠 Generating editorial brief from today's news... give me a moment ⏳", reply_to=msg_id, thread_id=thread_id)
                            threading.Thread(target=send_ai_journalist_brief, daemon=True).start()

                        # /pending — list all cards waiting for approval
                        elif text.strip().lower() in ["/pending", "/queue", "/list"]:
                            if not approval_queue:
                                send_text(chat_id, "📭 No cards waiting for approval right now.", reply_to=msg_id, thread_id=thread_id)
                            else:
                                lines = ["📋 <b>Cards waiting for approval:</b>\n"]
                                now_ = utcnow()
                                for k, v in approval_queue.items():
                                    age_min = int((now_ - v["created_at"]).total_seconds() / 60)
                                    lang_flag = "🇲🇻" if v["lang"] == "dv" else "🇬🇧"
                                    if v["lang"] == "en":
                                        left = max(0, 30 - age_min)
                                        timing = f"auto-posts in {left}m"
                                    else:
                                        left = max(0, 120 - age_min)
                                        timing = f"expires in {left}m"
                                    lines.append(f"🔑 <b>{k.upper()}</b> {lang_flag} — {v['title'][:55]} <i>({timing})</i>")
                                send_text(chat_id, "\n".join(lines), reply_to=msg_id, thread_id=thread_id)

                        # @SamugaNewsBot card [dhivehi text] — manual card creation
                        elif tagged and (
                            "create card and post" in clean.lower() or
                            "create card and send to community" in clean.lower() or
                            "create card and send to core team" in clean.lower() or
                            "create card and post to core team" in clean.lower() or
                            "create card and post to community" in clean.lower()
                        ):
                            log.info(f"🃏 Manual card — raw text: {repr(text[:200])}")
                            log.info(f"🃏 Manual card — photo: {bool(photo)}")
                            cl = clean.lower()
                            if "core team" in cl or "coreteam" in cl:
                                destination = "coreteam"
                            elif "community" in cl:
                                destination = "community"
                            else:
                                destination = "all"

                            # Detect category from command
                            manual_cat = "LOCAL"
                            if any(w in cl for w in ["breaking", "breaking news"]):          manual_cat = "BREAKING"
                            elif any(w in cl for w in ["political", "politics", "parliament", "government"]): manual_cat = "POLITICAL"
                            elif any(w in cl for w in ["lifestyle", "culture", "health", "tourism", "travel", "resort", "weather", "storm"]): manual_cat = "LIFESTYLE"
                            elif any(w in cl for w in ["sports", "sport", "football", "soccer"]): manual_cat = "SPORTS"
                            elif any(w in cl for w in ["world", "international", "global"]): manual_cat = "LOCAL"

                            # Extract the content text (everything before @SamugaNewsBot)
                            # The text comes from the photo caption or message, minus the command
                            raw_text = text  # original full text including caption
                            # Remove the bot mention and ALL command variants
                            # Do this BEFORE any other processing
                            cmd_variants = [
                                "create card and post to coreteam",
                                "create card and post to core team",
                                "create card and send to coreteam",
                                "create card and send to core team",
                                "create card and post to community",
                                "create card and send to community",
                                "create card and post",
                            ]
                            raw_lower = raw_text.lower()
                            for cmd in cmd_variants:
                                idx = raw_lower.find(cmd)
                                if idx != -1:
                                    raw_text = raw_text[:idx].strip()
                                    raw_lower = raw_text.lower()
                                    break
                            # Remove bot mention (anywhere in text)
                            raw_text = re.sub(r"@\w+", "", raw_text).strip()
                            raw_text = raw_text.strip()

                            if video and not photo:
                                send_text(chat_id, "Videos are not supported for cards — please send a photo instead 📸", reply_to=msg_id, thread_id=thread_id)
                            elif not raw_text and not photo:
                                send_text(chat_id, "Send a photo with caption text, or just text, then add the command at the end.", reply_to=msg_id, thread_id=thread_id)
                            else:
                                # ── Parse headline / subheading split ──────────────────
                                # Split on blank lines first, then strip category keywords
                                # from each part individually (handles Dhivehi text with
                                # English category word at the bottom correctly).
                                CAT_KWS = ["breaking news","breaking","political","politics",
                                           "sports","sport","football","soccer","lifestyle",
                                           "world","international","global","tourism","weather",
                                           "local","culture","health","travel","resort","storm"]
                                def strip_cat_kws(t):
                                    """
                                    Return empty string if this paragraph IS a category keyword
                                    (possibly with punctuation/spaces). Otherwise return unchanged.
                                    We only discard a whole paragraph that is purely a category
                                    label — never strip keywords from inside real sentences.
                                    """
                                    cleaned = t.strip().rstrip("!.,;:").strip().lower()
                                    if cleaned in CAT_KWS:
                                        return ""
                                    return t

                                raw_parts = [p.strip() for p in raw_text.split("\n\n") if p.strip()]
                                # A part that is ONLY a category keyword (after stripping) = discard
                                parts = []
                                for p in raw_parts:
                                    cleaned = strip_cat_kws(p)
                                    if cleaned:          # still has real content → keep
                                        parts.append(cleaned)
                                    # else: it was just "Breaking" or "Sports" → discard silently

                                has_thaana_input = any('\u0780'<=c<='\u07bf' for c in raw_text)
                                SUBHEAD_CARD_LIMIT = 80 if has_thaana_input else 150
                                if len(parts) >= 2:
                                    card_headline = parts[0]
                                    card_subhead  = " ".join(parts[1:])  # everything after first blank line
                                    if len(card_subhead) <= SUBHEAD_CARD_LIMIT:
                                        # Fits on card — pass as one block with newline so
                                        # generate_card renders it as headline + smaller body.
                                        # We use ". " trick for English, space for Dhivehi path.
                                        if has_thaana_input:
                                            content_text = card_headline + " " + card_subhead
                                        else:
                                            content_text = card_headline.rstrip(".") + ". " + card_subhead
                                        caption_subhead = ""   # already on card, not needed in caption
                                    else:
                                        # Too long — card gets headline only, subhead goes to caption
                                        content_text  = card_headline
                                        caption_subhead = card_subhead
                                else:
                                    # No blank line = just headline, no subhead
                                    content_text    = raw_text
                                    caption_subhead = ""

                                content_text = content_text or "Samuga Media"
                                try:
                                    send_text(chat_id, "⏳ Creating card...", thread_id=thread_id)

                                    # Use uploaded photo as background if available
                                    if photo:
                                        bg = download_telegram_photo(photo)
                                        log.info("🖼️ Using uploaded photo as card background")
                                    else:
                                        bg = fetch_background_image(None, cat=manual_cat)

                                    ts_now = (utcnow() + timedelta(hours=5)).strftime("%d %b %Y • %H:%M")
                                    card = generate_card(content_text, "Samuga Media", ts_now, manual_cat, bg)
                                    cat_emoji = {"BREAKING":"🚨","LOCAL":"🇲🇻","POLITICAL":"🏛️","LIFESTYLE":"🌴","SPORTS":"🏅","FOOTBALL":"⚽","DISASTER":"🚨","WORLD":"🌍","WEATHER":"🌤️","TOURISM":"✈️"}.get(manual_cat,"📰")
                                    breaking_prefix = "🚨 <b>BREAKING NEWS</b>\n\n" if manual_cat in ["BREAKING", "DISASTER"] else ""
                                    # Caption: headline (always) + subhead if it didn't fit on card
                                    caption_body = card_headline if len(parts) >= 2 else content_text
                                    if caption_subhead:
                                        caption_body = caption_body + "\n\n" + caption_subhead
                                    full_caption = (
                                        breaking_prefix + cat_emoji + " " + caption_body + "\n\n"
                                        "📡 <b>Samuga Media</b> | @samugacommunity"
                                    )

                                    posted = []
                                    _social_fired = False

                                    if destination == "community":
                                        card.seek(0)
                                        if send_to_telegram(card, full_caption):
                                            posted.append("Community ✅")

                                    elif destination == "coreteam":
                                        card.seek(0)
                                        if send_photo(CORE_TEAM_CHAT_ID, card, full_caption, thread_id=CONTENT_LAB_THREAD_ID):
                                            posted.append("Content Lab ✅")

                                    elif destination == "all":
                                        # ── PREVIEW + CONFIRM gate ────────────────────────
                                        # Do NOT post anywhere public yet.
                                        # Send the card as a PREVIEW to the core team only,
                                        # then wait for /confirm (posts everywhere) or /cancel.
                                        card_bytes_stored = card.getvalue()
                                        _pending_manual_post.clear()
                                        _pending_manual_post.update({
                                            "card_bytes":   card_bytes_stored,
                                            "full_caption": full_caption,
                                            "chat_id":      chat_id,
                                            "thread_id":    thread_id,
                                            "first_name":   first_name,
                                            "created_at":   utcnow(),
                                        })
                                        # Send preview card to core team
                                        preview = io.BytesIO(card_bytes_stored)
                                        preview_caption = (
                                            f"👀 <b>PREVIEW — not posted yet</b>\n\n"
                                            f"{full_caption}\n\n"
                                            f"━━━━━━━━━━━━━━\n"
                                            f"📲 This will post to <b>Telegram Community + Facebook + Instagram + X</b>.\n"
                                            f"✅ <code>/confirm</code> to post everywhere\n"
                                            f"❌ <code>/cancel</code> to discard"
                                        )
                                        send_photo(chat_id, preview, preview_caption, thread_id=thread_id)
                                        log.info(f"🃏 Manual card PREVIEW sent to core team by {first_name} — awaiting /confirm")
                                        _social_fired = True  # block fallthrough

                                    if not _social_fired:
                                        if posted:
                                            send_text(chat_id, "✅ Posted to: " + ", ".join(posted), reply_to=msg_id, thread_id=thread_id)
                                            log.info(f"✅ Manual card posted to: {posted}")
                                        else:
                                            send_text(chat_id, "❌ Failed to post.", reply_to=msg_id, thread_id=thread_id)

                                except Exception as e:
                                    log.error(f"Manual card: {e}")
                                    send_text(chat_id, f"❌ Error: {e}", reply_to=msg_id, thread_id=thread_id)

                        # /read command — store context for this session
                        elif text.strip().lower().startswith("/read"):
                            context_text = text.strip()[5:].strip()
                            if context_text:
                                core_team_session_context[chat_id] = context_text
                                send_text(chat_id, "Got it! I have read that and will use it as context for this session 📖", reply_to=msg_id, thread_id=thread_id)
                                log.info(f"📖 Session context stored: {context_text[:60]}...")
                            else:
                                send_text(chat_id, "Send it like this: /read [paste your content here]", reply_to=msg_id, thread_id=thread_id)

                        # /confirm — post pending preview card EVERYWHERE (Telegram + FB + IG + X)
                        elif text.strip().lower() in ["/confirm"]:
                            if not _pending_manual_post:
                                send_text(chat_id,
                                    "Nothing waiting to confirm. "
                                    "Use <code>create card and post</code> first.",
                                    reply_to=msg_id, thread_id=thread_id)
                            else:
                                age = (utcnow() - _pending_manual_post["created_at"]).total_seconds()
                                if age > 600:
                                    _pending_manual_post.clear()
                                    send_text(chat_id,
                                        "⏰ That preview expired (10 min window). "
                                        "Create a new one with <code>create card and post</code>.",
                                        reply_to=msg_id, thread_id=thread_id)
                                else:
                                    try:
                                        cap = _pending_manual_post["full_caption"]
                                        cbytes = _pending_manual_post["card_bytes"]
                                        send_text(chat_id, "🚀 Posting to all platforms... ⏳",
                                                  reply_to=msg_id, thread_id=thread_id)

                                        done = []
                                        # 1) Telegram community
                                        tg_buf = io.BytesIO(cbytes)
                                        if send_to_telegram(tg_buf, cap):
                                            done.append("Telegram ✅")

                                        # 2) Socials (FB + IG + Twitter) in background
                                        social_buf = io.BytesIO(cbytes)
                                        threading.Thread(
                                            target=post_to_social,
                                            args=(social_buf, cap),
                                            daemon=True
                                        ).start()
                                        done.append("FB + IG + X ✅")

                                        _pending_manual_post.clear()
                                        send_text(chat_id,
                                            f"✅ <b>Confirmed by {first_name}</b>\n"
                                            f"📡 Posted to: {', '.join(done)}",
                                            reply_to=msg_id, thread_id=thread_id)
                                        log.info(f"✅ Manual card confirmed by {first_name} — posted everywhere")
                                    except Exception as e:
                                        log.error(f"/confirm: {e}")
                                        send_text(chat_id, f"❌ Error posting: {e}",
                                                  reply_to=msg_id, thread_id=thread_id)

                        # /cancel — discard the pending preview card
                        elif text.strip().lower() in ["/cancel"]:
                            if not _pending_manual_post:
                                send_text(chat_id, "Nothing to cancel.",
                                          reply_to=msg_id, thread_id=thread_id)
                            else:
                                _pending_manual_post.clear()
                                send_text(chat_id,
                                    f"❌ <b>Cancelled by {first_name}</b> — card discarded, nothing posted.",
                                    reply_to=msg_id, thread_id=thread_id)
                                log.info(f"❌ Manual card cancelled by {first_name}")

                        # Respond only when directly tagged — no more jumping in proactively
                        elif tagged:
                            if not clean: clean = text.strip()
                            log.info(f"🧠 Core team {'[tagged]' if tagged else '[proactive]'} {display_name}: {clean[:50]}")
                            session_ctx = core_team_session_context.get(chat_id, "")

                            if is_dhivehi(clean):
                                headlines = get_local_headlines()
                                ctx = "\n".join(headlines[:5]) if headlines else ""
                                reply = chat_with_gemini_dhivehi(clean, ctx, history)
                                if not reply:
                                    reply = chat_with_coreteam(clean, display_name, sender_info, history, session_ctx)
                            else:
                                reply = chat_with_coreteam(clean, display_name, sender_info, history, session_ctx)

                            if reply:
                                add_to_conversation(user_id, "user", clean)
                                add_to_conversation(user_id, "assistant", reply)
                                send_text(chat_id, reply, reply_to=msg_id if tagged else None, thread_id=thread_id)

                    # Regular group — only respond when tagged
                    elif tagged and clean:
                        log.info(f"💬 Group {display_name}: {clean[:50]}")
                        if is_dhivehi(clean):
                            log.info("🇲🇻 Dhivehi group mention — using Gemini")
                            headlines = get_local_headlines()
                            context = "\n".join(headlines[:5]) if headlines else ""
                            history = get_conversation(user_id)
                            reply = chat_with_gemini_dhivehi(clean, context, history)
                            if reply:
                                add_to_conversation(user_id, "user", clean)
                                add_to_conversation(user_id, "assistant", reply)
                            else:
                                reply = chat_with_claude(clean, user_id)
                        else:
                            reply = chat_with_claude(clean, user_id)
                        send_text(chat_id, reply, reply_to=msg_id, thread_id=thread_id)
        except Exception as e:
            log.error(f"Update loop: {e}"); time.sleep(5)

# ── Entry ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("🚀 Samuga News Bot v6.2 starting (archive + learning + FB/IG insights)...")
    # Install Noto fonts for Thaana/Dhivehi support
    if not os.path.exists("/usr/share/fonts/truetype/noto/NotoSansThaana-Bold.ttf") and not os.path.exists("/app/NotoSansThaana-Bold.ttf"):
        try:
            import subprocess
            subprocess.run(["apt-get", "install", "-y", "fonts-noto"], capture_output=True, timeout=60)
            log.info("✅ Noto fonts installed via apt")
        except Exception as e:
            log.warning(f"Noto font install failed: {e}")
    else:
        log.info("✅ Thaana fonts available")
    log.info("📅 6AM-10PM: every 15min | Night: breaking only | v4 strategy")
    log.info("🌅 7AM Brief | 🌙 12AM Summary | 🌤️ 8AM/8PM Weather | 📊 Friday Digest")
    log.info("💬 Smart chat with history, Tavily search, Dhivehi support")

    init_database()  # connect to Postgres (falls back to JSON if unavailable)
    restore_state()  # bring back dedup memory, daily counters, pending cards, analytics
    seen_on_start=load_seen()
    log.info(f"📚 Loaded {len(seen_on_start)} seen articles")

    threading.Thread(target=handle_updates, daemon=True).start()

    scheduler=BlockingScheduler(timezone="UTC")
    scheduler.add_job(scheduled_check, "interval", minutes=15)
    # Breaking news fast check every 5 min (LOCAL/DISASTER only)
    scheduler.add_job(breaking_news_check, "interval", minutes=5)
    # Approval lifecycle — English auto-posts at 15min, Dhivehi expires at 2h. Check every 5 min.
    scheduler.add_job(expire_old_approvals, "interval", minutes=5)
    # Morning brief 7AM MVT = 2AM UTC
    scheduler.add_job(send_morning_brief, "cron", hour=1, minute=0)  # 6AM MVT
    # AI Nightly Journalist brief 10:30PM MVT = 5:30PM UTC (before night summary)
    scheduler.add_job(send_ai_journalist_brief, "cron", hour=17, minute=30)  # 10:30PM MVT
    # Night summary 12AM MVT = 7PM UTC
    scheduler.add_job(send_night_summary, "cron", hour=18, minute=0)  # 11PM MVT
    # Weekly digest Friday 6PM MVT = 1PM UTC Friday
    scheduler.add_job(send_weekly_digest, "cron", day_of_week="fri", hour=13, minute=0)
    # Weekly analytics report Friday 6:30PM MVT = 1:30PM UTC Friday
    scheduler.add_job(send_weekly_analytics, "cron", day_of_week="fri", hour=13, minute=30)
    # Phase 2: mid-week view backfill — Tue 10PM UTC = 3AM Wed MVT (quiet hours)
    scheduler.add_job(backfill_tg_views, "cron", day_of_week="tue", hour=22, minute=0)
    # Phase 2.5: mid-week Meta (FB+IG) engagement refresh — Tue 10PM UTC too
    scheduler.add_job(fetch_meta_insights, "cron", day_of_week="tue", hour=22, minute=15)
    # Weather update 8AM MVT = 3AM UTC
    scheduler.add_job(lambda: send_weather_update("morning"), "cron", hour=3, minute=0)
    # Weather update 8PM MVT = 3PM UTC
    scheduler.add_job(lambda: send_weather_update("evening"), "cron", hour=15, minute=0)
    # Tip/story CTA 8:30AM MVT = 3:30AM UTC
    scheduler.add_job(send_tip_cta, "cron", hour=3, minute=30)  # 8:30AM MVT
    # Tip/story CTA 8:30PM MVT = 3:30PM UTC
    scheduler.add_job(send_tip_cta, "cron", hour=15, minute=30)  # 8:30PM MVT

    log.info("⏰ Scheduler started!")
    scheduler.start()
