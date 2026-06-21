import os
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

ai = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

RSS_FEEDS = [
    "https://news.google.com/rss/search?q=maldives&hl=en-MV&gl=MV&ceid=MV:en",
    "https://news.google.com/rss/search?q=maldives+news&hl=en&gl=US&ceid=US:en",
]

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
            json.dump(list(seen)[-500:], f)
    except Exception as e:
        log.warning(f"Save seen: {e}")

def is_fresh(entry, hours=24):
    """Only accept articles published in last 24 hours"""
    try:
        pub = entry.get("published", "")
        if pub:
            pub_dt = parsedate_to_datetime(pub)
            # Make timezone-naive for comparison
            if pub_dt.tzinfo:
                pub_dt = pub_dt.replace(tzinfo=None)
            age = datetime.utcnow() - pub_dt
            return age < timedelta(hours=hours)
    except Exception as e:
        log.warning(f"Date parse error: {e}")
    return True  # if can't parse date, include it

def fetch_news():
    articles = []
    seen_titles = set()
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:15]:
                title = entry.get("title", "")
                title_key = title.lower()[:50]
                if title_key in seen_titles:
                    continue
                if not is_fresh(entry, hours=24):
                    log.info(f"⏭️ Old article skipped: {title[:50]}")
                    continue
                seen_titles.add(title_key)
                article_id = hashlib.md5(entry.get("link", title).encode()).hexdigest()
                articles.append({
                    "id": article_id,
                    "title": title,
                    "summary": entry.get("summary", title),
                    "link": entry.get("link", ""),
                    "source": entry.get("source", {}).get("title", "Google News"),
                    "published": entry.get("published", ""),
                })
        except Exception as e:
            log.error(f"Feed error: {e}")
    log.info(f"Found {len(articles)} fresh articles")
    return articles

def rewrite_news(title, summary):
    prompt = f"""You are a news writer for Samuga Media, a Maldivian digital media outlet.

Rewrite the following news into a short, punchy, engaging English post for a Telegram channel.
- Max 3 sentences
- Clear and direct
- No hashtags, no emojis
- Professional but easy to read

Also provide a 2-3 word Pexels image search keyword relevant to the topic (e.g. "tropical ocean", "government meeting", "coral reef").

Title: {title}
Summary: {summary}

Respond in EXACTLY this format:
TEXT: [rewritten news]
IMAGE: [2-3 word keyword]"""

    try:
        msg = ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )
        response = msg.content[0].text.strip()
        text, keyword = "", "maldives ocean"
        for line in response.split('\n'):
            if line.startswith("TEXT:"):
                text = line[5:].strip()
            elif line.startswith("IMAGE:"):
                keyword = line[6:].strip()
        return (text or title), keyword
    except Exception as e:
        log.error(f"Claude error: {e}")
        return title, "maldives"

def fetch_background_image(keyword):
    if not PEXELS_API_KEY:
        log.warning("No Pexels key set")
        return None
    try:
        headers = {"Authorization": PEXELS_API_KEY}
        url = f"https://api.pexels.com/v1/search?query={keyword}&per_page=5&orientation=square"
        resp = requests.get(url, headers=headers, timeout=15)
        log.info(f"Pexels status: {resp.status_code} for '{keyword}'")
        if resp.status_code == 200:
            data = resp.json()
            photos = data.get("photos", [])
            if photos:
                img_url = photos[0]["src"]["large"]
                img_resp = requests.get(img_url, timeout=20)
                if img_resp.status_code == 200:
                    log.info(f"✅ Got Pexels image for: {keyword}")
                    return Image.open(BytesIO(img_resp.content)).convert("RGB")
        else:
            log.error(f"Pexels failed: {resp.status_code} — {resp.text[:200]}")
    except Exception as e:
        log.error(f"Pexels exception: {e}")
    return None

BG_TOP     = (10, 40, 75)
BG_BOTTOM  = (5, 20, 45)
ACCENT     = (41, 171, 226)
WHITE      = (255, 255, 255)
LIGHT_GRAY = (200, 215, 230)

def generate_card(text, source, timestamp, bg_image=None):
    W, H = 1080, 1080
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
            t = y / H
            d.line([(0, y), (W, y)], fill=(
                int(BG_TOP[0]+(BG_BOTTOM[0]-BG_TOP[0])*t),
                int(BG_TOP[1]+(BG_BOTTOM[1]-BG_TOP[1])*t),
                int(BG_TOP[2]+(BG_BOTTOM[2]-BG_TOP[2])*t),
            ))

    # Bottom dark gradient
    ov = Image.new("RGBA", (W, H), (0,0,0,0))
    od = ImageDraw.Draw(ov)
    for y in range(H//2, H):
        t = (y-H//2)/(H//2)
        od.line([(0,y),(W,y)], fill=(5,20,50,int(215*t)))
    img = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")

    # Top dark gradient
    ov2 = Image.new("RGBA", (W, H), (0,0,0,0))
    od2 = ImageDraw.Draw(ov2)
    for y in range(0, 170):
        t = 1 - y/170
        od2.line([(0,y),(W,y)], fill=(5,20,50,int(190*t)))
    img = Image.alpha_composite(img.convert("RGBA"), ov2).convert("RGB")

    draw = ImageDraw.Draw(img)
    draw.rectangle([(0,0),(W,5)], fill=ACCENT)

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

    tag_y = 590
    draw.rectangle([(50, tag_y),(222, tag_y+34)], fill=ACCENT)
    draw.text((63, tag_y+6), "● LATEST NEWS", font=f_tag, fill=WHITE)

    def wrap(text, font, max_w):
        words = text.split()
        lines, cur = [], ""
        for w in words:
            test = (cur+" "+w).strip()
            if draw.textbbox((0,0), test, font=font)[2] <= max_w:
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
        draw.text((50, y), line, font=f_title, fill=WHITE)
        y += 56

    if body:
        y += 4
        for line in wrap(body, f_body, max_w)[:3]:
            draw.text((50, y), line, font=f_body, fill=LIGHT_GRAY)
            y += 36

    draw.rectangle([(0,H-78),(W,H)], fill=(3,12,30))
    draw.rectangle([(0,H-78),(W,H-75)], fill=ACCENT)
    draw.text((50, H-53), f"Source: {source}", font=f_sm, fill=LIGHT_GRAY)
    draw.text((W-260, H-53), timestamp, font=f_sm, fill=LIGHT_GRAY)

    buf = BytesIO()
    img.save(buf, format="PNG", quality=95)
    buf.seek(0)
    return buf

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

def get_mvt_hour():
    return (datetime.utcnow().hour + 5) % 24

def is_day_mode():
    h = get_mvt_hour()
    return 7 <= h < 18

def run_job():
    h = get_mvt_hour()
    mode = "DAY (every 30min)" if is_day_mode() else "NIGHT (every 3hr)"
    log.info(f"🕐 MVT {h:02d}:xx | {mode}")
    log.info("🔍 Fetching fresh news...")

    seen     = load_seen()
    articles = fetch_news()
    posted   = 0

    for article in articles:
        if article["id"] in seen:
            continue

        log.info(f"📰 {article['title'][:70]}...")
        rewritten, keyword = rewrite_news(article["title"], article["summary"])

        log.info(f"🖼️ Keyword: {keyword}")
        bg = fetch_background_image(keyword)

        ts   = datetime.now().strftime("%d %b %Y • %H:%M")
        card = generate_card(rewritten, article["source"], ts, bg)

        caption = (
            f"<b>{article['title']}</b>\n\n"
            f"{rewritten}\n\n"
            f"🔗 <a href='{article['link']}'>Read more</a>\n\n"
            f"📡 <b>Samuga Media</b> | @samugacommunity"
        )

        if send_to_telegram(card, caption):
            seen.add(article["id"])
            save_seen(seen)
            posted += 1
            time.sleep(5)
            break

    if posted == 0:
        log.info("No fresh articles this run.")

def scheduled_check():
    h = get_mvt_hour()
    if not is_day_mode() and h not in [18, 21, 0, 3, 6]:
        log.info(f"💤 Night skip (MVT {h:02d}:xx)")
        return
    run_job()

if __name__ == "__main__":
    log.info("🚀 Samuga News Bot starting...")
    log.info("📅 7AM-6PM MVT: every 30min | 6PM-7AM MVT: every 3hrs")
    log.info("💬 Chat assistant active — DMs + group mentions")

    # Start chat listener in background thread
    chat_thread = threading.Thread(target=handle_updates, daemon=True)
    chat_thread.start()

    run_job()
    scheduler = BlockingScheduler()
    scheduler.add_job(scheduled_check, "interval", minutes=30)
    log.info("⏰ Scheduler started")
    scheduler.start()

# ── Chat Assistant ────────────────────────────────────────────────────────────
import threading

BOT_USERNAME = os.environ.get("BOT_USERNAME", "SamugaNewsBot")

def chat_with_claude(user_message, user_name):
    try:
        msg = ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            system="""You are Samuga AI, the assistant for Samuga Media — a Maldivian digital media outlet.
You help people with:
- Latest news about the Maldives
- Questions about Maldivian politics, tourism, culture, economy
- General questions
- Info about Samuga Media and the @samugacommunity Telegram channel

Keep responses short, friendly and conversational. Max 3-4 sentences.
Always respond in English. If asked about very recent news, mention they can check @samugacommunity for the latest updates.
You are powered by Claude AI by Anthropic.""",
            messages=[{"role": "user", "content": user_message}]
        )
        return msg.content[0].text.strip()
    except Exception as e:
        log.error(f"Chat Claude error: {e}")
        return "Sorry, I'm having trouble right now. Please try again in a moment! 🙏"

def send_text(chat_id, text, reply_to=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
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

            updates = resp.json().get("result", [])
            for update in updates:
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

                # DM — always respond
                if chat_type == "private":
                    if text.startswith("/start"):
                        send_text(chat_id,
                            f"👋 Hey {user_name}! I'm <b>Samuga AI</b> — your Maldives news assistant!\n\n"
                            f"Ask me anything about Maldives news, politics, tourism or culture.\n\n"
                            f"📡 Follow <b>@samugacommunity</b> for live news updates!",
                            reply_to=msg_id)
                    else:
                        log.info(f"💬 DM from {user_name}: {text[:50]}")
                        reply = chat_with_claude(text, user_name)
                        send_text(chat_id, reply, reply_to=msg_id)

                # Group — only respond when mentioned
                elif chat_type in ["group", "supergroup"]:
                    if bot_mention in text.lower():
                        clean = text.lower().replace(bot_mention, "").strip()
                        if clean:
                            log.info(f"💬 Group mention from {user_name}: {clean[:50]}")
                            reply = chat_with_claude(clean, user_name)
                            send_text(chat_id, reply, reply_to=msg_id)

        except Exception as e:
            log.error(f"Update loop error: {e}")
            time.sleep(5)
