"""PostgreSQL database layer and fallbacks."""
import json
from typing import Optional, Any, List, Tuple
from config import DATABASE_URL, log, SAMUGA_PUBLIC_LINK, SAMUGA_PUBLIC_SOURCE, canonical_category
from utils import strip_source_links, match_key, clean_text

_db_pool = None
DB_ENABLED = False


def init_database():
    global _db_pool, DB_ENABLED
    if not DATABASE_URL:
        log.warning("🗄️ No DATABASE_URL — DB disabled, JSON fallback only")
        DB_ENABLED = False
        return
    try:
        from psycopg2 import pool as pgpool
        url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        _db_pool = pgpool.SimpleConnectionPool(1, 8, dsn=url)
        conn = _db_pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS articles (
                        id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        summary TEXT,
                        link TEXT,
                        source TEXT,
                        category TEXT,
                        lang TEXT,
                        score INTEGER DEFAULT 0,
                        reliability INTEGER DEFAULT 0,
                        confidence TEXT DEFAULT 'low',
                        is_breaking BOOLEAN DEFAULT FALSE,
                        status TEXT DEFAULT 'seen',
                        found_at TIMESTAMPTZ DEFAULT NOW(),
                        published_at TIMESTAMPTZ,
                        posted_at TIMESTAMPTZ,
                        tg_message_id BIGINT,
                        match_key TEXT,
                        article_slug TEXT,
                        article_excerpt TEXT,
                        article_body TEXT,
                        article_generated_at TIMESTAMPTZ,
                        meta JSONB DEFAULT '{}'::jsonb
                    );
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_articles_found_at ON articles(found_at);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(status);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_articles_category ON articles(category);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_articles_matchkey ON articles(match_key);")
                for col_sql in [
                    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS confidence TEXT DEFAULT 'low'",
                    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ",
                    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS meta JSONB DEFAULT '{}'::jsonb",
                    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS article_slug TEXT",
                    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS article_excerpt TEXT",
                    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS article_body TEXT",
                    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS article_generated_at TIMESTAMPTZ",
                ]:
                    cur.execute(col_sql)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS bot_kv (
                        key TEXT PRIMARY KEY,
                        value JSONB,
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS learning (
                        id SERIAL PRIMARY KEY,
                        article_id TEXT,
                        action TEXT,
                        member TEXT,
                        category TEXT,
                        source TEXT,
                        score INTEGER,
                        theme TEXT,
                        original_caption TEXT,
                        final_caption TEXT,
                        lang TEXT,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS public_chat_messages (
                        id SERIAL PRIMARY KEY,
                        platform TEXT,
                        session_id TEXT,
                        user_key TEXT,
                        user_message TEXT,
                        bot_reply TEXT,
                        lang TEXT,
                        intent TEXT,
                        topics TEXT[],
                        used_search BOOLEAN DEFAULT FALSE,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS public_interest_daily (
                        day DATE,
                        topic TEXT,
                        platform TEXT,
                        count INTEGER DEFAULT 0,
                        updated_at TIMESTAMPTZ DEFAULT NOW(),
                        PRIMARY KEY(day, topic, platform)
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS source_health (
                        source TEXT,
                        method TEXT,
                        ok BOOLEAN,
                        count INTEGER DEFAULT 0,
                        note TEXT,
                        checked_at TIMESTAMPTZ DEFAULT NOW(),
                        PRIMARY KEY(source, method)
                    );
                """)
            conn.commit()
        finally:
            _db_pool.putconn(conn)
        DB_ENABLED = True
        log.info("🗄️ ✅ PostgreSQL connected — modular archive active")
    except Exception as e:
        DB_ENABLED = False
        log.error(f"🗄️ Database init failed: {e}")


def db_execute(query: str, params: tuple = None, fetch: Optional[str] = None):
    if not DB_ENABLED or not _db_pool:
        return None
    conn = None
    try:
        conn = _db_pool.getconn()
        with conn.cursor() as cur:
            cur.execute(query, params or ())
            if fetch == "one":
                res = cur.fetchone()
            elif fetch == "all":
                res = cur.fetchall()
            else:
                res = None
        conn.commit()
        return res
    except Exception as e:
        log.error(f"🗄️ db_execute: {e}")
        try:
            if conn: conn.rollback()
        except Exception:
            pass
        return None
    finally:
        if conn and _db_pool:
            _db_pool.putconn(conn)


def kv_get(key: str, default=None):
    row = db_execute("SELECT value FROM bot_kv WHERE key=%s", (key,), fetch="one")
    return row[0] if row and row[0] is not None else default


def kv_set(key: str, value: Any):
    db_execute("""
        INSERT INTO bot_kv(key, value, updated_at) VALUES(%s, %s::jsonb, NOW())
        ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
    """, (key, json.dumps(value)))


def record_source_health(source: str, method: str, ok: bool, count: int = 0, note: str = ""):
    db_execute("""
        INSERT INTO source_health(source, method, ok, count, note, checked_at)
        VALUES(%s,%s,%s,%s,%s,NOW())
        ON CONFLICT(source, method) DO UPDATE SET ok=EXCLUDED.ok, count=EXCLUDED.count,
        note=EXCLUDED.note, checked_at=NOW()
    """, (source, method, bool(ok), int(count or 0), str(note or "")[:300]))


def get_source_health(limit: int = 60):
    return db_execute("""
        SELECT source, method, ok, count, note, checked_at FROM source_health
        ORDER BY checked_at DESC LIMIT %s
    """, (limit,), fetch="all") or []


def record_article(article, status="seen"):
    if not article or not article.title:
        return
    db_execute("""
        INSERT INTO articles(id,title,summary,link,source,category,lang,score,reliability,confidence,is_breaking,status,published_at,match_key,meta)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
        ON CONFLICT(id) DO UPDATE SET
            title=COALESCE(NULLIF(EXCLUDED.title,''), articles.title),
            summary=COALESCE(NULLIF(EXCLUDED.summary,''), articles.summary),
            link=COALESCE(NULLIF(EXCLUDED.link,''), articles.link),
            source=COALESCE(NULLIF(EXCLUDED.source,''), articles.source),
            category=COALESCE(NULLIF(EXCLUDED.category,''), articles.category),
            lang=COALESCE(NULLIF(EXCLUDED.lang,''), articles.lang),
            score=GREATEST(COALESCE(articles.score,0), COALESCE(EXCLUDED.score,0)),
            reliability=GREATEST(COALESCE(articles.reliability,0), COALESCE(EXCLUDED.reliability,0)),
            confidence=CASE WHEN EXCLUDED.confidence IN ('high','medium') THEN EXCLUDED.confidence ELSE articles.confidence END,
            is_breaking=COALESCE(EXCLUDED.is_breaking, articles.is_breaking),
            status=CASE WHEN articles.status IN ('posted','published','social_posted') AND EXCLUDED.status IN ('seen','queued') THEN articles.status ELSE EXCLUDED.status END,
            published_at=COALESCE(articles.published_at, EXCLUDED.published_at),
            match_key=COALESCE(NULLIF(EXCLUDED.match_key,''), articles.match_key),
            meta=COALESCE(articles.meta, '{}'::jsonb) || COALESCE(EXCLUDED.meta, '{}'::jsonb)
    """, (
        article.id, strip_source_links(article.title)[:500], strip_source_links(article.summary)[:3000], article.link,
        article.source, canonical_category(article.cat, article.title, article.summary), article.lang,
        int(article.score or 0), int(article.reliability or 0), article.confidence,
        bool(article.is_breaking), status, article.published_at, match_key(article.title), json.dumps(article.meta or {})
    ))


def mark_status(article_id: str, status: str, posted: bool = False, tg_message_id: int = None):
    if not article_id:
        return
    if posted:
        db_execute("UPDATE articles SET status=%s, posted_at=NOW(), tg_message_id=COALESCE(%s,tg_message_id) WHERE id=%s", (status, tg_message_id, article_id))
    else:
        db_execute("""
            UPDATE articles SET status=CASE WHEN status IN ('posted','published','social_posted') AND %s IN ('seen','queued') THEN status ELSE %s END WHERE id=%s
        """, (status, status, article_id))


def make_slug(title: str, article_id: str = "") -> str:
    import re, unicodedata
    t = strip_source_links(title or "samuga-story").lower()
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode("ascii")
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-") or "samuga-story"
    return f"{t[:70]}-{str(article_id)[:8]}"


def publish_article_for_website(article, body: str = ""):
    if not article:
        return
    title = strip_source_links(article.title)[:500]
    summary = clean_text(article.summary or title, 2600)
    excerpt = summary[:280].rsplit(" ", 1)[0] + "..." if len(summary) > 280 else summary
    slug = make_slug(title, article.id)
    db_execute("""
        INSERT INTO articles(id,title,summary,link,source,category,lang,score,reliability,confidence,is_breaking,status,posted_at,published_at,match_key,article_slug,article_excerpt,article_body,article_generated_at,meta)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'posted',NOW(),%s,%s,%s,%s,%s,NOW(),%s::jsonb)
        ON CONFLICT(id) DO UPDATE SET
            title=EXCLUDED.title, summary=EXCLUDED.summary, link=EXCLUDED.link, source=EXCLUDED.source,
            category=EXCLUDED.category, lang=EXCLUDED.lang,
            score=GREATEST(COALESCE(articles.score,0), COALESCE(EXCLUDED.score,0)),
            reliability=GREATEST(COALESCE(articles.reliability,0), COALESCE(EXCLUDED.reliability,0)),
            confidence=EXCLUDED.confidence, is_breaking=EXCLUDED.is_breaking, status='posted',
            posted_at=COALESCE(articles.posted_at,NOW()), published_at=COALESCE(articles.published_at, EXCLUDED.published_at),
            match_key=EXCLUDED.match_key, article_slug=COALESCE(articles.article_slug, EXCLUDED.article_slug),
            article_excerpt=EXCLUDED.article_excerpt, article_body=COALESCE(NULLIF(EXCLUDED.article_body,''), articles.article_body),
            article_generated_at=COALESCE(articles.article_generated_at, NOW()), meta=COALESCE(articles.meta,'{}'::jsonb)||COALESCE(EXCLUDED.meta,'{}'::jsonb)
    """, (
        article.id, title, summary, SAMUGA_PUBLIC_LINK, SAMUGA_PUBLIC_SOURCE,
        canonical_category(article.cat, title, summary), article.lang, article.score, article.reliability,
        article.confidence, article.is_breaking, article.published_at, match_key(title), slug, excerpt,
        body or summary, json.dumps(article.meta or {})
    ))


def recent_articles(limit=50, lang=None):
    where = "status IN ('posted','published','social_posted')"
    params: List[Any] = []
    if lang:
        where += " AND lang=%s"
        params.append(lang)
    rows = db_execute(f"""
        SELECT id,title,summary,category,lang,source,score,is_breaking,COALESCE(posted_at,found_at),article_excerpt,article_body
        FROM articles WHERE {where}
        ORDER BY COALESCE(posted_at,found_at) DESC NULLS LAST LIMIT %s
    """, tuple(params + [limit]), fetch="all") or []
    return rows


def get_article(article_id: str):
    return db_execute("""
        SELECT id,title,summary,category,lang,source,score,is_breaking,COALESCE(posted_at,found_at),article_excerpt,article_body,article_slug
        FROM articles WHERE id=%s
    """, (article_id,), fetch="one")


def story_search(query: str, limit: int = 6):
    q = f"%{query.lower()}%"
    return db_execute("""
        SELECT title, summary, category, COALESCE(posted_at, found_at) FROM articles
        WHERE status IN ('posted','published','social_posted') AND (LOWER(title) LIKE %s OR LOWER(summary) LIKE %s)
        ORDER BY COALESCE(posted_at,found_at) DESC LIMIT %s
    """, (q, q, limit), fetch="all") or []


def log_learning(article_id, action, member="", category="", source="", score=0, original_caption="", final_caption="", lang="en"):
    db_execute("""
        INSERT INTO learning(article_id,action,member,category,source,score,original_caption,final_caption,lang)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (article_id, action, member[:80], category[:40], source[:100], int(score or 0), original_caption[:1200], final_caption[:1200], lang))


def log_public_chat(platform, session_id, user_key, user_message, bot_reply, lang, intent, topics, used_search=False):
    db_execute("""
        INSERT INTO public_chat_messages(platform,session_id,user_key,user_message,bot_reply,lang,intent,topics,used_search)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (platform, session_id, user_key, user_message[:3000], bot_reply[:5000], lang, intent, topics or [], bool(used_search)))
    for topic in topics or [intent or "general"]:
        db_execute("""
            INSERT INTO public_interest_daily(day,topic,platform,count,updated_at)
            VALUES(CURRENT_DATE,%s,%s,1,NOW())
            ON CONFLICT(day,topic,platform) DO UPDATE SET count=public_interest_daily.count+1, updated_at=NOW()
        """, (topic, platform))


def public_interest(days: int = 1, limit: int = 20):
    return db_execute("""
        SELECT topic, platform, SUM(count)::int FROM public_interest_daily
        WHERE day >= CURRENT_DATE - %s::int
        GROUP BY topic, platform ORDER BY SUM(count) DESC LIMIT %s
    """, (days, limit), fetch="all") or []
