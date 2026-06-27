"""News fetchers: RSS, latest-page scraping, Telegram public channels, Tavily/world search."""
import re
import time
import html
import requests
import feedparser
from bs4 import BeautifulSoup
from typing import List
from config import (
    log, RSS_FEEDS, LATEST_PAGE_SOURCES, TELEGRAM_SOURCE_CHANNELS, WORLD_SEARCH_QUERIES,
    USER_AGENT, REQUEST_TIMEOUT, TAVILY_API_KEY, canonical_category, utcnow
)
from models import Article
from utils import clean_text, parse_dt, absolute_url, has_thaana
from db import record_source_health

HEADERS = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}


def _article(title, summary="", link="", source="", cat="LOCAL", lang="en", published_at=None, priority=70, method=""):
    title = clean_text(html.unescape(title or ""), 500)
    summary = clean_text(html.unescape(summary or ""), 2500)
    if not title or len(title) < 8:
        return None
    art = Article.from_dict({
        "title": title,
        "summary": summary,
        "link": link,
        "source": source,
        "cat": canonical_category(cat, title, summary),
        "lang": lang,
        "published_at": published_at,
        "fetched_at": utcnow(),
        "reliability": priority,
        "meta": {"method": method, "source_priority": priority},
    })
    return art


def fetch_rss_feeds(limit_per_feed=6) -> List[Article]:
    articles = []
    for feed in RSS_FEEDS:
        src = feed.get("source") or feed.get("url")
        try:
            parsed = feedparser.parse(feed["url"], request_headers={"User-Agent": USER_AGENT})
            entries = parsed.entries[:limit_per_feed]
            ok = bool(entries) and not getattr(parsed, "bozo", False)
            record_source_health(src, "rss", ok, len(entries), "ok" if ok else str(getattr(parsed, "bozo_exception", "empty/blocked"))[:250])
            if not entries:
                continue
            for e in entries:
                published = parse_dt(e.get("published") or e.get("updated"))
                art = _article(
                    e.get("title", ""),
                    e.get("summary", "") or e.get("description", ""),
                    e.get("link", ""), src, feed.get("cat", "LOCAL"), feed.get("lang", "en"),
                    published, feed.get("priority", 70), "rss"
                )
                if art:
                    articles.append(art)
        except Exception as e:
            record_source_health(src, "rss", False, 0, str(e)[:250])
            log.warning(f"[FETCH] RSS failed {src}: {e}")
    return articles


def _extract_time_from_text(text: str):
    # Basic extraction only; many local sites use relative times. If unknown, fetched_at is used.
    m = re.search(r"(\d{1,2}\s+[A-Za-z]{3,9}\s+20\d{2}\s*[•,]?\s*\d{1,2}:\d{2})", text or "")
    if m:
        return parse_dt(m.group(1).replace("•", ""))
    return None


def fetch_latest_pages(limit_per_source=5) -> List[Article]:
    articles = []
    for src in LATEST_PAGE_SOURCES:
        name = src["name"]
        url = src["url"]
        try:
            r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if r.status_code >= 400:
                record_source_health(name, "latest-page", False, 0, f"HTTP {r.status_code}")
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            candidates = []
            for a in soup.find_all("a", href=True):
                text = clean_text(a.get_text(" "), 260)
                href = absolute_url(url, a.get("href"))
                if len(text.split()) < 3:
                    continue
                if any(x in href.lower() for x in ["/tag/", "/category/", "javascript:", "mailto:", "#"]):
                    continue
                # prefer article-looking URLs, but allow official site paths too
                articleish = any(x in href.lower() for x in ["/news/", "/article/", "/story/", "/en/", "/dv/", "mihaaru.com/", "avas.mv/", "sun.mv/", "raajje.mv/", "police.gov.mv", "mndf.gov.mv"])
                if not articleish:
                    continue
                key = (text.lower(), href.split("?")[0])
                if key not in [(c[0].lower(), c[1].split("?")[0]) for c in candidates]:
                    candidates.append((text, href, _extract_time_from_text(a.parent.get_text(" ") if a.parent else text)))
                if len(candidates) >= limit_per_source:
                    break
            count = len(candidates)
            record_source_health(name, "latest-page", count > 0, count, "ok" if count else "no headlines found")
            log.info(f"[FETCH] {name} latest page: {count} headline(s)")
            for title, link, published in candidates:
                lang = src.get("lang", "dv")
                if has_thaana(title):
                    lang = "dv"
                art = _article(title, "", link, name, src.get("cat", "LOCAL"), lang, published, src.get("priority", 80), "latest-page")
                if art:
                    articles.append(art)
        except Exception as e:
            record_source_health(name, "latest-page", False, 0, str(e)[:250])
            log.warning(f"[FETCH] latest-page failed {name}: {e}")
    return articles


def fetch_telegram_sources(limit_per_channel=8) -> List[Article]:
    articles = []
    for ch in TELEGRAM_SOURCE_CHANNELS:
        handle = ch["handle"].lstrip("@")
        source = ch.get("source", handle)
        url = f"https://t.me/s/{handle}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if r.status_code >= 400:
                record_source_health(source, "telegram", False, 0, f"HTTP {r.status_code}")
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            posts = soup.select(".tgme_widget_message")[-limit_per_channel:]
            kept = 0
            for p in posts:
                body = p.select_one(".tgme_widget_message_text")
                if not body:
                    continue
                text = clean_text(body.get_text(" "), 2500)
                if len(text) < 20:
                    continue
                time_tag = p.select_one("time")
                published = parse_dt(time_tag.get("datetime")) if time_tag else None
                lines = [x.strip() for x in re.split(r"[\n。.!?]+", text) if x.strip()]
                title = lines[0][:220] if lines else text[:220]
                link_tag = p.select_one("a.tgme_widget_message_date")
                link = absolute_url(url, link_tag.get("href")) if link_tag else url
                lang = "dv" if has_thaana(text) else ch.get("lang", "dv")
                art = _article(title, text, link, source, "LOCAL", lang, published, ch.get("priority", 80), "telegram")
                if art:
                    articles.append(art); kept += 1
            record_source_health(source, "telegram", kept > 0, kept, "ok" if kept else "no usable posts")
            log.info(f"[FETCH] Telegram {source}: {kept} item(s)")
        except Exception as e:
            record_source_health(source, "telegram", False, 0, str(e)[:250])
            log.warning(f"[FETCH] Telegram failed {source}: {e}")
    return articles


def tavily_search(query: str, max_results=5):
    if not TAVILY_API_KEY:
        return []
    try:
        r = requests.post("https://api.tavily.com/search", json={
            "api_key": TAVILY_API_KEY,
            "query": query,
            "search_depth": "basic",
            "max_results": max_results,
            "include_answer": False,
        }, timeout=25)
        if r.status_code != 200:
            return []
        return r.json().get("results", []) or []
    except Exception as e:
        log.warning(f"Tavily search failed: {e}")
        return []


def fetch_world_updates() -> List[Article]:
    articles = []
    for q in WORLD_SEARCH_QUERIES:
        for res in tavily_search(q, max_results=4):
            title = res.get("title") or ""
            summary = res.get("content") or ""
            link = res.get("url") or ""
            art = _article(title, summary, link, res.get("source") or "World", "WORLD", "en", None, 65, "tavily-world")
            if art:
                articles.append(art)
        time.sleep(0.2)
    record_source_health("World/Tavily", "search", bool(articles), len(articles), "ok" if articles else "no api/no results")
    return articles


def fetch_all_sources(include_world=True, breaking_only=False) -> List[Article]:
    articles = []
    # Breaking scan prioritizes direct/latest/Telegram; normal scan includes RSS too.
    if not breaking_only:
        articles.extend(fetch_rss_feeds())
    articles.extend(fetch_latest_pages())
    articles.extend(fetch_telegram_sources())
    if include_world and not breaking_only:
        articles.extend(fetch_world_updates())
    # Deduplicate by title/link key
    out = []
    seen = set()
    for a in articles:
        key = (a.title.lower()[:100], (a.link or "").split("?")[0])
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
    log.info(f"[FETCH] total unique articles: {len(out)}")
    return out
