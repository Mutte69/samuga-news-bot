import os
import time
import logging
import hashlib
import json
import feedparser
import requests
import anthropic
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from datetime import datetime
from apscheduler.schedulers.blocking import BlockingScheduler
from io import BytesIO

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ── Config from env ───────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "@samugacommunity")
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

# ── Anthropic client ──────────────────────────────────────────────────────────
ai = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── RSS Feeds (Maldives focused) ──────────────────────────────────────────────
RSS_FEEDS = [
    "https://news.google.com/rss/search?q=maldives&hl=en-MV&gl=MV&ceid=MV:en",
    "https://news.google.com/rss/search?q=maldives+news&hl=en&gl=US&ceid=US:en",
]

# ── Seen articles cache ───────────────────────────────────────────────────────
SEEN_FILE = "seen_articles.json"

def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    return set()

def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen)[-500:], f)  # keep last 500

# ── Fetch news ────────────────────────────────────────────────────────────────
def fetch_news():
    articles = []
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:5]:
                article_id = hashlib.md5(entry.get("link", entry.title).encode()).hexdigest()
                articles.append({
                    "id": article_id,
                    "title": entry.get("title", ""),
                    "summary": entry.get("summary", entry.get("title", "")),
                    "link": entry.get("link", ""),
                    "source": entry.get("source", {}).get("title", "Google News"),
                })
        except Exception as e:
            log.error(f"Feed error {url}: {e}")
    return articles

# ── Rewrite with Claude ───────────────────────────────────────────────────────
def rewrite_news(title, summary):
    prompt = f"""You are a news writer for Samuga Media, a Maldivian digital media outlet.

Rewrite the following news into a short, punchy, engaging English post for a Telegram channel.
- Max 3 sentences
- Clear and direct
- No hashtags
- No emojis
- Professional but easy to read
- End with one strong sentence that adds context or impact

Title: {title}
Summary: {summary}

Return ONLY the rewritten post text, nothing else."""

    try:
        message = ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        return message.content[0].text.strip()
    except Exception as e:
        log.error(f"Claude API error: {e}")
        return f"{title}\n\n{summary[:200]}"

# ── Card colors ───────────────────────────────────────────────────────────────
BG_TOP    = (10, 40, 75)       # deep dark blue
BG_BOTTOM = (5, 20, 45)        # even darker at bottom
ACCENT    = (41, 171, 226)     # Samuga light blue #29ABE2
WHITE     = (255, 255, 255)
LIGHT_GRAY = (200, 215, 230)

# ── Generate image card ───────────────────────────────────────────────────────
def generate_card(rewritten_text, source, timestamp):
    W, H = 1080, 1080
    img = Image.new("RGB", (W, H), BG_TOP)
    draw = ImageDraw.Draw(img)

    # Gradient background
    for y in range(H):
        t = y / H
        r = int(BG_TOP[0] + (BG_BOTTOM[0] - BG_TOP[0]) * t)
        g = int(BG_TOP[1] + (BG_BOTTOM[1] - BG_TOP[1]) * t)
        b = int(BG_TOP[2] + (BG_BOTTOM[2] - BG_TOP[2]) * t)
        draw.line([(0, y), (W, y)], fill=(r, g, b))

    # Accent line at top
    draw.rectangle([(0, 0), (W, 6)], fill=ACCENT)

    # Subtle accent glow strip
    for i in range(20):
        alpha = int(30 * (1 - i / 20))
        draw.rectangle([(0, 6 + i), (W, 7 + i)], fill=(41, 171, 226, alpha))

    # Logo
    try:
        logo = Image.open("logo.png").convert("RGBA")
        logo_h = 80
        ratio = logo_h / logo.height
        logo_w = int(logo.width * ratio)
        logo = logo.resize((logo_w, logo_h), Image.LANCZOS)
        img.paste(logo, (60, 50), logo)
    except Exception as e:
        log.warning(f"Logo error: {e}")
        draw.text((60, 50), "SAMUGA MEDIA", fill=ACCENT)

    # Divider line
    draw.rectangle([(60, 160), (W - 60, 163)], fill=ACCENT)

    # "BREAKING NEWS" tag
    try:
        tag_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22)
    except:
        tag_font = ImageFont.load_default()

    draw.rectangle([(60, 185), (245, 220)], fill=ACCENT)
    draw.text((75, 190), "● LATEST NEWS", font=tag_font, fill=WHITE)

    # Main news text
    try:
        text_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 44)
        small_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 28)
        source_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)
    except:
        text_font = ImageFont.load_default()
        small_font = ImageFont.load_default()
        source_font = ImageFont.load_default()

    # Word wrap text
    words = rewritten_text.split()
    lines = []
    current = ""
    max_chars = 28

    for word in words:
        if len(current) + len(word) + 1 <= max_chars:
            current += (" " if current else "") + word
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)

    # Limit to first 6 lines for main display
    display_lines = lines[:6]
    remaining = lines[6:]

    y_text = 260
    for line in display_lines:
        draw.text((60, y_text), line, font=text_font, fill=WHITE)
        y_text += 58

    # Remaining text in smaller font
    if remaining:
        remaining_text = " ".join(remaining)
        draw.text((60, y_text + 10), remaining_text, font=small_font, fill=LIGHT_GRAY)

    # Bottom bar
    draw.rectangle([(0, H - 90), (W, H)], fill=(5, 15, 35))
    draw.rectangle([(0, H - 90), (W, H - 87)], fill=ACCENT)

    # Source + timestamp
    draw.text((60, H - 65), f"Source: {source}", font=source_font, fill=LIGHT_GRAY)
    draw.text((W - 300, H - 65), timestamp, font=source_font, fill=LIGHT_GRAY)

    # Watermark
    draw.text((W - 280, 55), "samugamedia.com", font=source_font, fill=(41, 171, 226, 150))

    buf = BytesIO()
    img.save(buf, format="PNG", quality=95)
    buf.seek(0)
    return buf

# ── Send to Telegram ──────────────────────────────────────────────────────────
def send_to_telegram(image_buf, caption):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    try:
        resp = requests.post(url, data={
            "chat_id": TELEGRAM_CHANNEL_ID,
            "caption": caption,
            "parse_mode": "HTML",
        }, files={"photo": ("card.png", image_buf, "image/png")}, timeout=30)
        resp.raise_for_status()
        log.info("✅ Posted to Telegram")
        return True
    except Exception as e:
        log.error(f"Telegram error: {e}")
        return False

# ── Main job ──────────────────────────────────────────────────────────────────
def run_job():
    log.info("🔍 Fetching news...")
    seen = load_seen()
    articles = fetch_news()

    posted = 0
    for article in articles:
        if article["id"] in seen:
            continue

        log.info(f"📰 Processing: {article['title'][:60]}...")
        rewritten = rewrite_news(article["title"], article["summary"])

        timestamp = datetime.now().strftime("%d %b %Y • %H:%M")
        card = generate_card(rewritten, article["source"], timestamp)

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
            time.sleep(5)  # small delay between posts
            break  # post 1 article per run to avoid spam

    if posted == 0:
        log.info("No new articles found this run.")

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("🚀 Samuga News Bot starting...")
    run_job()  # run immediately on start

    scheduler = BlockingScheduler()
    scheduler.add_job(run_job, "interval", minutes=30)
    log.info("⏰ Scheduler running — every 30 minutes")
    scheduler.start()
