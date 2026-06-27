"""Claude/Gemini/Tavily intelligence and one public Samuga AI brain."""
import json
import requests
import anthropic
from config import ANTHROPIC_API_KEY, GEMINI_API_KEY, TAVILY_API_KEY, log, SAMUGA_PUBLIC_SOURCE
from utils import strip_source_links, clean_text, has_thaana, looks_latin_thaana
from db import story_search, log_public_chat

_ai = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
_public_sessions = {}  # session_id -> [{'role':'user'|'assistant','content':'...'}]


def _claude(prompt, max_tokens=700, temperature=0.3):
    if not _ai:
        return ""
    try:
        msg = _ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        log.error(f"Claude failed: {e}")
        return ""


def tavily_search(query, max_results=5):
    if not TAVILY_API_KEY:
        return []
    try:
        r = requests.post("https://api.tavily.com/search", json={
            "api_key": TAVILY_API_KEY,
            "query": query,
            "search_depth": "basic",
            "max_results": max_results,
            "include_answer": True,
        }, timeout=25)
        if r.status_code != 200:
            return []
        data = r.json()
        return data.get("results", []) or []
    except Exception as e:
        log.warning(f"Tavily failed: {e}")
        return []


def gemini_text(prompt, max_tokens=700):
    if not GEMINI_API_KEY:
        return ""
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
        payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.25}}
        r = requests.post(url, json=payload, timeout=30)
        if r.status_code != 200:
            log.warning(f"Gemini HTTP {r.status_code}: {r.text[:200]}")
            return ""
        cand = r.json().get("candidates", [{}])[0]
        parts = cand.get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts).strip()
    except Exception as e:
        log.warning(f"Gemini failed: {e}")
        return ""


def normalize_article_language(article):
    """Fix Latin Thaana and rough language before public website/social use."""
    text = f"{article.title}\n{article.summary}"
    if article.lang == "dv" and not has_thaana(text) and looks_latin_thaana(text):
        fixed = gemini_text(f"""Rewrite this Latin Thaana news text into proper Dhivehi Thaana news style. Keep facts only. No markdown.\n\n{text}""", 900)
        if fixed and has_thaana(fixed):
            lines = [x.strip() for x in fixed.splitlines() if x.strip()]
            article.title = lines[0][:220] if lines else article.title
            article.summary = "\n".join(lines[1:])[:2500] if len(lines) > 1 else fixed[:2500]
            article.lang = "dv"
            article.meta["gemini_language_fix"] = True
        else:
            # Fallback: convert to English for website instead of broken Latin Dhivehi.
            eng = gemini_text(f"""Translate/rewrite this Maldives news text into clean English. Keep facts only. No markdown.\n\n{text}""", 700)
            if eng:
                lines = [x.strip() for x in eng.splitlines() if x.strip()]
                article.title = lines[0][:220] if lines else article.title
                article.summary = " ".join(lines[1:])[:2500] if len(lines) > 1 else eng[:2500]
                article.lang = "en"
                article.meta["gemini_language_fix"] = "english_fallback"
            else:
                article.meta["skip_reason"] = "Latin Thaana cleanup failed"
    return article


def rewrite_article(article):
    """Return Samuga-style title, summary/caption. No external links."""
    normalize_article_language(article)
    if article.meta.get("skip_reason"):
        return article.title, article.summary
    lang_rule = "Write in Dhivehi Thaana." if article.lang == "dv" else "Write in clean English."
    prompt = f"""You are Samuga Media's newsroom editor.
Rewrite this news into Samuga style for a public post and website card.
Rules:
- {lang_rule}
- Be fast, clear, factual and people-first.
- Do not mention source websites or external links.
- Do not invent facts, numbers, quotes, names or allegations.
- If details are limited, say it carefully.
- Return JSON only with keys: title, summary, caption.

Source: {article.source}
Category: {article.cat}
Breaking: {article.is_breaking}
Headline: {article.title}
Details: {article.summary}
"""
    out = _claude(prompt, max_tokens=900, temperature=0.2)
    try:
        data = json.loads(out[out.find("{"):out.rfind("}")+1])
        article.title = strip_source_links(data.get("title") or article.title)[:220]
        article.summary = strip_source_links(data.get("summary") or article.summary)[:2500]
        article.meta["caption"] = strip_source_links(data.get("caption") or article.summary)[:3500]
    except Exception:
        # fallback to cleaned source copy
        article.title = strip_source_links(article.title)[:220]
        article.summary = strip_source_links(article.summary or article.title)[:2500]
        article.meta["caption"] = article.summary
    return article.title, article.summary


def generate_website_article(article):
    if article.lang == "dv":
        return strip_source_links(article.summary or article.title)
    prompt = f"""Write a short website news article for Samuga Media.
Rules:
- English.
- 4 to 6 short paragraphs.
- First paragraph: what happened.
- Middle: context and why it matters, especially to Maldives when relevant.
- Final: what Samuga Media will watch next.
- No markdown. No external links. Do not invent facts.

Headline: {article.title}
Known details: {article.summary}
Category: {article.cat}
Breaking: {article.is_breaking}
"""
    body = _claude(prompt, max_tokens=950, temperature=0.25)
    body = strip_source_links(body)
    if len(body.split()) < 35:
        body = f"{article.summary}\n\nSamuga Media will continue to follow this story and update readers as more confirmed information becomes available."
    return body[:4200]


def detect_public_intent_topics(message):
    t = (message or "").lower()
    topics = []
    mapping = {
        "politics": ["politic", "president", "minister", "majlis", "election"],
        "economy": ["dollar", "usd", "price", "economy", "debt", "cost"],
        "housing": ["flat", "housing", "rent", "hiya", "apartment"],
        "visa": ["visa", "passport", "immigration"],
        "weather": ["weather", "rain", "storm", "swell"],
        "world": ["iran", "us", "israel", "gaza", "ukraine", "world", "global"],
        "tourism": ["tourism", "resort", "tourist"],
        "crime": ["police", "crime", "arrest", "murder", "stab"],
    }
    for topic, kws in mapping.items():
        if any(k in t for k in kws): topics.append(topic)
    intent = "latest_news" if any(k in t for k in ["latest", "today", "news", "happening", "breaking"]) else "chat"
    if "world" in topics: intent = "world_question"
    return intent, topics or ["general"]


def public_samuga_ai_chat(message, platform="website", session_id="default", user_key="anon", lang="en"):
    message = clean_text(message, 1200)
    intent, topics = detect_public_intent_topics(message)
    history = _public_sessions.setdefault(session_id, [])[-8:]
    local_rows = story_search(message, limit=5) if intent != "chat" else []
    used_search = False
    web_context = ""
    if intent in ("world_question", "latest_news") and ("world" in topics or not local_rows):
        results = tavily_search(message, max_results=4)
        used_search = bool(results)
        web_context = "\n".join(f"- {r.get('title')}: {r.get('content','')[:350]}" for r in results)
    local_context = "\n".join(f"- {r[0]}: {r[1][:280]}" for r in local_rows)
    hist_text = "\n".join(f"{h['role']}: {h['content']}" for h in history)
    prompt = f"""You are Samuga AI, the public assistant for Samuga Media.
Be helpful, natural, short and factual. You can discuss Maldives news and global news when asked.
Do not expose admin/core team commands. Do not mention source URLs.
If news is uncertain, say it is developing.

Conversation history:
{hist_text}

Samuga local context:
{local_context or 'No matching local story in database.'}

Live search context:
{web_context or 'No live search context.'}

User: {message}
"""
    reply = _claude(prompt, max_tokens=700, temperature=0.45) or "I’m having trouble checking that right now. Please try again in a moment."
    reply = strip_source_links(reply).replace("**", "")
    history.extend([{"role": "user", "content": message}, {"role": "assistant", "content": reply}])
    _public_sessions[session_id] = history[-10:]
    log_public_chat(platform, session_id, user_key, message, reply, lang, intent, topics, used_search)
    return {"reply": reply, "intent": intent, "topics": topics, "used_search": used_search}
