"""Flask website API: /api/stories /api/article /api/chat /api/public-interest."""
import os
import threading
from flask import Flask, jsonify, request
from config import log, SAMUGA_VERSION
from db import recent_articles, get_article, public_interest
from brain import public_samuga_ai_chat
from utils import strip_source_links, has_thaana, looks_latin_thaana

api_app = Flask(__name__)
api_app.json.ensure_ascii = False


def _story_row(row):
    aid, title, summary, cat, lang, source, score, is_breaking, dt, excerpt, body = row
    # Hide broken Latin Thaana from Dhivehi side.
    if lang == "dv" and not has_thaana((title or "") + (summary or "")):
        lang = "en"
    return {
        "id": aid,
        "title": strip_source_links(title),
        "summary": strip_source_links(excerpt or summary or title),
        "category": cat or "LOCAL",
        "lang": lang or "en",
        "source": "Samuga Media",
        "score": score or 0,
        "is_breaking": bool(is_breaking),
        "time": dt.strftime("%d %b %Y • %H:%M") if dt else "Recent",
        "url": f"article.html?id={aid}",
    }


@api_app.route("/api/health")
def health():
    return jsonify({"status": "online", "name": "Samuga News Bot API", "version": SAMUGA_VERSION, "endpoints": ["/api/stories", "/api/article", "/api/chat", "/api/public-interest"]})


@api_app.route("/api/stories")
def stories():
    lang = request.args.get("lang")
    limit = min(int(request.args.get("limit", 50)), 100)
    rows = recent_articles(limit=limit, lang=lang if lang in ("en", "dv") else None)
    data = [_story_row(r) for r in rows]
    return jsonify({"stories": data, "count": len(data)})


@api_app.route("/api/article")
def article():
    aid = request.args.get("id", "")
    row = get_article(aid)
    if not row:
        return jsonify({"error": "not found"}), 404
    aid, title, summary, cat, lang, source, score, is_breaking, dt, excerpt, body, slug = row
    return jsonify({
        "id": aid, "title": strip_source_links(title), "summary": strip_source_links(summary),
        "category": cat, "lang": lang, "source": "Samuga Media", "score": score or 0,
        "is_breaking": bool(is_breaking), "time": dt.strftime("%d %b %Y • %H:%M") if dt else "Recent",
        "excerpt": strip_source_links(excerpt or summary), "body": strip_source_links(body or summary), "slug": slug,
    })


@api_app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}
    message = data.get("message", "")
    session_id = data.get("session_id") or request.headers.get("X-Session-ID") or request.remote_addr or "website"
    lang = data.get("lang", "en")
    if not message.strip():
        return jsonify({"reply": "Ask me about Maldives news or global updates."})
    res = public_samuga_ai_chat(message, platform="website", session_id=session_id, user_key=session_id, lang=lang)
    return jsonify(res)


@api_app.route("/api/public-interest")
def interest():
    days = int(request.args.get("days", 1))
    rows = public_interest(days=days, limit=50)
    return jsonify({"items": [{"topic": r[0], "platform": r[1], "count": r[2]} for r in rows]})


def start_api():
    port = int(os.environ.get("PORT", 8080))
    log.info(f"🌐 Website API starting on port {port}")
    t = threading.Thread(target=lambda: api_app.run(host="0.0.0.0", port=port, use_reloader=False), daemon=True)
    t.start()
