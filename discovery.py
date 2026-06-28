"""
discovery.py - Samuga Discovery Engine v1.0
Matches fetchers.py v7.0 injection pattern exactly.

WHAT IT DOES:
  Actively hunts for unreported stories every hour.
  Searches Google News for topics your team cares about
  (dollar rates, cost of living, corruption, banks, etc.)
  and surfaces articles that haven't been seen yet.

  Topics are managed from Telegram:
    /discovery list        - see all active topics
    /discovery add <topic> - add a new hunt topic
    /discovery remove <n>  - remove topic by number
    /discovery run         - trigger hunt right now
    /discovery pause       - pause hunting
    /discovery resume      - resume hunting

  Results go to Content Lab (thread 9061) as a brief,
  NOT auto-published. Human editor decides what to chase.

Dependencies injected by bot.py at startup:
  _gemini_post    = bot.py's Gemini fallback-chain function
  kv_get          = db.kv_get
  kv_set          = db.kv_set
  send_text       = bot.py's send_text
  CORE_TEAM_CHAT_ID    = bot.py constant
  ALERT_THREAD_ID      = bot.py constant
"""

import os, hashlib, logging, feedparser, re, time
from datetime import datetime, timezone, timedelta
from urllib.parse import quote_plus

log = logging.getLogger(__name__)

# -- Injected by bot.py after import (same pattern as fetchers.py) -------------
_gemini_post      = None
kv_get            = None
kv_set            = None
send_text         = None
CORE_TEAM_CHAT_ID = None
ALERT_THREAD_ID   = None

# -- Config --------------------------------------------------------------------
DISCOVERY_KV_TOPICS  = "discovery_topics"     # kv key for topic list
DISCOVERY_KV_SEEN    = "discovery_seen_links"  # kv key for seen URLs
DISCOVERY_KV_PAUSED  = "discovery_paused"      # kv key for pause flag
DISCOVERY_MAX_SEEN   = 500                     # cap seen links memory
DISCOVERY_MAX_RESULTS = 5                      # max stories per hunt
DISCOVERY_COOLDOWN_H  = 6                      # don't re-surface same URL for 6h

# -- Default topics (the ones you care about most) ----------------------------
DEFAULT_TOPICS = [
    # Dollar & economy
    {"id": "dollar_black",    "query": "Maldives dollar black market rate rufiyaa",         "label": "Dollar Black Market",      "cat": "BUSINESS"},
    {"id": "cost_living",     "query": "Maldives cost of living prices expensive 2026",     "label": "Cost of Living",           "cat": "BUSINESS"},
    {"id": "goods_prices",    "query": "Maldives grocery food prices import inflation",     "label": "Goods & Food Prices",      "cat": "BUSINESS"},
    {"id": "bank_dollar",     "query": "Maldives bank dollar card limit BML SBI",          "label": "Bank Dollar Card Limits",  "cat": "BUSINESS"},
    {"id": "corruption",      "query": "Maldives corruption bribery embezzlement ACC 2026", "label": "Corruption",               "cat": "POLITICAL"},
    {"id": "housing_rent",    "query": "Maldives housing rent flat prices Male 2026",       "label": "Housing & Rent",           "cat": "LOCAL"},
    {"id": "economy_debt",    "query": "Maldives economy debt IMF budget deficit 2026",     "label": "Economy & Debt",           "cat": "BUSINESS"},
    {"id": "parliament",      "query": "Maldives parliament majlis vote bill 2026",         "label": "Parliament Activity",      "cat": "POLITICAL"},
]


# -- Storage helpers -----------------------------------------------------------
def _get_topics():
    """Load topics from KV store. Falls back to defaults if empty."""
    if not kv_get:
        return list(DEFAULT_TOPICS)
    stored = kv_get(DISCOVERY_KV_TOPICS, None)
    if stored and isinstance(stored, list) and len(stored) > 0:
        return stored
    # First run - seed with defaults
    if kv_set:
        kv_set(DISCOVERY_KV_TOPICS, DEFAULT_TOPICS)
    return list(DEFAULT_TOPICS)


def _save_topics(topics):
    if kv_set:
        kv_set(DISCOVERY_KV_TOPICS, topics)


def _get_seen():
    if not kv_get:
        return {}
    return kv_get(DISCOVERY_KV_SEEN, {})


def _save_seen(seen):
    if not kv_set:
        return
    # cap to most recent DISCOVERY_MAX_SEEN entries
    if len(seen) > DISCOVERY_MAX_SEEN:
        cutoff = time.time() - (DISCOVERY_COOLDOWN_H * 3600)
        seen = {k: v for k, v in seen.items() if v > cutoff}
    kv_set(DISCOVERY_KV_SEEN, seen)


def _is_paused():
    if not kv_get:
        return False
    return kv_get(DISCOVERY_KV_PAUSED, False)


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# -- Core: search Google News RSS for a topic ---------------------------------
def _search_google_news(query, max_items=8):
    """Search Google News RSS for a query. Returns list of {title, link, source, published}."""
    try:
        encoded = quote_plus(query)
        url = f"https://news.google.com/rss/search?q={encoded}&hl=en&gl=MV&ceid=MV:en"
        feed = feedparser.parse(url)
        items = []
        for e in feed.entries[:max_items]:
            link = e.get("link", "")
            title = e.get("title", "").strip()
            if not link or not title:
                continue
            # Clean Google News title (removes " - Source" suffix)
            title = re.sub(r"\s+-\s+[\w\s]+$", "", title).strip()
            source = e.get("source", {}).get("title", "") if hasattr(e.get("source", ""), "get") else ""
            pub = e.get("published", "")
            items.append({"title": title, "link": link,
                          "source": source, "published": pub})
        return items
    except Exception as ex:
        log.warning(f"[DISCOVERY] Google News search failed for '{query}': {ex}")
        return []


# -- Core: Gemini editorial filter -------------------------------------------
_FILTER_PROMPT = """You are the senior editor at Samuga, a Maldivian newsroom.
Your team is hunting for unreported stories about: {topic_label}

Here are {count} article headlines found online.
Filter and rank them. Keep only articles that:
- Are genuinely relevant to {topic_label} in the Maldives context
- Contain NEW information (not just opinions or reposts)
- Would matter to ordinary Maldivians in their daily life
- Have NOT been widely covered by Maldivian media yet

Return ONLY a valid JSON array. No markdown, no backticks.
Each item: {{"title": "...", "link": "...", "source": "...", "why": "one sentence why this matters to Maldivians"}}
Return empty array [] if nothing is worth surfacing.
Max 3 items. Most important first.

Articles to filter:
{articles}"""


def _gemini_filter(items, topic_label):
    """Let Gemini editorially filter and rank raw Google News results."""
    if not _gemini_post or not items:
        return items[:3]
    articles_text = "\n".join(
        [f"{i+1}. {it['title']} ({it['source']}) — {it['link']}" for i, it in enumerate(items)]
    )
    prompt = _FILTER_PROMPT.format(
        topic_label=topic_label,
        count=len(items),
        articles=articles_text
    )
    raw = _gemini_post(prompt, timeout=20)
    if not raw:
        return items[:3]
    raw = raw.strip().replace("```json", "").replace("```", "").strip()
    try:
        import json
        filtered = json.loads(raw)
        if isinstance(filtered, list):
            return filtered
    except Exception:
        pass
    return items[:3]


# -- Core: run discovery for all topics ---------------------------------------
def run_discovery(notify=True):
    """
    Main discovery loop. Runs every hour via scheduler.
    Searches all active topics, filters via Gemini,
    deduplicates against seen links, sends brief to Content Lab.
    """
    if _is_paused():
        log.info("[DISCOVERY] paused - skipping")
        return

    topics = _get_topics()
    if not topics:
        log.info("[DISCOVERY] no topics configured")
        return

    seen = _get_seen()
    now_ts = time.time()
    all_hits = []

    for topic in topics:
        if not topic.get("active", True):
            continue
        query   = topic.get("query", "")
        label   = topic.get("label", query)
        cat     = topic.get("cat", "LOCAL")

        raw = _search_google_news(query)
        if not raw:
            continue

        # filter out already-seen links
        fresh = [r for r in raw
                 if hashlib.md5(r["link"].encode()).hexdigest()[:12] not in seen]
        if not fresh:
            log.info(f"[DISCOVERY] {label}: all {len(raw)} results already seen")
            continue

        # Gemini editorial filter
        filtered = _gemini_filter(fresh, label)
        if not filtered:
            continue

        for hit in filtered:
            link = hit.get("link", "")
            if not link:
                continue
            fid = hashlib.md5(link.encode()).hexdigest()[:12]
            seen[fid] = now_ts
            all_hits.append({
                "topic":  label,
                "cat":    cat,
                "title":  hit.get("title", ""),
                "link":   link,
                "source": hit.get("source", ""),
                "why":    hit.get("why", ""),
            })

        time.sleep(1)  # polite pause between searches

    _save_seen(seen)

    if not all_hits:
        log.info("[DISCOVERY] hunt complete - nothing new found this cycle")
        return

    # Build Content Lab brief
    mvt = _utcnow() + timedelta(hours=5)
    time_str = mvt.strftime("%H:%M MVT")

    lines = [f"🔍 <b>SAMUGA DISCOVERY — {time_str}</b>",
             f"<i>Hunted {len(topics)} topics — {len(all_hits)} new story lead(s)</i>\n"]

    # Group by topic
    by_topic = {}
    for h in all_hits:
        by_topic.setdefault(h["topic"], []).append(h)

    for topic_label, hits in by_topic.items():
        lines.append(f"<b>📌 {topic_label}</b>")
        for h in hits[:3]:
            lines.append(f"• <a href=\"{h['link']}\">{h['title']}</a>")
            if h.get("why"):
                lines.append(f"  <i>↳ {h['why']}</i>")
            if h.get("source"):
                lines.append(f"  <i>Source: {h['source']}</i>")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━")
    lines.append("<i>Discovery Engine - not auto-published. Your call.</i>")

    msg = "\n".join(lines)

    if notify and send_text and CORE_TEAM_CHAT_ID and ALERT_THREAD_ID:
        send_text(CORE_TEAM_CHAT_ID, msg, thread_id=ALERT_THREAD_ID)
        log.info(f"[DISCOVERY] sent {len(all_hits)} leads to Content Lab")
    else:
        log.info(f"[DISCOVERY] found {len(all_hits)} leads (notify=False)")

    return all_hits


# -- Topic management (called from bot.py command handler) --------------------
def discovery_list():
    """Returns formatted list of current topics for Telegram."""
    topics = _get_topics()
    if not topics:
        return "📭 No discovery topics yet. Add one with /discovery add <topic>"
    paused = _is_paused()
    status = "⏸ PAUSED" if paused else "✅ ACTIVE"
    lines = [f"🔍 <b>Discovery Topics</b> — {status}\n"]
    for i, t in enumerate(topics, 1):
        active = "✅" if t.get("active", True) else "⏸"
        lines.append(f"{active} <b>{i}.</b> {t['label']}")
        lines.append(f"   <i>{t['query'][:70]}</i>")
    lines.append(f"\n<i>Runs every hour. Use /discovery add, remove, pause, resume, run</i>")
    return "\n".join(lines)


def discovery_add(label, query=None, cat="LOCAL"):
    """Add a new discovery topic."""
    topics = _get_topics()
    # if no query given, build one from the label
    if not query:
        query = f"Maldives {label}"
    # check duplicate
    existing = [t for t in topics if t["label"].lower() == label.lower()]
    if existing:
        return f"⚠️ Topic '{label}' already exists."
    topic_id = re.sub(r"\W+", "_", label.lower())[:20]
    topics.append({
        "id":     topic_id,
        "query":  query,
        "label":  label,
        "cat":    cat,
        "active": True,
    })
    _save_topics(topics)
    return f"✅ Added: <b>{label}</b>\nQuery: <i>{query}</i>\nWill hunt next cycle."


def discovery_remove(index):
    """Remove topic by 1-based index."""
    topics = _get_topics()
    if index < 1 or index > len(topics):
        return f"❌ Invalid number. Use /discovery list to see topics."
    removed = topics.pop(index - 1)
    _save_topics(topics)
    return f"🗑 Removed: <b>{removed['label']}</b>"


def discovery_pause():
    if kv_set:
        kv_set(DISCOVERY_KV_PAUSED, True)
    return "⏸ Discovery paused. Use /discovery resume to restart."


def discovery_resume():
    if kv_set:
        kv_set(DISCOVERY_KV_PAUSED, False)
    return "▶️ Discovery resumed. Hunting every hour."
