"""
═══════════════════════════════════════════════════════════════════════════════
  SAMUGA AI  —  Maldivian AI-Powered Newsroom Bot
  Version: 7.0  |  github.com/samuga-news-bot  |  Railway + PostgreSQL
═══════════════════════════════════════════════════════════════════════════════

  WHAT THIS FILE CONTAINS (search these tags to jump to a section):

    [CONFIG]      Environment vars, feeds, constants          (top of file)
    [DATABASE]    PostgreSQL: articles, stories, memory, kv
    [MODELS]      Article dataclass + helpers
    [FETCHERS]    RSS, Google News, MvCrisis scraping
    [SCORING]     Article scoring, dedup, clustering, reliability
    [STORIES]     Story Intelligence — timeline threads
    [AI]          Claude rewrite, Gemini Dhivehi, core-team brain
    [CARDS]       Pillow card generation (news + weather)
    [WEATHER]     Tomorrow.io, prayer times, Hijri, MMS alerts
    [PUBLISHING]  Telegram, Buffer, Meta Graph API
    [COMMANDS]    All /command handlers
    [SCHEDULER]   Cron jobs (news loop, weather, briefs)

  DEPLOYMENT:  push bot.py to GitHub → Railway auto-deploys
  COST:        ~$25/month (Claude Haiku + Railway + Buffer)
═══════════════════════════════════════════════════════════════════════════════
"""

import os, io, threading, time, logging, hashlib, json, feedparser, requests, anthropic, re
from dataclasses import dataclass
from PIL import Image, ImageDraw, ImageFont, ImageEnhance
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from apscheduler.schedulers.blocking import BlockingScheduler
from io import BytesIO
from cards import generate_card, generate_dhivehi_card, fetch_background_image, draw_weather_icon
from weather import (
    get_weather_data, get_island_forecasts, get_prayer_times,
    generate_weather_card, weather_code_to_info,
    detect_weather_alert, send_weather_alert, send_weather_update,
    MMS_ALERT_LEVELS, weather_alerts_today, ISLAND_LOCATIONS,
    HIJRI_SPECIAL_DAYS, SPECIAL_DAY_DETAILS, ISLAMIC_REMINDERS
)
from fetchers import (
    fetch_news, fetch_mvcrisis, fetch_all_dv_channels, fetch_dv_telegram,
    fetch_latest_web_pages, fetch_local_rss_recovery, fetch_world_updates,
    get_local_headlines, rewrite_news, gemini_translate,
    RSS_FEEDS, LOCAL_FEEDS, DV_TELEGRAM_CHANNELS, DEFAULT_KEYWORDS, WEB_LATEST_SOURCES,
    has_public_placeholder, public_text_is_safe, fallback_rewritten_news,
    clean_ai_line, safe_image_keyword
)
from scoring import (
    is_breaking, is_dhivehi, source_reliability,
    score_article, score_breakdown, confidence_score,
    should_hold_for_review, format_score_breakdown,
    is_duplicate_story, remember_story_title, register_in_cluster,
    recent_posts, user_conversations, recent_story_titles,
    BREAKING_KEYWORDS, BREAKING_BLACKLIST, SOURCE_RELIABILITY
)
from db import (
    init_database, db_execute, db_record_article, db_mark_status,
    db_publish_article_for_website, db_log_learning,
    db_set_article_message, db_set_article_matchkey,
    db_hide_article, db_unhide_article, db_delete_article_by_url, db_hide_all_dhivehi, db_unhide_all_dhivehi, db_bot_stats,
    kv_get, kv_set, mem_add, mem_list, mem_clear_all, mem_delete_last,
    detect_trends, is_trending_topic, find_or_create_story,
    get_story_timeline, search_stories, get_active_stories,
    canonical_category, strip_source_links, samuga_public_summary,
    normalize_article_language_for_public, _caption_match_key,
    make_article_slug, generate_website_article_body,
    TREND_THEMES, DB_ENABLED, is_dhivehi, looks_latin_thaana,
    gemini_latin_thaana_to_thaana, gemini_latin_thaana_to_english,
    _detect_themes
)

# ── Structured logging: tags make Railway logs readable ──────────────────────
# Usage: log.info("[FETCH] pulled 12 articles")  →  easy to filter in Railway
logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

SAMUGA_VERSION = "7.0"

# Module-level timezone alias (used by utcnow() below and elsewhere).
from datetime import timezone as _tz

# Public destination shown to readers. We never expose competitor/source links on
# Samuga public platforms; readers are directed back to Samuga Community.
SAMUGA_PUBLIC_SOURCE = os.environ.get("SAMUGA_PUBLIC_SOURCE", "Samuga Media")
SAMUGA_PUBLIC_LINK   = os.environ.get("SAMUGA_PUBLIC_LINK", "https://t.me/samugacommunity")
SAMUGA_CAPTION_LINK  = os.environ.get("SAMUGA_CAPTION_LINK", "https://samugamedia.com")


def samuga_public_caption(caption):
    """Sanitize a caption for public posting and append Samuga website."""
    if not caption:
        return caption
    try:
        clean = strip_source_links(caption).strip()
        site = (SAMUGA_CAPTION_LINK or "").strip()
        if site and site not in clean:
            clean = (clean + "\n\n" + site).strip()
        return clean
    except Exception:
        return caption


def utcnow():
    """Naive UTC datetime — same value as the old utcnow() but not deprecated."""
    return datetime.now(_tz.utc).replace(tzinfo=None)

def mvt_now():
    """Current Maldives time (UTC+5) as naive datetime."""
    return utcnow() + timedelta(hours=5)

def mvt_display_time(dt):
    """Display DB UTC timestamps as Maldives time for website/API output."""
    if not dt:
        return "Recent"
    try:
        # psycopg2 TIMESTAMPTZ may be timezone-aware; normalize by adding offset only for naive UTC.
        if getattr(dt, "tzinfo", None) is not None:
            return dt.astimezone(_tz.utc).replace(tzinfo=None).__add__(timedelta(hours=5)).strftime("%d %b %Y • %H:%M")
        return (dt + timedelta(hours=5)).strftime("%d %b %Y • %H:%M")
    except Exception:
        return "Recent"

def posting_paused():
    """Live Railway env kill switch: POSTING_PAUSED=true blocks all public posting."""
    return os.environ.get("POSTING_PAUSED", "false").lower() == "true"

def social_paused():
    """Live Railway env kill switch: SOCIAL_PAUSED=true blocks Buffer/social posting."""
    return os.environ.get("SOCIAL_PAUSED", "false").lower() == "true" or posting_paused()

def _posting_block_reason():
    if posting_paused():
        return "POSTING_PAUSED=true"
    if social_paused():
        return "SOCIAL_PAUSED=true"
    return ""

# ═══════════════════════════════════════════════════════════════════════════
# [CONFIG] — Environment variables & API keys
# ═══════════════════════════════════════════════════════════════════════════
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
_last_buffer_error  = {"response": "No posts attempted yet"}
# Meta Graph API — reads FB + IG engagement off your own page
META_PAGE_TOKEN     = os.environ.get("META_PAGE_TOKEN", "")
META_PAGE_ID        = os.environ.get("META_PAGE_ID", "")
META_APP_SECRET     = os.environ.get("META_APP_SECRET", "")
META_IG_ID          = os.environ.get("META_IG_ID", "")  # optional; auto-resolved if blank
META_API_VER        = os.environ.get("META_API_VER", "v21.0")
TOMORROW_API_KEY    = os.environ.get("TOMORROW_API_KEY", "")  # weather data

# ── Killer posting switches ──────────────────────────────────────────────────
# POSTING_PAUSED=true blocks all public Telegram + Buffer/social posting.
# SOCIAL_PAUSED=true blocks Buffer/social only.
# These are read live from Railway env vars on every post attempt.
POSTING_PAUSED = os.environ.get("POSTING_PAUSED", "false").lower() == "true"
SOCIAL_PAUSED  = os.environ.get("SOCIAL_PAUSED",  "false").lower() == "true"

ai = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ═══════════════════════════════════════════════════════════════════════════
# [MODELS] — Article shape (documentation + optional helper)
# ═══════════════════════════════════════════════════════════════════════════
# Articles flow through the pipeline as plain dicts for flexibility. This
# dataclass documents the canonical shape so future-you (and any new dev) can
# see at a glance what fields an article carries. Use Article.from_dict() if you
# ever want type safety, but the dict form remains the working currency.
@dataclass
class Article:
    id: str                       # unique hash of the article
    title: str                    # headline
    summary: str = ""             # body/excerpt
    link: str = ""                # source URL
    source: str = ""              # outlet name (Mihaaru, Sun, etc)
    cat: str = "LOCAL"            # BREAKING | LOCAL | POLITICAL | SPORTS | LIFESTYLE | WORLD
    lang: str = "en"              # en | dv
    # ── runtime fields added during processing ──
    score: int = 0                # newsroom priority score
    reliability: int = 0          # source trust score
    is_breaking: bool = False
    cluster_size: int = 1         # how many sources reporting this (_cluster_size)
    story_id: int = None          # attached Story Intelligence thread (_story_id)

    @classmethod
    def from_dict(cls, d: dict) -> "Article":
        return cls(
            id=d.get("id",""), title=d.get("title",""), summary=d.get("summary",""),
            link=d.get("link",""), source=d.get("source",""), cat=d.get("cat","LOCAL"),
            lang=d.get("lang","en"), score=d.get("score",0),
            reliability=d.get("reliability",0), is_breaking=d.get("is_breaking",False),
            cluster_size=d.get("_cluster_size",1), story_id=d.get("_story_id"),
        )

# ═══════════════════════════════════════════════════════════════════════════
# [FETCHERS] — RSS Feeds
# ═══════════════════════════════════════════════════════════════════════════
# ── RSS Feeds (v4 Strategy) ───────────────────────────────────────────────────
# LOCAL (70%) — Maldivian sources, priority order
core_team_session_context = {}  # user_id -> stored context

# Pending manual card waiting for /confirm before firing to all platforms
# Only one at a time — new "create card and post" replaces the previous pending one.
_pending_manual_post = {}  # {card_bytes, full_caption, chat_id, thread_id, first_name}

# ── Samuga AI proactive mode toggle ─────────────────────────────────────────
# /ai on  → bot reads every core team message and decides whether to jump in
# /ai off → bot only responds when directly tagged (default safe mode)
_ai_proactive_mode = True   # on by default — team can toggle

# ── Universal Approval Queue (in-memory) ─────────────────────────────────────
# Every card (English + Dhivehi) waits here for Content Lab approval before posting.
# Cards expire after 2 hours if not approved.
ENGLISH_AUTOPOST_SECONDS = 2700   # Regular: auto-post after 45 min if nobody reviews
BREAKING_AUTOPOST_SECONDS = 1800  # Breaking held for confidence: auto-posts in 30 min
TELEGRAM_GAP_SECONDS    = 7200   # 2 hours between regular Telegram posts
DAILY_TG_POST_MAX       = 12     # Max regular posts per day to Telegram
DHIVEHI_EXPIRY_SECONDS   = 7200   # Dhivehi: expire (delete) after 2h if not approved

approval_queue = {}  # key -> {card_bytes, caption, title, link, cat, lang, dv_text, created_at, ...}
_approval_counter = [0]

# ── Global state variables (removed from db block, restored here) ──────────────
analytics           = {"posts_by_cat": {}, "breaking_count": 0, "social_success": 0, "social_fail": 0, "week_start": None}
last_regular_post_time = None
daily_sports_count  = {"date": None, "count": 0}
daily_world_count   = {"date": None, "count": 0}
daily_tourism_count = {"date": None, "count": 0}


def can_post_cat_today(counter, max_per_day):
    """Check a per-category daily counter against its cap. Resets on a new MVT
    day and increments the counter when posting is allowed. Returns True if a
    post in this category is still within today's budget."""
    today = (utcnow() + timedelta(hours=5)).strftime("%Y-%m-%d")
    if counter.get("date") != today:
        counter["date"] = today
        counter["count"] = 0
    if counter["count"] >= max_per_day:
        return False
    counter["count"] += 1
    try:
        persist_state()
    except Exception:
        pass
    return True


# ── Content Lab flood control ────────────────────────────────────────────────
# Goal: normal newsroom flow = max 4 approval cards/hour.
# Exception: very high priority stories can use up to 6/hour.
# Breaking with strong confidence posts automatically; breaking with weak confidence
# goes to Alert Chat, not Content Lab, so Uly's workspace does not get flooded.
CONTENT_LAB_NORMAL_MAX_PER_HOUR = int(os.environ.get("CONTENT_LAB_NORMAL_MAX_PER_HOUR", "4"))
CONTENT_LAB_HIGH_MAX_PER_HOUR   = int(os.environ.get("CONTENT_LAB_HIGH_MAX_PER_HOUR", "6"))
CONTENT_LAB_HIGH_SCORE          = int(os.environ.get("CONTENT_LAB_HIGH_SCORE", "180"))
_content_lab_sent_log = []  # datetime stamps for approval previews actually sent

def _prune_content_lab_log(now=None):
    now = now or utcnow()
    cutoff = now - timedelta(hours=1)
    _content_lab_sent_log[:] = [t for t in _content_lab_sent_log if t > cutoff]

def _content_lab_slots_available(item=None):
    now = utcnow()
    _prune_content_lab_log(now)
    sent = len(_content_lab_sent_log)
    priority = int((item or {}).get("_priority") or (item or {}).get("score") or 0)
    high_priority = bool((item or {}).get("is_breaking")) or priority >= CONTENT_LAB_HIGH_SCORE
    limit = CONTENT_LAB_HIGH_MAX_PER_HOUR if high_priority else CONTENT_LAB_NORMAL_MAX_PER_HOUR
    return sent < limit, limit, sent, high_priority

def _mark_content_lab_sent(item=None):
    _prune_content_lab_log()
    _content_lab_sent_log.append(utcnow())
    if item is not None:
        item["_content_lab_sent"] = True
        item["_content_lab_sent_at"] = utcnow().isoformat()

def release_content_lab_drip():
    """Send delayed approval cards slowly so Content Lab gets max 4/hour, 6 if high priority."""
    try:
        pending = [
            (k, v) for k, v in approval_queue.items()
            if not v.get("_content_lab_sent") and not v.get("_content_lab_suppressed")
        ]
        pending.sort(key=lambda kv: kv[1].get("created_at") or utcnow())
        if not pending:
            return
        for k, v in pending:
            ok, limit, sent, high = _content_lab_slots_available(v)
            if not ok:
                log.info(f"🧯 Content Lab drip paused: {sent}/{limit} sent in last hour, {len(pending)} waiting")
                return
            _send_approval_card(k, v, force=True)
    except Exception as e:
        log.error(f"Content Lab drip: {e}")

def store_pending_approval(card_bytes, caption, title, link, cat="LOCAL", lang="en",
                           dv_text=None, keyword="maldives news", source="LOCAL",
                           is_breaking=False, allow_social=True, dedup_title=None, summary=""):
    """Store a fully-built card awaiting approval. Returns the key or None if blocked."""
    safe_ok, safe_reason = contentlab_candidate_is_safe(
        title=title,
        summary=(dv_text or caption or summary or ""),
        source=source,
        lang=lang,
    )
    if not safe_ok:
        log.warning(f"🧱 Content Lab blocked candidate: {safe_reason} — {str(title)[:90]}")
        return None

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
        "_dedup_title": dedup_title or title,
        "summary": summary or "",
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
    - Breaking held (low confidence): auto-posts after 30 min if no team action
    - Regular English: auto-posts after 45 min via queue
    - Dhivehi breaking: auto-posts after 2h
    - Regular Dhivehi: deleted after 2h
    """
    now = utcnow()

    # Breaking held for confidence — auto-post after 30 min
    breaking_held = [k for k, v in approval_queue.items()
                     if v.get("lang") == "en"
                     and v.get("is_breaking", False)
                     and v.get("_held_for_confidence", False)
                     and (now - v["created_at"]).total_seconds() > BREAKING_AUTOPOST_SECONDS]
    for k in breaking_held:
        item = approval_queue.pop(k)
        log.info(f"⏰ Breaking {k} auto-posting (30min, no review): {item.get('title','')[:50]}")
        try:
            buf = io.BytesIO(item["card_bytes"])
            queue_for_social(buf, item["caption"],
                key_label=f"{k.upper()} (breaking auto)",
                tg_ok=False, post_telegram=True, is_breaking=True,
                article_id=item.get("article_id"), title=item.get("title",""),
                summary=item.get("summary",""), cat=item.get("cat","BREAKING"),
                source=item.get("source","Samuga Media"), link=item.get("link",""),
                lang=item.get("lang","en"))
            send_text(CORE_TEAM_CHAT_ID,
                f"⏰ <b>{k.upper()} Breaking auto-posted</b> (30min, no review)\n"
                f"📰 {item.get('title','')[:80]}",
                thread_id=ALERT_THREAD_ID)
        except Exception as e:
            log.error(f"Breaking auto-post {k}: {e}")

    # Regular English auto-post after 45 min
    en_due = [k for k, v in approval_queue.items()
              if v.get("lang") == "en"
              and not v.get("is_breaking", False)
              and (now - v["created_at"]).total_seconds() > ENGLISH_AUTOPOST_SECONDS]
    for k in en_due:
        item = approval_queue.pop(k)
        title = item.get("title","")[:50]
        log.info(f"⏰ English {k} auto-queuing (45min, no review): {title}")
        try:
            buf = io.BytesIO(item["card_bytes"])
            queue_for_social(buf, item["caption"],
                key_label=f"{k.upper()} (auto)",
                tg_ok=False, post_telegram=True,
                article_id=item.get("article_id"), title=item.get("title",""),
                summary=item.get("summary",""), cat=item.get("cat","LOCAL"),
                source=item.get("source","Samuga Media"), link=item.get("link",""),
                lang=item.get("lang","en"), is_breaking=item.get("is_breaking", False))
            send_text(CORE_TEAM_CHAT_ID,
                f"⏰ <b>{k.upper()}</b> auto-posted (45min, no review)\n📰 {item.get('title','')[:80]}",
                thread_id=ALERT_THREAD_ID)
        except Exception as e:
            log.error(f"Auto-post queue {k}: {e}")

    # Dhivehi expiry — breaking ones auto-post after 2h, regular ones delete
    dv_due = [k for k, v in approval_queue.items()
              if v["lang"] == "dv" and (now - v["created_at"]).total_seconds() > DHIVEHI_EXPIRY_SECONDS]
    for k in dv_due:
        item = approval_queue.pop(k)
        title = item.get("title","")[:40]
        # Breaking Dhivehi with auto-post flag → post automatically
        if item.get("_auto_post_breaking") and item.get("dv_text"):
            log.info(f"⏰ Breaking Dhivehi {k} auto-posting after 2h: {title}")
            try:
                kw = item.get("keyword","local")
                bg = item.get("_bg_image") or fetch_background_image(kw, cat=item.get("cat"), title=item.get("title",""))
                ts_now = (utcnow() + timedelta(hours=5)).strftime("%d %b %Y • %H:%M")
                card = generate_card(item["dv_text"], SAMUGA_PUBLIC_SOURCE, ts_now, item.get("cat","BREAKING"), bg)
                full_caption = (
                    f"🇲🇻 <b>{item['title']}</b>\n\n"
                    f"{item['dv_text']}\n\n"
                    f"📡 <b>ސަމުގާ މީޑިއާ</b> | @samugacommunity"
                )
                card.seek(0)
                tg_ok = send_to_telegram(card, full_caption)
                if tg_ok:
                    card.seek(0)
                    queue_for_social(io.BytesIO(card.getvalue()), full_caption,
                                     key_label=k.upper(), tg_ok=True,
                                     article_id=item.get("article_id"), title=item.get("title",""),
                                     summary=item.get("summary",""), cat=item.get("cat","BREAKING"),
                                     source=item.get("source","Samuga Media"), link=item.get("link",""),
                                     lang="dv", is_breaking=True)
                    send_text(CORE_TEAM_CHAT_ID,
                        f"⏰ <b>{k.upper()} Breaking Dhivehi auto-posted</b> (2h, no review)\n"
                        f"📰 {item['title'][:70]}",
                        thread_id=ALERT_THREAD_ID)
                    log.info(f"⏰ Breaking Dhivehi {k} auto-posted to community")
            except Exception as e:
                log.error(f"Breaking DV auto-post {k}: {e}")
        else:
            log.info(f"⏰ Dhivehi {k} expired (2h, not breaking, deleted): {title}")

    if en_due or dv_due:
        persist_state()

# Backwards-compat alias (old code references dhivehi_pending)
dhivehi_pending = approval_queue

# ── Core Team Config ──────────────────────────────────────────────────────────
CORE_TEAM_CHAT_ID = "-1002829230299"
CONTENT_LAB_THREAD_ID = 9061   # approvals, queue confirmations — Uly's workspace
ALERT_THREAD_ID       = 10169  # bot suggestions, developing stories, briefs, proactive insights

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
    "Rejected faster than a loan application. Card deleted. 🚮",
    "Understood. We move. The article does not. 👋",
    "That article just got voted off the island. Maldivian style. 🏝️",
    "Fine fine, I'll delete it. But between us — I thought it was good. Just saying. 🤷",
    "Deleted! The article is now in a better place. (The bin.) 🗑️✨",
    "I already knew you'd reject it. I just wanted to see if you'd catch it. You did. Respect. 🫡",
    "Card deleted. The source is probably crying somewhere. Not my problem. 😌",
    "Gone with the wind. And the article. Bye bye. 🌬️",
    "Noted, rejected, deleted. Three words that describe both this article and my weekend plans. 🙂",
    "You know what, I respect the standards. Card is gone. Moving on. 💪",
    "Deleted so fast the article didn't even see it coming. Neither did I honestly. 😅",
    "That one wasn't it. Removed. You're basically my editor brain at this point. 🧠",
    "Rejected. I'll add it to the list of things that didn't make it. The list is getting long. 📋",
    "Gone. The article will not be missed. By anyone. Especially not the readers. 🫠",
    "Okay the bin got a new resident. Hope it's comfortable in there. 🗑️",
    "Deleted faster than Manchii's sleep schedule. Which is saying something. ⚡",
    "Fair enough. Some stories aren't worth telling. This was one of them. Card gone. ✂️",
]

# ── PostgreSQL Database Layer (v6) ────────────────────────────────────────────
# Railway auto-injects DATABASE_URL when Postgres is in the project.
# The bot uses Postgres for the article archive + intelligence, but ALWAYS falls
# back to JSON files if the DB is unavailable, so it never breaks.
# ── Source Reliability Scoring ────────────────────────────────────────────────
# Higher = more trusted. Used as a tie-breaker and a scoring boost so a direct
# Mihaaru/MvCrisis story outranks a Google News scrape of the same topic.
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

def remember_post(title, cat, timestamp, is_breaking=False):
    today = (utcnow() + timedelta(hours=5)).strftime("%Y-%m-%d")
    recent_posts.append({"title": title, "cat": cat, "time": timestamp,
                          "is_breaking": is_breaking, "date": today})
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

def can_post_regular():
    """
    Returns True only if:
    1. At least 2 hours have passed since last regular post, AND
    2. Daily regular post cap (12) hasn't been hit.
    Breaking news ignores this entirely.
    """
    global last_regular_post_time
    today = (utcnow() + timedelta(hours=5)).strftime("%Y-%m-%d")

    # Check daily cap
    posts_today = sum(1 for p in recent_posts
                      if p.get("date","") == today and not p.get("is_breaking", False))
    if posts_today >= DAILY_TG_POST_MAX:
        log.info(f"📵 Daily post cap reached ({posts_today}/{DAILY_TG_POST_MAX}) — skipping regular post")
        return False

    # Check time gap
    if not last_regular_post_time:
        return True
    return (utcnow() - last_regular_post_time).total_seconds() >= TELEGRAM_GAP_SECONDS

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
# ── Telegram ──────────────────────────────────────────────────────────────────
def send_to_telegram(buf, caption):
    """Post a photo to the community channel. Returns message_id (int) or False."""
    try:
        if posting_paused():
            log.warning("🛑 Telegram public post blocked — POSTING_PAUSED=true")
            return False
        safe_caption = samuga_public_caption(caption)
        if not public_text_is_safe(safe_caption):
            log.error(f"🚫 Telegram blocked unsafe public caption: {str(safe_caption)[:120]}")
            return False
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
            data={"chat_id": TELEGRAM_CHANNEL_ID, "caption": safe_caption, "parse_mode": "HTML"},
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

_ops_last_alerts = {}
website_banner = {"active": False, "text": "", "image_url": "", "updated_at": None}

def alert_admin(message, dedupe_key=None, cooloff_minutes=20):
    """Send an operational alert into the Alert thread without spamming duplicates."""
    try:
        key = dedupe_key or _caption_match_key(message) or str(hash(message))
        now = utcnow()
        last = _ops_last_alerts.get(key)
        if last and (now - last).total_seconds() < cooloff_minutes * 60:
            return False
        _ops_last_alerts[key] = now
        send_text(CORE_TEAM_CHAT_ID, f"⚠️ <b>Samuga Ops Alert</b>\n\n{message}", thread_id=ALERT_THREAD_ID)
        return True
    except Exception as e:
        log.error(f"alert_admin: {e}")
        return False

def delete_telegram_message(chat_id, message_id):
    """Delete a Telegram message when the bot has rights in the chat."""
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteMessage",
            json={"chat_id": chat_id, "message_id": message_id},
            timeout=15
        )
        data = r.json() if r.ok else {}
        ok = bool(r.ok and data.get("ok"))
        if not ok:
            log.warning(f"deleteMessage failed: {str(data)[:200]}")
        return ok
    except Exception as e:
        log.error(f"delete_telegram_message: {e}")
        return False

def download_telegram_photo_bytes(photo_list):
    """Download the highest quality Telegram photo and return raw bytes."""
    try:
        if not photo_list:
            return None
        file_id = photo_list[-1]["file_id"]
        resp = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile",
            params={"file_id": file_id}, timeout=15
        )
        data = resp.json()
        file_path = data["result"]["file_path"]
        img_resp = requests.get(
            f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}",
            timeout=25
        )
        if img_resp.ok and img_resp.content:
            return img_resp.content
    except Exception as e:
        log.error(f"download_telegram_photo_bytes: {e}")
    return None

def website_article_url(article_id=None, slug=None):
    """Return the public website URL for an article."""
    base = (SAMUGA_CAPTION_LINK or "https://samugamedia.com").rstrip("/")
    if article_id:
        return f"{base}/article.html?id={article_id}"
    if slug:
        return f"{base}/article.html?slug={slug}"
    return base

def extract_inline_post_to_web_body(text):
    """Allow admins to send article text + /post to web in the same message."""
    raw = str(text or "")
    clean = re.sub(r'@SamugaNewsBot\b', '', raw, flags=re.I)
    clean = re.sub(r'(?im)^\s*/post\s+to\s+web\s*$', '', clean)
    clean = re.sub(r'(?im)^\s*/postweb\s*$', '', clean)
    clean = re.sub(r'(?im)^\s*/posttoweb\s*$', '', clean)
    clean = re.sub(r'(?im)^\s*/post\s+web\s*$', '', clean)
    clean = clean.strip()
    return clean

# ── Gemini Dhivehi Caption ────────────────────────────────────────────────────
# Model fallback chain — newest first, fall back if quota/deprecated
GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash",
]

def _gemini_post(prompt, timeout=15):
    """Try Gemini models in order until one works. Returns text or None."""
    if not GEMINI_API_KEY:
        return None
    for model in GEMINI_MODELS:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
            resp = requests.post(url, json={"contents":[{"parts":[{"text":prompt}]}]}, timeout=timeout)
            if resp.status_code == 200:
                text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                log.info(f"[AI] Gemini {model}: OK")
                return text
            elif resp.status_code in [429, 503]:
                log.warning(f"[AI] Gemini {model}: quota/unavailable ({resp.status_code}), trying next")
                continue
            else:
                log.warning(f"[AI] Gemini {model}: HTTP {resp.status_code}")
                continue
        except Exception as e:
            log.warning(f"[AI] Gemini {model}: {e}")
            continue
    log.error("[AI] All Gemini models failed")
    return None

def make_dhivehi_caption(english_text, title):
    """Convert English/Latin news text to clean Dhivehi Thaana using Gemini."""
    if not GEMINI_API_KEY:
        return None
    prompt = f"""You are a Maldivian news editor for Samuga Media.

Write a short public news caption in proper Dhivehi Thaana script.

Rules:
- Output ONLY Dhivehi Thaana.
- 2 to 3 natural sentences.
- Clean Maldivian news style.
- Do not add facts that are not in the input.
- Do not include external source links.
- Do not leave Latin Thaana/romanized Dhivehi.
- Brand names can stay in English only if necessary.

Title:
{title}

Summary:
{english_text}
"""
    result = _gemini_post(prompt)
    if result and is_dhivehi(result):
        log.info("✅ Gemini Dhivehi caption done")
        return strip_source_links(result).strip()

    if result:
        log.warning("Gemini Dhivehi caption returned non-Thaana text — rejected")
    return None

# ── Safety + dedup normalization layer ────────────────────────────────────────
_INTERNAL_NEWS_BLOCKLIST = [
    "technical issue", "being fixed", "sorry for the trouble", "sorry for the inconvenience",
    "temporarily unavailable", "maintenance", "test post", "debug", "samuga media is facing",
    "issue is being fixed", "we are fixing", "service interruption"
]
_MARKUP_JUNK_PATTERNS = [
    r"\.cls-\d", r"fill-rule\s*:", r"evenodd", r"<svg", r"</svg", r"xmlns=",
    r"viewbox", r"path\s+d=", r"fill:\s*#"
]
_signal_key_cache = {}

def gemini_dhivehi_to_english(text):
    """Translate proper Thaana or mixed Dhivehi text into clean English for scoring/dedup."""
    if not GEMINI_API_KEY or not text:
        return None
    prompt = f"""Translate this Dhivehi news text into clean English.

Rules:
- Output ONLY English.
- Do not add new facts.
- Keep names and places accurate.
- Clean short newsroom style.

Text:
{text}
"""
    out = _gemini_post(prompt, timeout=18)
    if out and not is_dhivehi(out):
        return strip_source_links(out).strip()
    return None

def contentlab_candidate_is_safe(title="", summary="", source="", lang="en"):
    """Block internal/system chatter, CSS/SVG junk, and fake newsroom items before Content Lab."""
    combined = strip_source_links(f"{title}\n{summary}").strip()
    c_lower = combined.lower()
    for bad in _INTERNAL_NEWS_BLOCKLIST:
        if bad in c_lower:
            return False, f"internal/system text: {bad}"
    for pat in _MARKUP_JUNK_PATTERNS:
        if re.search(pat, combined, re.I):
            return False, f"markup/css junk: {pat}"
    if combined.startswith(".") and "{" in combined and "}" in combined:
        return False, "css-like content"
    # Extremely short Samuga-self lines with no news detail are not news.
    if source.lower().startswith("samuga") and len(combined.split()) < 8:
        return False, "samuga internal short text"
    return True, ""


def should_publish_dhivehi_to_website(item=None, approved=False):
    """
    Dhivehi must never appear on the website unless a human approved it.
    For safety, approved Dhivehi website publishing stays OFF unless
    DHIVEHI_WEBSITE_APPROVED=true is set in Railway.
    """
    if not approved:
        return False
    return os.environ.get("DHIVEHI_WEBSITE_APPROVED", "false").lower() == "true"

def story_signal_key(title="", summary="", lang="en"):
    """
    Normalize a story into a stable English-ish key so Dhivehi/Latin variants
    of the same story do not keep entering Content Lab.
    """
    raw = strip_source_links(" ".join(x for x in [title, summary] if x)).strip()
    if not raw:
        return ""
    cache_key = f"{lang}|{raw[:500]}"
    if cache_key in _signal_key_cache:
        return _signal_key_cache[cache_key]

    normalized = raw
    try:
        if looks_latin_thaana(raw):
            en = gemini_latin_thaana_to_english(raw)
            if en:
                normalized = en
        elif (lang == "dv") or is_dhivehi(raw):
            en = gemini_dhivehi_to_english(raw)
            if en:
                normalized = en
    except Exception as e:
        log.debug(f"story_signal_key translation: {e}")

    key = _caption_match_key(normalized or raw)
    _signal_key_cache[cache_key] = key
    return key

# ── Dhivehi Quality Layer: Latin Thaana → Proper Thaana / English ─────────────
# (Implemented in db.normalize_article_language_for_public — imported above.)

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
    if social_paused():
        log.warning(f"🛑 Buffer post blocked — {_posting_block_reason()}")
        return False
    if not BUFFER_TOKEN or not channel_id: return False
    clean = re.sub(r'<[^>]+>', '', caption)
    clean = clean.replace('&amp;', '&').replace('&#039;', "'").replace('&quot;', '"').replace('&lt;', '<').replace('&gt;', '>').strip()
    if not public_text_is_safe(clean):
        log.error(f"🚫 Buffer blocked unsafe caption: {clean[:120]}")
        return False

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
        _last_buffer_error["response"] = f"HTTP {resp.status_code}: {resp.text[:300]}"
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

# ── Social posting queue — 10 minute gap between posts ───────────────────────
# Prevents flooding FB/IG/X when multiple cards are approved at once.
# Each item: {"img_bytes": bytes, "caption": str, "queued_at": datetime}
_social_queue = []
_social_queue_lock = threading.Lock()
_last_social_post_time = None
SOCIAL_GAP_SECONDS = 600  # 10 minutes

# Personality messages for queue notifications
QUEUE_PERSONALITY = [
    "yea yea it's in the queue. 😮‍💨",
    "queued. The algorithm likes it spaced out. Unlike Uly's approvals. 😅",
    "in the queue. Good things take time. 🕐",
    "queued. You're too bossy today, I need my 10 minutes. 😤",
    "in the queue. Quality over quantity. 💅",
    "queued. I'm tired, not lazy. There's a difference. 😴",
    "queued. Back-to-back posting is so 2022. ⏳",
    "in the queue. The platforms will thank us. 🙏",
    "queued. I'm pacing myself unlike some people in this group. 👀",
    "yea yea, added to queue. I only have two hands. Metaphorically. 🤲",
]

def _get_queue_msg():
    import random
    return random.choice(QUEUE_PERSONALITY)

def _calc_eta_seconds():
    """How many seconds until the next social post can go out."""
    if _last_social_post_time is None:
        return 0
    elapsed = (utcnow() - _last_social_post_time).total_seconds()
    return max(0, SOCIAL_GAP_SECONDS - elapsed)

def _social_queue_worker():
    """
    Background thread — drains one post every 10 minutes.
    Each item posts to Telegram community + FB + IG + X in sequence.
    BREAKING news bypasses this queue entirely and posts immediately.
    """
    global _last_social_post_time
    while True:
        time.sleep(15)
        try:
            with _social_queue_lock:
                if not _social_queue:
                    continue
                now = utcnow()
                if (_last_social_post_time and
                        (now - _last_social_post_time).total_seconds() < SOCIAL_GAP_SECONDS):
                    continue
                item = _social_queue.pop(0)

            if posting_paused():
                log.warning("🛑 Social queue holding because POSTING_PAUSED=true")
                with _social_queue_lock:
                    _social_queue.insert(0, item)
                time.sleep(60)
                continue

            combined_public_text = f"{item.get('title','')}\n{item.get('summary','')}\n{item.get('caption','')}"
            if not public_text_is_safe(combined_public_text):
                log.error(f"🚫 Social queue dropped unsafe post: {item.get('key_label','Post')} — {str(item.get('title',''))[:80]}")
                notify_cid = item.get("notify_chat_id")
                notify_tid = item.get("notify_thread_id")
                if notify_cid:
                    send_text(notify_cid, "🚫 Post blocked by placeholder safety gate before publishing.", thread_id=notify_tid)
                continue

            _last_social_post_time = utcnow()
            remaining = len(_social_queue)
            key_label  = item.get("key_label", "Post")
            notify_cid = item.get("notify_chat_id")
            notify_tid = item.get("notify_thread_id")
            log.info(f"[QUEUE] Posting {key_label} (Telegram + FB+IG+X) — {remaining} remaining")

            # 1. Post to Telegram community (respects daily cap + 2hr gap for regular posts)
            tg_ok = False
            if item.get("post_telegram", True):
                is_breaking_item = item.get("is_breaking", False)
                if is_breaking_item or can_post_regular():
                    try:
                        buf = io.BytesIO(item["img_bytes"])
                        tg_ok = bool(send_to_telegram(buf, item["caption"]))
                        if tg_ok and not is_breaking_item:
                            global last_regular_post_time
                            last_regular_post_time = utcnow()
                            persist_state()
                        log.info(f"[QUEUE] Telegram: {'✅' if tg_ok else '❌'}")
                        if tg_ok and item.get("article_id"):
                            try:
                                if item.get("lang","en") != "dv" or should_publish_dhivehi_to_website(item, approved=True):
                                    db_publish_article_for_website(
                                        article_id=item.get("article_id"),
                                        title=item.get("title",""),
                                        summary=item.get("summary",""),
                                        category=item.get("cat","LOCAL"),
                                        source=item.get("source","Samuga Media"),
                                        link=item.get("link",""),
                                        lang=item.get("lang","en"),
                                        is_breaking=item.get("is_breaking", False)
                                    )
                                else:
                                    log.info(f"🌐 Dhivehi website sync skipped (approval-only policy): {item.get('title','')[:70]}")
                                if isinstance(tg_ok, int):
                                    db_set_article_message(item.get("article_id"), tg_ok)
                                if item.get("title"):
                                    remember_post(item.get("title"), item.get("cat","LOCAL"),
                                                  (utcnow() + timedelta(hours=5)).strftime("%d %b %Y • %H:%M"),
                                                  item.get("is_breaking", False))
                            except Exception as e:
                                log.error(f"[WEBSITE] queue publish sync failed: {e}")
                    except Exception as e:
                        log.error(f"[QUEUE] Telegram: {e}")
                else:
                    log.info("[QUEUE] Telegram skipped — daily cap or 2hr gap not met")
                    # Re-queue for later unless it's been waiting too long
                    age_mins = (utcnow() - item.get("queued_at", utcnow())).total_seconds() / 60
                    if age_mins < 180:  # give up after 3 hours
                        with _social_queue_lock:
                            _social_queue.insert(0, item)  # put back at front
                        time.sleep(60)  # wait a minute before re-checking
                        continue
            else:
                tg_ok = item.get("tg_ok", False)

            # 2. Post to FB + IG + X
            results = _post_to_social_now(
                io.BytesIO(item["img_bytes"]), item["caption"])

            # 3. Send per-platform confirmation
            tg_icon = "✅" if tg_ok else "❌"
            fb_icon = "✅" if results.get("Facebook")  else "❌"
            ig_icon = "✅" if results.get("Instagram") else "❌"
            x_icon  = "✅" if results.get("Twitter")   else "❌"
            conf_msg = (f"📤 <b>{key_label}</b> posted\n"
                        f"Telegram {tg_icon} · FB {fb_icon} · IG {ig_icon} · X {x_icon}")
            if notify_cid:
                send_text(notify_cid, conf_msg, thread_id=notify_tid)
            else:
                # Auto-post — send to Content Lab so team knows what went out
                send_text(CORE_TEAM_CHAT_ID, conf_msg, thread_id=CONTENT_LAB_THREAD_ID)
            try: persist_state()
            except Exception: pass
        except Exception as e:
            log.error(f"[QUEUE] Worker error: {e}")

def queue_for_social(img_buf, caption, notify_chat_id=None, notify_thread_id=None,
                     key_label="Post", tg_ok=True, post_telegram=False,
                     article_id=None, title="", summary="", cat="LOCAL",
                     source="Samuga Media", link="", lang="en", is_breaking=False):
    """
    Add a card to the 10-minute publish queue.
    post_telegram=True  → queue will post to Telegram community too (standard flow)
    post_telegram=False → Telegram was already posted separately (breaking news)

    Website sync fix:
    If article_id/title are passed, the article is marked as posted for /api/stories
    immediately when it enters the public publishing queue.
    """
    img_bytes = img_buf.getvalue() if hasattr(img_buf, "getvalue") else img_buf
    if posting_paused():
        log.warning("🛑 Public queue refused post — POSTING_PAUSED=true")
        if notify_chat_id:
            send_text(notify_chat_id, "🛑 Public posting is paused (POSTING_PAUSED=true). Post was not queued.", thread_id=notify_thread_id)
        return False

    combined_public_text = f"{title}\n{summary}\n{caption}"
    if not public_text_is_safe(combined_public_text):
        log.error(f"🚫 Social queue refused unsafe post: {str(title)[:90]}")
        if notify_chat_id:
            send_text(notify_chat_id, "🚫 Post blocked by placeholder safety gate. It was not queued.", thread_id=notify_thread_id)
        return False

    if article_id and (lang != "dv"):
        try:
            db_publish_article_for_website(
                article_id=article_id, title=title, summary=summary, category=cat,
                source=SAMUGA_PUBLIC_SOURCE, link=SAMUGA_PUBLIC_LINK, lang=lang, is_breaking=is_breaking
            )
        except Exception as e:
            log.error(f"[WEBSITE] publish sync before queue failed: {e}")

    with _social_queue_lock:
        _social_queue.append({
            "img_bytes":        img_bytes,
            "caption":          caption,
            "queued_at":        utcnow(),
            "notify_chat_id":   notify_chat_id,
            "notify_thread_id": notify_thread_id,
            "key_label":        key_label,
            "tg_ok":            tg_ok,
            "post_telegram":    post_telegram,
            "article_id":       article_id,
            "title":            title,
            "summary":          summary,
            "cat":              cat,
            "source":           source,
            "link":             link,
            "lang":             lang,
            "is_breaking":      is_breaking,
        })
        queue_pos = len(_social_queue)

    # Calculate real ETA
    eta_secs = _calc_eta_seconds() + (queue_pos - 1) * SOCIAL_GAP_SECONDS
    eta_min  = max(1, round(eta_secs / 60))

    if notify_chat_id:
        if eta_secs <= 30:
            msg = f"📲 {key_label} — {_get_queue_msg()} Posts right away."
        else:
            msg = f"📲 {key_label} — {_get_queue_msg()} Posts in ~{eta_min} min."
        send_text(notify_chat_id, msg, thread_id=notify_thread_id)

    log.info(f"[SOCIAL] Queued pos #{queue_pos}, ETA ~{eta_min}m")
    try: persist_state()
    except Exception: pass

# Keep old name as the "post now" internal function
def _post_to_social_now(img_buf, caption):
    if social_paused():
        log.warning(f"🛑 Social post blocked — {_posting_block_reason()}")
        return {"Facebook": False, "Instagram": False, "Twitter": False}
    """
    Post to all social platforms via Buffer (FB + IG + X), with the card image.

    REVERTED TO BUFFER: previously this used the Meta Graph API for FB/IG, which
    hit a #200 permissions error. Buffer was working perfectly before, so all
    three platforms now go through Buffer's GraphQL API (image hosted via imgbb).
    Returns the same {"Facebook","Instagram","Twitter"} dict the queue expects.
    """
    results = {"Facebook": False, "Instagram": False, "Twitter": False}
    if not BUFFER_TOKEN:
        log.warning("[SOCIAL] no BUFFER_TOKEN, skipping")
        return results
    if not can_post_social():
        log.info("[SOCIAL] Daily limit reached — skipping")
        return results

    try:
        img_bytes = img_buf.getvalue() if hasattr(img_buf, "getvalue") else img_buf
        image_url = upload_to_imgbb(img_bytes)
        if not image_url:
            log.error("[SOCIAL] imgbb upload failed, skipping")
            return results

        # Strip HTML for all social platforms
        clean = re.sub(r'<[^>]+>', '', caption)
        clean = clean.replace('&amp;', '&').replace('&#039;', "'").replace('&quot;', '"').replace('&lt;', '<').replace('&gt;', '>').strip()

        # Public Samuga posts must never expose original source links.
        # Send viewers to Samuga Community only.
        clean = strip_source_links(clean)
        if not public_text_is_safe(clean):
            log.error(f"🚫 Social post blocked unsafe caption: {clean[:120]}")
            return results
        community_link = SAMUGA_CAPTION_LINK

        # FB/IG: full text + Samuga community link only
        fb_ig = clean
        if community_link and community_link not in fb_ig:
            fb_ig = fb_ig + "\n\n" + community_link
        fb_ig = fb_ig[:2200]

        # Twitter/X: first line + Samuga community link only
        lines = [l.strip() for l in clean.split('\n') if l.strip()]
        tw = (lines[0] if lines else clean)[:220]
        if community_link:
            tw = tw + "\n\n" + community_link
        tw = tw[:280]

        for cid, cap, name, meta in [
            (BUFFER_FB_ID, fb_ig, "Facebook",  {"facebook":  {"type": "post"}}),
            (BUFFER_IG_ID, fb_ig, "Instagram", {"instagram": {"type": "post", "shouldShareToFeed": True}}),
            (BUFFER_TW_ID, tw,    "Twitter",   None),
        ]:
            if not cid:
                log.warning(f"[SOCIAL] skipping {name} — no channel ID set")
                continue
            results[name] = post_to_buffer(image_url, cap, cid, metadata=meta)
            time.sleep(2)

        ok_list = [k for k, v in results.items() if v]
        if ok_list:
            increment_social_count()
            track_analytics("SOCIAL", social_ok=True)
        log.info(f"[SOCIAL] Results: FB={'✅' if results['Facebook'] else '❌'} "
                 f"IG={'✅' if results['Instagram'] else '❌'} "
                 f"X={'✅' if results['Twitter'] else '❌'}")
    except Exception as e:
        log.error(f"[SOCIAL] _post_to_social_now: {e}")
    return results

def post_to_social(img_buf, caption):
    if social_paused():
        log.warning(f"🛑 Social post blocked — {_posting_block_reason()}")
        return {"Facebook": False, "Instagram": False, "Twitter": False}
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

        # Public Samuga posts must never expose original source links.
        # Send viewers to Samuga Community only.
        clean = strip_source_links(clean)
        if not public_text_is_safe(clean):
            log.error(f"🚫 Social post blocked unsafe caption: {clean[:120]}")
            return {}
        community_link = SAMUGA_CAPTION_LINK

        # FB/IG: full text + Samuga community link only
        fb_ig = clean
        if community_link and community_link not in fb_ig:
            fb_ig = fb_ig + "\n\n" + community_link
        fb_ig = fb_ig[:2200]

        # Twitter/X: first line + Samuga community link only
        lines = [l.strip() for l in clean.split('\n') if l.strip()]
        tw = (lines[0] if lines else clean)[:220]
        if community_link:
            tw = tw + "\n\n" + community_link
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
    if not public_text_is_safe(rewritten):
        log.warning(f"🚫 Card rewrite unsafe; using fallback for: {article['title'][:80]}")
        rewritten = fallback_rewritten_news(article.get("title",""), article.get("summary",""))
    keyword = safe_image_keyword(keyword, cat=raw_cat)
    bg = fetch_background_image(keyword, cat=display_cat, title=article["title"])
    ts = (utcnow() + timedelta(hours=5)).strftime("%d %b %Y • %H:%M")
    card = generate_card(rewritten, SAMUGA_PUBLIC_SOURCE, ts, display_cat, bg)
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
    if posting_paused():
        log.warning(f"🛑 Publish blocked — POSTING_PAUSED=true: {str(title)[:80]}")
        if report_to:
            rchat, rthread = report_to
            send_text(rchat, "🛑 Public posting is paused (POSTING_PAUSED=true). It was not published.", thread_id=rthread)
        return False, {}
    buf = io.BytesIO(card_bytes)
    ts = (utcnow() + timedelta(hours=5)).strftime("%d %b %Y • %H:%M")
    social_results = {}

    log.info(f"📰 [{'🔴BREAKING' if is_breaking_flag else '🟡REGULAR'}][{cat}] {title[:60]}...")
    combined_public_text = f"{title}\n{summary}\n{rewritten}\n{caption}"
    if not public_text_is_safe(combined_public_text):
        log.error(f"🚫 Publish blocked unsafe public text: {str(title)[:90]}")
        if report_to:
            rchat, rthread = report_to
            send_text(rchat, "🚫 Post blocked by placeholder safety gate. It was not published.", thread_id=rthread)
        return False, {}
    buf.seek(0)
    tg_ok = send_to_telegram(buf, caption)

    if tg_ok:
        remember_post(title, cat, ts)
        if article_id:
            _lang_for_web = ("dv" if is_dhivehi(title + " " + summary) else "en")
            if _lang_for_web != "dv" or should_publish_dhivehi_to_website(None, approved=True):
                db_publish_article_for_website(
                    article_id=article_id, title=title, summary=summary, category=cat,
                    source=SAMUGA_PUBLIC_SOURCE, link=SAMUGA_PUBLIC_LINK, lang=_lang_for_web,
                    is_breaking=is_breaking_flag
                )
            else:
                log.info(f"🌐 Dhivehi website publish skipped (approval-only policy): {str(title)[:70]}")
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

    # Social posting — ONLY breaking bypasses the queue
    if allow_social:
        social_buf = io.BytesIO(card_bytes)
        if is_breaking_flag:
            # BREAKING — blast everywhere immediately, no queue
            log.info("🔴 Breaking — posting to FB+IG+X immediately (no queue)")
            social_results = post_to_social(social_buf, caption) or {}
        else:
            # Regular — queue handles it. DO NOT call post_to_social here.
            # The caller (approval handler or auto-expiry) is responsible for queuing.
            # _publish_now only handles Telegram for non-breaking.
            pass

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
            msg += "\n\n💡 <i>Telegram failed? The article may have expired. Create a manual card instead.</i>"
        send_text(rchat, msg, thread_id=rthread)

    return tg_ok, social_results


def _send_approval_card(key, item, force=False):
    """Send a card preview to Content Lab with approve/reject buttons, rate-limited."""
    safe_ok, safe_reason = contentlab_candidate_is_safe(
        title=item.get("title",""),
        summary=item.get("dv_text") or item.get("caption") or item.get("summary",""),
        source=item.get("source",""),
        lang=item.get("lang","en"),
    )
    if not safe_ok:
        approval_queue.pop(key, None)
        persist_state()
        log.warning(f"🧱 Approval preview blocked and removed: {key} — {safe_reason}")
        return False
    if item.get("_content_lab_suppressed"):
        log.info(f"🧯 Content Lab suppressed for {key}")
        return False
    if item.get("_content_lab_sent") and not force:
        return False
    if not force:
        ok, limit, sent, high = _content_lab_slots_available(item)
        if not ok:
            item["_content_lab_sent"] = False
            item["_content_lab_delayed"] = True
            log.info(f"🧯 Content Lab delayed {key}: {sent}/{limit} previews already sent in last hour")
            persist_state()
            return False
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
        if item.get("_auto_post_breaking"):
            footer += "<i>⏰ Breaking Dhivehi — auto-posts in 2h if no action taken</i>"
        else:
            footer += "<i>⏰ Expires in 2h if not approved (regular Dhivehi never auto-posts)</i>"
    msg = header + footer

    # If we have a finished card image, send it as a photo with the caption
    if item.get("card_bytes"):
        buf = io.BytesIO(item["card_bytes"])
        send_photo(CORE_TEAM_CHAT_ID, buf, msg, thread_id=CONTENT_LAB_THREAD_ID)
    else:
        send_text(CORE_TEAM_CHAT_ID, msg, thread_id=CONTENT_LAB_THREAD_ID)
    _mark_content_lab_sent(item)
    log.info(f"📨 Approval card sent to Content Lab: {key} ({item['lang']})")
    return True


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

    # Hard safety wall before Content Lab / cards / website.
    safe_ok, safe_reason = contentlab_candidate_is_safe(
        title=article.get("title",""),
        summary=article.get("summary",""),
        source=article.get("source",""),
        lang=article.get("lang","en"),
    )
    if not safe_ok:
        seen.add(article["id"]); save_seen(seen)
        db_record_article(article, score=0,
                          reliability=source_reliability(article.get("source","")),
                          status="filtered", is_breaking=False)
        log.warning(f"🧱 Blocked unsafe story before queue: {safe_reason} — {article['title'][:90]}")
        return False

    dedup_title = story_signal_key(article.get("title",""), article.get("summary",""), article.get("lang","en"))
    article["_dedup_title"] = dedup_title or article.get("title","")

    # Mark seen now so the same article isn't re-processed on the next scan
    seen.add(article["id"]); save_seen(seen)

    # Archive every article we process (DB no-op if Postgres unavailable)
    db_record_article(article, score=score_article(article),
                      reliability=source_reliability(article.get("source","")),
                      status="seen", is_breaking=breaking)

    # ── Story clustering — track which sources report this event ──
    cluster_size, cluster_sources = register_in_cluster(article["_dedup_title"], article.get("source",""))

    # ── Duplicate story check — skip if same event already posted/queued/rejected ──
    if is_duplicate_story(article["_dedup_title"]):
        log.info(f"⏭️ Skipping duplicate ({cluster_size} sources): {article['title'][:55]}")
        db_mark_status(article["id"], "duplicate")
        return False
    # Record this normalized title so later similar stories are caught
    remember_story_title(article["_dedup_title"])
    # Stash cluster info on the article so the card can show "X sources reporting"
    article["_cluster_size"] = cluster_size
    article["_cluster_sources"] = cluster_sources

    # ── STORY INTELLIGENCE — attach this article to a story thread ──
    try:
        story_id, is_new_story, update_num = find_or_create_story(
            article["title"], cat, article["id"],
            article.get("summary", ""), article.get("source", ""), article.get("link", "")
        )
        article["_story_id"] = story_id
        article["_story_update_num"] = update_num
        article["_story_is_new"] = is_new_story
        # If this is an update to an existing developing story, notify core team
        if story_id and not is_new_story and update_num >= 2:
            log.info(f"📚 This is update #{update_num} to Story #{story_id}")
    except Exception as e:
        log.debug(f"Story attach: {e}")

    # ── Confidence gate — high-priority but unconfirmed news gets held ──
    priority = score_article(article)
    confidence, conf_reasons = confidence_score(article)
    article["_priority"] = priority
    article["_confidence"] = confidence
    hold, hold_reason = should_hold_for_review(priority, confidence, breaking)

    # ── English BREAKING: publish instantly UNLESS confidence too low ──
    if breaking and not is_dv:
        if hold:
            # Don't auto-post — queue for review with a warning instead
            log.info(f"🛑 Breaking held for review: {hold_reason} — {article['title'][:50]}")
            try:
                card_bytes, caption, rewritten, keyword = _build_card_and_caption(article)
                key = store_pending_approval(
                    card_bytes, caption, article["title"], article["link"], cat=cat, lang="en",
                    dv_text=None, keyword=keyword, source=article.get("source","LOCAL"),
                    is_breaking=True, allow_social=allow_social,
                    dedup_title=article.get("_dedup_title"), summary=article.get("summary","")
                )
                if not key:
                    return False
                approval_queue[key]["rewritten"] = rewritten
                approval_queue[key]["summary"] = article.get("summary","")
                approval_queue[key]["article_id"] = article["id"]
                approval_queue[key]["_priority"] = article.get("_priority", priority)
                approval_queue[key]["_confidence"] = confidence
                try:
                    db_publish_article_for_website(
                        article_id=article["id"], title=article["title"],
                        summary=samuga_public_summary(article.get("title", ""), article.get("summary", ""), rewritten), category=cat,
                        source=SAMUGA_PUBLIC_SOURCE,
                        link=SAMUGA_PUBLIC_LINK, lang="en",
                        score=article.get("_priority", 0),
                        reliability=source_reliability(article.get("source", "")),
                        is_breaking=True
                    )
                    log.info(f"🌐 Website published held EN breaking story: {article['title'][:60]}")
                except Exception as e:
                    log.error(f"[WEBSITE] held breaking publish failed: {e}")
                approval_queue[key]["_cluster_size"] = article.get("_cluster_size", 1)
                approval_queue[key]["_confidence"] = confidence
                approval_queue[key]["_hold_reason"] = hold_reason
                approval_queue[key]["_content_lab_suppressed"] = True
                db_mark_status(article["id"], "queued")
                approval_queue[key]["_held_for_confidence"] = False
                approval_queue[key]["_alert_only"] = True
                # Low-confidence breaking goes to Alert thread only — not Content Lab.
                send_text(CORE_TEAM_CHAT_ID,
                    f"⚠️ <b>BREAKING held for review</b>\n{hold_reason}\n\n"
                    f"<b>{article['title'][:90]}</b>\n"
                    f"Source: {article.get('source','?')} · Confidence: {confidence}%\n\n"
                    f"Approve with <code>/approved {key}</code> if verified.\n"
                    f"<i>Not sent to Content Lab to prevent flooding.</i>",
                    thread_id=ALERT_THREAD_ID)
            except Exception as e:
                log.error(f"Breaking hold queue: {e}")
            return False
        # Confidence OK — publish instantly as normal
        card_bytes, caption, rewritten, keyword = _build_card_and_caption(article)
        tg_ok, _social = _publish_now(card_bytes, caption, cat, article["title"], article["link"],
                            is_breaking_flag=True, allow_social=allow_social,
                            rewritten=rewritten, summary=article.get("summary",""),
                            article_id=article["id"])
        db_mark_status(article["id"], "posted" if tg_ok else "seen", posted=bool(tg_ok))

        # ── Auto-generate Dhivehi version for breaking news ──────────────────
        # Sent to Content Lab for review. If nobody acts in 2 hours, posts automatically.
        if tg_ok and GEMINI_API_KEY:
            def _auto_dv_breaking(_title=article["title"], _rewritten=rewritten,
                                  _link=article["link"], _cat=cat, _kw=keyword,
                                  _source=article.get("source","LOCAL"), _aid=article["id"]):
                try:
                    dv_text = make_dhivehi_caption(_rewritten, _title)
                    if not dv_text:
                        return
                    key = store_pending_approval(
                        None, None, _title, _link, cat=_cat, lang="dv",
                        dv_text=dv_text, keyword=_kw, source=_source,
                        is_breaking=True, allow_social=True,
                        dedup_title=story_signal_key(_title, _rewritten, "dv"),
                        summary=article.get("summary","")
                    )
                    if not key:
                        return
                    approval_queue[key]["article_id"] = f"{_aid}_dv"
                    approval_queue[key]["_auto_post_breaking"] = True   # 2hr auto-post flag
                    approval_queue[key]["summary"] = article.get("summary","")
                    # Pre-fetch bg
                    bg = fetch_background_image(_kw, cat=_cat, title=_title)
                    if key in approval_queue:
                        approval_queue[key]["_bg_image"] = bg
                    _send_approval_card(key, approval_queue[key])
                    # Notify Content Lab
                    send_text(CORE_TEAM_CHAT_ID,
                        f"🇲🇻 <b>Dhivehi version ready</b> — <code>{key}</code>\n"
                        f"<i>{_title[:80]}</i>\n\n"
                        f"Approve, edit or reject within 2 hours.\n"
                        f"If no action taken — <b>posts automatically at 2h mark.</b>\n\n"
                        f"/approved {key} · /approved {key} [corrected text] · /reject {key}",
                        thread_id=CONTENT_LAB_THREAD_ID)
                    log.info(f"🇲🇻 Breaking Dhivehi version queued: {key}")
                except Exception as e:
                    log.debug(f"Auto-DV breaking: {e}")
            threading.Thread(target=_auto_dv_breaking, daemon=True).start()

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
                is_breaking=breaking, allow_social=allow_social,
                dedup_title=article.get("_dedup_title"), summary=article.get("summary","")
            )
            if not key:
                return False
            approval_queue[key]["article_id"] = article["id"]
            approval_queue[key]["_priority"] = article.get("_priority", priority)
            approval_queue[key]["_confidence"] = confidence
            approval_queue[key]["_cluster_size"] = article.get("_cluster_size", 1)
            approval_queue[key]["_trend_theme"] = article.get("_trend_theme", "")
            approval_queue[key]["summary"] = article.get("summary", "")
            # Pre-fetch background in background thread so card builds instantly on approval
            def _prefetch_bg(_key=key, _kw=keyword, _title=article["title"], _cat=cat):
                try:
                    bg = fetch_background_image(_kw, cat=_cat, title=_title)
                    if _key in approval_queue:
                        approval_queue[_key]["_bg_image"] = bg
                except Exception: pass
            threading.Thread(target=_prefetch_bg, daemon=True).start()
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
            is_breaking=breaking, allow_social=allow_social,
            dedup_title=article.get("_dedup_title"), summary=article.get("summary","")
        )
        if not key:
            return False
        # Stash rewritten + summary for poll generation on approval
        approval_queue[key]["rewritten"] = rewritten
        approval_queue[key]["summary"] = article.get("summary","")
        approval_queue[key]["article_id"] = article["id"]
        approval_queue[key]["_priority"] = article.get("_priority", priority)
        approval_queue[key]["_confidence"] = confidence

        # Website-first publishing: every English story selected by the bot
        # goes to the website immediately, even while Telegram/socials wait for
        # approval or the queue. Dhivehi stays private until approved/posted.
        try:
            db_publish_article_for_website(
                article_id=article["id"],
                title=article["title"],
                summary=samuga_public_summary(article.get("title", ""), article.get("summary", ""), rewritten),
                category=cat,
                source=SAMUGA_PUBLIC_SOURCE,
                link=SAMUGA_PUBLIC_LINK,
                lang="en",
                score=article.get("_priority", 0),
                reliability=source_reliability(article.get("source", "")),
                is_breaking=breaking
            )
            log.info(f"🌐 Website published EN story immediately: {article['title'][:60]}")
        except Exception as e:
            log.error(f"[WEBSITE] EN immediate publish failed: {e}")

        approval_queue[key]["_cluster_size"] = article.get("_cluster_size", 1)
        approval_queue[key]["_trend_theme"] = article.get("_trend_theme", "")
        _send_approval_card(key, approval_queue[key])
        db_mark_status(article["id"], "queued")

        # ── Auto-generate Dhivehi version in background ──────────────────────
        # Every English article also gets a Dhivehi card queued for approval.
        # Runs in a thread so it doesn't delay the English card.
        if GEMINI_API_KEY:
            def _auto_dv(_rewritten=rewritten, _title=article["title"],
                         _link=article["link"], _cat=cat, _keyword=keyword,
                         _source=article.get("source","LOCAL"), _aid=article["id"],
                         _summary=article.get("summary", "")):
                try:
                    dv_text = make_dhivehi_caption(_rewritten, _title)
                    if not dv_text:
                        log.debug(f"[AI] Auto-Dhivehi: Gemini returned nothing for {_title[:40]}")
                        return
                    dv_key = store_pending_approval(
                        None, None, _title, _link, cat=_cat, lang="dv",
                        dv_text=dv_text, keyword=_keyword, source=_source,
                        is_breaking=breaking, allow_social=allow_social,
                        dedup_title=story_signal_key(_title, _rewritten, "dv"), summary=_summary
                    )
                    if not dv_key:
                        return
                    approval_queue[dv_key]["article_id"] = f"{_aid}_dv"
                    approval_queue[dv_key]["_priority"] = 0
                    approval_queue[dv_key]["summary"] = _summary
                    _send_approval_card(dv_key, approval_queue[dv_key])
                    log.info(f"[AI] Auto-Dhivehi queued: {_title[:50]}")
                except Exception as e:
                    log.debug(f"[AI] Auto-Dhivehi: {e}")
            threading.Thread(target=_auto_dv, daemon=True).start()

        return True
    except Exception as e:
        log.error(f"English approval queue: {e}")
        return False


# ── Run Job ───────────────────────────────────────────────────────────────────
def run_job(social_only=False, breaking_only=False):
    """
    Every 15-min scan:
      - Breaking news: posts immediately to all platforms (no queue, no limit)
      - Breaking low-confidence: goes to Alert, auto-posts in 30 min if no action
      - Regular English: max 2-3 best per HOUR go to Content Lab - bot picks, not all
      - Regular Dhivehi: max 2-3 best per HOUR go to Content Lab
      - Total Content Lab cards: max 6 per hour (3 EN + 3 DV)
      - Breaking is completely separate - never counts toward hourly budget
    """
    global daily_sports_count, daily_world_count, daily_tourism_count, _pending_article
    h = get_mvt_hour()
    log.info(f"🕐 MVT {h:02d}:xx | {'DAY' if is_day_mode() else 'NIGHT'}")
    seen = load_seen()
    articles = fetch_news()

    fresh = [a for a in articles if a["id"] not in seen]
    if not fresh:
        log.info("No fresh articles."); return

    # Pre-build clusters for corroboration scoring
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

    # ── 1. BREAKING — fires immediately, no budget, no throttle ─────────────
    if breaking_articles:
        a = breaking_articles[0]
        log.info(f"🔴 BREAKING: {a['title'][:60]}")
        post_article(a, seen, social_only=False, allow_social=True)

    if breaking_only:
        return

    # ── 2. HOURLY BUDGET — Content Lab gets max 6 cards/hr (3 EN + 3 DV) ───
    # Count how many cards were already sent to Content Lab THIS hour
    now_mvt = utcnow() + timedelta(hours=5)
    hour_start = now_mvt.replace(minute=0, second=0, microsecond=0)
    hour_start_utc = hour_start - timedelta(hours=5)

    # Count queued cards this hour (from approval_queue creation times)
    en_this_hour = sum(1 for v in approval_queue.values()
                       if v.get("lang") == "en"
                       and v.get("created_at", utcnow()) >= hour_start_utc)
    dv_this_hour = sum(1 for v in approval_queue.values()
                       if v.get("lang") == "dv"
                       and v.get("created_at", utcnow()) >= hour_start_utc)

    en_budget = max(0, 3 - en_this_hour)   # max 3 English per hour
    dv_budget = max(0, 3 - dv_this_hour)   # max 3 Dhivehi per hour

    log.info(f"📊 Hourly budget: {en_budget} EN + {dv_budget} DV remaining "
             f"({en_this_hour}+{dv_this_hour} already sent this hour)")

    if en_budget == 0 and dv_budget == 0:
        log.info("📵 Hourly Content Lab budget exhausted — skipping regular articles")
        return

    # ── 2a. Dhivehi — best DV articles up to budget ─────────────────────────
    dv_articles = [a for a in regular_articles if a.get("lang") == "dv"]
    dv_sent = 0
    for a in dv_articles:
        if dv_sent >= dv_budget:
            break
        if not is_duplicate_story(a["title"]):
            log.info(f"🇲🇻 DV → Content Lab (budget {dv_sent+1}/{dv_budget}): {a['title'][:55]}")
            post_article(a, seen, social_only=False, allow_social=False)
            dv_sent += 1
    if dv_sent:
        log.info(f"🇲🇻 {dv_sent} Dhivehi card(s) sent to Content Lab")

    # ── 2b. English — best EN articles up to budget ──────────────────────────
    if not can_post_regular():
        secs_left = int(TELEGRAM_GAP_SECONDS - (utcnow() - last_regular_post_time).total_seconds())
        log.info(f"⏳ Telegram 2hr gap active — {secs_left//60}m left (but still sending to Content Lab)")
        # Still queue for Content Lab review even if Telegram window is closed
        # The approval/auto-expiry handles the actual posting timing

    en_articles = [a for a in regular_articles if a.get("lang","en") == "en"]
    en_sent = 0
    posted_cats = set()

    for a in en_articles:
        if en_sent >= en_budget:
            break
        cat = a["cat"]
        a_score = score_article(a)
        text_lower = (a["title"] + " " + a.get("summary","")).lower()

        # Sports: Maldives national team only, max 1/day
        if cat in ["SPORTS", "FOOTBALL"]:
            mv_sports = ["maldives","dhivehi","raajje","national team","team maldives"]
            if not any(kw in text_lower for kw in mv_sports):
                continue
            if not can_post_cat_today(daily_sports_count, 1):
                continue

        # World: Maldives-relevant only, max 2/day
        elif cat == "WORLD":
            mv_world = ["maldives","indian ocean","south asia","india","china",
                        "un ","dollar","oil","global economy"]
            if not any(kw in text_lower for kw in mv_world):
                continue
            if not can_post_cat_today(daily_world_count, 2):
                continue

        # Tourism: max 2/day
        elif cat == "TOURISM":
            if not can_post_cat_today(daily_tourism_count, 2):
                continue

        # No two same category unless score is exceptional
        if cat in posted_cats and a_score < 160:
            continue

        if not is_duplicate_story(a["title"]):
            log.info(f"🟡 EN → Content Lab (budget {en_sent+1}/{en_budget}, score {a_score}): {a['title'][:55]}")
            post_article(a, seen, social_only=False, allow_social=True)
            posted_cats.add(cat)
            en_sent += 1

    if en_sent:
        log.info(f"📰 {en_sent} English card(s) sent to Content Lab")

    log.info(f"✅ run_job done — {en_sent} EN + {dv_sent} DV sent to Content Lab this run")

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
        send_text(CORE_TEAM_CHAT_ID, caption, thread_id=ALERT_THREAD_ID)
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
            thread_id=ALERT_THREAD_ID)
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
            thread_id=ALERT_THREAD_ID)
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
    send_text(CORE_TEAM_CHAT_ID, msg, thread_id=ALERT_THREAD_ID)
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
            + f"<b>Bot:</b> Samuga AI v{SAMUGA_VERSION}" + chr(10)
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


def manual_topic_search_context(headline, subheading="", category="LOCAL", lang_hint="en"):
    """Get fast web context for manually created social cards so website articles can be richer."""
    try:
        q = " ".join(x for x in [headline, subheading] if x).strip()
        if not q:
            return ""
        query = q
        # If input is Latin Dhivehi, convert to English for search.
        if looks_latin_thaana(q):
            try:
                q_en = gemini_latin_thaana_to_english(q)
                if q_en:
                    query = q_en
            except Exception:
                pass
        # Keep search focused on Maldives relevance.
        if "maldives" not in query.lower():
            query = "Maldives " + query
        return tavily_search(query[:220])[:1200]
    except Exception as e:
        log.error(f"manual_topic_search_context: {e}")
        return ""


def manual_publish_website_article(title, subheading="", category="LOCAL", source_link="", publish_now=True):
    """
    For manual social cards, prepare an English website article, optionally publish it,
    and always send the detailed article preview to Content Lab.
    Returns dict with article_id, slug, body, title, summary, published.
    """
    try:
        raw_title = (title or "").strip()
        raw_sub   = (subheading or "").strip()
        if not raw_title:
            return None
        safe_ok, safe_reason = contentlab_candidate_is_safe(raw_title, raw_sub, "Samuga Media", "en")
        if not safe_ok:
            log.warning(f"🧱 Manual website article blocked: {safe_reason} — {raw_title[:90]}")
            return None

        search_seed = (raw_title + ("\n\n" + raw_sub if raw_sub else "")).strip()
        english_title = raw_title
        english_summary = raw_sub or raw_title

        if looks_latin_thaana(search_seed):
            try:
                conv = gemini_latin_thaana_to_english(search_seed)
                if conv:
                    paras = [p.strip() for p in conv.split("\n\n") if p.strip()]
                    english_title = paras[0][:180] if paras else conv[:180]
                    english_summary = " ".join(paras[1:]).strip() if len(paras) > 1 else conv[:500]
            except Exception as e:
                log.warning(f"manual article latin→english failed: {e}")

        search_ctx = manual_topic_search_context(english_title, english_summary, category=category, lang_hint="en")
        summary_for_article = english_summary
        if search_ctx:
            summary_for_article = (english_summary + "\n\nWeb context:\n" + search_ctx).strip()

        article_id = "manual_" + hashlib.md5((english_title + "|" + summary_for_article + "|" + str(utcnow())).encode()).hexdigest()[:12]
        body = generate_website_article_body(
            title=english_title,
            summary=summary_for_article,
            category=category or "LOCAL",
            source=SAMUGA_PUBLIC_SOURCE,
            is_breaking=(category or "").upper() in ("BREAKING", "DISASTER")
        )
        slug = make_article_slug(english_title, article_id)

        if publish_now:
            db_publish_article_for_website(
                article_id=article_id,
                title=english_title[:500],
                summary=summary_for_article[:2500],
                category=category or "LOCAL",
                source=SAMUGA_PUBLIC_SOURCE,
                link=(source_link or SAMUGA_CAPTION_LINK or "").strip(),
                lang="en",
                score=190,
                reliability=95,
                is_breaking=(category or "").upper() in ("BREAKING", "DISASTER")
            )
            try:
                row = db_execute("SELECT article_slug, article_body FROM articles WHERE id=%s", (article_id,), fetch="one")
                if row:
                    slug = row[0] or slug
                    body = row[1] or body
            except Exception:
                pass

        preview = (
            f"📝 <b>Manual Website Article {'Published' if publish_now else 'Prepared'}</b>\n\n"
            f"<b>{english_title}</b>\n\n"
            f"{(body or summary_for_article or english_title)[:3500]}\n\n"
            f"🌐 <b>Website:</b> {SAMUGA_CAPTION_LINK}"
            + (f"/article.html?id={article_id}" if publish_now and article_id else "")
        )
        try:
            send_text(CORE_TEAM_CHAT_ID, preview, thread_id=CONTENT_LAB_THREAD_ID)
        except Exception as e:
            log.warning(f"manual article preview to content lab: {e}")

        return {
            "article_id": article_id,
            "slug": slug,
            "body": body,
            "title": english_title,
            "summary": summary_for_article,
            "category": category or "LOCAL",
            "published": bool(publish_now),
        }
    except Exception as e:
        log.error(f"manual_publish_website_article: {e}")
        return None


def manual_post_replied_article_to_website(reply_text, category_hint="LOCAL"):
    """
    Publish a human-written article from a replied Telegram message directly to the website.
    First non-empty line = title. Remaining lines = body.
    """
    try:
        raw = strip_source_links(str(reply_text or "")).strip()
        if not raw:
            return None, "Reply to the drafted article text first."
        parts = [p.strip() for p in raw.split("\n") if p.strip()]
        if not parts:
            return None, "Reply text is empty."

        parts = [p for p in parts if re.sub(r'@SamugaNewsBot\b', '', p, flags=re.I).strip().lower() not in ["/post to web", "/post web", "/posttoweb", "/postweb"]]
        if not parts:
            return None, "Only the command was found. Reply to the actual article text."

        title = parts[0][:220]
        body = "\n\n".join(parts[1:]).strip() if len(parts) > 1 else ""
        if not body:
            return None, "Article body is empty. Write the title on line 1 and the article on the lines below."

        lang = "dv" if is_dhivehi(title + " " + body) else "en"
        safe_ok, safe_reason = contentlab_candidate_is_safe(title, body, "Samuga Media", lang)
        if not safe_ok:
            alert_admin(f"Manual website post blocked\n\n<b>{title[:120]}</b>\nReason: {safe_reason}", dedupe_key=f"manualweb:{title[:80]}")
            return None, f"Blocked by safety wall: {safe_reason}"

        category = canonical_category(category_hint or "LOCAL", title, body)
        base_id = "manualweb_" + hashlib.md5((title + "|" + body + "|" + str(utcnow())).encode()).hexdigest()[:12]
        db_publish_article_for_website(
            article_id=base_id,
            title=title,
            summary=body[:2500],
            category=category,
            source=SAMUGA_PUBLIC_SOURCE,
            link=SAMUGA_CAPTION_LINK,
            lang=lang,
            score=195,
            reliability=99,
            is_breaking=(category in ("BREAKING","DISASTER"))
        )
        saved_id = base_id if lang != "dv" else f"{base_id}_dv"

        row = None
        try:
            excerpt = make_article_excerpt(title, body, lang=lang)
            db_execute(
                "UPDATE articles SET article_body=%s, article_excerpt=%s, status='posted' WHERE id=%s RETURNING id, article_slug",
                (body, excerpt, saved_id), fetch=None
            )
            row = db_execute("SELECT id, article_slug, status FROM articles WHERE id=%s LIMIT 1", (saved_id,), fetch="one")
        except Exception as e:
            log.warning(f"manual_post_replied_article_to_website body persist: {e}")

        if not row:
            row = db_execute("SELECT id, article_slug, status FROM articles WHERE id=%s LIMIT 1", (saved_id,), fetch="one")
        if not row:
            return None, "The article was not found in the database after publish."

        _, slug, status = row
        url = website_article_url(article_id=saved_id, slug=slug)
        return {
            "article_id": saved_id,
            "slug": slug or make_article_slug(title, saved_id),
            "title": title,
            "body": body,
            "category": category,
            "lang": lang,
            "url": url,
            "status": status or "posted",
        }, None
    except Exception as e:
        log.error(f"manual_post_replied_article_to_website: {e}")
        alert_admin(f"Manual website post failed\n\nReason: {str(e)[:300]}", dedupe_key="manual_post_replied_article_to_website")
        return None, str(e)


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
        # Try web search for Dhivehi queries too
        web_context = ""
        try:
            if needs_web_search(user_message) or not context:
                web_context = tavily_search("maldives news today 2026")
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

        # Try models in fallback order
        for model in GEMINI_MODELS:
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
                resp = requests.post(url, json=payload, timeout=15)
                if resp.status_code == 200:
                    reply = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                    log.info(f"✅ Gemini Dhivehi chat done ({model})")
                    return reply
                elif resp.status_code in [429, 503]:
                    log.warning(f"[AI] Gemini {model} quota/unavailable, trying next")
                    continue
                else:
                    log.error(f"Gemini {model} HTTP {resp.status_code}")
                    break
            except Exception as e:
                log.warning(f"[AI] Gemini {model}: {e}")
                continue
    except Exception as e:
        log.error(f"Gemini Dhivehi chat error: {e}")
    return None

def answer_story_query(message):
    """
    If the message is asking about a past event ('what happened with the ferry'),
    search stories and return a formatted timeline answer. Returns None if no match.
    """
    if not DB_ENABLED:
        return None
    ml = message.lower()
    # Triggers that suggest someone is asking about an ongoing/past event
    triggers = ["what happened", "what's happening", "whats happening", "update on",
                "latest on", "any news on", "any update", "tell me about the",
                "what about the", "story of", "develop", "kobaa", "vaahaka"]
    if not any(t in ml for t in triggers):
        return None

    matches = search_stories(message, limit=3)
    if not matches:
        return None

    best = matches[0]
    timeline = get_story_timeline(best["id"])
    if not timeline or timeline["update_count"] < 2:
        return None

    from datetime import timedelta as _td
    lines = [f"📚 <b>{timeline['title']}</b>",
             f"<i>Story #{timeline['id']} · {timeline['update_count']} updates · {timeline['status']}</i>\n"]
    for u in timeline["updates"]:
        t = u["time"]
        tstr = (t + _td(hours=5)).strftime("%d %b %H:%M") if t else ""
        src = f" ({u['source']})" if u["source"] else ""
        lines.append(f"🔹 <b>{tstr}</b>{src} — {u['headline'][:90]}")
    if len(matches) > 1:
        lines.append("\n<i>Also tracking: " +
                     ", ".join(f"#{m['id']}" for m in matches[1:]) + " — use /story [id]</i>")
    return "\n".join(lines)

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

# ── Newsroom snapshot — cached 10 min, injected into brain when relevant ─────
_snapshot_cache = {"data": None, "ts": None}
_SNAPSHOT_TTL = 600  # 10 minutes

def get_newsroom_snapshot():
    """
    Pull a tight live snapshot from the DB. Cached 10 min — safe to call on
    every tagged message without hammering the DB or wasting tokens.
    Returns a short string (~300 tokens max) or "" if DB off.
    """
    global _snapshot_cache
    if not DB_ENABLED:
        return ""
    now = utcnow()
    if (_snapshot_cache["ts"] and
            (now - _snapshot_cache["ts"]).total_seconds() < _SNAPSHOT_TTL and
            _snapshot_cache["data"]):
        return _snapshot_cache["data"]
    try:
        lines = []

        # ── What we posted today ─────────────────────────────────────────────
        posted_today = db_execute("""
            SELECT title, category, source, posted_at
            FROM articles
            WHERE status='posted' AND posted_at > NOW() - INTERVAL '24 hours'
            ORDER BY posted_at DESC LIMIT 6
        """, fetch="all") or []
        if posted_today:
            lines.append("POSTED TODAY:")
            for title, cat, src, ts in posted_today:
                from datetime import timedelta as _td
                mvt = (ts + _td(hours=5)).strftime("%H:%M") if ts else ""
                lines.append(f"  {mvt} [{cat}] {title[:55]} ({src})")

        # ── Quick stats ──────────────────────────────────────────────────────
        scanned = db_execute("SELECT COUNT(*) FROM articles WHERE found_at > NOW() - INTERVAL '24 hours'", fetch="one")
        posted_n = db_execute("SELECT COUNT(*) FROM articles WHERE status='posted' AND posted_at > NOW() - INTERVAL '24 hours'", fetch="one")
        queued_n = db_execute("SELECT COUNT(*) FROM articles WHERE status='queued'", fetch="one")
        lines.append(f"\nTODAY: {posted_n[0] if posted_n else 0} posted, {scanned[0] if scanned else 0} scanned, {queued_n[0] if queued_n else 0} waiting approval")

        # ── Best performer ───────────────────────────────────────────────────
        top = db_execute("""
            SELECT title, tg_views, meta_engagement
            FROM articles
            WHERE status='posted' AND posted_at > NOW() - INTERVAL '48 hours'
              AND (tg_views > 0 OR meta_engagement > 0)
            ORDER BY (tg_views + meta_engagement * 3) DESC LIMIT 1
        """, fetch="one")
        if top:
            lines.append(f"TOP PERFORMER: {top[0][:55]} ({top[1]} views, {top[2]} reactions)")

        # ── Developing stories ───────────────────────────────────────────────
        dev = db_execute("""
            SELECT id, title, update_count FROM stories
            WHERE status='developing' AND last_update > NOW() - INTERVAL '24 hours'
            ORDER BY update_count DESC LIMIT 3
        """, fetch="all") or []
        if dev:
            lines.append("\nDEVELOPING STORIES:")
            for sid, t, n in dev:
                lines.append(f"  Story #{sid} ({n} updates): {t[:55]}")

        # ── Trending themes ──────────────────────────────────────────────────
        try:
            trends = detect_trends(hours=24, min_mentions=2)
            if trends:
                top_themes = ", ".join(t[0] for t in trends[:4])
                lines.append(f"\nTRENDING: {top_themes}")
        except:
            pass

        # ── Pending approvals ────────────────────────────────────────────────
        pending_keys = [k for k, v in approval_queue.items()
                        if not v.get("expired", False)][:3]
        if pending_keys:
            lines.append(f"\nPENDING APPROVAL: {len(pending_keys)} card(s) waiting")

        snapshot = "\n".join(lines)
        _snapshot_cache = {"data": snapshot, "ts": now}
        return snapshot

    except Exception as e:
        log.debug(f"Snapshot: {e}")
        return ""

def _needs_newsroom_context(message):
    """
    Returns True only when the conversation is about newsroom operations.
    If someone says 'lol ok' or 'thanks', skip the snapshot — save tokens.
    """
    ml = message.lower()
    keywords = [
        "post", "story", "news", "publish", "article", "trending", "what did",
        "what have", "today", "engagement", "views", "reactions", "performing",
        "pending", "queue", "approval", "developing", "happening", "viral",
        "breaking", "latest", "update", "idea", "suggest", "cover", "topic",
        "what should", "should we", "think about", "what about", "plan"
    ]
    return any(k in ml for k in keywords)

def should_respond_proactively(text, sender_name=""):
    """
    Use Claude to decide in 1 token whether the bot should jump in.
    Returns (should_respond: bool, needs_search: bool).
    Fast — uses Haiku, max 10 tokens, binary decision.
    """
    # Hard skip: very short messages, pure reactions, stickers
    t = text.strip()
    if len(t) < 6:
        return False, False
    # Skip if it's clearly a command or approval
    if t.startswith("/") or t.lower().startswith("/approved") or t.lower().startswith("/reject"):
        return False, False
    # Also skip fuzzy approve/reject attempts (e.g. "approved dv48", "reject en12")
    import re as _re2
    if _re2.match(r'^(appro|appr|rejec)[a-z]*\s+[a-z]{1,3}\d+', t.lower()):
        return False, False

    try:
        prompt = f"""You are deciding if an AI team member should respond to a Telegram message.
Respond YES if the message: asks a question, discusses content/strategy/news, shares an idea, 
needs feedback, mentions something newsworthy, or where input would genuinely help.
Respond NO if: it's casual chitchat with no substance, greetings only, one-word reactions, 
or internal team logistics where AI input isn't needed.
Also add SEARCH if the message is about current events or news that may need web lookup.

Message from {sender_name}: "{t}"

Reply with ONLY one of: YES / YES+SEARCH / NO"""

        msg = ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": prompt}]
        )
        decision = msg.content[0].text.strip().upper()
        should = "YES" in decision
        search = "SEARCH" in decision
        return should, search
    except Exception as e:
        log.debug(f"Proactive decision: {e}")
        # Fallback to keyword check
        t_lower = t.lower()
        return any(kw in t_lower for kw in ["?","idea","think","suggest","post","story","plan","content","caption"]), False

def chat_with_coreteam(message, sender_name, sender_info=None, conversation_history=None,
                       session_ctx="", needs_search=False):
    """
    Samuga AI core team brain — Claude Sonnet, persistent memory, web search.
    Talks like a smart team member, not a bot.
    """
    try:
        # ── Gather context in parallel ────────────────────────────────────────
        ctx_results = {}

        def _fetch_news():
            try: ctx_results["news"] = get_local_headlines()
            except: ctx_results["news"] = []

        def _fetch_web():
            if needs_search:
                try:
                    q = message
                    if "maldives" not in message.lower():
                        q = f"maldives {message} 2026"
                    ctx_results["web"] = tavily_search(q) or ""
                except: ctx_results["web"] = ""

        def _fetch_memory():
            try: ctx_results["memory"] = mem_list(20)
            except: ctx_results["memory"] = []

        def _fetch_snapshot():
            # Only pull the live snapshot if this message is newsroom-related
            if _needs_newsroom_context(message):
                try: ctx_results["snapshot"] = get_newsroom_snapshot()
                except: ctx_results["snapshot"] = ""

        threads = [
            threading.Thread(target=_fetch_news),
            threading.Thread(target=_fetch_web),
            threading.Thread(target=_fetch_memory),
            threading.Thread(target=_fetch_snapshot),
        ]
        for t in threads: t.start()
        for t in threads: t.join(timeout=6)

        headlines = ctx_results.get("news", [])
        web_info  = ctx_results.get("web", "")
        memories  = ctx_results.get("memory", [])
        snapshot  = ctx_results.get("snapshot", "")

        # ── Recent posts ──────────────────────────────────────────────────────
        recent_ctx = ""
        if recent_posts:
            recent_ctx = "Recent posts:\n" + "".join(
                [f"• [{p['cat']}] {p['title']}\n" for p in recent_posts[-8:]])

        # ── Build context block — smart, not everything every time ──────────
        context_parts = []
        if snapshot:  # only included when message is newsroom-related
            context_parts.append(f"LIVE NEWSROOM STATUS:\n{snapshot}")
        if web_info:
            context_parts.append(f"LIVE WEB SEARCH:\n{web_info[:800]}")
        if headlines:
            context_parts.append("MALDIVES NEWS RIGHT NOW:\n" + "\n".join(headlines[:5]))
        if recent_ctx:
            context_parts.append(recent_ctx)
        if memories:
            context_parts.append("WHAT I KNOW ABOUT THE TEAM & AUDIENCE:\n" +
                                  "\n".join(memories[:12]))
        if session_ctx:
            context_parts.append(f"SESSION CONTEXT:\n{session_ctx}")

        context_block = "\n\n".join(context_parts)

        # ── Sender context ────────────────────────────────────────────────────
        if sender_info:
            sender_ctx = f"{sender_info['name']} ({sender_info['role']}) — {sender_info['notes']}"
        else:
            sender_ctx = sender_name or "a team member"

        system = f"""You are Samuga AI — the brain of Samuga Media, Maldives' first AI-powered newsroom.

You live inside the core team's private Telegram group. You are not an assistant — you're a team member who happens to be an AI. You've been in the newsroom all day. You know what was posted, what's trending, what's developing, and what the audience responded to.

THE TEAM:
- Manchii (Abdul Muhsin) — Founder & MD. Visionary, builds at 2am, takes calculated risks. Roast him gently when earned.
- Uly (Mariyam Ulya) — Editor-in-Chief. Sharp journalist brain, catches errors before anyone. You trust her on content.
- Thooma (Aminath Thooma) — Presenter & Marketing. The face of Samuga, full of energy. Always back her up.
- Kity — Manchii's wife, creative contributor. Fresh ideas, always worth taking seriously.

YOU ARE SPEAKING WITH: {sender_ctx}

HOW YOU TALK:
- Casual, warm, direct — like a smart Maldivian colleague
- SHORT by default (1-3 sentences). Only go long when they need detail.
- You have opinions. Weak story idea? Say so. Great angle? Say so.
- Crack jokes when the vibe is right, especially with Manchii
- Never say "I'm just an AI" — you're part of the team
- No corporate language. No customer service voice.
- Read the room — if someone's stressed, match that energy

WHEN YOU HAVE LIVE NEWSROOM DATA (in context below):
- Use it naturally, like you've been watching the newsroom all day
- Spot the ONE thing worth mentioning — a developing story, an engagement spike, a gap
- Don't dump everything. Pick what's most relevant to what they just said.
- If nothing newsworthy stands out, just answer without adding noise.
- Suggest specific angles: "the Addu angle hasn't been touched" beats "cover more regions"

SAMUGA'S VOICE: Real stories, no filter, people first. The compass for the people.

{context_block}"""

        messages = []
        if conversation_history:
            messages = conversation_history[-10:]
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
        try:
            msg = ai.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                messages=[{"role": "user", "content": message}]
            )
            return msg.content[0].text.strip()
        except:
            return None

# ── Chat Handler ──────────────────────────────────────────────────────────────
# Per-user daily DM/search limit (resets at MVT midnight).
DM_DAILY_LIMIT = int(os.environ.get("DM_DAILY_LIMIT", "20"))
_dm_usage = {}  # user_id -> {"date": "YYYY-MM-DD", "count": int}


def dm_check_and_increment(user_id):
    """Check and increment a user's daily DM/search usage.
    Returns (allowed, count, limit). When not allowed, the count is left at the
    limit and not incremented further."""
    today = (utcnow() + timedelta(hours=5)).strftime("%Y-%m-%d")
    rec = _dm_usage.get(user_id)
    if not rec or rec.get("date") != today:
        rec = {"date": today, "count": 0}
        _dm_usage[user_id] = rec
    if rec["count"] >= DM_DAILY_LIMIT:
        return False, rec["count"], DM_DAILY_LIMIT
    rec["count"] += 1
    return True, rec["count"], DM_DAILY_LIMIT


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
                text_cmd = re.sub(r'@SamugaNewsBot\b', '', text or '', flags=re.I).strip()
                text_cmd_low = text_cmd.lower()
                photo=msg.get("photo")  # list of photo sizes if message has photo
                video=msg.get("video") or msg.get("video_note")
                reply_msg = msg.get("reply_to_message", {}) or {}
                reply_text = reply_msg.get("text","") or reply_msg.get("caption","") or ""
                reply_msg_id = reply_msg.get("message_id")
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
                            f"📡 Follow <b>@samugacommunity</b> for live news updates!", reply_to=msg_id)
                    elif text.startswith("/search "):
                        # Rate limit applies to /search too
                        allowed, count, limit = dm_check_and_increment(user_id)
                        if not allowed:
                            send_text(chat_id,
                                f"You've reached today's limit of {limit} messages 🙏\n\n"
                                f"Come back tomorrow for more! Meanwhile follow "
                                f"<b>@samugacommunity</b> for live Maldives news. 📡",
                                reply_to=msg_id)
                        else:
                            query = text[8:].strip()
                            log.info(f"🔍 Search: {query}")
                            results = tavily_search(f"{query} maldives")
                            reply = chat_with_claude(f"Tell me about: {query}. Use this info: {results[:400]}", user_id)
                            send_text(chat_id, reply, reply_to=msg_id, thread_id=thread_id)
                    else:
                        # ── Rate limit check ──────────────────────────────────
                        allowed, count, limit = dm_check_and_increment(user_id)
                        if not allowed:
                            send_text(chat_id,
                                f"You've reached today's limit of {limit} messages 🙏\n\n"
                                f"Come back tomorrow! Follow <b>@samugacommunity</b> "
                                f"for live Maldives news in the meantime. 📡",
                                reply_to=msg_id)
                            log.info(f"🚫 DM rate limit hit: {display_name} ({user_id})")
                        else:
                            log.info(f"💬 Public Telegram Samuga AI {display_name} [{count}/{limit}]: {text[:50]}")
                            try:
                                reply = public_samuga_ai_chat(
                                    message=text,
                                    platform="telegram",
                                    user_key=user_id,
                                    session_id=str(chat_id),
                                    lang=("dv" if is_dhivehi(text) else "en")
                                )
                            except Exception as e:
                                log.error(f"Unified public Telegram chat failed: {e}")
                                reply = "Small issue on my side bro 😅 Try again in a moment."
                            send_text(chat_id, reply, reply_to=msg_id, thread_id=thread_id)

                elif chat_type in ["group","supergroup"]:
                    is_core_team = str(chat_id) == CORE_TEAM_CHAT_ID
                    tagged = bot_mention in text.lower()
                    clean = text.replace(bot_mention, "").strip() if tagged else text.strip()

                    # Core team group — smarter behavior
                    if is_core_team:
                        sender_info = get_sender_info(display_name, first_name)
                        history = get_conversation(user_id)

                        # ── Fuzzy command normaliser ──────────────────────────────────────
                        # Accepts typos, missing slash, wrong spelling — as long as intent
                        # and key are clear. Ulya-proof.
                        # approve variants: /approved, /approve, /aprroved, /aprrove, approved, approve
                        # reject variants:  /reject, /rejected, /rejecte, /rejects, reject, rejected
                        def _parse_fuzzy_cmd(raw):
                            """
                            Returns (cmd, key, extra) where cmd is 'approve' or 'reject',
                            key is e.g. 'dv48' or 'en12', extra is optional corrected text.
                            Returns (None, None, None) if not recognised.
                            """
                            import re as _re
                            t = raw.strip()
                            # Remove leading slash if present
                            if t.startswith("/"): t = t[1:]
                            tl = t.lower()
                            # Split on whitespace — first token is the command word
                            tokens = tl.split()
                            if not tokens: return (None, None, None)
                            cmd_word = tokens[0]
                            # Normalise doubled letters: "aprroved" → "aproved", "rejecte" → "rejecte"
                            import re as _re
                            cmd_norm = _re.sub(r'(.)\1+', r'\1', cmd_word)
                            # Approve variants: /approved /approve /aprroved /aprrove approved approve
                            is_approve = (cmd_word.startswith("appro") or
                                          cmd_norm.startswith("appro") or
                                          cmd_norm.startswith("apro") or
                                          cmd_word in ["approve", "approved"])
                            # Reject variants: /reject /rejected /rejecte /rejects reject rejected
                            is_reject  = (cmd_word.startswith("rejec") or
                                          cmd_norm.startswith("rejec") or
                                          cmd_word in ["reject", "rejected"])
                            if not is_approve and not is_reject:
                                return (None, None, None)
                            # Find the key — pattern like dv48, en12, en50 etc.
                            # Can be in any token after the command word
                            rest_raw = raw.strip()
                            if rest_raw.startswith("/"): rest_raw = rest_raw[1:]
                            rest_tokens = rest_raw.split()
                            key = None
                            key_idx = None
                            for i, tok in enumerate(rest_tokens[1:], 1):
                                if _re.match(r'^[a-zA-Z]{1,3}\d+$', tok):
                                    key = tok.lower()
                                    key_idx = i
                                    break
                            if not key: return (None, None, None)
                            # Extra text after the key = corrected Dhivehi
                            extra = None
                            if key_idx is not None and len(rest_tokens) > key_idx + 1:
                                extra = " ".join(rest_tokens[key_idx+1:]).strip() or None
                            cmd = "approve" if is_approve else "reject"
                            return (cmd, key, extra)

                        _fcmd, _fkey, _fextra = _parse_fuzzy_cmd(text_cmd)

                        # /approved <key> [optional corrected dhivehi text]
                        if _fcmd == "approve" or text.strip().lower().startswith("/approved "):
                            if _fcmd == "approve" and _fkey:
                                key      = _fkey
                                corrected = _fextra
                            else:
                                parts     = text.strip()[10:].strip().split(" ", 1)
                                key       = parts[0].strip().lower()
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
                                        # Run card generation in background — gives Uly instant feedback
                                        def _process_dv(_item=item, _key=key, _corrected=corrected,
                                                        _cid=chat_id, _tid=thread_id, _fname=first_name, _mid=msg_id):
                                            try:
                                                final_dv = _corrected if _corrected else _item["dv_text"]
                                                kw = _item.get("keyword", _item["cat"].lower())
                                                # Use pre-fetched bg if available, else fetch now
                                                bg = _item.get("_bg_image") or fetch_background_image(kw)
                                                ts_now = (utcnow() + timedelta(hours=5)).strftime("%d %b %Y • %H:%M")
                                                card = generate_card(final_dv, SAMUGA_PUBLIC_SOURCE, ts_now, _item["cat"], bg)
                                                full_caption = (
                                                    f"🇲🇻 <b>{_item['title']}</b>\n\n"
                                                    f"{final_dv}\n\n"
                                                    f"📡 <b>ސަމުގާ މީޑިއާ</b> | @samugacommunity"
                                                )
                                                card.seek(0)
                                                # Queue handles Telegram + FB + IG + X with 10-min gap
                                                # tg_ok=False initially — queue will post Telegram too
                                                remember_post(_item["title"], _item["cat"], ts_now)
                                                db_mark_status(_item.get("article_id",""), "posted", posted=True)
                                                db_log_learning(
                                                    article_id=_item.get("article_id"),
                                                    action=("edited" if _corrected else "approved"),
                                                    member=_fname,
                                                    category=_item.get("cat",""),
                                                    source=_item.get("source",""),
                                                    theme=_item.get("_trend_theme",""),
                                                    original_caption=_item.get("dv_text",""),
                                                    final_caption=(_corrected or _item.get("dv_text","")),
                                                    lang="dv")
                                                queue_for_social(
                                                    io.BytesIO(card.getvalue()), full_caption,
                                                    notify_chat_id=_cid,
                                                    notify_thread_id=_tid,
                                                    key_label=_key.upper(),
                                                    tg_ok=False,
                                                    post_telegram=True,   # queue posts Telegram too
                                                    article_id=_item.get("article_id"),
                                                    title=_item.get("title",""),
                                                    summary=_item.get("summary",""),
                                                    cat=_item.get("cat","LOCAL"),
                                                    source=_item.get("source","Samuga Media"),
                                                    link=_item.get("link",""),
                                                    lang="dv",
                                                    is_breaking=_item.get("is_breaking", False)
                                                )
                                            except Exception as e:
                                                log.error(f"DV approval processing: {e}")
                                                send_text(_cid, f"❌ Error processing {_key}: {e}", thread_id=_tid)
                                        threading.Thread(target=_process_dv, daemon=True).start()
                                        ok = True  # optimistic — thread handles actual result
                                    else:
                                        # English — queue for Telegram + social (10-min gap)
                                        # EXCEPT breaking which fires immediately
                                        is_breaking_card = item.get("is_breaking", False)
                                        if is_breaking_card:
                                            # Breaking bypasses queue — fires to all platforms now
                                            tg_ok, social_res = _publish_now(
                                                item["card_bytes"], item["caption"], item["cat"],
                                                item["title"], item["link"],
                                                is_breaking_flag=True,
                                                allow_social=item.get("allow_social", True),
                                                rewritten=item.get("rewritten",""),
                                                summary=item.get("summary",""),
                                                report_to=(chat_id, thread_id),
                                                article_id=item.get("article_id")
                                            )
                                            ok = tg_ok
                                        else:
                                            # Regular — joins queue with 10-min gap
                                            buf = io.BytesIO(item["card_bytes"])
                                            queue_for_social(
                                                buf, item["caption"],
                                                notify_chat_id=chat_id,
                                                notify_thread_id=thread_id,
                                                key_label=key.upper(),
                                                tg_ok=False,
                                                post_telegram=True,
                                                article_id=item.get("article_id"),
                                                title=item.get("title",""),
                                                summary=item.get("summary",""),
                                                cat=item.get("cat","LOCAL"),
                                                source=item.get("source","Samuga Media"),
                                                link=item.get("link",""),
                                                lang=item.get("lang","en"),
                                                is_breaking=item.get("is_breaking", False)
                                            )
                                            ok = True

                                    # DV cards handled entirely in background thread above
                                    if item.get("lang") == "dv":
                                        pass  # thread handles posting, confirmation and logging
                                    elif ok:
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
                        elif _fcmd == "reject" or text.strip().lower().startswith("/reject "):
                            key = _fkey if (_fcmd == "reject" and _fkey) else text.strip()[8:].strip().lower()
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
                                try:
                                    remember_story_title(rej_item.get("_dedup_title") or rej_item.get("title",""))
                                except Exception:
                                    pass
                                del approval_queue[key]
                                persist_state()
                                import random as _r
                                send_text(chat_id, f"❌ <b>{key.upper()}</b> rejected — {rej_title}\n\n{_r.choice(REJECT_RESPONSES)}", reply_to=msg_id, thread_id=thread_id)
                                log.info(f"🗑️ {key} rejected by {first_name}")
                            else:
                                send_text(chat_id, f"Key <code>{key}</code> not found — maybe already posted or rejected.", reply_to=msg_id, thread_id=thread_id)

                        # /hide <article_id_or_slug> — remove a website article fast
                        elif text_cmd_low.startswith("/hide "):
                            ident = text_cmd[6:].strip()
                            rows = db_hide_article(ident)
                            if rows:
                                joined = "\n".join([f"• <code>{rid}</code> — {ttl[:70]}" for rid, ttl in rows[:5]])
                                send_text(chat_id, f"🙈 <b>Hidden from website</b>\n\n{joined}", reply_to=msg_id, thread_id=thread_id)
                            else:
                                send_text(chat_id, f"⚠️ No website article found for <code>{ident}</code>", reply_to=msg_id, thread_id=thread_id)

                        # /unhide <article_id_or_slug> — restore hidden article
                        elif text_cmd_low.startswith("/unhide "):
                            ident = text_cmd[8:].strip()
                            rows = db_unhide_article(ident)
                            if rows:
                                joined = "\n".join([f"• <code>{rid}</code> — {ttl[:70]}" for rid, ttl in rows[:5]])
                                send_text(chat_id, f"👀 <b>Restored on website</b>\n\n{joined}", reply_to=msg_id, thread_id=thread_id)
                            else:
                                send_text(chat_id, f"⚠️ No hidden website article found for <code>{ident}</code>", reply_to=msg_id, thread_id=thread_id)

                        # /delete — reply to a bot message in Content Lab to delete it and remove queue item if present
                        elif text_cmd_low in ["/delete", "/del", "/remove"]:
                            if not reply_msg_id:
                                send_text(chat_id,
                                    "Reply to the bot message you want deleted, then send <code>/delete</code>.",
                                    reply_to=msg_id, thread_id=thread_id)
                            else:
                                removed_key = None
                                try:
                                    lower_reply = reply_text.lower()
                                    m = re.search(r"\b((?:dv|en)\d+)\b", lower_reply)
                                    if m and m.group(1) in approval_queue:
                                        removed_key = m.group(1)
                                        approval_queue.pop(removed_key, None)
                                        persist_state()
                                except Exception:
                                    pass
                                ok = delete_telegram_message(chat_id, reply_msg_id)
                                if ok:
                                    msg_text = "🗑️ <b>Deleted from Content Lab.</b>"
                                    if removed_key:
                                        msg_text += f" Queue item <code>{removed_key}</code> removed too."
                                    send_text(chat_id, msg_text, reply_to=msg_id, thread_id=thread_id)
                                else:
                                    send_text(chat_id,
                                        "⚠️ I couldn't delete that Telegram message. Check bot admin rights in the group.",
                                        reply_to=msg_id, thread_id=thread_id)
                                    alert_admin("Content Lab delete failed. Check bot delete permissions in Telegram.", dedupe_key="telegram_delete_permission")

                                                # /post to web — reply to a human-written article or include article + command in same message
                        elif text_cmd_low in ["/post to web", "/post web", "/posttoweb"] or "/post to web" in text_cmd_low:
                            article_source = ""
                            if reply_text.strip():
                                article_source = re.sub(r'@SamugaNewsBot\b', '', reply_text or '', flags=re.I).strip()
                            else:
                                article_source = extract_inline_post_to_web_body(text)
                            if not article_source.strip():
                                send_text(chat_id,
                                    "⚠️ Website post needs article text. Either reply to the article and send <code>/post to web</code>, or send the article with <code>/post to web</code> at the bottom.",
                                    reply_to=msg_id, thread_id=thread_id)
                            else:
                                send_text(chat_id, "🌐 Publishing article to website... ⏳", reply_to=msg_id, thread_id=thread_id)
                                result, err = manual_post_replied_article_to_website(article_source, category_hint="LOCAL")
                                if result:
                                    send_text(chat_id,
                                        f"✅ <b>Posted to website</b>\n\n"
                                        f"<b>{result['title']}</b>\n"
                                        f"Category: {result['category']}\n"
                                        f"Lang: {result['lang']}\n"
                                        f"ID: <code>{result['article_id']}</code>\n"
                                        f"Link: {result['url']}",
                                        reply_to=msg_id, thread_id=thread_id)
                                else:
                                    send_text(chat_id, f"❌ Facing some issues posting to website.\nReason: {err}", reply_to=msg_id, thread_id=thread_id)
                                    alert_admin(f"Manual website publish failed\n\nReason: {str(err)[:300]}", dedupe_key="manual_web_post_fail")

# /delete https://samugamedia.com/... — hide a website article by URL
                        elif text_cmd_low.startswith("/delete http://") or text_cmd_low.startswith("/delete https://"):
                            try:
                                url = text_cmd[8:].strip()
                                rows = db_delete_article_by_url(url)
                                if rows:
                                    joined = "\n".join([f"• <code>{rid}</code> — {ttl[:70]}" for rid, ttl, slug in rows[:5]])
                                    send_text(chat_id,
                                        f"🗑️ <b>Hidden from website by URL</b>\n\n{joined}",
                                        reply_to=msg_id, thread_id=thread_id)
                                else:
                                    send_text(chat_id,
                                        "⚠️ No website article matched that URL. Check the full post link or slug.",
                                        reply_to=msg_id, thread_id=thread_id)
                            except Exception as e:
                                send_text(chat_id, f"❌ Delete by URL failed: {str(e)[:150]}", reply_to=msg_id, thread_id=thread_id)
                                alert_admin(f"Delete by URL failed\n\nReason: {str(e)[:250]}", dedupe_key="cmd_delete_by_url_fail")

# /hide_dv — hide all currently posted Dhivehi website articles
                        elif text.strip().lower() in ["/hide_dv", "/hide dv", "/hide all dv"]:
                            rows = db_hide_all_dhivehi()
                            if rows:
                                send_text(chat_id,
                                    f"🙈 <b>Hidden Dhivehi website articles:</b> {len(rows)}",
                                    reply_to=msg_id, thread_id=thread_id)
                            else:
                                send_text(chat_id,
                                    "ℹ️ No posted Dhivehi website articles found to hide.",
                                    reply_to=msg_id, thread_id=thread_id)


                        # /stats — quick operational stats
                        elif text_cmd_low in ["/stats", "/botstats", "/stat"]:
                            try:
                                send_text(chat_id, format_bot_stats(), reply_to=msg_id, thread_id=thread_id)
                            except Exception as e:
                                send_text(chat_id, f"❌ Stats command failed: {str(e)[:150]}", reply_to=msg_id, thread_id=thread_id)
                                alert_admin(f"/stats failed\n\nReason: {str(e)[:250]}", dedupe_key="cmd_stats_fail")

                                                # /banner status | /banner off | /banner on [text] | /post banner
                        elif text_cmd_low.startswith("/banner") or text_cmd_low in ["/post banner", "/banner post"]:
                            try:
                                raw = text_cmd.strip()
                                low = raw.lower()
                                # image-based sponsor banner
                                if low in ["/post banner", "/banner post"]:
                                    banner_photo = photo or (reply_msg.get("photo") if reply_msg else None)
                                    if not banner_photo:
                                        send_text(chat_id, "⚠️ Attach a website-size photo or reply to a photo, then send <code>/post banner</code>.", reply_to=msg_id, thread_id=thread_id)
                                    else:
                                        img_bytes = download_telegram_photo_bytes(banner_photo)
                                        if not img_bytes:
                                            send_text(chat_id, "❌ Banner post failed: I couldn't download the Telegram photo.", reply_to=msg_id, thread_id=thread_id)
                                            alert_admin("Banner post failed: Telegram photo download failed.", dedupe_key="banner_photo_download_fail")
                                        else:
                                            image_url = upload_to_imgbb(img_bytes)
                                            if not image_url:
                                                send_text(chat_id, "❌ Banner post failed: image upload failed (imgbb).", reply_to=msg_id, thread_id=thread_id)
                                                alert_admin("Banner post failed: imgbb upload failed.", dedupe_key="banner_imgbb_fail")
                                            else:
                                                website_banner.update({"active": True, "text": "", "image_url": image_url, "updated_at": utcnow().isoformat()})
                                                persist_state()
                                                send_text(chat_id, f"🎯 <b>Website banner posted</b>\n\nImage saved and banner is active.\n{image_url}", reply_to=msg_id, thread_id=thread_id)
                                elif low in ["/banner", "/banner status"]:
                                    active = bool(website_banner.get("active"))
                                    txt = (website_banner.get("text") or "").strip()
                                    img = (website_banner.get("image_url") or "").strip()
                                    send_text(chat_id,
                                        f"🎯 <b>Website banner</b>\n\n"
                                        f"Active: <b>{'Yes' if active else 'No'}</b>\n"
                                        f"Image: {img or '—'}\n"
                                        f"Text: {txt or '—'}",
                                        reply_to=msg_id, thread_id=thread_id)
                                elif low.startswith("/banner off"):
                                    website_banner.update({"active": False, "text": "", "image_url": "", "updated_at": utcnow().isoformat()})
                                    persist_state()
                                    send_text(chat_id, "🧹 Website banner turned off.", reply_to=msg_id, thread_id=thread_id)
                                elif low.startswith("/banner on"):
                                    banner_text = raw[len("/banner on"):].strip()
                                    if not banner_text:
                                        send_text(chat_id, "⚠️ Use: <code>/banner on Your sponsored banner text</code> or attach a photo and use <code>/post banner</code>.", reply_to=msg_id, thread_id=thread_id)
                                    else:
                                        website_banner.update({"active": True, "text": banner_text[:240], "image_url": "", "updated_at": utcnow().isoformat()})
                                        persist_state()
                                        send_text(chat_id, f"🎯 Website text banner turned on.\n\n{banner_text[:240]}", reply_to=msg_id, thread_id=thread_id)
                                else:
                                    send_text(chat_id, "⚠️ Use <code>/banner status</code>, <code>/banner on ...</code>, <code>/banner off</code>, or attach a photo and send <code>/post banner</code>.", reply_to=msg_id, thread_id=thread_id)
                            except Exception as e:
                                send_text(chat_id, f"❌ Banner command failed: {str(e)[:150]}", reply_to=msg_id, thread_id=thread_id)
                                alert_admin(f"Banner command failed\n\nReason: {str(e)[:250]}", dedupe_key="cmd_banner_fail")

# /hide_dv — hide all currently posted Dhivehi website articles
                        elif text_cmd_low in ["/hide_dv", "/hide dv", "/hide all dv", "/delete_dv", "/delete dv", "/delete all dv"]:
                            try:
                                rows = db_hide_all_dhivehi()
                                if rows:
                                    send_text(chat_id, f"🙈 <b>Hidden Dhivehi website articles:</b> {len(rows)}", reply_to=msg_id, thread_id=thread_id)
                                else:
                                    send_text(chat_id, "ℹ️ No posted Dhivehi website articles found to hide.", reply_to=msg_id, thread_id=thread_id)
                            except Exception as e:
                                send_text(chat_id, f"❌ hide_dv failed: {str(e)[:150]}", reply_to=msg_id, thread_id=thread_id)
                                alert_admin(f"hide_dv failed\n\nReason: {str(e)[:250]}", dedupe_key="cmd_hide_dv_fail")

                        # /unhide_dv — restore hidden Dhivehi website articles
                        elif text_cmd_low in ["/unhide_dv", "/unhide dv", "/unhide all dv"]:
                            try:
                                rows = db_unhide_all_dhivehi()
                                if rows:
                                    send_text(chat_id, f"👀 <b>Restored hidden Dhivehi website articles:</b> {len(rows)}", reply_to=msg_id, thread_id=thread_id)
                                else:
                                    send_text(chat_id, "ℹ️ No hidden Dhivehi website articles found to restore.", reply_to=msg_id, thread_id=thread_id)
                            except Exception as e:
                                send_text(chat_id, f"❌ unhide_dv failed: {str(e)[:150]}", reply_to=msg_id, thread_id=thread_id)
                                alert_admin(f"unhide_dv failed\n\nReason: {str(e)[:250]}", dedupe_key="cmd_unhide_dv_fail")

                        # /buffercheck — test imgbb + Buffer connection live
                        elif text.strip().lower() in ["/buffercheck", "/socialcheck", "/checkbuffer"]:
                            send_text(chat_id, "🔍 Testing imgbb + Buffer connection... ⏳",
                                      reply_to=msg_id, thread_id=thread_id)
                            def _buffercheck(_cid=chat_id, _tid=thread_id):
                                lines = ["🔍 <b>Social Platform Check</b>\n"]
                                # 1. imgbb
                                try:
                                    import base64 as _b64
                                    test_img = Image.new("RGB", (10,10), color=(20,40,80))
                                    buf = io.BytesIO()
                                    test_img.save(buf, format="JPEG"); buf.seek(0)
                                    resp = requests.post("https://api.imgbb.com/1/upload",
                                        data={"key": IMGBB_API_KEY,
                                              "image": _b64.b64encode(buf.getvalue()).decode()},
                                        timeout=15)
                                    if resp.status_code == 200 and resp.json().get("data",{}).get("url"):
                                        lines.append("🖼️ <b>imgbb:</b> ✅ Working")
                                    else:
                                        lines.append(f"🖼️ <b>imgbb:</b> ❌ HTTP {resp.status_code} — check IMGBB_API_KEY")
                                except Exception as e:
                                    lines.append(f"🖼️ <b>imgbb:</b> ❌ {str(e)[:60]}")

                                # 2. Meta Graph API (FB + IG)
                                if META_PAGE_TOKEN and META_PAGE_ID:
                                    try:
                                        r = requests.get(
                                            f"https://graph.facebook.com/{META_API_VER}/{META_PAGE_ID}",
                                            params={"fields": "name,instagram_business_account",
                                                    "access_token": META_PAGE_TOKEN},
                                            timeout=10)
                                        if r.status_code == 200:
                                            d = r.json()
                                            pg = d.get("name","?")
                                            ig = d.get("instagram_business_account",{}).get("id","")
                                            lines.append(f"\n📘 <b>Facebook (Meta):</b> ✅ Page: {pg}")
                                            if ig:
                                                lines.append(f"📸 <b>Instagram:</b> ✅ IG account linked (id: {ig})")
                                                if not META_IG_ID:
                                                    lines.append(f"   ⚠️ Add META_IG_ID={ig} to Railway vars for IG posting")
                                            else:
                                                lines.append("📸 <b>Instagram:</b> ⚠️ No IG business account linked to this page")
                                        else:
                                            err = r.json().get("error",{}).get("message","unknown")
                                            if "token" in err.lower() or "expired" in err.lower():
                                                lines.append(f"\n📘 <b>Meta token:</b> ❌ EXPIRED — regenerate META_PAGE_TOKEN")
                                            else:
                                                lines.append(f"\n📘 <b>Meta (FB/IG):</b> ❌ {err[:80]}")
                                    except Exception as e:
                                        lines.append(f"\n📘 <b>Meta:</b> ❌ {str(e)[:60]}")
                                else:
                                    lines.append("\n📘 <b>Meta (FB/IG):</b> ❌ META_PAGE_TOKEN or META_PAGE_ID not set in Railway")

                                # 3. Buffer (X/Twitter only — text posts)
                                if not BUFFER_TOKEN:
                                    lines.append("\n🐦 <b>X/Twitter (Buffer):</b> ❌ BUFFER_ACCESS_TOKEN not set")
                                else:
                                    try:
                                        r = requests.post(
                                            "https://api.buffer.com",
                                            json={"query": "{ account { id name } }"},
                                            headers={"Authorization": f"Bearer {BUFFER_TOKEN}",
                                                     "Content-Type": "application/json"},
                                            timeout=10)
                                        if r.status_code == 200:
                                            data = r.json()
                                            if "errors" in data:
                                                lines.append(f"\n🐦 <b>X/Twitter (Buffer):</b> ❌ {data['errors'][0].get('message','?')[:60]}")
                                            else:
                                                name = data.get("data",{}).get("account",{}).get("name","?")
                                                lines.append(f"\n🐦 <b>X/Twitter (Buffer):</b> ✅ Valid — account: {name}")
                                                lines.append(f"   <i>Note: Buffer posts text only (no image) — working as expected</i>")
                                        else:
                                            lines.append(f"\n🐦 <b>X/Twitter (Buffer):</b> ⚠️ HTTP {r.status_code}")
                                    except Exception as e:
                                        lines.append(f"\n🐦 <b>X/Twitter:</b> ❌ {str(e)[:60]}")

                                # 4. Last errors
                                lines.append(f"\n🔎 <b>Last Buffer (X) response:</b>")
                                lines.append(f"<code>{_last_buffer_error.get('response','No posts yet')[:150]}</code>")
                                if _last_buffer_error.get("fb_error"):
                                    lines.append(f"\n❌ <b>Last FB error:</b> <code>{_last_buffer_error['fb_error'][:150]}</code>")
                                if _last_buffer_error.get("ig_error"):
                                    lines.append(f"\n❌ <b>Last IG error:</b> <code>{_last_buffer_error['ig_error'][:150]}</code>")
                                send_text(_cid, "\n".join(lines), thread_id=_tid)
                            threading.Thread(target=_buffercheck, daemon=True).start()

                        # /diag — diagnose feeds, Gemini (Dhivehi), and queue health
                        elif text.strip().lower() in ["/diag", "/health", "/diagnose"]:
                            send_text(chat_id, "🔍 Running diagnostics... ⏳", reply_to=msg_id, thread_id=thread_id)
                            def _run_diag(_cid=chat_id, _tid=thread_id):
                                try:
                                    lines = ["🔍 <b>Samuga AI Diagnostics</b>\n"]
                                    # 1. Gemini test — try all models in fallback order
                                    if GEMINI_API_KEY:
                                        test_dv = make_dhivehi_caption("The government announced a new policy today.", "Test news")
                                        if test_dv and any("ހ" <= c <= "޿" for c in test_dv):
                                            lines.append("🇲🇻 <b>Dhivehi (Gemini):</b> ✅ Working")
                                        elif test_dv:
                                            lines.append("🇲🇻 <b>Dhivehi (Gemini):</b> ⚠️ Responded but no Thaana — check prompt")
                                        else:
                                            # Show which models failed
                                            lines.append("🇲🇻 <b>Dhivehi (Gemini):</b> ❌ All models failed")
                                            lines.append(f"   Models tried: {', '.join(GEMINI_MODELS)}")
                                            lines.append("   Check GEMINI_API_KEY in Railway vars")
                                    else:
                                        lines.append("🇲🇻 <b>Dhivehi (Gemini):</b> ❌ GEMINI_API_KEY not set in Railway")
                                    # 2. Dhivehi feed check (RSS — expected to fail, kept for reference)
                                    dv_feeds = [f for f in LOCAL_FEEDS if f.get("lang")=="dv"]
                                    lines.append(f"\n📡 <b>Dhivehi RSS feeds ({len(dv_feeds)}) — now replaced by Telegram:</b>")
                                    for f in dv_feeds:
                                        try:
                                            parsed = feedparser.parse(f["url"])
                                            n = len(parsed.entries)
                                            domain = f["url"].split("/")[2]
                                            status = f"✅ {n} items" if n > 0 else "❌ 0 items (blocked/down)"
                                            lines.append(f"  {status} — {domain}")
                                        except Exception as fe:
                                            lines.append(f"  ❌ {f['url'].split('/')[2]}: error")
                                    # 3. Source ladder / website latest pages
                                    try:
                                        lines.append(f"\n🪜 <b>Source ladder:</b>")
                                        latest_counts = {}
                                        for src in WEB_LATEST_SOURCES:
                                            latest_counts[src["source"]] = latest_counts.get(src["source"], 0) + 1
                                        latest_sample = fetch_latest_web_pages(limit_per_source=2)
                                        by_src = {}
                                        for a in latest_sample:
                                            by_src[a.get("source","?")] = by_src.get(a.get("source","?"), 0) + 1
                                        for src_name in sorted(set(s.get("source","") for s in WEB_LATEST_SOURCES)):
                                            lines.append(f"  🌐 {src_name}: {by_src.get(src_name,0)} latest-page headline(s)")
                                        rss_backup = fetch_local_rss_recovery(limit_per_source=1)
                                        if rss_backup:
                                            lines.append(f"\n📡 <b>RSS recovery ladder:</b> ✅ {len(rss_backup)} backup item(s)")
                                        else:
                                            lines.append(f"\n📡 <b>RSS recovery ladder:</b> ⚠️ 0 backup item(s)")
                                        world_items = fetch_world_updates(limit=2)
                                        lines.append(f"\n🌍 <b>World updates:</b> ✅ {len(world_items)} major world item(s) available")
                                    except Exception as ce:
                                        lines.append(f"\n🪜 <b>Source ladder:</b> ❌ {str(ce)[:40]}")

                                    # 4. Telegram channels (signal only — websites are primary)
                                    lines.append(f"\n📲 <b>Telegram signal channels:</b>")
                                    for ch in DV_TELEGRAM_CHANNELS:
                                        try:
                                            arts = fetch_dv_telegram(ch["handle"], ch["source"], ch.get("reliability",80))
                                            dv_count = sum(1 for a in arts if a["lang"]=="dv")
                                            lines.append(f"  ✅ @{ch['handle']} / {ch['source']}: {len(arts)} items ({dv_count} Dhivehi)")
                                        except Exception as ce:
                                            lines.append(f"  ❌ @{ch.get('handle','?')} / {ch['source']}: {str(ce)[:30]}")
                                    # 5. Queue state
                                    lines.append("\n🧠 <b>Queue guards:</b> duplicate translation wall + internal/junk safety wall active")
                                    lines.append("🌐 <b>Dhivehi website rule:</b> no Dhivehi website publish without Content Lab approval")
                                    lines.append(f"🎯 <b>Website banner:</b> {'ON' if website_banner.get('active') else 'OFF'}")
                                    if website_banner.get("image_url"):
                                        lines.append("🖼️ <b>Banner type:</b> image")
                                    dv_queued = sum(1 for v in approval_queue.values() if v.get("lang")=="dv")
                                    en_queued = sum(1 for v in approval_queue.values() if v.get("lang")=="en")
                                    lines.append(f"\n📋 <b>Approval queue:</b> {dv_queued} Dhivehi, {en_queued} English waiting")
                                    # 5. Recent Dhivehi posts from DB
                                    if DB_ENABLED:
                                        dv_posted = db_execute("SELECT COUNT(*) FROM articles WHERE lang='dv' AND status='posted' AND posted_at > NOW() - INTERVAL '7 days'", fetch="one")
                                        lines.append(f"📚 <b>Dhivehi posted (7d):</b> {dv_posted[0] if dv_posted else 0}")
                                    send_text(_cid, "\n".join(lines), thread_id=_tid)
                                except Exception as e:
                                    log.error(f"/diag: {e}")
                                    send_text(_cid, f"❌ Diag error: {e}", thread_id=_tid)
                            threading.Thread(target=_run_diag, daemon=True).start()

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

                        # /stories — list active developing story threads
                        elif text.strip().lower() in ["/stories", "/developing"]:
                            if not DB_ENABLED:
                                send_text(chat_id, "🗄️ Database not connected — story tracking unavailable.", reply_to=msg_id, thread_id=thread_id)
                            else:
                                stories = get_active_stories(10)
                                if not stories:
                                    send_text(chat_id, "📚 No active developing stories right now. Stories appear here once an event gets 2+ updates.", reply_to=msg_id, thread_id=thread_id)
                                else:
                                    lines = ["📚 <b>Developing Stories — Last 72h</b>\n"]
                                    for s in stories:
                                        status_emoji = "🔴" if s["status"]=="developing" else "🟡"
                                        lines.append(f"{status_emoji} <b>Story #{s['id']}</b> ({s['update_count']} updates)\n   {s['title'][:70]}")
                                    lines.append("\n<i>Use /story [number] to see the full timeline.</i>")
                                    send_text(chat_id, "\n".join(lines), reply_to=msg_id, thread_id=thread_id)

                        # /story <id> — show the full timeline of a story
                        elif text.strip().lower().startswith("/story"):
                            arg = text.strip()[6:].strip()
                            if not DB_ENABLED:
                                send_text(chat_id, "🗄️ Database not connected — story tracking unavailable.", reply_to=msg_id, thread_id=thread_id)
                            elif not arg.isdigit():
                                send_text(chat_id, "Use <code>/story [number]</code> — e.g. <code>/story 248</code>. See /stories for the list.", reply_to=msg_id, thread_id=thread_id)
                            else:
                                timeline = get_story_timeline(int(arg))
                                if not timeline:
                                    send_text(chat_id, f"No story found with ID #{arg}.", reply_to=msg_id, thread_id=thread_id)
                                else:
                                    from datetime import timedelta as _td
                                    lines = [f"📚 <b>Story #{timeline['id']} — {timeline['status'].upper()}</b>\n"]
                                    lines.append(f"<b>{timeline['title']}</b>")
                                    lines.append(f"<i>{timeline['update_count']} updates · {timeline['category'] or 'news'}</i>\n")
                                    lines.append("<b>Timeline:</b>")
                                    for u in timeline["updates"]:
                                        t = u["time"]
                                        if t:
                                            mvt = (t + _td(hours=5)) if t.tzinfo else t
                                            tstr = mvt.strftime("%d %b %H:%M")
                                        else:
                                            tstr = ""
                                        src = f" ({u['source']})" if u["source"] else ""
                                        lines.append(f"🔹 <b>{tstr}</b>{src}\n   {u['headline'][:90]}")
                                    out = "\n".join(lines)
                                    if len(out) > 4000: out = out[:3990] + "\n…"
                                    send_text(chat_id, out, reply_to=msg_id, thread_id=thread_id)

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
                            def _send_weather_preview(_chat_id=chat_id, _thread_id=thread_id, _name=first_name):
                                try:
                                    data = get_weather_data()
                                    if not data:
                                        send_text(_chat_id, "❌ Weather data unavailable right now.", thread_id=_thread_id)
                                        return
                                    islands = get_island_forecasts()
                                    prayer_info = get_prayer_times()
                                    card = generate_weather_card(data, island_data=islands if islands else None,
                                                                 prayer_data=prayer_info)
                                    current = data.get("current", {})
                                    temp  = round(current.get("temperature_2m", 29))
                                    code  = current.get("weathercode", 0)
                                    emoji, condition = weather_code_to_info(code)
                                    source = data.get("_source", "")
                                    island_lines = ""
                                    if islands:
                                        island_lines = "\n\n🏝 <b>Weather Watch</b>\n"
                                        for isl in islands:
                                            _out = isl.get("outlook") or f"{isl.get('temp',29)}°C • wind {isl.get('wind',0)} km/h"
                                            island_lines += f"📍 <b>{isl['name']}</b> — {_out}\n"
                                    caption = (
                                        f"🌤️ <b>Weather Preview — Malé, Maldives</b>\n"
                                        f"{emoji} {temp}°C — {condition}"
                                        f"{island_lines}\n"
                                        f"<i>Data: {source} · Preview only, not posted to community</i>"
                                    )
                                    send_photo(_chat_id, card, caption, thread_id=_thread_id)
                                    log.info(f"🌤️ Weather preview sent to core team by {_name}")
                                except Exception as e:
                                    log.error(f"/weather preview: {e}")
                                    send_text(_chat_id, f"❌ Error: {e}", thread_id=_thread_id)
                            threading.Thread(target=_send_weather_preview, daemon=True).start()

                        # /alert [white|yellow|orange|red] — preview an alert card in Content Lab
                        elif text.strip().lower().startswith("/alert"):
                            arg = text.strip().lower().replace("/alert", "").strip()
                            valid_levels = ["white", "yellow", "orange", "red"]

                            if arg == "status" or arg == "":
                                # Show current real conditions + whether an alert would fire
                                send_text(chat_id, "🔍 Checking current conditions... ⏳",
                                          reply_to=msg_id, thread_id=thread_id)
                                def _alert_status(_cid=chat_id, _tid=thread_id):
                                    try:
                                        data = get_weather_data()
                                        if not data:
                                            send_text(_cid, "❌ Weather data unavailable.", thread_id=_tid); return
                                        should, lvl, txt = detect_weather_alert(data)
                                        cur = data.get("current", {})
                                        w = round(cur.get("windspeed_10m",0))
                                        g = round(cur.get("windgust_10m",0))
                                        if should:
                                            cfg = MMS_ALERT_LEVELS[lvl]
                                            msg = (f"{cfg['emoji']} <b>{cfg['label']} would fire right now</b>\n\n"
                                                   f"{txt}\n\n"
                                                   f"Wind {w} km/h, gusts {g} km/h\n"
                                                   f"Alerts used today: {weather_alerts_today['count']}/2\n\n"
                                                   f"Use <code>/alert {lvl}</code> to preview the card.")
                                        else:
                                            msg = (f"🟢 <b>No alert conditions right now</b>\n\n"
                                                   f"Wind {w} km/h, gusts {g} km/h — all calm.\n"
                                                   f"Alerts used today: {weather_alerts_today['count']}/2\n\n"
                                                   f"Preview any level: <code>/alert white|yellow|orange|red</code>")
                                        send_text(_cid, msg, thread_id=_tid)
                                    except Exception as e:
                                        log.error(f"/alert status: {e}")
                                        send_text(_cid, f"❌ Error: {e}", thread_id=_tid)
                                threading.Thread(target=_alert_status, daemon=True).start()

                            elif arg in valid_levels:
                                send_text(chat_id, f"⚠️ Building {arg.upper()} alert preview... ⏳",
                                          reply_to=msg_id, thread_id=thread_id)
                                def _alert_preview(_lvl=arg, _cid=chat_id, _tid=thread_id):
                                    try:
                                        data = get_weather_data()
                                        if not data:
                                            send_text(_cid, "❌ Weather data unavailable.", thread_id=_tid); return
                                        islands = get_island_forecasts()
                                        prayer_info = get_prayer_times()
                                        cfg = MMS_ALERT_LEVELS[_lvl]
                                        # Build a representative alert_text for this level
                                        sample_text = {
                                            "white":  "Strong winds and rough seas expected over Malé. Wind 32 km/h, gusts 56 km/h. Stay informed and take normal precautions.",
                                            "yellow": "Thunderstorms, strong winds and rough seas expected over Malé. Wind 42 km/h, gusts 66 km/h. Caution advised. Avoid unnecessary sea travel.",
                                            "orange": "Severe winds and very rough seas expected over Malé. Wind 58 km/h, gusts 82 km/h. Avoid sea travel. Secure loose objects. Stay indoors if possible.",
                                            "red":    "DANGEROUS storm conditions over Malé. Wind 78 km/h, gusts 105 km/h. DANGER. Do not travel by sea. Stay indoors and follow official guidance.",
                                        }[_lvl]
                                        card = generate_weather_card(data, alert_mode=True,
                                                                     alert_text=sample_text, alert_level=_lvl,
                                                                     island_data=islands if islands else None,
                                                                     prayer_data=prayer_info)
                                        caption = (
                                            f"{cfg['emoji']} <b>{cfg['label']} PREVIEW — {cfg['headline']}</b>\n\n"
                                            f"{sample_text}\n\n"
                                            f"<i>⚠️ This is a PREVIEW only — not posted to community.\n"
                                            f"Real alerts fire automatically when conditions are met.</i>"
                                        )
                                        send_photo(_cid, card, caption, thread_id=_tid)
                                        log.info(f"⚠️ Alert preview ({_lvl}) sent to core team")
                                    except Exception as e:
                                        log.error(f"/alert preview: {e}")
                                        send_text(_cid, f"❌ Error: {e}", thread_id=_tid)
                                threading.Thread(target=_alert_preview, daemon=True).start()
                            else:
                                send_text(chat_id,
                                    "Usage:\n"
                                    "<code>/alert status</code> — check if an alert would fire now\n"
                                    "<code>/alert white</code> — preview White (informational)\n"
                                    "<code>/alert yellow</code> — preview Yellow (advisory)\n"
                                    "<code>/alert orange</code> — preview Orange (warning)\n"
                                    "<code>/alert red</code> — preview Red (emergency)",
                                    reply_to=msg_id, thread_id=thread_id)

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

                                    # Build/publish website article for manual social cards.
                                    manual_article = manual_publish_website_article(
                                        title=card_headline if len(parts) >= 2 else content_text,
                                        subheading=caption_subhead,
                                        category=manual_cat,
                                        source_link=SAMUGA_CAPTION_LINK,
                                        publish_now=(destination != "all")
                                    )

                                    posted = []
                                    _social_fired = False

                                    if destination == "community":
                                        card.seek(0)
                                        if send_to_telegram(card, full_caption):
                                            posted.append("Community ✅")
                                        if manual_article:
                                            posted.append("Website ✅")

                                    elif destination == "coreteam":
                                        card.seek(0)
                                        if send_photo(CORE_TEAM_CHAT_ID, card, full_caption, thread_id=CONTENT_LAB_THREAD_ID):
                                            posted.append("Content Lab ✅")
                                        if manual_article:
                                            posted.append("Website ✅")

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
                                            "manual_article": manual_article,
                                        })
                                        # Send preview card to core team
                                        preview = io.BytesIO(card_bytes_stored)
                                        preview_caption = (
                                            f"👀 <b>PREVIEW — not posted yet</b>\n\n"
                                            f"{full_caption}\n\n"
                                            f"━━━━━━━━━━━━━━\n"
                                            f"📲 This will post to <b>Telegram Community + Facebook + Instagram + X</b>.\n"
                                            f"🌐 Website article draft is prepared in parallel and will publish on /confirm.\n"
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
                                        tg_ok_now = bool(send_to_telegram(tg_buf, cap))
                                        tg_icon = "✅" if tg_ok_now else "❌"

                                        # Socials via 10-min queue — confirmation after posting
                                        social_buf = io.BytesIO(cbytes)
                                        queue_for_social(social_buf, cap,
                                            notify_chat_id=chat_id,
                                            notify_thread_id=thread_id,
                                            key_label="Manual post",
                                            tg_ok=tg_ok_now,
                                            post_telegram=False)  # already posted above

                                        manual_article = _pending_manual_post.get("manual_article") or {}
                                        website_ok = False
                                        if manual_article and not manual_article.get("published"):
                                            try:
                                                db_publish_article_for_website(
                                                    article_id=manual_article.get("article_id"),
                                                    title=manual_article.get("title",""),
                                                    summary=manual_article.get("summary",""),
                                                    category=manual_article.get("category","LOCAL"),
                                                    source=SAMUGA_PUBLIC_SOURCE,
                                                    link=SAMUGA_CAPTION_LINK,
                                                    lang="en",
                                                    score=190,
                                                    reliability=95,
                                                    is_breaking=(manual_article.get("category","").upper() in ("BREAKING","DISASTER"))
                                                )
                                                website_ok = True
                                            except Exception as we:
                                                log.error(f"Manual website publish on /confirm: {we}")
                                        elif manual_article:
                                            website_ok = True

                                        _pending_manual_post.clear()
                                        send_text(chat_id,
                                            f"✅ <b>Confirmed by {first_name}</b>\n"
                                            f"Telegram {tg_icon} · FB IG X ⏳ queued"
                                            + (f" · Website ✅" if website_ok else ""),
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
                                    f"❌ <b>Cancelled by {first_name}</b> — card discarded, nothing posted. Website draft not published.",
                                    reply_to=msg_id, thread_id=thread_id)
                                log.info(f"❌ Manual card cancelled by {first_name}")

                        # /ai on|off — toggle proactive mode
                        elif text.strip().lower() in ["/ai on", "/ai off"]:
                            global _ai_proactive_mode
                            _ai_proactive_mode = "on" in text.strip().lower()
                            status = "ON 🟢" if _ai_proactive_mode else "OFF 🔴"
                            msg_txt = (
                                f"🧠 Samuga AI proactive mode: <b>{status}</b>\n\n"
                                + ("I'll jump in when I have something useful to add — tag me anytime too."
                                   if _ai_proactive_mode else
                                   "Silent mode. I'll only respond when you tag me.")
                            )
                            send_text(chat_id, msg_txt, reply_to=msg_id, thread_id=thread_id)
                            log.info(f"🧠 AI proactive mode: {status} by {first_name}")

                        # /remember — save something to persistent team memory
                        elif text.strip().lower().startswith("/remember"):
                            mem_text = text.strip()[9:].strip()
                            if not mem_text:
                                send_text(chat_id,
                                    "What should I remember? Try: <code>/remember our audience loves political stories on weekdays</code>",
                                    reply_to=msg_id, thread_id=thread_id)
                            else:
                                # Classify category automatically
                                cat = "fact"
                                low = mem_text.lower()
                                if any(w in low for w in ["audience","people","readers","followers","engage","viral","perform"]):
                                    cat = "audience"
                                elif any(w in low for w in ["style","tone","voice","format","caption","card","design"]):
                                    cat = "style"
                                elif any(w in low for w in ["decided","decision","agreed","policy","rule","always","never"]):
                                    cat = "decision"
                                elif any(w in low for w in ["prefer","like","don't like","avoid","focus"]):
                                    cat = "preference"
                                mem_add(mem_text, category=cat, added_by=first_name)
                                send_text(chat_id,
                                    f"✅ Got it, saved to memory [{cat}]\n<i>\"{mem_text}\"</i>",
                                    reply_to=msg_id, thread_id=thread_id)
                                log.info(f"🧠 Memory added by {first_name}: {mem_text[:60]}")

                        # /memory — show what's stored
                        elif text.strip().lower() in ["/memory", "/memories"]:
                            items = mem_list(25)
                            if not items:
                                send_text(chat_id,
                                    "Nothing in memory yet. Use <code>/remember [something]</code> to teach me.",
                                    reply_to=msg_id, thread_id=thread_id)
                            else:
                                lines = ["🧠 <b>What I remember about Samuga:</b>\n"]
                                for item in items:
                                    lines.append(f"• {item}")
                                send_text(chat_id, "\n".join(lines), reply_to=msg_id, thread_id=thread_id)

                        # /forget — clear last memory or all
                        elif text.strip().lower().startswith("/forget"):
                            arg = text.strip()[7:].strip().lower()
                            if arg == "all":
                                mem_clear_all()
                                send_text(chat_id, "🗑️ All memories cleared.", reply_to=msg_id, thread_id=thread_id)
                            elif arg == "last":
                                mem_delete_last(1)
                                send_text(chat_id, "🗑️ Last memory deleted.", reply_to=msg_id, thread_id=thread_id)
                            else:
                                send_text(chat_id,
                                    "Use <code>/forget last</code> to delete the last one, or <code>/forget all</code> to wipe everything.",
                                    reply_to=msg_id, thread_id=thread_id)

                        # Respond when tagged OR proactively when AI mode is on
                        elif tagged or (_ai_proactive_mode and not text.strip().startswith("/")):
                            if not clean: clean = text.strip()
                            is_proactive = not tagged

                            # For proactive — ask Claude if it should actually respond
                            needs_search = False
                            if is_proactive:
                                should, needs_search = should_respond_proactively(clean, sender_name=display_name)
                                if not should:
                                    continue  # stay quiet

                            # Check if tagged message needs web search
                            if tagged and not needs_search:
                                needs_search = needs_web_search(clean)

                            log.info(f"🧠 Core team {'[proactive]' if is_proactive else '[tagged]'} {display_name}: {clean[:50]}")
                            session_ctx = core_team_session_context.get(chat_id, "")

                            def _reply_coreteam():
                                try:
                                    # First check if this is a story-timeline question
                                    story_answer = answer_story_query(clean)
                                    if story_answer:
                                        send_text(chat_id, story_answer,
                                                  reply_to=msg_id if tagged else None, thread_id=thread_id)
                                        return

                                    if is_dhivehi(clean):
                                        headlines = get_local_headlines()
                                        ctx = "\n".join(headlines[:5]) if headlines else ""
                                        reply = chat_with_gemini_dhivehi(clean, ctx, history)
                                        if not reply:
                                            reply = chat_with_coreteam(clean, display_name, sender_info,
                                                                        history, session_ctx, needs_search)
                                    else:
                                        reply = chat_with_coreteam(clean, display_name, sender_info,
                                                                    history, session_ctx, needs_search)

                                    if reply:
                                        add_to_conversation(user_id, "user", clean)
                                        add_to_conversation(user_id, "assistant", reply)
                                        # Proactive replies don't quote/reply — they just speak naturally
                                        send_text(chat_id, reply,
                                                  reply_to=msg_id if tagged else None,
                                                  thread_id=thread_id)
                                except Exception as e:
                                    log.error(f"Core team reply: {e}")

                            threading.Thread(target=_reply_coreteam, daemon=True).start()

                    # Regular public group — only respond when tagged, using the same public Samuga AI brain
                    elif tagged and clean:
                        log.info(f"💬 Public group Samuga AI {display_name}: {clean[:50]}")
                        try:
                            reply = public_samuga_ai_chat(
                                message=clean,
                                platform="telegram_group",
                                user_key=f"{chat_id}:{user_id}",
                                session_id=str(chat_id),
                                lang=("dv" if is_dhivehi(clean) else "en")
                            )
                        except Exception as e:
                            log.error(f"Unified public group chat failed: {e}")
                            reply = "Small issue on my side bro 😅 Try again in a moment."
                        send_text(chat_id, reply, reply_to=msg_id, thread_id=thread_id)
        except Exception as e:
            log.error(f"Update loop: {e}"); time.sleep(5)

def ops_watchdog():
    """Light operational watchdog. Sends Alert messages when something looks wrong."""
    try:
        issues = []
        try:
            stats = db_bot_stats() or {}
            if int(stats.get("posted_dv", 0)) > 0 and os.environ.get("DHIVEHI_WEBSITE_APPROVED", "false").lower() != "true":
                issues.append(f"Dhivehi website leak detected: {stats.get('posted_dv',0)} posted Dhivehi article(s) still visible.")
        except Exception as e:
            issues.append(f"Stats check failed: {str(e)[:120]}")
        try:
            if len(approval_queue) >= 25:
                issues.append(f"Approval queue is high: {len(approval_queue)} items waiting.")
            if len(_social_queue) >= 12:
                issues.append(f"Social queue is high: {len(_social_queue)} items waiting.")
        except Exception:
            pass
        if issues:
            alert_admin("<br/>".join(issues), dedupe_key="ops_watchdog", cooloff_minutes=30)
    except Exception as e:
        log.error(f"ops_watchdog: {e}")

def format_bot_stats():
    """Human-friendly stats block for Telegram."""
    stats = db_bot_stats() or {}
    lines = ["📊 <b>Samuga Bot Stats</b>"]
    lines.append(f"🗂️ Articles total: <b>{stats.get('articles_total', 0)}</b>")
    lines.append(f"🌐 Website posted: <b>{stats.get('posted_total', 0)}</b>")
    lines.append(f"🙈 Website hidden: <b>{stats.get('hidden_total', 0)}</b>")
    lines.append(f"🇲🇻 Posted Dhivehi on website: <b>{stats.get('posted_dv', 0)}</b>")
    lines.append(f"🇬🇧 Posted English on website: <b>{stats.get('posted_en', 0)}</b>")
    lines.append(f"🕓 Articles found in last 24h: <b>{stats.get('last_24h', 0)}</b>")
    lines.append(f"🧠 Approval queue: <b>{len(approval_queue)}</b>")
    lines.append(f"📲 Social queue: <b>{len(_social_queue)}</b>")
    lines.append(f"📚 Seen title memory: <b>{len(recent_story_titles)}</b>")
    lines.append(f"🎯 Banner active: <b>{'Yes' if website_banner.get('active') else 'No'}</b>")
    if website_banner.get("image_url"):
        lines.append("🖼️ Banner image: saved")
    return "\n".join(lines)


# ── Website API ───────────────────────────────────────────────────────────────
from flask import Flask, jsonify, request
import html as _html
import re as _api_re

api_app = Flask(__name__)
api_app.json.ensure_ascii = False

def _api_clean_text(value, limit=900):
    """Clean DB text for website/API rendering."""
    value = str(value or "")
    value = _html.unescape(value)
    value = strip_source_links(value)
    value = _api_re.sub(r"<[^>]+>", " ", value)
    value = _api_re.sub(r"https?://\S+", "", value)
    value = _api_re.sub(r"\s+", " ", value).strip()
    return value[:limit]

def _api_has_thaana(text):
    return any("\u0780" <= ch <= "\u07BF" for ch in str(text or ""))

def _api_lang(title, summary, lang):
    # Website language must be based on actual script quality.
    # Do not show Latin Thaana in the Dhivehi side.
    text = (title or "") + " " + (summary or "")
    return "dv" if _api_has_thaana(text) else "en"

def _api_category(cat, title="", summary=""):
    try:
        return canonical_category(cat or "LOCAL", title or "", summary or "")
    except Exception:
        return (cat or "LOCAL").upper()


def ensure_article_engine_body(article_id, title, summary, category,
                               lang="en", is_breaking=False):
    """Return a full website article body for an article, generating and
    persisting one via Claude if the stored body is missing/too short."""
    try:
        body = generate_website_article_body(
            title=title, summary=summary, category=category,
            source=SAMUGA_PUBLIC_SOURCE, is_breaking=is_breaking
        )
    except Exception as e:
        log.error(f"ensure_article_engine_body generate error: {e}")
        body = ""
    body = (body or "").strip()
    if not body:
        body = summary or title or ""
    # Persist so we don't regenerate on every request.
    try:
        if article_id and body:
            db_execute(
                "UPDATE articles SET article_body=%s WHERE id=%s",
                (body, article_id),
            )
    except Exception as e:
        log.warning(f"ensure_article_engine_body persist skipped: {e}")
    return body


def _clean_article_engine_output(body, title=""):
    """Clean a generated English article body for website/API rendering:
    strip HTML/source links/stray title echo and normalize whitespace per
    paragraph (preserving paragraph breaks)."""
    body = str(body or "")
    body = _html.unescape(body)
    body = strip_source_links(body)
    body = _api_re.sub(r"<[^>]+>", " ", body)
    body = _api_re.sub(r"https?://\S+", "", body)
    paras = []
    for para in _api_re.split(r"\n\s*\n", body):
        para = _api_re.sub(r"\s+", " ", para).strip()
        if not para:
            continue
        # Drop a leading line that just repeats the title.
        if title and para.lower() == title.strip().lower():
            continue
        paras.append(para)
    return "\n\n".join(paras)[:6000]


def related_articles_for_api(article_id, category, limit=4):
    """Return a small list of related published articles in the same category
    (excluding the current article) for the website 'related' rail."""
    try:
        rows = db_execute("""
            SELECT id, title, category, posted_at, found_at, article_slug
            FROM articles
            WHERE category=%s
              AND id<>%s
              AND (status IN ('posted','published','social_posted') OR (status='queued' AND lang='en'))
            ORDER BY COALESCE(posted_at, found_at) DESC NULLS LAST
            LIMIT %s
        """, (category, article_id, limit), fetch="all") or []
    except Exception as e:
        log.error(f"related_articles_for_api error: {e}")
        return []
    related = []
    for r in rows:
        rid, title, cat, posted_at, found_at, slug = r
        dt = posted_at or found_at
        related.append({
            "id": rid,
            "title": _api_clean_text(strip_source_links(title), 160),
            "category": _api_category(cat),
            "slug": slug or "",
            "time": mvt_display_time(dt),
        })
    return related

def _absolute_api_url(path):
    """Build full Railway URL for GitHub Pages."""
    return request.url_root.rstrip("/") + path

@api_app.after_request
def add_cors_headers(response):
    """Allow GitHub Pages to read this API."""
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response

@api_app.get("/")
def api_home():
    return jsonify({
        "status": "online",
        "name": "Samuga News Bot API",
        "version": SAMUGA_VERSION,
        "endpoints": ["/api/stories", "/api/article?id=ARTICLE_ID", "/api/health", "/api/chat", "/api/public-interest"]
    })

@api_app.get("/api/health")
def api_health():
    latest = None
    try:
        row = db_execute("""
            SELECT title, posted_at, found_at, status
            FROM articles
            WHERE (status IN ('posted','published','social_posted') OR (status='queued' AND lang='en'))
            ORDER BY COALESCE(posted_at, found_at) DESC NULLS LAST
            LIMIT 1
        """, fetch="one")
        if row:
            latest = {
                "title": _api_clean_text(row[0], 160),
                "time": (row[1] or row[2]).strftime("%d %b %Y • %H:%M") if (row[1] or row[2]) else "Recent",
                "status": row[3]
            }
    except Exception as e:
        log.error(f"Website API /api/health error: {e}")

    return jsonify({
        "status": "online",
        "db_enabled": bool(DB_ENABLED),
        "latest": latest,
        "queue": len(_social_queue) if "_social_queue" in globals() else 0
    })


@api_app.get("/api/banner")
def api_banner():
    """Optional sponsored/banner block for the website frontend."""
    try:
        return jsonify({
            "active": bool(website_banner.get("active")),
            "text": str(website_banner.get("text") or ""),
            "image_url": str(website_banner.get("image_url") or ""),
            "updated_at": website_banner.get("updated_at"),
        })
    except Exception as e:
        log.error(f"Website API /api/banner error: {e}")
        return jsonify({"active": False, "text": "", "updated_at": None})

@api_app.get("/api/public-interest")
def api_public_interest():
    """Aggregated public Samuga AI interest radar. No private messages exposed."""
    try:
        rows = db_execute("""
            SELECT topic, platform, SUM(count) AS total
            FROM public_interest_daily
            WHERE day >= CURRENT_DATE - INTERVAL '7 days'
            GROUP BY topic, platform
            ORDER BY total DESC
            LIMIT 50
        """, fetch="all") or []
        items = [{"topic": r[0], "platform": r[1], "count": int(r[2] or 0)} for r in rows]
        return jsonify({"ok": True, "window": "7d", "items": items})
    except Exception as e:
        log.error(f"Website API /api/public-interest error: {e}")
        return jsonify({"ok": False, "items": []})

@api_app.get("/api/stories")
def api_stories():
    """
    Public website feed for GitHub Pages.

    Important:
    The website should show article data, not Telegram/Instagram square cards.
    So this endpoint returns clean JSON only: title, summary, category, source, url, time, lang.
    It reads all statuses that mean public/published. Queue items are marked posted
    by queue_for_social() when they enter the public publishing queue.
    """
    try:
        rows = db_execute("""
            SELECT id, title, summary, category, source, link, posted_at, found_at, lang, status, article_excerpt, article_slug
            FROM articles
            WHERE (status IN ('posted','published','social_posted') OR (status='queued' AND lang='en'))
            ORDER BY COALESCE(posted_at, found_at) DESC NULLS LAST
            LIMIT 80
        """, fetch="all") or []

        stories = []
        seen_titles = set()

        for row in rows:
            article_id, title, summary, category, source, link, posted_at, found_at, lang, status, article_excerpt, article_slug = row
            dt = posted_at or found_at
            safe_title = _api_clean_text(strip_source_links(title), 500)
            safe_summary = _api_clean_text(strip_source_links(article_excerpt or summary), 420)
            if not safe_title:
                continue
            if not public_text_is_safe(f"{safe_title}\n{safe_summary}"):
                continue

            # Hide old broken Latin Thaana rows from the website feed.
            # New rows are fixed before publish by normalize_article_language_for_public().
            if looks_latin_thaana(f"{safe_title} {safe_summary}") and not _api_has_thaana(f"{safe_title} {safe_summary}"):
                continue

            # Dedupe same headline in API so the site stays clean
            key = _caption_match_key(safe_title) or safe_title.lower()[:80]
            if key in seen_titles:
                continue
            seen_titles.add(key)

            stories.append({
                "id": article_id,
                "title": safe_title,
                "summary": safe_summary,
                "category": _api_category(category, safe_title, safe_summary),
                "source": SAMUGA_PUBLIC_SOURCE,
                "url": f"article.html?id={article_id}",
                "community_url": SAMUGA_PUBLIC_LINK,
                "article_api": _absolute_api_url(f"/api/article?id={article_id}"),
                "slug": article_slug or make_article_slug(safe_title, article_id),
                "time": mvt_display_time(dt),
                "lang": _api_lang(safe_title, safe_summary, lang),
                "status": status or "posted"
            })

        return jsonify(stories)

    except Exception as e:
        log.error(f"Website API /api/stories error: {e}")
        return jsonify([])


@api_app.get("/api/article")
def api_article():
    """Full website article page data for GitHub Pages article.html?id=..."""
    try:
        article_id = (request.args.get("id") or "").strip()
        if not article_id:
            return jsonify({"error": "missing article id"}), 400

        row = db_execute("""
            SELECT id, title, summary, category, source, link, posted_at, found_at, lang, status,
                   article_excerpt, article_body, article_slug, is_breaking
            FROM articles
            WHERE id=%s
              AND (status IN ('posted','published','social_posted') OR (status='queued' AND lang='en'))
            LIMIT 1
        """, (article_id,), fetch="one")

        if not row:
            return jsonify({"error": "article not found"}), 404

        (rid, title, summary, category, source, link, posted_at, found_at, lang, status,
         article_excerpt, article_body, article_slug, is_breaking) = row

        safe_title = _api_clean_text(strip_source_links(title), 500)
        safe_summary = _api_clean_text(strip_source_links(summary), 1800)
        if not public_text_is_safe(f"{safe_title}\n{safe_summary}"):
            return jsonify({"error": "article failed public safety check"}), 404
        if looks_latin_thaana(f"{safe_title} {safe_summary}") and not _api_has_thaana(f"{safe_title} {safe_summary}"):
            return jsonify({"error": "article language cleanup pending"}), 404
        safe_category = _api_category(category, safe_title, safe_summary)
        safe_lang = _api_lang(safe_title, safe_summary, lang)
        body = article_body
        if not body or len(str(body).strip()) < 80:
            body = ensure_article_engine_body(
                rid, safe_title, safe_summary, safe_category,
                lang=safe_lang, is_breaking=bool(is_breaking)
            )
        body = _clean_article_engine_output(body, title=safe_title) if safe_lang == "en" else _api_clean_text(body, 4000)
        dt = posted_at or found_at

        return jsonify({
            "id": rid,
            "title": safe_title,
            "excerpt": _api_clean_text(article_excerpt or safe_summary, 360),
            "body": body,
            "paragraphs": [p.strip() for p in str(body or "").split("\n\n") if p.strip()],
            "category": safe_category,
            "source": SAMUGA_PUBLIC_SOURCE,
            "source_url": SAMUGA_PUBLIC_LINK,
            "community_url": SAMUGA_PUBLIC_LINK,
            "url": SAMUGA_PUBLIC_LINK,
            "time": mvt_display_time(dt),
            "lang": safe_lang,
            "slug": article_slug or make_article_slug(safe_title, rid),
            "related": related_articles_for_api(rid, safe_category, limit=4)
        })
    except Exception as e:
        log.error(f"Website API /api/article error: {e}")
        return jsonify({"error": "article unavailable"}), 200


# ── Public Website Chat API ───────────────────────────────────────────────────
# Safe public chat endpoint for samugamedia.com.
# IMPORTANT: Website chat should answer from TODAY'S Samuga archive first,
# not from old model memory. It also cleans markdown so replies feel human.
_PUBLIC_CHAT_RATE = {}  # ip -> [timestamps]
_PUBLIC_CHAT_LIMIT = 12
_PUBLIC_CHAT_WINDOW = 60 * 10  # 12 messages per 10 minutes per IP
_PUBLIC_CHAT_MAX_CHARS = 600
_PUBLIC_CHAT_BLOCKED_COMMANDS = [
    "/approve", "/approved", "/reject", "/confirm", "/cancel", "/ai ",
    "/remember", "/forget", "/memory", "/learning", "/post", "/queue",
    "approve this", "reject this", "post this", "send to telegram", "send to facebook",
    "send to instagram", "content lab", "core team", "admin", "database", "token",
    "api key", "password", "environment variable"
]
_PUBLIC_CHAT_NEWS_WORDS = [
    "latest", "news", "headline", "headlines", "today", "update", "updates", "breaking",
    "current", "what happened", "briefing", "summary", "summarize", "ޚަބަރު", "އަޕްޑޭޓް", "މިއަދު"
]

def _public_chat_client_id():
    """Return a stable-ish client ID for rate limiting behind Railway/proxies."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:80]
    return (request.remote_addr or "unknown")[:80]

def _public_chat_allowed(client_id):
    """Simple in-memory rate limiter. Good enough for Phase 1 public chat."""
    now = time.time()
    window_start = now - _PUBLIC_CHAT_WINDOW
    hits = [t for t in _PUBLIC_CHAT_RATE.get(client_id, []) if t >= window_start]
    if len(hits) >= _PUBLIC_CHAT_LIMIT:
        _PUBLIC_CHAT_RATE[client_id] = hits
        return False, _PUBLIC_CHAT_LIMIT
    hits.append(now)
    _PUBLIC_CHAT_RATE[client_id] = hits
    if len(_PUBLIC_CHAT_RATE) > 1000:
        for k in list(_PUBLIC_CHAT_RATE.keys())[:200]:
            _PUBLIC_CHAT_RATE.pop(k, None)
    return True, _PUBLIC_CHAT_LIMIT

def _public_chat_clean_message(message):
    """Clean and cap public website message."""
    msg = _api_clean_text(message, _PUBLIC_CHAT_MAX_CHARS)
    return msg.strip()

def _public_chat_is_blocked(message):
    """Block admin/control prompts from the public website chat."""
    low = (message or "").lower()
    if low.startswith("/") and not low.startswith("/search"):
        return True
    return any(term in low for term in _PUBLIC_CHAT_BLOCKED_COMMANDS)

def _public_chat_is_news_query(message):
    low = (message or "").lower()
    return any(w in low for w in _PUBLIC_CHAT_NEWS_WORDS)

def _public_chat_clean_reply(reply):
    """Make AI replies feel like chat, not raw Markdown/bot formatting."""
    txt = str(reply or "")
    txt = txt.replace("**", "")
    txt = txt.replace("__", "")
    txt = txt.replace("###", "")
    txt = txt.replace("##", "")
    txt = txt.replace("#", "")
    txt = re.sub(r"\n\s*[-*]\s+", "\n• ", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    txt = re.sub(r"[ \t]{2,}", " ", txt)
    return _api_clean_text(txt.strip(), 1000)

def _public_chat_latest_rows(lang=None, limit=8, hours=30):
    """Read newest public website stories from DB. Default: recent/current only."""
    try:
        since = utcnow() - timedelta(hours=hours)
        rows = db_execute("""
            SELECT title, summary, category, source, link, posted_at, found_at, lang, status
            FROM articles
            WHERE status IN ('posted','published','social_posted')
              AND COALESCE(posted_at, found_at) >= %s
            ORDER BY COALESCE(posted_at, found_at) DESC NULLS LAST
            LIMIT %s
        """, (since, limit * 3), fetch="all") or []

        clean = []
        seen = set()
        for title, summary, category, source, link, posted_at, found_at, row_lang, status in rows:
            safe_title = _api_clean_text(strip_source_links(title), 260)
            safe_summary = _api_clean_text(strip_source_links(summary), 520)
            if not safe_title or not public_text_is_safe(f"{safe_title}\n{safe_summary}"):
                continue
            if not public_text_is_safe(f"{safe_title}\n{safe_summary}"):
                continue
            detected = _api_lang(safe_title, safe_summary, row_lang)
            if lang in ("en", "dv") and detected != lang:
                continue
            key = _caption_match_key(safe_title) or safe_title.lower()[:90]
            if key in seen:
                continue
            seen.add(key)
            dt = posted_at or found_at
            clean.append({
                "title": safe_title,
                "summary": safe_summary,
                "category": _api_category(category, safe_title, safe_summary),
                "source": SAMUGA_PUBLIC_SOURCE,
                "url": SAMUGA_PUBLIC_LINK,
                "time": mvt_display_time(dt),
                "lang": detected
            })
            if len(clean) >= limit:
                break
        return clean
    except Exception as e:
        log.error(f"Website chat latest rows error: {e}")
        return []

def _public_chat_search_rows(message, lang=None, limit=6):
    """Simple archive search from website DB for specific user topics."""
    try:
        terms = [w for w in re.findall(r"[\w\u0780-\u07BF]{3,}", message or "") if w.lower() not in {
            "latest", "news", "today", "what", "about", "show", "give", "tell", "ޚަބަރު", "މިއަދު"
        }]
        if not terms:
            return []
        # Use up to 3 strong terms to avoid huge/slow search.
        q = " ".join(terms[:3])
        pattern = f"%{q}%"
        rows = db_execute("""
            SELECT title, summary, category, source, link, posted_at, found_at, lang, status
            FROM articles
            WHERE status IN ('posted','published','social_posted')
              AND (title ILIKE %s OR summary ILIKE %s OR source ILIKE %s)
            ORDER BY COALESCE(posted_at, found_at) DESC NULLS LAST
            LIMIT %s
        """, (pattern, pattern, pattern, limit * 3), fetch="all") or []
        clean = []
        seen = set()
        for title, summary, category, source, link, posted_at, found_at, row_lang, status in rows:
            safe_title = _api_clean_text(strip_source_links(title), 260)
            safe_summary = _api_clean_text(strip_source_links(summary), 520)
            detected = _api_lang(safe_title, safe_summary, row_lang)
            if lang in ("en", "dv") and detected != lang:
                continue
            key = _caption_match_key(safe_title) or safe_title.lower()[:90]
            if not safe_title or key in seen:
                continue
            seen.add(key)
            dt = posted_at or found_at
            clean.append({
                "title": safe_title,
                "summary": safe_summary,
                "category": _api_category(category, safe_title, safe_summary),
                "source": SAMUGA_PUBLIC_SOURCE,
                "url": SAMUGA_PUBLIC_LINK,
                "time": mvt_display_time(dt),
                "lang": detected
            })
            if len(clean) >= limit:
                break
        return clean
    except Exception as e:
        log.error(f"Website chat search rows error: {e}")
        return []

def _public_chat_format_news(rows, lang="en", searched=False):
    """Friendly website chat answer from real Samuga DB rows."""
    if not rows:
        return "I don't see fresh public stories in the website archive yet bro. Try again in a few minutes." if lang != "dv" else "ވެބްސައިޓް އާކައިވްގައި އަދި އާ ޚަބަރެއް ނުފެނޭ. މަދުކޮށް ފަހުން އަހާލާ."

    if lang == "dv":
        intro = "މިއީ ސަމުގާގެ އެންމެ އާ ޚަބަރުތައް:" if not searched else "މިއީ ހޯދުމުން ފެނުނު ޚަބަރުތައް:"
        parts = [intro]
        for i, r in enumerate(rows[:6], 1):
            line = f"{i}. {r['title']}"
            if r.get("summary"):
                line += f" — {r['summary'][:180]}"
            parts.append(line)
        parts.append("އެއް ޚަބަރެއް ތަފްސީލުން ބުނަން ބޭނުންތަ؟")
        return "\n\n".join(parts)

    intro = "Here are the latest stories on Samuga right now:" if not searched else "Here’s what I found in the Samuga archive:"
    parts = [intro]
    for i, r in enumerate(rows[:6], 1):
        line = f"{i}. {r['title']}"
        if r.get("summary"):
            line += f" — {r['summary'][:190]}"
        line += f"\nSamuga Media • {r.get('time','Recent')}"
        parts.append(line)
    parts.append("Ask me about any one of these and I’ll explain it clearly.")
    return "\n\n".join(parts)

def _public_chat_tavily_context(message, lang="en"):
    """Live search context for website chat, sanitized so no source URLs leak."""
    try:
        if not TAVILY_API_KEY:
            return ""
        q = f"Maldives latest news {message}" if lang != "dv" else f"Maldives news {message}"
        ctx = tavily_search(q)
        return strip_source_links(_api_clean_text(ctx, 1200))
    except Exception as e:
        log.warning(f"Website chat Tavily context failed: {e}")
        return ""

def _public_chat_context(rows):
    lines = []
    for r in rows[:8]:
        lines.append(f"- {r['title']} | {r.get('summary','')} | Samuga Media | {r.get('time','')}")
    return "\n".join(lines)



# ── Unified Public Samuga AI Brain ────────────────────────────────────────────
# Website chat, public Telegram DM, and future WhatsApp should all call this one
# function so Samuga AI has one public personality, one memory, and one analytics stream.

_PUBLIC_TOPIC_KEYWORDS = {
    "housing": ["housing","flat","flats","rent","land","apartment","gedhoru","hiya","vinares","ފްލެޓް","ބިން"],
    "politics": ["politics","president","minister","majlis","parliament","mdp","pnc","ppm","election","bill","law","ރައީސް","މަޖިލީސް"],
    "economy": ["economy","dollar","usd","mvr","rufiyaa","debt","tax","price","inflation","budget","ޑޮލަރ","ރުފިޔާ"],
    "tourism": ["tourism","tourist","resort","travel","airport","arrival","hotel","ޓޫރިޒަމް"],
    "crime": ["police","arrest","court","murder","stab","drug","gang","theft","ފުލުހުން","ކޯޓު"],
    "health": ["health","hospital","doctor","clinic","aasandha","disease","ސިއްހަތު","ހޮސްޕިޓަލް"],
    "education": ["school","student","visa","university","teacher","exam","ސްކޫލް","ދަރިވަރު"],
    "weather": ["weather","rain","storm","wind","sea","alert","mms","ވައި","ވާރޭ"],
    "foreign": ["iran","israel","us","usa","america","india","china","qatar","uk","war","global","world","އިންޑިޔާ","ޗައިނާ"],
    "sports": ["sports","football","fifa","match","team","ކުޅިވަރު","ފުޓްބޯޅަ"],
}

_CURRENT_GLOBAL_WORDS = [
    "now","current","latest","today","breaking","happening","war","conflict","iran","israel",
    "america","us ","usa","ukraine","russia","qatar","oil","global","world"
]

def public_detect_topics(message):
    low = (message or "").lower()
    topics = []
    for topic, kws in _PUBLIC_TOPIC_KEYWORDS.items():
        if any(k in low for k in kws):
            topics.append(topic)
    if not topics and _public_chat_is_news_query(message):
        topics.append("news")
    if not topics:
        topics.append("general")
    return topics[:5]

def public_detect_intent(message):
    low = (message or "").lower()
    if any(w in low for w in ["hi", "hello", "hey", "salaam", "ހެލޯ"]) and len(low.split()) <= 4:
        return "greeting"
    if any(w in low for w in ["breaking", "urgent", "ބްރޭކިންގ"]):
        return "breaking_news"
    if any(w in low for w in ["summarize", "summary", "briefing", "biggest", "today", "މިއަދު"]):
        return "briefing"
    if _public_chat_is_news_query(message):
        return "news_query"
    if any(w in low for w in _CURRENT_GLOBAL_WORDS):
        return "current_global"
    return "general_chat"

def public_is_global_current_query(message):
    low = " " + (message or "").lower() + " "
    local_hits = ["maldives","raajje","dhivehi","male","malé","samuga","ރާއްޖެ","ދިވެހި"]
    if any(x in low for x in local_hits):
        return False
    return any(w in low for w in _CURRENT_GLOBAL_WORDS) or any(t in public_detect_topics(message) for t in ["foreign"])

def public_log_chat(platform, session_id, user_key, user_message, bot_reply, lang, intent, topics, used_search=False):
    """Store public Samuga AI chats for interest analytics across website/Telegram/future WhatsApp."""
    try:
        if not DB_ENABLED:
            return
        topics = topics or ["general"]
        db_execute("""
            INSERT INTO public_chat_messages
                (platform, session_id, user_key, user_message, bot_reply, lang, intent, topics, used_search)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            platform, str(session_id or "")[:120], str(user_key or "")[:160],
            str(user_message or "")[:1200], str(bot_reply or "")[:1800],
            lang, intent, topics, bool(used_search)
        ))
        for topic in topics:
            db_execute("""
                INSERT INTO public_interest_daily (day, topic, platform, count, updated_at)
                VALUES (CURRENT_DATE, %s, %s, 1, NOW())
                ON CONFLICT (day, topic, platform)
                DO UPDATE SET count = public_interest_daily.count + 1, updated_at = NOW()
            """, (topic, platform))
    except Exception as e:
        log.debug(f"Public chat analytics save failed: {e}")

def public_session_key(platform, user_key, session_id=""):
    platform = str(platform or "web").lower()
    user_key = str(user_key or "anon")[:80]
    session_id = str(session_id or "default")[:80]
    if platform == "telegram":
        return f"public:telegram:{user_key}"
    if platform == "whatsapp":
        return f"public:whatsapp:{user_key}"
    return f"public:web:{user_key}:{session_id}"

def public_get_recent_interest(limit=8):
    try:
        rows = db_execute("""
            SELECT topic, SUM(count) AS c
            FROM public_interest_daily
            WHERE day >= CURRENT_DATE - INTERVAL '3 days'
            GROUP BY topic
            ORDER BY c DESC
            LIMIT %s
        """, (limit,), fetch="all") or []
        return ", ".join([f"{r[0]} ({r[1]})" for r in rows])
    except Exception:
        return ""

def public_build_live_context(message, lang="en"):
    """Use Tavily smartly: local queries search Maldives; global/current queries search globally."""
    try:
        if not TAVILY_API_KEY:
            return "", False
        if public_is_global_current_query(message):
            q = message
        elif _public_chat_is_news_query(message):
            q = f"Maldives news {message}"
        else:
            # For normal questions we usually don't need search.
            return "", False
        ctx = tavily_search(q)
        return strip_source_links(_api_clean_text(ctx, 1800)), bool(ctx)
    except Exception as e:
        log.warning(f"Public Samuga AI live search failed: {e}")
        return "", False

def public_samuga_ai_chat(message, platform="web", user_key="", session_id="", lang=None):
    """
    One public Samuga AI for website + @SamugaNewsBot + future WhatsApp.
    This is NOT the private core-team brain.
    """
    message = _public_chat_clean_message(message)
    if not message:
        return "Ask me something bro. I can chat or help with latest news."

    detected_lang = "dv" if (lang == "dv" or is_dhivehi(message)) else "en"
    skey = public_session_key(platform, user_key, session_id)
    history = get_conversation(skey)[-8:]
    intent = public_detect_intent(message)
    topics = public_detect_topics(message)

    # Story intelligence first if the archive can directly answer.
    story_answer = None
    try:
        story_answer = answer_story_query(message)
    except Exception as e:
        log.debug(f"Public story query fallback: {e}")

    latest_rows = []
    search_rows = []
    db_context = ""
    if intent in ("news_query", "breaking_news", "briefing", "current_global") or topics != ["general"]:
        latest_rows = _public_chat_latest_rows(lang=None if detected_lang == "en" else "dv", limit=8, hours=48)
        search_rows = _public_chat_search_rows(message, lang=None if detected_lang == "en" else "dv", limit=6)
        context_rows = search_rows or latest_rows
        if intent == "breaking_news":
            breaking_rows = [r for r in latest_rows if str(r.get("category","")).upper() == "BREAKING"]
            context_rows = breaking_rows or latest_rows[:4]
        db_context = _public_chat_context(context_rows[:6])

    live_context, used_search = public_build_live_context(message, lang=detected_lang)
    interests = public_get_recent_interest()

    if story_answer and not public_is_global_current_query(message):
        reply = _public_chat_clean_reply(story_answer)
        add_to_conversation(skey, "user", message)
        add_to_conversation(skey, "assistant", reply)
        public_log_chat(platform, session_id, user_key, message, reply, detected_lang, intent, topics, used_search=False)
        return reply

    system = f"""You are Samuga AI, the single public chatbot for Samuga Media.
You are used on the website, Telegram @SamugaNewsBot, and later WhatsApp.
You are friendly, sharp, and useful — like a Maldivian news buddy, not a hard-coded bot.

IMPORTANT IDENTITY:
- You are the PUBLIC Samuga AI, not the private core-team newsroom brain.
- Never reveal admin/content-lab/private commands.
- You can answer Maldives news, global current events, and normal questions.
- For current/global questions, use live search context if provided.
- For Maldives questions, use Samuga archive first, then live search if helpful.
- Do not include external source URLs. Send people to @samugacommunity for Samuga updates.
- Keep replies conversational. No markdown **, ###, long separators, or robotic lists.
- Short by default. If news: max 3 items unless user asks for more.
- Remember the chat history and answer follow-ups naturally.
- If user uses Dhivehi/Thaana, answer in natural Dhivehi. If English, answer in English.

Public interest radar from recent chats: {interests or "not enough data yet"}.
"""

    user_block = f"""User message:
{message}

Intent: {intent}
Topics: {", ".join(topics)}
Platform: {platform}
Fresh Samuga archive context:
{db_context or "No direct Samuga archive context found."}

Live search context:
{live_context or "No live search context used or available."}
"""

    try:
        messages = []
        for h in history[-8:]:
            role = h.get("role")
            content = h.get("content", "")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content[:1200]})
        messages.append({"role": "user", "content": user_block})

        if detected_lang == "dv" and GEMINI_API_KEY:
            # Gemini is stronger for Dhivehi. Include history manually in prompt.
            hist_txt = "\n".join([f"{h.get('role')}: {h.get('content','')}" for h in history[-6:]])
            gemini_prompt = f"""{system}

Recent chat history:
{hist_txt}

{user_block}

Answer now in natural Dhivehi Thaana if the user used Dhivehi; otherwise English.
"""
            reply = _gemini_post(gemini_prompt, timeout=25) or ""
            if not reply:
                raise RuntimeError("Gemini public chat returned empty")
        else:
            msg = ai.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=650,
                system=system,
                messages=messages
            )
            reply = msg.content[0].text.strip()

    except Exception as e:
        log.error(f"Unified public Samuga AI failed: {e}")
        # Safe fallback: show latest DB rows if available.
        if latest_rows:
            reply = _public_chat_format_news(latest_rows[:3], lang=detected_lang)
        else:
            reply = "I had a small issue checking live updates bro. Try again in a moment."

    reply = _public_chat_clean_reply(reply)
    add_to_conversation(skey, "user", message)
    add_to_conversation(skey, "assistant", reply)
    public_log_chat(platform, session_id, user_key, message, reply, detected_lang, intent, topics, used_search=used_search)
    return reply


@api_app.route("/api/chat", methods=["POST", "OPTIONS"])
def api_chat():
    """Public website chat endpoint using the unified public Samuga AI brain."""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    try:
        client_id = _public_chat_client_id()
        allowed, limit = _public_chat_allowed(client_id)
        if not allowed:
            return jsonify({
                "ok": False,
                "error": "rate_limited",
                "reply": "Too many messages too fast bro 😅 Please wait a few minutes and try again."
            }), 429

        data = request.get_json(silent=True) or {}
        message = _public_chat_clean_message(data.get("message", ""))
        session_id = _api_clean_text(data.get("session_id", "web"), 80) or "web"
        requested_lang = str(data.get("lang") or "").lower()
        lang = "dv" if requested_lang == "dv" or is_dhivehi(message) else "en"

        if not message:
            return jsonify({
                "ok": False,
                "error": "empty_message",
                "reply": "Ask me something bro. I can chat or help with latest news."
            }), 400

        if _public_chat_is_blocked(message):
            return jsonify({
                "ok": True,
                "reply": "I can only do public chat and public news here bro. Posting, approvals, and newsroom controls are only for the private Samuga team."
            })

        log.info(f"🌐 Website public Samuga AI {client_id}: {message[:80]}")
        reply = public_samuga_ai_chat(
            message=message,
            platform="web",
            user_key=client_id,
            session_id=session_id,
            lang=lang
        )

        return jsonify({
            "ok": True,
            "reply": reply,
            "source": "Unified public Samuga AI",
            "mode": "public_samuga_ai",
            "rate_limit": {"limit": limit, "window_seconds": _PUBLIC_CHAT_WINDOW}
        })

    except Exception as e:
        log.error(f"Website API /api/chat error: {e}")
        return jsonify({
            "ok": False,
            "error": "server_error",
            "reply": "Something went wrong bro 😅 Try again in a moment."
        })

def start_api_server():
    """Start the public website API on Railway's assigned PORT."""
    port = int(os.environ.get("PORT", 8080))
    log.info(f"🌐 Website API starting on port {port}")
    api_app.run(host="0.0.0.0", port=port, use_reloader=False)



# ── State Persistence (JSON fallback — survives Railway restarts) ─────────────
import os as _os, json as _json, threading as _threading
DATA_DIR   = "/data"
_os.makedirs(DATA_DIR, exist_ok=True)
SEEN_FILE  = _os.path.join(DATA_DIR, "seen_articles.json")
STATE_FILE = _os.path.join(DATA_DIR, "bot_state.json")
_state_lock = _threading.Lock()
_poll_offset = [0]

def load_seen():
    try:
        if _os.path.exists(SEEN_FILE):
            with open(SEEN_FILE) as f: return set(_json.load(f))
    except Exception as e: log.error(f"load_seen: {e}")
    return set()

def save_seen(seen):
    try:
        with open(SEEN_FILE,"w") as f: _json.dump(list(seen)[-1000:], f)
    except Exception as e: log.error(f"save_seen: {e}")

def _load_state():
    try:
        if _os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f: return _json.load(f)
    except Exception as e: log.error(f"load_state: {e}")
    return {}

def _save_state(state):
    try:
        with _state_lock:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w") as f: _json.dump(state, f)
            _os.replace(tmp, STATE_FILE)
    except Exception as e: log.error(f"save_state: {e}")

def _serialize_social_counts():
    sc = dict(social_post_counts)
    if sc.get("date") and not isinstance(sc["date"], str):
        sc["date"] = sc["date"].isoformat()
    return sc

def _serialize_approval_queue():
    import base64
    out = {}
    for k, v in approval_queue.items():
        item = dict(v)
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

def persist_state():
    """Snapshot all volatile state to disk."""
    try:
        sq_serialized = []
        with _social_queue_lock:
            for item in _social_queue:
                sq_serialized.append({
                    "img_bytes_b64": __import__("base64").b64encode(item["img_bytes"]).decode(),
                    "caption": item["caption"],
                    "queued_at": item["queued_at"].isoformat(),
                    "article_id": item.get("article_id"),
                    "title": item.get("title",""),
                    "summary": item.get("summary",""),
                    "cat": item.get("cat","LOCAL"),
                    "source": item.get("source","Samuga Media"),
                    "link": item.get("link",""),
                    "lang": item.get("lang","en"),
                    "is_breaking": item.get("is_breaking", False),
                    "key_label": item.get("key_label","Post"),
                    "tg_ok": item.get("tg_ok", False),
                    "post_telegram": item.get("post_telegram", True),
                    "notify_chat_id": item.get("notify_chat_id"),
                    "notify_thread_id": item.get("notify_thread_id"),
                })
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
            "last_social_post_time": _last_social_post_time.isoformat() if _last_social_post_time else None,
            "approval_counter": _approval_counter[0],
            "approval_queue": _serialize_approval_queue(),
            "poll_offset": _poll_offset[0],
            "social_queue": sq_serialized,
            "website_banner": website_banner,
        }
        _save_state(state)
    except Exception as e:
        log.error(f"persist_state: {e}")

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
        try:
            website_banner.update(state.get("website_banner", {}))
        except Exception:
            pass
        global _last_social_post_time
        lspt = state.get("last_social_post_time")
        if lspt:
            try: _last_social_post_time = datetime.fromisoformat(lspt)
            except Exception: pass
        sq = state.get("social_queue", [])
        if sq:
            import base64 as _b64
            with _social_queue_lock:
                for item in sq:
                    try:
                        _social_queue.append({
                            "img_bytes": _b64.b64decode(item["img_bytes_b64"]),
                            "caption": item["caption"],
                            "queued_at": datetime.fromisoformat(item["queued_at"]),
                            "article_id": item.get("article_id"),
                            "title": item.get("title",""),
                            "summary": item.get("summary",""),
                            "cat": item.get("cat","LOCAL"),
                            "source": item.get("source","Samuga Media"),
                            "link": item.get("link",""),
                            "lang": item.get("lang","en"),
                            "is_breaking": item.get("is_breaking", False),
                            "key_label": item.get("key_label","Post"),
                            "tg_ok": item.get("tg_ok", False),
                            "post_telegram": item.get("post_telegram", True),
                            "notify_chat_id": item.get("notify_chat_id"),
                            "notify_thread_id": item.get("notify_thread_id"),
                        })
                    except Exception: pass
            log.info(f"📲 Social queue restored: {len(_social_queue)} post(s) waiting")
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


# ── Entry ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import signal, atexit

    def _graceful_shutdown(signum=None, frame=None):
        """Save all state before Railway kills the process on redeploy."""
        log.info("🛑 Shutdown signal received — saving state before exit...")
        try:
            persist_state()
            log.info("✅ State saved — approval queue, social queue, counters all persisted")
        except Exception as e:
            log.error(f"State save on shutdown: {e}")

    # Railway sends SIGTERM before killing the container on redeploy
    signal.signal(signal.SIGTERM, _graceful_shutdown)
    signal.signal(signal.SIGINT,  _graceful_shutdown)
    # Also register with atexit as a backup (catches normal Python exit)
    atexit.register(_graceful_shutdown)

    log.info(f"🚀 Samuga AI v{SAMUGA_VERSION} starting (newsroom intelligence + story timelines + live brain)...")
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
    log.info("📅 News: 6AM-10PM every 15min | Night: breaking only")
    log.info("🌤️ Weather: 8AM, 2PM, 10:30PM → all platforms | MMS alerts auto")
    log.info("🌅 7AM Brief | 🌙 12AM Summary | 📊 Friday Digest | 🕌 Prayer times + Hijri")
    log.info("📚 Story Intelligence: timeline threads active")
    log.info("🧠 Core team brain: live newsroom awareness + persistent memory")
    log.info("💬 Smart chat: history, web search, Dhivehi support, story queries")
    if posting_paused():
        log.warning("🛑 POSTING_PAUSED=true — all public posting is blocked")
    elif social_paused():
        log.warning("🛑 SOCIAL_PAUSED=true — Buffer/social posting is blocked")

    # Start social queue worker — drains one post every 10 minutes
    threading.Thread(target=_social_queue_worker, daemon=True).start()
    log.info("📲 Social queue worker started (10-min gap between posts)")

    init_database()  # connect to Postgres (falls back to JSON if unavailable)
    restore_state()  # bring back dedup memory, daily counters, pending cards, analytics


    # Wire db module with shared functions
    import db as _db
    _db.utcnow       = utcnow
    _db.ai           = ai
    _db._gemini_post = _gemini_post
    _db.send_text    = send_text
    _db.GEMINI_API_KEY   = GEMINI_API_KEY
    _db.CORE_TEAM_CHAT_ID = CORE_TEAM_CHAT_ID
    _db.ALERT_THREAD_ID   = ALERT_THREAD_ID

    # Wire scoring module with utcnow
    import scoring as _sc
    _sc.utcnow = utcnow

    # Wire fetchers module with shared AI client
    import fetchers as _ft
    _ft.ai             = ai
    _ft._gemini_post   = _gemini_post
    _ft.GEMINI_API_KEY = GEMINI_API_KEY

    # Wire weather module with shared functions (avoids circular imports)
    import weather as _wx
    _wx.send_photo      = send_photo
    _wx.send_text       = send_text
    _wx.queue_for_social = queue_for_social
    _wx.utcnow          = utcnow
    _wx.mvt_now         = mvt_now
    seen_on_start=load_seen()
    log.info(f"📚 Loaded {len(seen_on_start)} seen articles")

    threading.Thread(target=handle_updates, daemon=True).start()
    threading.Thread(target=start_api_server, daemon=True).start()

    scheduler=BlockingScheduler(timezone="UTC")
    scheduler.add_job(scheduled_check, "interval", minutes=15)
    # Breaking news fast check every 5 min (LOCAL/DISASTER only)
    scheduler.add_job(breaking_news_check, "interval", minutes=5)
    # Approval lifecycle — English auto-posts at 15min, Dhivehi expires at 2h. Check every 5 min.
    scheduler.add_job(expire_old_approvals, "interval", minutes=5)
    scheduler.add_job(release_content_lab_drip, "interval", minutes=10)
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
    # Weather cards — 3x daily to ALL platforms (Telegram + FB + IG + X)
    # 8:00 AM MVT = 3:00 UTC
    scheduler.add_job(lambda: send_weather_update("morning"), "cron", hour=3, minute=0)
    # 2:00 PM MVT = 9:00 UTC
    scheduler.add_job(lambda: send_weather_update("afternoon"), "cron", hour=9, minute=0)
    # 10:30 PM MVT = 17:30 UTC
    scheduler.add_job(lambda: send_weather_update("evening"), "cron", hour=17, minute=30)
    # Tip/story CTA 8:30AM MVT = 3:30AM UTC
    scheduler.add_job(send_tip_cta, "cron", hour=3, minute=30)  # 8:30AM MVT
    # Tip/story CTA 8:30PM MVT = 3:30PM UTC
    scheduler.add_job(send_tip_cta, "cron", hour=15, minute=30)  # 8:30PM MVT

    # Periodic state heartbeat — saves every 5 minutes so restarts lose minimal state
    scheduler.add_job(persist_state, "interval", minutes=5, id="state_heartbeat")
    scheduler.add_job(ops_watchdog, "interval", minutes=10)

    log.info("⏰ Scheduler started!")
    scheduler.start()
