"""
scoring.py — Samuga AI Scoring & Intelligence Module
Extracted from bot.py v7.0

Contains:
  - BREAKING_KEYWORDS / BREAKING_BLACKLIST
  - is_breaking()              Is this article breaking news?
  - is_dhivehi()               Does text contain Thaana script?
  - SOURCE_RELIABILITY         Source trust scores
  - source_reliability()       Get trust score for a source name
  - _dup_keywords()            Extract keywords for dedup comparison
  - is_duplicate_story()       Has this story already been posted?
  - remember_story_title()     Add title to dedup memory
  - register_in_cluster()      Track how many sources report same story
  - _detect_place()            Extract location from headline
  - _detect_event_type()       Extract event type from headline
  - score_article()            Full priority score (0-300+)
  - score_breakdown()          Itemized score breakdown
  - confidence_score()         How confident should we be in this story?
  - should_hold_for_review()   Should this be held back for team review?
  - format_score_breakdown()   Pretty HTML score card for Telegram

Shared state (recent_story_titles, recent_posts, user_conversations)
is also declared here and imported by bot.py.
"""

import re, logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

# ── Injected by bot.py at startup ─────────────────────────────────────────────
utcnow = None   # bot.py: import scoring; scoring.utcnow = utcnow


# ═══════════════════════════════════════════════════════════════════════════════
# Shared in-memory state
# ═══════════════════════════════════════════════════════════════════════════════

recent_posts       = []
user_conversations = {}

# Duplicate story detection — list of (title, timestamp)
recent_story_titles = []
DUP_WINDOW_HOURS    = 18
DUP_THRESHOLD       = 0.55

# Cluster store — tracks how many sources report the same event
_cluster_store = {}   # cluster_key -> {"sources": set(), "first_seen": datetime}
CLUSTER_WINDOW_HOURS = 6


# ═══════════════════════════════════════════════════════════════════════════════
# Breaking news keywords
# ═══════════════════════════════════════════════════════════════════════════════

BREAKING_KEYWORDS = [
    "killed", "dead", "dies", "murder", "shot", "stabbed", "explosion", "bomb", "attack",
    "tsunami", "earthquake", "flood", "disaster", "sinking", "collapsed", "hostage",
    "missing person", "fire broke", "crash landed", "emergency landing", "gas leak",
    "capsized", "swept away", "search and rescue"
]

BREAKING_BLACKLIST = [
    "world cup", "football", "cricket", "sports", "fifa", "champions league",
    "premier league", "tourism", "resort", "hotel", "travel", "award", "ranking",
    "luxury", "boutique", "hospitality", "destination", "lagoon", "civil war",
    "squad", "team", "player", "match", "game", "season", "transfer",
    "economy", "business", "market", "price", "investment", "opening", "launch", "event"
]

# Ad/promo markers (same as fetchers — self-contained copy so no cross-import)
_AD_MARKERS = [
    "hire", "rent", "for sale", "available", "booking", "book now", "contact",
    "call now", "whatsapp", "viber", "discount", "offer", "promo", "cheap",
    "price", "mvr ", "rufiyaa ", "delivery", "order now", "dm ", "inbox",
    "trip", "package", "tour", "charter", "ferry service", "speedboat",
]

def _looks_like_ad(text):
    t = text.lower()
    return any(m in t for m in _AD_MARKERS)


# ═══════════════════════════════════════════════════════════════════════════════
# Language helpers
# ═══════════════════════════════════════════════════════════════════════════════

def is_dhivehi(text):
    """Check if text contains Thaana script (Dhivehi)."""
    return any('\u0780' <= c <= '\u07BF' for c in text)

def is_breaking(title, summary="", cat=""):
    """Return True if article qualifies as breaking news."""
    text = (title + " " + summary).lower()

    if cat in ["FOOTBALL", "TOURISM", "WEATHER", "SPORTS", "LIFESTYLE"]:
        return False
    if _looks_like_ad(text):
        return False
    if any(bl in text for bl in BREAKING_BLACKLIST):
        return False
    if not any(kw in text for kw in BREAKING_KEYWORDS):
        return False
    if cat == "LOCAL":
        mv_terms = ["maldives", "male", "malé", "dhivehi", "maldivian", "raajje",
                    "atoll", "police", "court", "majlis", "minister", "president", "island"]
        if not any(t in text for t in mv_terms):
            return False
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# Source reliability
# ═══════════════════════════════════════════════════════════════════════════════

SOURCE_RELIABILITY = {
    "mvcrisis":            70,
    "mihaaru":             95,
    "sun":                 92,
    "sunonline":           92,
    "psm":                 90,
    "psmnews":             90,
    "presidency":          90,
    "edition":             88,
    "avas":                85,
    "see":                 82,
    "maldivesindependent": 82,
    "oneonline":           80,
    "maldivesvoice":       78,
    "visitmaldives":       75,
    "google news":         55,
}
DEFAULT_RELIABILITY = 60

def source_reliability(source_name):
    """Return 0-100 reliability score for a source string."""
    if not source_name:
        return DEFAULT_RELIABILITY
    s = source_name.lower()
    for key, val in SOURCE_RELIABILITY.items():
        if key in s:
            return val
    return DEFAULT_RELIABILITY


# ═══════════════════════════════════════════════════════════════════════════════
# Duplicate story detection
# ═══════════════════════════════════════════════════════════════════════════════

_DUP_STOPWORDS = {
    "a","an","the","in","on","at","to","of","for","and","or","but","is","are",
    "was","were","has","have","had","will","with","from","by","as","that","this",
    "it","its","be","been","being","do","did","does","maldives","maldivian",
    "says","said","after","over","into","out","up","about","than","more","new",
    "also","they","their","there","our","us","we","he","she","his","her","who","which",
}

_DUP_SYNONYMS = {
    "killed": "dead", "dies": "dead", "death": "dead", "deceased": "dead",
    "arrested": "detained", "detained": "detained", "remanded": "detained",
    "fire": "blaze", "blaze": "blaze", "burning": "blaze",
    "crash": "accident", "collision": "accident", "accident": "accident",
    "missing": "missing", "search": "missing",
    "flood": "flood", "flooding": "flood",
    "injured": "hurt", "hurt": "hurt", "wounded": "hurt",
}

def _dup_canon(word):
    return _DUP_SYNONYMS.get(word, word)

def _dup_keywords(title):
    """Extract canonicalized meaningful keywords from a title."""
    t = title.lower()
    t = re.sub(r"[^a-z0-9\u0780-\u07bf ]", " ", t)
    return set(_dup_canon(w) for w in t.split()
               if w not in _DUP_STOPWORDS and len(w) > 2)

def _now():
    """Safe utcnow — uses injected function or falls back."""
    if utcnow:
        return utcnow()
    return datetime.now(timezone.utc).replace(tzinfo=None)

def is_duplicate_story(title, threshold=DUP_THRESHOLD):
    """
    Return True if a very similar story was already posted/queued
    within DUP_WINDOW_HOURS. Uses keyword overlap similarity.
    """
    cutoff = _now() - timedelta(hours=DUP_WINDOW_HOURS)
    new_kw = _dup_keywords(title)
    if not new_kw:
        return False
    for old_title, ts in recent_story_titles:
        if ts < cutoff:
            continue
        old_kw = _dup_keywords(old_title)
        if not old_kw:
            continue
        overlap = len(new_kw & old_kw) / max(len(new_kw), len(old_kw))
        if overlap >= threshold:
            log.debug(f"Dup detected ({overlap:.0%}): {title[:50]!r} ~ {old_title[:50]!r}")
            return True
    return False

def remember_story_title(title):
    """Add a title to the dedup window."""
    recent_story_titles.append((title, _now()))
    # Prune old entries
    cutoff = _now() - timedelta(hours=DUP_WINDOW_HOURS + 1)
    recent_story_titles[:] = [(t, ts) for t, ts in recent_story_titles if ts > cutoff]


# ═══════════════════════════════════════════════════════════════════════════════
# Story clustering — how many sources report the same event
# ═══════════════════════════════════════════════════════════════════════════════

def _cluster_key(title):
    """Stable key for grouping articles about the same event."""
    t = title.lower()
    t = re.sub(r"[^a-z0-9\u0780-\u07bf ]", " ", t)
    words = [w for w in t.split() if w not in _DUP_STOPWORDS and len(w) > 2]
    return " ".join(sorted(words[:6]))

def _cluster_similarity(a, b):
    """Jaccard similarity between two title keyword sets."""
    ka = _dup_keywords(a)
    kb = _dup_keywords(b)
    if not ka or not kb:
        return 0.0
    return len(ka & kb) / len(ka | kb)

def register_in_cluster(title, source):
    """
    Register this article in the cluster store.
    Returns (cluster_size, list_of_sources).
    """
    global _cluster_store
    now = _now()
    cutoff = now - timedelta(hours=CLUSTER_WINDOW_HOURS)

    # Prune expired clusters
    _cluster_store = {
        k: v for k, v in _cluster_store.items()
        if v["first_seen"] > cutoff
    }

    key = _cluster_key(title)

    # Try to find an existing matching cluster
    best_key  = None
    best_sim  = 0.0
    for ck in _cluster_store:
        sim = _cluster_similarity(title, ck)
        if sim > best_sim:
            best_sim = sim
            best_key = ck

    if best_sim >= 0.40 and best_key:
        _cluster_store[best_key]["sources"].add(source)
        entry = _cluster_store[best_key]
    else:
        _cluster_store[key] = {"sources": {source}, "first_seen": now}
        entry = _cluster_store[key]

    sources = list(entry["sources"])
    return len(sources), sources


# ═══════════════════════════════════════════════════════════════════════════════
# Place and event detection (for Story Intelligence)
# ═══════════════════════════════════════════════════════════════════════════════

_MV_PLACES = [
    "male", "malé", "hulhumale", "addu", "fuvahmulah", "kulhudhuffushi",
    "thinadhoo", "naifaru", "ungoofaaru", "eydhafushi", "dhidhdhoo",
    "velidhoo", "mahibadhoo", "hithadhoo", "fonadhoo", "vilingili",
]

_EVENT_TYPES = {
    "fire":      ["fire", "blaze", "burning", "burned", "arson"],
    "accident":  ["crash", "collision", "accident", "capsize", "sinking", "overturned"],
    "death":     ["dead", "died", "killed", "murder", "death", "deceased"],
    "missing":   ["missing", "search", "rescue", "disappeared"],
    "arrest":    ["arrested", "detained", "remanded", "charged", "sentenced"],
    "flood":     ["flood", "flooding", "inundated", "submerged"],
    "assault":   ["assault", "stabbed", "attacked", "injured", "wounded"],
}

def _detect_place(title):
    t = title.lower()
    for place in _MV_PLACES:
        if place in t:
            return place
    return None

def _detect_event_type(title):
    t = title.lower()
    for etype, keywords in _EVENT_TYPES.items():
        if any(kw in t for kw in keywords):
            return etype
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Article priority scoring
# ═══════════════════════════════════════════════════════════════════════════════

def score_breakdown(a):
    """
    Return (total_score, [(label, points), ...]) for an article dict.
    This is the single source of truth for priority scoring.
    """
    title   = a.get("title", "")
    summary = a.get("summary", "") or ""
    cat     = a.get("cat", "LOCAL")
    source  = a.get("source", "")
    lang    = a.get("lang", "en")
    text    = (title + " " + summary).lower()
    items   = []

    def add(label, pts):
        items.append((label, pts))

    # ── Category base score ───────────────────────────────────────────────────
    cat_scores = {
        "BREAKING": 120, "DISASTER": 120,
        "LOCAL":     60, "POLITICAL": 70,
        "WORLD":     40,
        "SPORTS":    20, "FOOTBALL": 20,
        "LIFESTYLE": 10, "TOURISM": 10, "WEATHER": 10,
    }
    add(f"Category: {cat}", cat_scores.get(cat, 30))

    # ── Breaking signal ───────────────────────────────────────────────────────
    if is_breaking(title, summary, cat):
        add("Breaking signal", 60)

    # ── Source reliability ────────────────────────────────────────────────────
    rel = source_reliability(source)
    if rel >= 90:
        add(f"High-trust source ({source})", 25)
    elif rel >= 80:
        add(f"Trusted source ({source})", 15)
    elif rel >= 70:
        add(f"Reliable source ({source})", 8)
    elif rel < 60:
        add(f"Low-trust source ({source})", -10)

    # ── Cluster size boost ────────────────────────────────────────────────────
    cluster_size = a.get("_cluster_size", 1)
    if cluster_size >= 4:
        add(f"Multi-source ({cluster_size} outlets)", 30)
    elif cluster_size >= 3:
        add(f"Multi-source ({cluster_size} outlets)", 20)
    elif cluster_size >= 2:
        add(f"Multi-source ({cluster_size} outlets)", 10)

    # ── High-impact keywords ──────────────────────────────────────────────────
    high_impact = [
        ("death", ["killed", "dead", "murder", "stabbed", "shot"]),
        ("disaster", ["tsunami", "earthquake", "flood", "disaster", "explosion"]),
        ("rescue", ["missing", "search and rescue", "capsized", "swept away"]),
        ("political", ["parliament", "president", "minister", "majlis", "cabinet",
                       "resign", "arrested", "impeach"]),
        ("economy", ["imf", "world bank", "budget", "deficit", "sovereign", "gdp"]),
    ]
    for label, kws in high_impact:
        if any(kw in text for kw in kws):
            add(f"High-impact: {label}", 20)

    # ── Maldives relevance boost ──────────────────────────────────────────────
    mv_terms = ["maldives", "male", "malé", "dhivehi", "maldivian", "raajje",
                "atoll", "hulhumale", "addu", "mndf", "mps"]
    if any(t in text for t in mv_terms):
        add("Maldives relevance", 15)

    # ── Dhivehi language boost (native sourcing) ──────────────────────────────
    if lang == "dv":
        add("Native Dhivehi source", 10)

    # ── Trend boost ───────────────────────────────────────────────────────────
    trend_theme = a.get("_trend_theme")
    if trend_theme:
        add(f"Trending topic: {trend_theme}", 15)

    # ── Story update boost ────────────────────────────────────────────────────
    update_num = a.get("_story_update_num", 1)
    if update_num >= 3:
        add(f"Developing story (update #{update_num})", 20)
    elif update_num == 2:
        add("Story update #2", 10)

    # ── Low-value penalties ───────────────────────────────────────────────────
    low_value = [
        "sponsored", "advertisement", "promo", "promotion", "discount",
        "sale", "offer", "deal", "buy now", "shop now",
    ]
    if any(lv in text for lv in low_value):
        add("Low-value/promo content", -40)

    if _looks_like_ad(text):
        add("Ad/spam detected", -60)

    total = sum(pts for _, pts in items)
    return total, items


def score_article(a):
    """Return the total priority score for an article."""
    total, _ = score_breakdown(a)
    return max(0, total)


# ═══════════════════════════════════════════════════════════════════════════════
# Confidence scoring — how reliable is this story?
# ═══════════════════════════════════════════════════════════════════════════════

def confidence_score(a):
    """
    Return (confidence_pct: int, reasons: [(label, pts)]).
    Confidence is separate from priority:
      - Priority = how important/interesting is this?
      - Confidence = how sure are we it's accurate?
    """
    title   = a.get("title", "")
    summary = a.get("summary", "") or ""
    source  = a.get("source", "")
    text    = (title + " " + summary).lower()
    reasons = []

    def add(label, pts):
        reasons.append((label, pts))

    score = 50  # base confidence

    # Source trust
    rel = source_reliability(source)
    if rel >= 90:
        add("High-trust source", +25)
        score += 25
    elif rel >= 80:
        add("Trusted source", +15)
        score += 15
    elif rel >= 70:
        add("Reliable source", +8)
        score += 8
    elif rel < 60:
        add("Low-trust source", -15)
        score -= 15

    # Multi-source confirmation
    cluster_size = a.get("_cluster_size", 1)
    if cluster_size >= 3:
        add(f"Confirmed by {cluster_size} sources", +20)
        score += 20
    elif cluster_size == 2:
        add("Confirmed by 2 sources", +10)
        score += 10

    # Unverified/alleged language
    unverified = ["alleged", "reportedly", "rumour", "rumor", "unconfirmed",
                  "claims", "claim", "sources say", "according to sources"]
    if any(u in text for u in unverified):
        add("Unverified/alleged language", -20)
        score -= 20

    # Very short content (headline only)
    if len(summary.strip()) < 30:
        add("Headline-only (no body)", -10)
        score -= 10

    # Google News aggregator (often indirect)
    if "google news" in source.lower():
        add("Aggregator source (Google News)", -10)
        score -= 10

    # Has body content
    if len(summary.strip()) > 100:
        add("Good body content", +5)
        score += 5

    # Story thread confidence (multiple updates = more credible)
    update_num = a.get("_story_update_num", 1)
    if update_num >= 3:
        add(f"Developing story ({update_num} updates)", +10)
        score += 10

    return max(0, min(100, score)), reasons


# ═══════════════════════════════════════════════════════════════════════════════
# Hold gate — should this be held for team review?
# ═══════════════════════════════════════════════════════════════════════════════

def should_hold_for_review(priority, confidence, is_breaking_flag):
    """
    Returns (should_hold: bool, reason: str).
    High-priority but low-confidence breaking news gets held for team review
    instead of auto-posting.
    """
    if not is_breaking_flag:
        return False, ""

    if priority >= 200 and confidence < 50:
        return True, f"Very high priority ({priority}) but low confidence ({confidence}%) — needs verification"

    if priority >= 150 and confidence < 40:
        return True, f"High priority ({priority}) but very low confidence ({confidence}%) — unverified"

    if confidence < 30:
        return True, f"Confidence too low ({confidence}%) regardless of priority"

    return False, ""


# ═══════════════════════════════════════════════════════════════════════════════
# Score breakdown formatter — pretty Telegram HTML card
# ═══════════════════════════════════════════════════════════════════════════════

def format_score_breakdown(a):
    """Pretty HTML block for Telegram showing itemized score + confidence."""
    total, items = score_breakdown(a)
    lines = [
        f"🧮 <b>Why this scored {total}</b>",
        f"<i>{a['title'][:90]}</i>",
        "",
        "<b>PRIORITY — how important:</b>",
    ]
    for label, pts in items:
        sign = "➕" if pts > 0 else "➖"
        lines.append(f"  {sign} {label}: <b>{pts:+d}</b>")
    if not items:
        lines.append("  <i>(no scoring signals matched)</i>")

    # Confidence breakdown
    try:
        conf, conf_reasons = confidence_score(a)
        lines.append("")
        lines.append(f"<b>CONFIDENCE — how sure: {conf}%</b>")
        for label, pts in conf_reasons:
            sign = "➕" if pts > 0 else "➖"
            lines.append(f"  {sign} {label}: <b>{pts:+d}</b>")

        breaking_flag = is_breaking(a["title"], a.get("summary", ""), a["cat"])
        hold, hold_reason = should_hold_for_review(total, conf, breaking_flag)
        lines.append("")
        if hold:
            lines.append(f"🛑 <b>HOLD:</b> {hold_reason}")
        elif conf >= 75:
            lines.append("✅ <b>High confidence</b> — safe to post.")
        elif conf >= 55:
            lines.append("🟡 <b>Moderate confidence</b> — fine to post.")
        else:
            lines.append("⚠️ <b>Low confidence</b> — consider verifying.")

        try:
            if is_breaking(a["title"], a.get("summary", ""), a["cat"]):
                lines.append("📌 <b>Breaking</b> → posts immediately (if confidence OK).")
            elif a.get("lang") == "dv":
                lines.append("📌 <b>Dhivehi</b> → always queued for Content Lab review.")
            else:
                lines.append("📌 <b>Regular English</b> → queued; auto-posts in 15 min if not reviewed.")
        except Exception:
            pass
    except Exception as e:
        lines.append(f"<i>(confidence error: {e})</i>")

    return "\n".join(lines)
