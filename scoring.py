"""Freshness, confidence, duplicate and low-value filters."""
import re
from datetime import timedelta
from config import (
    utcnow, canonical_category, BREAKING_KEYWORDS, BREAKING_BLACKLIST, LOW_VALUE_KEYWORDS,
    LOCAL_ENTITY_KEYWORDS, DAILY_PUBLIC_POST_MAX
)
from utils import match_key, looks_latin_thaana

_recent_match_keys = []  # in-process duplicate memory


def article_age_minutes(article) -> float:
    dt = article.published_at or article.fetched_at or utcnow()
    try:
        return max(0, (utcnow() - dt).total_seconds() / 60)
    except Exception:
        return 9999


def freshness_score(article):
    age = article_age_minutes(article)
    if age <= 10: return 80
    if age <= 30: return 55
    if age <= 90: return 25
    if age <= 240: return -15
    if age <= 720: return -55
    return -150


def is_breaking_story(article):
    text = f"{article.title} {article.summary}".lower()
    if any(x in text for x in BREAKING_BLACKLIST):
        return False
    return any(k.lower() in text for k in BREAKING_KEYWORDS)


def has_maldives_angle(article):
    if article.cat == "WORLD":
        return False
    text = f"{article.title} {article.summary} {article.source}".lower()
    return any(k.lower() in text for k in LOCAL_ENTITY_KEYWORDS) or article.lang == "dv"


def is_low_value(article):
    text = f"{article.title} {article.summary}".lower()
    if len(article.title.split()) < 3:
        return True, "headline too short"
    if any(k in text for k in LOW_VALUE_KEYWORDS):
        return True, "promo/ad/low-value"
    age = article_age_minutes(article)
    if age > 720:
        return True, "12h+ old — background only"
    if age > 240 and not is_breaking_story(article) and not has_maldives_angle(article):
        return True, "4h+ old and not important/local"
    if looks_latin_thaana(article.title + " " + article.summary):
        # not low-value by itself; language normalizer can fix. Mark only.
        article.meta["latin_thaana"] = True
    if article.cat == "WORLD" and not any(w in text for w in ["maldives", "south asia", "india", "sri lanka", "iran", "us", "israel", "gaza", "oil", "dollar", "tsunami", "earthquake"]):
        return True, "world story no useful angle"
    return False, ""


def is_duplicate(article):
    mk = match_key(article.title)
    if not mk:
        return False
    for old in _recent_match_keys[-300:]:
        if mk == old or (len(mk) > 30 and (mk in old or old in mk)):
            return True
    _recent_match_keys.append(mk)
    if len(_recent_match_keys) > 500:
        del _recent_match_keys[:200]
    return False


def score_article(article):
    article.cat = canonical_category(article.cat, article.title, article.summary)
    score = int(article.reliability or 0)
    score += freshness_score(article)
    text = f"{article.title} {article.summary}".lower()

    if is_breaking_story(article):
        article.is_breaking = True
        article.cat = "BREAKING"
        score += 90
    if has_maldives_angle(article):
        score += 35
    if article.source.lower() in ["police", "mndf", "presidency", "psm news"]:
        score += 25
    if any(k in text for k in ["housing", "flat", "dollar", "usd", "cost of living", "debt", "aasandha", "health", "education", "corruption", "court", "parliament"]):
        score += 25
    if article.cat == "WORLD":
        score -= 15

    low, reason = is_low_value(article)
    if low:
        article.score = score - 100
        article.confidence = "low"
        article.meta["skip_reason"] = reason
        return article

    article.score = score
    if article.is_breaking and score >= 170:
        article.confidence = "high"
    elif score >= 165:
        article.confidence = "high"
    elif score >= 120:
        article.confidence = "medium"
    else:
        article.confidence = "low"
    return article


def should_publish_to_website(article):
    # Website can update 24/7, but still strict.
    if article.meta.get("skip_reason"):
        return False, article.meta["skip_reason"]
    if article.score >= 115 or article.is_breaking:
        return True, "selected"
    return False, "score below website threshold"


def decision_lane(article):
    """Return lane: instant_breaking, content_lab_high, content_lab_medium, alert_low, skip."""
    if article.meta.get("skip_reason"):
        return "skip"
    if article.is_breaking and article.confidence == "high":
        return "instant_breaking"
    if article.is_breaking and article.confidence in ("medium", "low"):
        return "alert_low"
    if article.lang == "dv":
        return "content_lab_dhivehi"
    if article.confidence == "high":
        return "content_lab_high"
    if article.confidence == "medium":
        return "content_lab_medium"
    return "skip"
