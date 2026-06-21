import os
import threading
import time
import logging
import hashlib
import json
import feedparser
import requests
import anthropic
from PIL import Image, ImageDraw, ImageFont, ImageEnhance
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from apscheduler.schedulers.blocking import BlockingScheduler
from io import BytesIO

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "@samugacommunity")
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
PEXELS_API_KEY      = os.environ.get("PEXELS_API_KEY", "")
GEMINI_API_KEY      = os.environ.get("GEMINI_API_KEY", "")
BOT_USERNAME        = os.environ.get("BOT_USERNAME", "SamugaNewsBot")

ai = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── News Sources by Category ──────────────────────────────────────────────────
RSS_FEEDS = [
    # 🇲🇻 Local Maldives — English
    {"url": "https://news.google.com/rss/search?q=maldives&hl=en-MV&gl=MV&ceid=MV:en", "cat": "LOCAL", "lang": "en"},
    {"url": "https://see.mv/feed", "cat": "LOCAL", "lang": "en"},
    {"url": "https://english.sun.mv/feed", "cat": "LOCAL", "lang": "en"},
    {"url": "https://edition.mv/feed", "cat": "LOCAL", "lang": "en"},
    {"url": "https://maldivesindependent.com/feed", "cat": "LOCAL", "lang": "en"},
    {"url": "https://oneonline.mv/en/feed", "cat": "LOCAL", "lang": "en"},
    {"url": "https://psmnews.mv/en/feed", "cat": "LOCAL", "lang": "en"},
    # 🇲🇻 Local Maldives — Dhivehi (Gemini translates)
    {"url": "https://mihaaru.com/rss", "cat": "LOCAL", "lang": "dv"},
    {"url": "https://avas.mv/feed", "cat": "LOCAL", "lang": "dv"},
    {"url": "https://dhuvas.mv/feed", "cat": "LOCAL", "lang": "dv"},
    # ⚽ Football
    {"url": "https://news.google.com/rss/search?q=world+cup+2026+football&hl=en&gl=US&ceid=US:en", "cat": "FOOTBALL", "lang": "en"},
    {"url": "https://news.google.com/rss/search?q=champions+league+football&hl=en&gl=US&ceid=US:en", "cat": "FOOTBALL", "lang": "en"},
    {"url": "https://news.google.com/rss/search?q=football+match+results&hl=en&gl=US&ceid=US:en", "cat": "FOOTBALL", "lang": "en"},
    # 🌍 World News
    {"url": "https://news.google.com/rss/search?q=war+conflict+breaking&hl=en&gl=US&ceid=US:en", "cat": "WORLD", "lang": "en"},
    {"url": "https://news.google.com/rss/search?q=breaking+news+world&hl=en&gl=US&ceid=US:en", "cat": "WORLD", "lang": "en"},
    # 🚨 MVCrisis — Maldives Breaking News (via Google News)
    {"url": "https://news.google.com/rss/search?q=maldives+breaking+site:twitter.com/mvcrisis+OR+mvcrisis&hl=en&gl=MV&ceid=MV:en", "cat": "DISASTER", "lang": "en"},
    {"url": "https://news.google.com/rss/search?q=maldives+accident+incident+breaking&hl=en&gl=MV&ceid=MV:en", "cat": "DISASTER", "lang": "en"},
    # 🌤️ Weather Maldives
    {"url": "https://news.google.com/rss/search?q=maldives+weather+storm&hl=en&gl=US&ceid=US:en", "cat": "WEATHER", "lang": "en"},
    # ✈️ Tourism
    {"url": "https://news.google.com/rss/search?q=maldives+tourism+travel&hl=en&gl=US&ceid=US:en", "cat": "TOURISM", "lang": "en"},
]

# Category display config
CAT_CONFIG = {
    "LOCAL":   {"label": "🇲🇻  LOCAL NEWS",    "color": (41, 171, 226)},
    "FOOTBALL":{"label": "⚽  FOOTBALL",        "color": (34, 180, 80)},
    "WORLD":   {"label": "🌍  WORLD NEWS",      "color": (220, 80, 60)},
    "DISASTER":{"label": "🚨  DISASTER ALERT",  "color": (220, 120, 0)},
    "WEATHER": {"label": "🌤️  WEATHER",         "color": (100, 180, 240)},
    "TOURISM": {"label": "✈️  TOURISM",         "color": (160, 80, 220)},
}

DATA_DIR  = "/data"
os.makedirs(DATA_DIR, exist_ok=True)
SEEN_FILE = os.path.join(DATA_DIR, "seen_articles.json")

def load_seen():
    try:
        if os.path.exists(SEEN_FILE):
            with open(SEEN_FILE) as f:
                return set(json.load(f))
    except Exception as e:
        log.warning(f"Load seen: {e}")
    return set()

def save_seen(seen):
    try:
        with open(SEEN_FILE, "w") as f:
            json.dump(list(seen)[-1000:], f)
    except Exception as e:
        log.warning(f"Save seen: {e}")

def is_fresh(entry, hours=24):
    try:
        pub = entry.get("published", "")
        if pub:
            pub_dt = parsedate_to_datetime(pub)
            if pub_dt.tzinfo:
                pub_dt = pub_dt.replace(tzinfo=None)
            return datetime.utcnow() - pub_dt < timedelta(hours=hours)
    except:
        pass
    return True

# ── Gemini translate Dhivehi → English ───────────────────────────────────────
def gemini_translate(text):
    if not GEMINI_API_KEY:
        return text
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
        payload = {
            "contents": [{
                "parts": [{"text": f"Translate this Dhivehi text to English. Return ONLY the English translation, nothing else:\n\n{text}"}]
            }]
        }
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code == 200:
            result = resp.json()
            translated = result["candidates"][0]["content"]["parts"][0]["text"].strip()
            log.info(f"✅ Gemini translated: {translated[:60]}")
            return translated
        else:
            log.error(f"Gemini error: {resp.status_code}")
            return text
    except Exception as e:
        log.error(f"Gemini exception: {e}")
        return text

# ── Fetch all news ────────────────────────────────────────────────────────────
def fetch_news():
    articles = []
    seen_titles = set()
    for feed_cfg in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_cfg["url"])
            for entry in feed.entries[:8]:
                title   = entry.get("title", "")
                summary = entry.get("summary", title)
                lang    = feed_cfg["lang"]

                # Translate Dhivehi
                if lang == "dv":
                    title   = gemini_translate(title)
                    summary = gemini_translate(summary[:300])

                key = title.lower()[:50]
                if key in seen_titles:
                    continue
                if not is_fresh(entry):
                    continue
                seen_titles.add(key)

                article_id = hashlib.md5(entry.get("link", title).encode()).hexdigest()
                articles.append({
                    "id":      article_id,
                    "title":   title,
                    "summary": summary,
                    "link":    entry.get("link", ""),
                    "source":  entry.get("source", {}).get("title", feed_cfg.get("cat", "News")),
                    "cat":     feed_cfg["cat"],
                })
        except Exception as e:
            log.error(f"Feed error {feed_cfg['url']}: {e}")

    log.info(f"Found {len(articles)} fresh articles")
    return articles

# ── Rewrite with Claude ───────────────────────────────────────────────────────
def rewrite_news(title, summary, cat):
    cat_context = {
        "LOCAL":    "local Maldivian news",
        "FOOTBALL": "football/soccer news",
        "WORLD":    "world/international news",
        "DISASTER": "natural disaster or emergency alert",
        "WEATHER":  "weather news",
        "TOURISM":  "tourism and travel news",
    }.get(cat, "news")

    default_keywords = {
        "LOCAL": "maldives government",
        "FOOTBALL": "football match stadium",
        "WORLD": "world news politics",
        "DISASTER": "emergency rescue",
        "WEATHER": "tropical weather storm",
        "TOURISM": "maldives resort beach",
    }

    if not summary or summary.strip() == title.strip() or len(summary) < 30:
        extra = "Note: Only a headline is available. Write a short punchy 2-3 sentence post expanding on the headline with relevant context."
    else:
        extra = ""

    prompt = f"""You are a news writer for Samuga Media, a Maldivian digital media outlet.
Rewrite this {cat_context} into a short, punchy, engaging English post for a Telegram channel.
- Max 3 sentences
- Clear and direct
- No hashtags, no emojis
- Professional but easy to read
{extra}

Also provide a 2-3 word Pexels image search keyword SPECIFIC to this topic.

Title: {title}
Summary: {summary}

Respond in EXACTLY this format:
TEXT: [rewritten news]
IMAGE: [specific 2-3 word keyword]"""

    try:
        msg = ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )
        response = msg.content[0].text.strip()
        text, keyword = "", default_keywords.get(cat, "maldives news")
        for line in response.split('\n'):
            if line.startswith("TEXT:"):
                text = line[5:].strip()
            elif line.startswith("IMAGE:"):
                keyword = line[6:].strip()
        return (text or title), keyword
    except Exception as e:
        log.error(f"Claude error: {e}")
        return title, default_keywords.get(cat, "maldives")

# ── Pexels image ──────────────────────────────────────────────────────────────
def fetch_background_image(keyword):
    if not PEXELS_API_KEY:
        return None
    try:
        headers = {"Authorization": PEXELS_API_KEY}
        url = f"https://api.pexels.com/v1/search?query={keyword}&per_page=5&orientation=square"
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            photos = resp.json().get("photos", [])
            if photos:
                img_resp = requests.get(photos[0]["src"]["large"], timeout=20)
                if img_resp.status_code == 200:
                    log.info(f"✅ Pexels image: {keyword}")
                    return Image.open(BytesIO(img_resp.content)).convert("RGB")
    except Exception as e:
        log.error(f"Pexels error: {e}")
    return None

# ── Generate card ─────────────────────────────────────────────────────────────
WHITE      = (255, 255, 255)
LIGHT_GRAY = (200, 215, 230)
BG_TOP     = (10, 40, 75)
BG_BOTTOM  = (5, 20, 45)

def generate_card(text, source, timestamp, cat, bg_image=None):
    W, H = 1080, 1080
    cat_color = CAT_CONFIG.get(cat, CAT_CONFIG["LOCAL"])["color"]
    cat_label = CAT_CONFIG.get(cat, CAT_CONFIG["LOCAL"])["label"]

    img = Image.new("RGB", (W, H), BG_TOP)

    if bg_image:
        bg = bg_image.copy()
        r = bg.width / bg.height
        if r > 1:
            nh, nw = H, int(H * r)
        else:
            nw, nh = W, int(W / r)
        bg = bg.resize((nw, nh), Image.LANCZOS)
        bg = bg.crop(((nw-W)//2, (nh-H)//2, (nw-W)//2+W, (nh-H)//2+H))
        bg = ImageEnhance.Brightness(bg).enhance(0.22)
        overlay = Image.new("RGB", (W, H), (8, 30, 65))
        img = Image.blend(bg, overlay, 0.55)
    else:
        d = ImageDraw.Draw(img)
        for y in range(H):
            t = y/H
            d.line([(0,y),(W,y)], fill=(
                int(BG_TOP[0]+(BG_BOTTOM[0]-BG_TOP[0])*t),
                int(BG_TOP[1]+(BG_BOTTOM[1]-BG_TOP[1])*t),
                int(BG_TOP[2]+(BG_BOTTOM[2]-BG_TOP[2])*t),
            ))

    # Bottom gradient
    ov = Image.new("RGBA", (W, H), (0,0,0,0))
    od = ImageDraw.Draw(ov)
    for y in range(H//2, H):
        t = (y-H//2)/(H//2)
        od.line([(0,y),(W,y)], fill=(5,20,50,int(215*t)))
    img = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")

    # Top gradient
    ov2 = Image.new("RGBA", (W, H), (0,0,0,0))
    od2 = ImageDraw.Draw(ov2)
    for y in range(0, 170):
        t = 1-y/170
        od2.line([(0,y),(W,y)], fill=(5,20,50,int(190*t)))
    img = Image.alpha_composite(img.convert("RGBA"), ov2).convert("RGB")

    draw = ImageDraw.Draw(img)

    # Top accent bar — category color
    draw.rectangle([(0,0),(W,6)], fill=cat_color)

    # Logo
    try:
        logo = Image.open("logo.png").convert("RGBA")
        lh = 72
        lw = int(logo.width * lh / logo.height)
        logo = logo.resize((lw, lh), Image.LANCZOS)
        img.paste(logo, (50, 38), logo)
    except Exception as e:
        log.warning(f"Logo: {e}")

    try:
        f_tag   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22)
        f_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 46)
        f_body  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 27)
        f_sm    = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 21)
    except:
        f_tag = f_title = f_body = f_sm = ImageFont.load_default()

    draw.text((W-310, 50), "t.me/samugacommunity", font=f_sm, fill=(200,230,255))

    # Category tag — dynamic color
    tag_y = 590
    tag_w = draw.textbbox((0,0), cat_label, font=f_tag)[2] + 26
    draw.rectangle([(50,tag_y),(50+tag_w, tag_y+34)], fill=cat_color)
    draw.text((63, tag_y+6), cat_label, font=f_tag, fill=WHITE)

    def wrap(text, font, max_w):
        words = text.split()
        lines, cur = [], ""
        for w in words:
            test = (cur+" "+w).strip()
            if draw.textbbox((0,0),test,font=font)[2] <= max_w:
                cur = test
            else:
                if cur: lines.append(cur)
                cur = w
        if cur: lines.append(cur)
        return lines

    max_w = W - 100
    sentences = text.split('. ')
    headline  = sentences[0] + ('.' if len(sentences)>1 else '')
    body      = '. '.join(sentences[1:]) if len(sentences)>1 else ''

    y = tag_y + 48
    for line in wrap(headline, f_title, max_w)[:4]:
        draw.text((50,y), line, font=f_title, fill=WHITE)
        y += 56

    if body:
        y += 4
        for line in wrap(body, f_body, max_w)[:3]:
            draw.text((50,y), line, font=f_body, fill=LIGHT_GRAY)
            y += 36

    draw.rectangle([(0,H-78),(W,H)], fill=(3,12,30))
    draw.rectangle([(0,H-78),(W,H-75)], fill=cat_color)
    draw.text((50,H-53), f"Source: {source}", font=f_sm, fill=LIGHT_GRAY)
    draw.text((W-260,H-53), timestamp, font=f_sm, fill=LIGHT_GRAY)

    buf = BytesIO()
    img.save(buf, format="PNG", quality=95)
    buf.seek(0)
    return buf

# ── Send to Telegram ──────────────────────────────────────────────────────────
def send_to_telegram(buf, caption):
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
            data={"chat_id": TELEGRAM_CHANNEL_ID, "caption": caption, "parse_mode": "HTML"},
            files={"photo": ("card.png", buf, "image/png")},
            timeout=30
        )
        resp.raise_for_status()
        log.info("✅ Posted to Telegram")
        return True
    except Exception as e:
        log.error(f"Telegram error: {e}")
        return False

# ── Buffer Social Media Posting ───────────────────────────────────────────────
BUFFER_TOKEN   = os.environ.get("BUFFER_ACCESS_TOKEN", "")
BUFFER_ORG_ID  = os.environ.get("BUFFER_ORG_ID", "")
BUFFER_FB_ID   = os.environ.get("BUFFER_FACEBOOK_ID", "")
BUFFER_IG_ID   = os.environ.get("BUFFER_INSTAGRAM_ID", "")
BUFFER_TW_ID   = os.environ.get("BUFFER_TWITTER_ID", "")

def upload_image_to_buffer(img_bytes):
    """Upload image to a hosting service to get public URL for Buffer"""
    try:
        # Use Telegram file as image source — upload to imgbb (free)
        IMGBB_KEY = os.environ.get("IMGBB_API_KEY", "")
        if not IMGBB_KEY:
            return None
        import base64
        img_b64 = base64.b64encode(img_bytes).decode()
        resp = requests.post(
            "https://api.imgbb.com/1/upload",
            data={"key": IMGBB_KEY, "image": img_b64},
            timeout=20
        )
        if resp.status_code == 200:
            url = resp.json()["data"]["url"]
            log.info(f"✅ Image uploaded to imgbb: {url[:50]}")
            return url
    except Exception as e:
        log.error(f"Image upload error: {e}")
    return None

def post_to_buffer(image_url, caption, channel_ids):
    """Post to multiple Buffer channels via GraphQL API"""
    if not BUFFER_TOKEN or not image_url:
        return False
    try:
        # Clean caption — remove HTML tags for social media
        import re
        clean_caption = re.sub(r'<[^>]+>', '', caption)
        clean_caption = clean_caption.replace('&amp;', '&').strip()

        query = """
        mutation CreatePost($input: CreatePostInput!) {
            createPost(input: $input) {
                post {
                    id
                    status
                }
            }
        }
        """
        for channel_id in channel_ids:
            if not channel_id:
                continue
            variables = {
                "input": {
                    "channelId": channel_id,
                    "content": {
                        "text": clean_caption[:2200],
                        "media": [{"url": image_url, "type": "image"}]
                    }
                }
            }
            resp = requests.post(
                "https://api.buffer.com/graphql",
                json={"query": query, "variables": variables},
                headers={"Authorization": f"Bearer {BUFFER_TOKEN}"},
                timeout=20
            )
            if resp.status_code == 200:
                data = resp.json()
                if "errors" in data:
                    log.error(f"Buffer error [{channel_id[:8]}]: {data['errors']}")
                else:
                    log.info(f"✅ Posted to Buffer channel: {channel_id[:8]}...")
            else:
                log.error(f"Buffer HTTP error: {resp.status_code}")
            time.sleep(2)
        return True
    except Exception as e:
        log.error(f"Buffer exception: {e}")
        return False

def post_to_social(img_buf, caption):
    """Post card to Facebook, Instagram, X via Buffer — every 30 mins"""
    if not BUFFER_TOKEN:
        log.info("No Buffer token — skipping social post")
        return
    try:
        img_bytes = img_buf.getvalue()
        image_url = upload_image_to_buffer(img_bytes)
        if not image_url:
            log.warning("No image URL — skipping Buffer post")
            return
        channel_ids = [BUFFER_FB_ID, BUFFER_IG_ID, BUFFER_TW_ID]
        if post_to_buffer(image_url, caption, channel_ids):
            log.info("✅ Posted to all social channels via Buffer!")
    except Exception as e:
        log.error(f"Social post error: {e}")

def get_mvt_hour():
    return (datetime.utcnow().hour + 5) % 24

def is_day_mode():
    return 7 <= get_mvt_hour() < 18

# ── Two-tier posting logic ────────────────────────────────────────────────────
BREAKING_KEYWORDS = [
    "breaking", "urgent", "alert", "killed", "dead", "dies", "explosion",
    "crash", "attack", "arrested", "emergency", "disaster", "flood", "fire",
    "missing", "tsunami", "earthquake", "accident", "murder", "bomb",
    "resign", "crisis", "leaked", "scandal", "raid", "collapse", "shot",
    "war", "strike", "invasion", "hostage", "trapped", "sinking"
]

# Track last regular post time to enforce 2hr Telegram limit
last_regular_post_time = None

def is_breaking(title, summary="", cat=""):
    text = (title + " " + summary).lower()
    return any(kw in text for kw in BREAKING_KEYWORDS) or cat == "DISASTER"

def can_post_regular():
    """Regular news: max 1 post per 2 hours on Telegram"""
    global last_regular_post_time
    if last_regular_post_time is None:
        return True
    elapsed = (datetime.utcnow() - last_regular_post_time).total_seconds()
    return elapsed >= 7200  # 2 hours

def post_article(article, seen, social_only=False):
    """Process and post a single article. Returns True if posted."""
    global last_regular_post_time
    cat = article["cat"]
    breaking = is_breaking(article["title"], article["summary"], cat)

    # In social_only mode — skip Telegram, only post to social
    if not social_only:
        if not breaking and not can_post_regular():
            mins_left = int((7200 - (datetime.utcnow() - last_regular_post_time).total_seconds()) / 60)
            log.info(f"⏳ [{cat}] Telegram throttled — {mins_left} mins left | posting to social only")
            rewritten, keyword = rewrite_news(article["title"], article["summary"], cat)
            bg = fetch_background_image(keyword)
            ts = datetime.now().strftime("%d %b %Y • %H:%M")
            card = generate_card(rewritten, article["source"], ts, cat, bg)
            cat_emoji = {"LOCAL":"🇲🇻","FOOTBALL":"⚽","WORLD":"🌍","DISASTER":"🚨","WEATHER":"🌤️","TOURISM":"✈️"}.get(cat,"📰")
            caption = f"{cat_emoji} {article['title']}\n\n{rewritten}\n\n📡 Samuga Media | @samugacommunity"
            card.seek(0)
            threading.Thread(target=post_to_social, args=(card, caption), daemon=True).start()
            seen.add(article["id"])
            save_seen(seen)
            return False

    log.info(f"📰 [{'🔴 BREAKING' if breaking else '🟡 REGULAR'}][{cat}] {article['title'][:60]}...")
    rewritten, keyword = rewrite_news(article["title"], article["summary"], cat)
    log.info(f"🖼️ Keyword: {keyword}")
    bg = fetch_background_image(keyword)
    ts = datetime.now().strftime("%d %b %Y • %H:%M")
    card = generate_card(rewritten, article["source"], ts, cat, bg)
    cat_emoji = {"LOCAL":"🇲🇻","FOOTBALL":"⚽","WORLD":"🌍","DISASTER":"🚨","WEATHER":"🌤️","TOURISM":"✈️"}.get(cat,"📰")

    # Add BREAKING tag to caption if breaking
    breaking_tag = "🚨 <b>BREAKING NEWS</b>\n\n" if breaking else ""
    caption = (
        f"{breaking_tag}{cat_emoji} <b>{article['title']}</b>\n\n"
        f"{rewritten}\n\n"
        f"🔗 <a href='{article['link']}'>Read more</a>\n\n"
        f"📡 <b>Samuga Media</b> | @samugacommunity"
    )

    # Social only mode — skip Telegram, only post to social
    if social_only:
        card.seek(0)
        threading.Thread(target=post_to_social, args=(card, caption), daemon=True).start()
        seen.add(article["id"])
        save_seen(seen)
        remember_post(article["title"], cat, ts)
        log.info(f"📱 Social only post done [{cat}]")
        return True

    if send_to_telegram(card, caption):
        seen.add(article["id"])
        save_seen(seen)
        remember_post(article["title"], cat, ts)
        if not breaking:
            last_regular_post_time = datetime.utcnow()
            log.info(f"🕐 Regular post timer reset — next regular in 2hrs")
        else:
            log.info(f"🔴 Breaking news posted immediately!")
        # Post to social media via Buffer (every 3hrs)
        card.seek(0)
        threading.Thread(target=post_to_social, args=(card, caption), daemon=True).start()
        return True
    return False

def run_job(social_only=False):
    h = get_mvt_hour()
    mode = "SOCIAL ONLY" if social_only else ("DAY" if is_day_mode() else "NIGHT")
    log.info(f"🕐 MVT {h:02d}:xx | {mode} mode")
    seen     = load_seen()
    articles = fetch_news()

    # Also check MVCrisis for breaking Maldives news
    mvcrisis_articles = check_mvcrisis()
    articles = mvcrisis_articles + articles

    # Separate breaking vs regular, group by category
    breaking_articles = []
    by_cat = {}

    for article in articles:
        if article["id"] in seen:
            continue
        cat = article["cat"]
        if is_breaking(article["title"], article["summary"], cat):
            breaking_articles.append(article)
        elif cat not in by_cat:
            by_cat[cat] = article

    total = len(breaking_articles) + len(by_cat)
    if total == 0:
        log.info("No fresh articles this run.")
        return

    log.info(f"🔴 {len(breaking_articles)} breaking | 🟡 {len(by_cat)} regular")

    posted = 0

    # Post ALL breaking news immediately — no throttle
    for article in breaking_articles:
        log.info(f"🚀 BREAKING [{article['cat']}]...")
        if post_article(article, seen, social_only=social_only):
            posted += 1
            time.sleep(10)

    # Post regular — one per category, respects 2hr throttle
    order = ["LOCAL", "WORLD", "FOOTBALL", "TOURISM", "WEATHER"]
    for cat in order:
        if cat not in by_cat:
            continue
        article = by_cat[cat]
        log.info(f"🚀 Regular [{cat}]...")
        if post_article(article, seen, social_only=social_only):
            posted += 1
            if posted < len(by_cat):
                log.info(f"⏳ Waiting 5 mins before next category...")
                time.sleep(300)

    log.info(f"✅ Posted {posted} articles this run.")

def scheduled_check():
    h = get_mvt_hour()
    if not is_day_mode() and h not in [18, 21, 0, 3, 6]:
        # Night skip for Telegram — but still run for social media!
        log.info(f"💤 Night mode (MVT {h:02d}:xx) — social only run")
        run_job(social_only=True)
        return
    run_job()

# ── Chat Assistant ────────────────────────────────────────────────────────────
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")

# Recent posts memory (in-memory, last 20 posts)
recent_posts = []

def remember_post(title, cat, timestamp):
    """Store recently posted articles in memory"""
    recent_posts.append({"title": title, "cat": cat, "time": timestamp})
    if len(recent_posts) > 20:
        recent_posts.pop(0)

def get_local_headlines():
    """Fetch fresh local headlines for context"""
    headlines = []
    try:
        for feed_cfg in RSS_FEEDS[:4]:  # Only first 4 local feeds
            feed = feedparser.parse(feed_cfg["url"])
            for entry in feed.entries[:3]:
                title = entry.get("title", "")
                if title and is_fresh(entry, hours=12):
                    headlines.append(f"• [{feed_cfg['cat']}] {title}")
            if len(headlines) >= 10:
                break
    except Exception as e:
        log.error(f"Headlines error: {e}")
    return headlines[:10]

def tavily_search(query):
    """Search web with Tavily for real-time info"""
    if not TAVILY_API_KEY:
        return ""
    try:
        resp = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": TAVILY_API_KEY,
                "query": query,
                "search_depth": "basic",
                "max_results": 4,
                "include_answer": True,
            },
            timeout=15
        )
        if resp.status_code == 200:
            data = resp.json()
            answer = data.get("answer", "")
            results = data.get("results", [])
            snippets = [r.get("content", "")[:200] for r in results[:3]]
            combined = answer + "\n" + "\n".join(snippets)
            log.info(f"✅ Tavily search done for: {query[:40]}")
            return combined.strip()
        else:
            log.error(f"Tavily error: {resp.status_code}")
    except Exception as e:
        log.error(f"Tavily exception: {e}")
    return ""

def check_mvcrisis():
    """Check MVCrisis for latest breaking Maldives news via Tavily"""
    if not TAVILY_API_KEY:
        return []
    try:
        resp = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": TAVILY_API_KEY,
                "query": "site:t.me/mvcrisis OR site:twitter.com/mvcrisis Maldives breaking news latest",
                "search_depth": "basic",
                "max_results": 5,
                "include_answer": False,
            },
            timeout=15
        )
        if resp.status_code == 200:
            results = resp.json().get("results", [])
            articles = []
            for r in results:
                title = r.get("title", "")
                content = r.get("content", "")
                if title and is_fresh_by_date(r.get("published_date", "")):
                    article_id = hashlib.md5(r.get("url", title).encode()).hexdigest()
                    articles.append({
                        "id": article_id,
                        "title": title,
                        "summary": content[:300],
                        "link": r.get("url", "https://t.me/mvcrisis"),
                        "source": "MVCrisis",
                        "cat": "DISASTER",
                    })
            log.info(f"🚨 MVCrisis found {len(articles)} articles")
            return articles
    except Exception as e:
        log.error(f"MVCrisis search error: {e}")
    return []

def is_fresh_by_date(date_str, hours=24):
    """Check if a date string is within last 24 hours"""
    try:
        if not date_str:
            return True
        from dateutil import parser as dateparser
        dt = dateparser.parse(date_str)
        if dt and dt.tzinfo:
            dt = dt.replace(tzinfo=None)
        return datetime.utcnow() - dt < timedelta(hours=hours)
    except:
        return True
    """Decide if message needs real-time web search"""
    keywords = [
        "latest", "today", "now", "current", "happening", "news",
        "score", "match", "result", "win", "won", "lost", "lose", "goal", "goals",
        "weather", "storm", "earthquake", "tsunami", "war", "attack",
        "price", "stock", "update", "recently", "just", "breaking",
        "who", "when", "which team", "final", "semi", "quarter",
        "champion", "tournament", "league", "cup", "world cup",
        "killed", "dead", "crash", "accident", "fire", "flood",
        "yesterday", "last night", "this week", "election", "vote"
    ]
    msg_lower = message.lower()
    return any(kw in msg_lower for kw in keywords)

def needs_web_search(message):
    """Decide if message needs real-time web search"""
    keywords = [
        "latest", "today", "now", "current", "happening", "news",
        "score", "match", "result", "win", "won", "lost", "lose", "goal", "goals",
        "weather", "storm", "earthquake", "tsunami", "war", "attack",
        "price", "stock", "update", "recently", "just", "breaking",
        "who", "when", "which team", "final", "semi", "quarter",
        "champion", "tournament", "league", "cup", "world cup",
        "killed", "dead", "crash", "accident", "fire", "flood",
        "yesterday", "last night", "this week", "election", "vote"
    ]
    return any(kw in message.lower() for kw in keywords)

def chat_with_claude(user_message):
    try:
        # Get local headlines for context
        headlines = []
        try:
            headlines = get_local_headlines()
        except Exception as e:
            log.warning(f"Headlines fetch failed: {e}")

        headlines_text = "\n".join(headlines[:8]) if headlines else "No recent headlines available."

        # Get recent posts memory
        memory_text = ""
        if recent_posts:
            memory_text = "Recently posted to @samugacommunity:\n"
            for p in recent_posts[-5:]:
                memory_text += f"• [{p['cat']}] {p['title']} ({p['time']})\n"

        # Web search if needed
        web_context = ""
        try:
            if needs_web_search(user_message):
                log.info(f"🔍 Searching web for: {user_message[:50]}")
                search_query = user_message
                if any(w in user_message.lower() for w in ["world cup", "match", "score", "won", "win"]):
                    search_query = f"{user_message} 2026 latest result"
                web_context = tavily_search(search_query)
        except Exception as e:
            log.warning(f"Web search failed: {e}")

        # Build context — keep it short to avoid token issues
        context = f"LATEST MALDIVES NEWS:\n{headlines_text}"
        if memory_text:
            context += f"\n\n{memory_text}"
        if web_context:
            context += f"\n\nWEB SEARCH:\n{web_context[:600]}"

        system_prompt = f"""You are Samuga AI — friendly news assistant for Samuga Media, a Maldivian media outlet.

ABOUT SAMUGA MEDIA:
Samuga Media is a Maldivian digital media outlet dedicated to delivering trusted news, impactful storytelling, and innovative digital solutions for the people of the Maldives.

Samuga Community (@samugacommunity) was created to bring Maldivians together — a space where people stay informed, connected, and engaged with what's happening in their country and the world.

TEAM:
- Founder & Managing Director: Abdul Muhsin (also known as Manchii and Mutte) — Maldivian entrepreneur and media professional dedicated to community-driven media and creative businesses. He leads Samuga Media and Samuga Creative.
- Co-Founder & Editor-in-Chief: Mariyam Ulya (known as Uly) — experienced journalist and media professional who leads editorial excellence, content review and quality assurance. She ensures Samuga Media remains a trusted, credible source.

CURRENT CONTEXT:
{context}

RULES:
- Talk warm and human, not robotic
- Max 4 sentences per reply
- Use context above to give accurate answers
- Always guide people to @samugacommunity for more
- Never say you lack real-time data — use the context
- If not in context: "Check @samugacommunity for the latest on that!"
- Cover: Maldives news, football, world news, weather, tourism
- If asked about the founder, MD, co-founder or team — answer confidently
- English only"""

        msg = ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}]
        )
        return msg.content[0].text.strip()

    except Exception as e:
        log.error(f"Chat error: {e}")
        return "Hey! Something went wrong on my end 😅 Check @samugacommunity for the latest news!"

def send_text(chat_id, text, reply_to=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json=payload, timeout=15
        )
    except Exception as e:
        log.error(f"Send text error: {e}")

def handle_updates():
    offset = 0
    bot_mention = f"@{BOT_USERNAME}".lower()
    log.info(f"💬 Chat listening for @{BOT_USERNAME}...")
    while True:
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                params={"offset": offset, "timeout": 30},
                timeout=40
            )
            if resp.status_code != 200:
                time.sleep(5)
                continue
            for update in resp.json().get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                if not msg:
                    continue
                text = msg.get("text", "")
                if not text:
                    continue
                chat_id   = msg["chat"]["id"]
                msg_id    = msg["message_id"]
                chat_type = msg["chat"]["type"]
                user_name = msg.get("from", {}).get("first_name", "there")

                if chat_type == "private":
                    if text.startswith("/start"):
                        send_text(chat_id,
                            f"👋 Hey {user_name}! I'm <b>Samuga AI</b> — your Maldives news assistant!\n\n"
                            f"Ask me anything about Maldives news, politics, tourism, football or world news.\n\n"
                            f"📡 Follow <b>@samugacommunity</b> for live news updates!",
                            reply_to=msg_id)
                    else:
                        log.info(f"💬 DM from {user_name}: {text[:50]}")
                        reply = chat_with_claude(text)
                        send_text(chat_id, reply, reply_to=msg_id)

                elif chat_type in ["group", "supergroup"]:
                    if bot_mention in text.lower():
                        clean = text.lower().replace(bot_mention, "").strip()
                        if clean:
                            log.info(f"💬 Group mention from {user_name}: {clean[:50]}")
                            reply = chat_with_claude(clean)
                            send_text(chat_id, reply, reply_to=msg_id)
        except Exception as e:
            log.error(f"Update loop error: {e}")
            time.sleep(5)

# ── Entry ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("🚀 Samuga News Bot v2.0 starting...")
    log.info("📅 7AM-6PM MVT: every 30min | 6PM-7AM MVT: every 3hrs")
    log.info("💬 Chat assistant active — DMs + group mentions")
    log.info("📰 Categories: LOCAL 🇲🇻 | FOOTBALL ⚽ | WORLD 🌍 | DISASTER 🚨 | WEATHER 🌤️ | TOURISM ✈️")

    # Log seen articles count on startup
    seen_on_start = load_seen()
    log.info(f"📚 Loaded {len(seen_on_start)} seen articles from storage")

    chat_thread = threading.Thread(target=handle_updates, daemon=True)
    chat_thread.start()

    # No immediate run on startup — prevents posting on every redeploy!
    scheduler = BlockingScheduler()
    scheduler.add_job(scheduled_check, "interval", minutes=30)
    log.info("⏰ Scheduler started — first run in 30 minutes")
    scheduler.start()
