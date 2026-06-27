"""APScheduler jobs and newsroom processing pipeline."""
import base64
from datetime import datetime
from apscheduler.schedulers.blocking import BlockingScheduler
from config import (
    log, BREAKING_SCAN_MINUTES, NORMAL_SCAN_MINUTES, CORE_TEAM_CHAT_ID, CONTENT_LAB_THREAD_ID,
    ALERT_THREAD_ID, DHIVEHI_EXPIRY_SECONDS, MEDIUM_CONF_AUTOPOST_SECONDS, LOW_CONF_REVIEW_SECONDS,
    is_day_mode, mvt_now
)
from fetchers import fetch_all_sources
from scoring import score_article, is_duplicate, should_publish_to_website, decision_lane, article_age_minutes
from brain import rewrite_article, generate_website_article, normalize_article_language
from cards import generate_card
from db import record_article, publish_article_for_website, mark_status
from publishing import send_text, send_approval_card, public_post_now, queue_for_social
from state import is_seen, mark_seen, store_pending_approval, approval_queue, pop_pending, save_state
from weather import send_weather_update, check_mms_alerts

scheduler = BlockingScheduler(timezone="UTC")


def _caption(article):
    cap = article.meta.get("caption") or article.summary or article.title
    if article.lang == "dv":
        return f"🇲🇻 <b>{article.title}</b>\n\n{cap}\n\n📡 <b>ސަމުގާ މީޑިއާ</b> | @samugacommunity"
    return f"<b>{article.title}</b>\n\n{cap}\n\n📡 <b>Samuga Media</b> | @samugacommunity"


def _store_preview(article, card_buf, lane, alert=False):
    card_b64 = base64.b64encode(card_buf.getvalue()).decode("ascii")
    item = {
        "article_id": article.id,
        "title": article.title,
        "summary": article.summary,
        "caption": _caption(article),
        "cat": article.cat,
        "lang": article.lang,
        "score": article.score,
        "confidence": article.confidence,
        "is_breaking": article.is_breaking,
        "lane": lane,
        "card_b64": card_b64,
        "created_at": datetime.utcnow().isoformat(),
        "alert": alert,
    }
    key = store_pending_approval(item)
    send_approval_card(key, item, alert=alert)
    return key


def process_article(article, breaking_only=False):
    if is_seen(article.id):
        return "seen"
    article = normalize_article_language(article)
    article = score_article(article)
    low_reason = article.meta.get("skip_reason")
    if low_reason:
        record_article(article, status="rejected")
        mark_seen(article.id)
        log.info(f"Selection skip: {low_reason} — {article.title[:70]}")
        return "skip"
    if is_duplicate(article):
        mark_seen(article.id)
        record_article(article, status="duplicate")
        log.info(f"Selection skip: duplicate — {article.title[:70]}")
        return "duplicate"

    rewrite_article(article)
    article = score_article(article)  # rescore after rewrite/lang cleanup
    record_article(article, status="seen")

    # Website publishes 24/7 for selected English + clean stories.
    ok_web, reason = should_publish_to_website(article)
    if ok_web and article.lang == "en":
        body = generate_website_article(article)
        publish_article_for_website(article, body=body)
        log.info(f"🌐 Website published: {article.title[:80]}")
    elif ok_web and article.lang == "dv":
        # Dhivehi website only if proper Thaana; no social autopost.
        publish_article_for_website(article, body=article.summary)
        log.info(f"🌐 Dhivehi website published: {article.title[:80]}")

    lane = decision_lane(article)
    card = generate_card(article.title, cat=article.cat)
    cap = _caption(article)

    if lane == "instant_breaking":
        public_post_now(article, card, cap, key_label="Breaking")
        mark_seen(article.id)
        return "instant_breaking"
    if lane == "alert_low":
        _store_preview(article, card, lane, alert=True)
        mark_seen(article.id)
        return "alert"
    if lane == "content_lab_dhivehi":
        _store_preview(article, card, lane, alert=False)
        mark_seen(article.id)
        return "content_lab_dv"
    if lane in ("content_lab_high", "content_lab_medium") and is_day_mode():
        key = _store_preview(article, card, lane, alert=False)
        # High confidence English goes to queue immediately after preview; medium waits 5 min by expire_old_approvals.
        if lane == "content_lab_high":
            queue_for_social(card, cap, article=article, key_label=f"{key.upper()} high confidence", post_telegram=True, priority=False)
            mark_status(article.id, "queued")
        mark_seen(article.id)
        return lane

    # Night mode protects socials/content lab, website already handled.
    mark_seen(article.id)
    return "website_only_or_skip"


def run_job(breaking_only=False):
    log.info(f"🔎 News scan started | breaking_only={breaking_only} | MVT={mvt_now().strftime('%H:%M')}")
    articles = fetch_all_sources(include_world=not breaking_only, breaking_only=breaking_only)
    counts = {}
    for a in articles:
        result = process_article(a, breaking_only=breaking_only)
        counts[result] = counts.get(result, 0) + 1
    save_state()
    log.info(f"✅ News scan done: {counts}")


def scheduled_check():
    # Website updates 24/7; content lab/social actions are gated inside process_article.
    run_job(breaking_only=False)


def breaking_news_check():
    run_job(breaking_only=True)


def expire_old_approvals():
    from datetime import datetime
    now = datetime.utcnow()
    due = []
    for key, item in list(approval_queue.items()):
        try:
            created = datetime.fromisoformat(item.get("created_at"))
        except Exception:
            created = now
        age = (now - created).total_seconds()
        if item.get("lang") == "dv" and age > DHIVEHI_EXPIRY_SECONDS:
            due.append((key, "expire_dv"))
        elif item.get("lang") == "en" and item.get("lane") == "content_lab_medium" and age > MEDIUM_CONF_AUTOPOST_SECONDS:
            due.append((key, "auto_post"))
        elif item.get("lang") == "en" and item.get("alert") and age > LOW_CONF_REVIEW_SECONDS:
            due.append((key, "expire_alert"))
    for key, action in due:
        item = pop_pending(key)
        if not item: continue
        if action == "auto_post":
            import io, base64
            from models import Article
            art = Article.from_dict({"id": item["article_id"], "title": item["title"], "summary": item.get("summary",""), "cat": item.get("cat","LOCAL"), "lang": "en", "score": item.get("score",0), "confidence": item.get("confidence","medium")})
            buf = io.BytesIO(base64.b64decode(item["card_b64"])); buf.seek(0)
            queue_for_social(buf, item["caption"], article=art, key_label=f"{key.upper()} auto", post_telegram=True)
            send_text(CORE_TEAM_CHAT_ID, f"⏰ <b>{key.upper()}</b> auto-queued after review window\n📰 {item['title'][:100]}", thread_id=ALERT_THREAD_ID)
        elif action == "expire_dv":
            send_text(CORE_TEAM_CHAT_ID, f"⏰ <b>{key.upper()}</b> Dhivehi card expired after 2h\n📰 {item['title'][:100]}", thread_id=CONTENT_LAB_THREAD_ID)
        else:
            send_text(CORE_TEAM_CHAT_ID, f"⏰ <b>{key.upper()}</b> low-confidence alert expired\n📰 {item['title'][:100]}", thread_id=ALERT_THREAD_ID)


def start_scheduler():
    scheduler.add_job(scheduled_check, "interval", minutes=NORMAL_SCAN_MINUTES, next_run_time=datetime.utcnow())
    scheduler.add_job(breaking_news_check, "interval", minutes=BREAKING_SCAN_MINUTES)
    scheduler.add_job(expire_old_approvals, "interval", minutes=5)
    scheduler.add_job(lambda: send_weather_update("morning"), "cron", hour=3, minute=0)  # 8AM MVT
    scheduler.add_job(check_mms_alerts, "interval", minutes=30)
    scheduler.add_job(save_state, "interval", minutes=5)
    log.info("⏰ Scheduler started!")
    scheduler.start()
