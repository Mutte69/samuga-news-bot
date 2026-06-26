
# ─────────────────────────────────────────────────────────────────────────────
# SAMUGA WEBSITE + PUBLIC CHAT PATCH
# Paste this block near your Flask api_app routes, after api_app is created
# and after db_execute(), chat_with_claude(), and Tavily/web search helpers exist.
# ─────────────────────────────────────────────────────────────────────────────

from datetime import datetime, timedelta
from flask import request, jsonify
import re, time, os

WEBSITE_PUBLIC_STATUSES = ("posted", "published", "website", "website_live", "social_posted", "approved")
PUBLIC_CHAT_RATE = {}
PUBLIC_CHAT_WINDOW_SECONDS = 600
PUBLIC_CHAT_MAX_MESSAGES = 12

def _now_utc():
    return datetime.utcnow()

def _clean_public_text(text, limit=900):
    text = str(text or "").strip()
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = text.replace("__", "").replace("###", "").replace("##", "").replace("#", "")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "..."
    return text.strip()

def _detect_lang_text(title="", summary=""):
    text = f"{title or ''} {summary or ''}"
    return "dv" if re.search(r"[\u0780-\u07BF]", text) else "en"

def _website_status_for_story(title="", summary="", lang=None, approved=False):
    lang = lang or _detect_lang_text(title, summary)
    # English goes to website immediately, even if sent to Content Lab.
    if lang == "en":
        return "posted"
    # Dhivehi only becomes website-visible after approval/public post.
    return "posted" if approved else "pending_approval"

def publish_story_to_website_archive(title, summary="", category="LOCAL", source="Samuga Media",
                                     link="#", lang=None, approved=False, status=None,
                                     posted_at=None, article_id=None):
    """
    Single truth function:
    - English: website immediately
    - Dhivehi: website only after approval/public post
    Call this whenever a story is selected for Content Lab, approved, auto-posted,
    or sent to Telegram/social.
    """
    try:
        title = str(title or "").strip()
        if not title:
            return False

        summary = str(summary or "").strip()
        source = str(source or "Samuga Media").strip()
        category = str(category or "LOCAL").strip().upper()
        link = str(link or "#").strip()
        lang = lang or _detect_lang_text(title, summary)

        final_status = status or _website_status_for_story(title, summary, lang=lang, approved=approved)
        ts = posted_at or _now_utc()

        # Use title+source as safe upsert key when no article_id.
        if article_id:
            db_execute("""
                INSERT INTO articles (id, title, summary, category, source, link, lang, status, posted_at, found_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET
                    title=EXCLUDED.title,
                    summary=EXCLUDED.summary,
                    category=EXCLUDED.category,
                    source=EXCLUDED.source,
                    link=EXCLUDED.link,
                    lang=EXCLUDED.lang,
                    status=EXCLUDED.status,
                    posted_at=COALESCE(EXCLUDED.posted_at, articles.posted_at, articles.found_at),
                    found_at=COALESCE(articles.found_at, EXCLUDED.found_at)
            """, (article_id, title, summary, category, source, link, lang, final_status, ts, ts))
        else:
            # If your articles table has no unique constraint on title/source, this still works with update-then-insert.
            existing = db_execute("""
                SELECT id FROM articles
                WHERE title = %s AND source = %s
                ORDER BY found_at DESC NULLS LAST
                LIMIT 1
            """, (title, source), fetch="one")

            if existing:
                row_id = existing[0] if not isinstance(existing, dict) else existing.get("id")
                db_execute("""
                    UPDATE articles
                    SET summary=%s, category=%s, source=%s, link=%s, lang=%s, status=%s,
                        posted_at=COALESCE(%s, posted_at, found_at),
                        found_at=COALESCE(found_at, %s)
                    WHERE id=%s
                """, (summary, category, source, link, lang, final_status, ts, ts, row_id))
            else:
                db_execute("""
                    INSERT INTO articles (title, summary, category, source, link, lang, status, posted_at, found_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (title, summary, category, source, link, lang, final_status, ts, ts))

        log.info(f"🌐 Website archive updated: {lang}/{final_status} — {title[:70]}")
        return True
    except Exception as e:
        log.error(f"Website archive save failed: {e}")
        return False

def publish_item_to_website_archive(item, approved=False, status=None):
    """
    Helper for dict/card/article objects.
    Call this with your article/card dict.
    """
    if not item:
        return False
    return publish_story_to_website_archive(
        title=item.get("title") or item.get("headline") or item.get("text") or "",
        summary=item.get("summary") or item.get("caption") or item.get("desc") or "",
        category=item.get("category") or item.get("cat") or "LOCAL",
        source=item.get("source") or item.get("source_name") or "Samuga Media",
        link=item.get("url") or item.get("link") or "#",
        lang=item.get("lang") or item.get("language"),
        approved=approved,
        status=status,
        posted_at=item.get("posted_at") or item.get("time") or None,
        article_id=item.get("id") or item.get("article_id") or None
    )

# IMPORTANT HOOKS TO ADD IN YOUR EXISTING CODE:
# 1) When English card is sent to Content Lab:
#       publish_item_to_website_archive(article_or_card, approved=True, status="posted")
#
# 2) When Dhivehi card is sent to Content Lab but not approved:
#       publish_item_to_website_archive(article_or_card, approved=False, status="pending_approval")
#
# 3) When Dhivehi card is approved/publicly posted:
#       publish_item_to_website_archive(article_or_card, approved=True, status="posted")
#
# 4) When anything is auto-posted as breaking/high confidence:
#       publish_item_to_website_archive(article_or_card, approved=True, status="posted")


@api_app.get("/api/stories")
def api_stories():
    try:
        rows = db_execute("""
            SELECT id, title, summary, category, source, link, lang, status, posted_at, found_at
            FROM articles
            WHERE status = ANY(%s)
            ORDER BY COALESCE(posted_at, found_at) DESC NULLS LAST
            LIMIT 80
        """, (list(WEBSITE_PUBLIC_STATUSES),), fetch="all")

        stories = []
        for r in rows or []:
            if isinstance(r, dict):
                row = r
            else:
                row = {
                    "id": r[0], "title": r[1], "summary": r[2], "category": r[3],
                    "source": r[4], "link": r[5], "lang": r[6], "status": r[7],
                    "posted_at": r[8], "found_at": r[9]
                }

            dt = row.get("posted_at") or row.get("found_at")
            time_txt = dt.strftime("%d %b %Y • %H:%M") if hasattr(dt, "strftime") else str(dt or "Recent")
            stories.append({
                "id": row.get("id"),
                "title": row.get("title") or "Untitled",
                "summary": row.get("summary") or "",
                "category": row.get("category") or "LOCAL",
                "source": row.get("source") or "Samuga Media",
                "url": row.get("link") or "#",
                "lang": row.get("lang") or _detect_lang_text(row.get("title"), row.get("summary")),
                "status": row.get("status") or "posted",
                "time": time_txt
            })

        return jsonify(stories)
    except Exception as e:
        log.error(f"Website API /api/stories error: {e}")
        return jsonify([]), 200


def _public_chat_rate_limited(ip):
    now = time.time()
    bucket = PUBLIC_CHAT_RATE.setdefault(ip, [])
    bucket[:] = [t for t in bucket if now - t < PUBLIC_CHAT_WINDOW_SECONDS]
    if len(bucket) >= PUBLIC_CHAT_MAX_MESSAGES:
        return True
    bucket.append(now)
    return False

def _is_news_question(msg):
    m = msg.lower()
    return any(k in m for k in [
        "news", "latest", "breaking", "today", "headline", "update", "maldives",
        "politics", "business", "sport", "weather", "ޚަބަރ", "އަޕްޑޭޓް"
    ])

def _latest_context_from_db(limit=8):
    rows = db_execute("""
        SELECT title, summary, category, source, link, lang, posted_at, found_at
        FROM articles
        WHERE status = ANY(%s)
        ORDER BY COALESCE(posted_at, found_at) DESC NULLS LAST
        LIMIT %s
    """, (list(WEBSITE_PUBLIC_STATUSES), limit), fetch="all")
    lines = []
    for r in rows or []:
        if isinstance(r, dict):
            title, summary, cat, source, link = r.get("title"), r.get("summary"), r.get("category"), r.get("source"), r.get("link")
        else:
            title, summary, cat, source, link = r[0], r[1], r[2], r[3], r[4]
        lines.append(f"- [{cat}] {title} — {summary} Source: {source}. Link: {link}")
    return "\n".join(lines)

def _tavily_public_search(query):
    """
    Uses Tavily if your existing bot has tavily_search/web_search helper or TAVILY_API_KEY.
    Safe fallback returns empty string.
    """
    try:
        if "tavily_search" in globals():
            return str(tavily_search(query))
        if "web_search" in globals():
            return str(web_search(query))
        key = os.environ.get("TAVILY_API_KEY")
        if not key:
            return ""
        import requests
        res = requests.post(
            "https://api.tavily.com/search",
            json={"api_key": key, "query": query, "search_depth": "basic", "max_results": 5},
            timeout=12
        )
        data = res.json()
        results = data.get("results", [])
        return "\n".join([f"- {x.get('title')}: {x.get('content')} ({x.get('url')})" for x in results[:5]])
    except Exception as e:
        log.warning(f"Tavily public search failed: {e}")
        return ""

@api_app.post("/api/chat")
def api_public_chat():
    try:
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
        if _public_chat_rate_limited(ip):
            return jsonify({"reply": "Bro too many messages too fast 😅 Try again in a few minutes."}), 429

        data = request.get_json(silent=True) or {}
        msg = str(data.get("message") or "").strip()
        lang = str(data.get("lang") or "").strip().lower() or _detect_lang_text(msg, "")
        if not msg:
            return jsonify({"reply": "Ask me anything about Maldives news bro."})

        blocked = ["approve", "reject", "post this", "content lab", "admin", "token", "password", "/approve", "/reject"]
        if any(b in msg.lower() for b in blocked):
            return jsonify({"reply": "That part is private for the Samuga team bro. I can help with public news and questions here."})

        db_context = _latest_context_from_db(limit=8) if _is_news_question(msg) else ""
        live_context = _tavily_public_search(f"Maldives news {msg}") if _is_news_question(msg) else _tavily_public_search(msg)

        prompt = f"""
You are Samuga AI, the public Telegram-style chatbot for Samuga Media.
Talk naturally, friendly, and helpful. You can say bro sometimes, but don't overdo it.
Do not sound hard-coded. Do not use markdown bold symbols like **.
Keep website chat answers short: 2 to 5 short paragraphs or max 3 bullets unless user asks for more.
For latest/breaking news, use Samuga database first, then Tavily live search if useful.
Never reveal private admin/core-team/content-lab controls.

User language hint: {lang}
User message: {msg}

Fresh Samuga website DB context:
{db_context}

Live Tavily/search context:
{live_context}

Answer now in the user's language/style.
"""

        if "chat_with_claude" in globals():
            reply = chat_with_claude(prompt, user_id=f"website:{ip}")
        elif "ask_claude" in globals():
            reply = ask_claude(prompt)
        else:
            # Fallback if AI helper name differs
            reply = "I’m online bro. I can help with Maldives news, but my AI brain function name needs to be connected in bot.py."

        return jsonify({"reply": _clean_public_text(reply, limit=1200)})
    except Exception as e:
        log.error(f"Website /api/chat error: {e}")
        return jsonify({"reply": "Samuga AI had a small issue bro. Try again in a moment."}), 200
