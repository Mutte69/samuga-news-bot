import os
import time
import logging
import hashlib
import json
import feedparser
import requests
import anthropic
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance
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
UNSPLASH_ACCESS_KEY = os.environ.get("UNSPLASH_ACCESS_KEY", "")

# ── Anthropic client ──────────────────────────────────────────────────────────
ai = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── RSS Feeds (Maldives focused) ──────────────────────────────────────────────
RSS_FEEDS = [
    "https://news.google.com/rss/search?q=maldives&hl=en-MV&gl=MV&ceid=MV:en",
    "https://news.google.com/rss/search?q=maldives+news&hl=en&gl=US&ceid=US:en",
]

# ── Seen articles cache ───────────────────────────────────────────────────────
DATA_DIR = "/data"
os.makedirs(DATA_DIR, exist_ok=True)
SEEN_FILE = os.path.join(DATA_DIR, "seen_articles.json")

def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    return set()

def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen)[-500:], f)

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

# ── Rewrite with Claude + get image keyword ───────────────────────────────────
def rewrite_news(title, summary):
    prompt = f"""You are a news writer for Samuga Media, a Maldivian digital media outlet.

Rewrite the following news into a short, punchy, engaging English post for a Telegram channel.
- Max 3 sentences
- Clear and direct
- No hashtags
- No emojis
- Professional but easy to read

Also provide a 2-3 word image search keyword relevant to the news topic (e.g. "ocean waves", "parliament building", "coral reef").

Title: {title}
Summary: {summary}

Respond in this exact format:
TEXT: [your rewritten news text]
IMAGE: [2-3 word search keyword]"""

    try:
        message = ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )
        response = message.content[0].text.strip()
        
        text = ""
        image_keyword = "maldives ocean"
        
        for line in response.split('\n'):
            if line.startswith("TEXT:"):
                text = line.replace("TEXT:", "").strip()
            elif line.startswith("IMAGE:"):
                image_keyword = line.replace("IMAGE:", "").strip()
        
        if not text:
            text = response
            
        return text, image_keyword
    except Exception as e:
        log.error(f"Claude API error: {e}")
        return f"{title}\n\n{summary[:200]}", "maldives"

# ── Fetch background image from Unsplash ─────────────────────────────────────
def fetch_background_image(keyword):
    try:
        if UNSPLASH_ACCESS_KEY:
            url = f"https://api.unsplash.com/photos/random?query={keyword}&orientation=squarish&client_id={UNSPLASH_ACCESS_KEY}"
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                img_url = data["urls"]["regular"]
                img_resp = requests.get(img_url, timeout=15)
                if img_resp.status_code == 200:
                    log.info(f"✅ Got Unsplash image for: {keyword}")
                    return Image.open(BytesIO(img_resp.content)).convert("RGB")
        
        # Fallback: search for maldives image without API key using picsum
        log.warning("No Unsplash key or failed — using gradient background")
        return None
    except Exception as e:
        log.error(f"Image fetch error: {e}")
        return None

# ── Card colors ───────────────────────────────────────────────────────────────
BG_TOP     = (10, 40, 75)
BG_BOTTOM  = (5, 20, 45)
ACCENT     = (41, 171, 226)
WHITE      = (255, 255, 255)
LIGHT_GRAY = (200, 215, 230)

# ── Generate image card ───────────────────────────────────────────────────────
def generate_card(rewritten_text, source, timestamp, bg_image=None):
    W, H = 1080, 1080
    img = Image.new("RGB", (W, H), BG_TOP)

    if bg_image:
        # Resize and crop background image to fill card
        bg = bg_image.copy()
        bg_ratio = bg.width / bg.height
        if bg_ratio > 1:
            new_h = H
            new_w = int(H * bg_ratio)
        else:
            new_w = W
            new_h = int(W / bg_ratio)
        bg = bg.resize((new_w, new_h), Image.LANCZOS)
        left = (new_w - W) // 2
        top = (new_h - H) // 2
        bg = bg.crop((left, top, left + W, top + H))

        # Darken the image significantly for text readability
        enhancer = ImageEnhance.Brightness(bg)
        bg = enhancer.enhance(0.25)

        # Add blue color overlay
        overlay = Image.new("RGB", (W, H), (8, 30, 65))
        img = Image.blend(bg, overlay, 0.55)
    else:
        # Gradient fallback
        draw_temp = ImageDraw.Draw(img)
        for y in range(H):
            t = y / H
            r = int(BG_TOP[0] + (BG_BOTTOM[0] - BG_TOP[0]) * t)
            g = int(BG_TOP[1] + (BG_BOTTOM[1] - BG_TOP[1]) * t)
            b = int(BG_TOP[2] + (BG_BOTTOM[2] - BG_TOP[2]) * t)
            draw_temp.line([(0, y), (W, y)], fill=(r, g, b))

    draw = ImageDraw.Draw(img)

    # Bottom gradient overlay for text area (bottom 60% of card)
    overlay_img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay_img)
    for y in range(H // 2, H):
        t = (y - H // 2) / (H // 2)
        alpha = int(200 * t)
        overlay_draw.line([(0, y), (W, y)], fill=(5, 20, 50, alpha))
    img = Image.alpha_composite(img.convert("RGBA"), overlay_img).convert("RGB")
    draw = ImageDraw.Draw(img)

    # Top overlay for logo area
    top_overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    top_draw = ImageDraw.Draw(top_overlay)
    for y in range(0, 180):
        t = 1 - (y / 180)
        alpha = int(180 * t)
        top_draw.line([(0, y), (W, y)], fill=(5, 20, 50, alpha))
    img = Image.alpha_composite(img.convert("RGBA"), top_overlay).convert("RGB")
    draw = ImageDraw.Draw(img)

    # Accent line at top
    draw.rectangle([(0, 0), (W, 5)], fill=ACCENT)

    # Logo
    try:
        logo = Image.open("logo.png").convert("RGBA")
        logo_h = 75
        ratio = logo_h / logo.height
        logo_w = int(logo.width * ratio)
        logo = logo.resize((logo_w, logo_h), Image.LANCZOS)
        img.paste(logo, (50, 40), logo)
    except Exception as e:
        log.warning(f"Logo error: {e}")
        draw.text((50, 40), "SAMUGA MEDIA", fill=WHITE)

    # Website watermark top right
    try:
        wm_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 22)
    except:
        wm_font = ImageFont.load_default()
    draw.text((W - 260, 55), "samugamedia.com", font=wm_font, fill=(255, 255, 255, 180))

    # Fonts
    try:
        tag_font   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22)
        title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 48)
        body_font  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 28)
        src_font   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 22)
    except:
        tag_font = title_font = body_font = src_font = ImageFont.load_default()

    # LATEST NEWS tag — positioned in lower half
    tag_y = 580
    draw.rectangle([(50, tag_y), (220, tag_y + 34)], fill=ACCENT)
    draw.text((62, tag_y + 6), "● LATEST NEWS", font=tag_font, fill=WHITE)

    # Word wrap helper
    def wrap_text(text, font, max_w):
        words = text.split()
        lines = []
        current = ""
        for word in words:
            test = (current + " " + word).strip()
            bbox = draw.textbbox((0, 0), test, font=font)
            if bbox[2] <= max_w:
                current = test
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines

    max_w = W - 100

    # Split into headline + body
    sentences = rewritten_text.split('. ')
    headline = sentences[0] + ('.' if len(sentences) > 1 else '')
    body = '. '.join(sentences[1:]) if len(sentences) > 1 else ''

    title_lines = wrap_text(headline, title_font, max_w)
    body_lines  = wrap_text(body, body_font, max_w) if body else []

    y = tag_y + 50
    for line in title_lines[:4]:
        draw.text((50, y), line, font=title_font, fill=WHITE)
        y += 58

    if body_lines:
        y += 6
        for line in body_lines[:3]:
            draw.text((50, y), line, font=body_font, fill=LIGHT_GRAY)
            y += 38

    # Bottom bar
    draw.rectangle([(0, H - 80), (W, H)], fill=(0, 0, 0, 180))
    draw.rectangle([(0, H - 80), (W, H - 77)], fill=ACCENT)
    draw.text((50, H - 55), f"Source: {source}", font=src_font, fill=LIGHT_GRAY)
    draw.text((W - 260, H - 55), timestamp, font=src_font, fill=LIGHT_GRAY)

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
        rewritten, image_keyword = rewrite_news(article["title"], article["summary"])

        log.info(f"🖼️ Fetching image for: {image_keyword}")
        bg_image = fetch_background_image(image_keyword)

        timestamp = datetime.now().strftime("%d %b %Y • %H:%M")
        card = generate_card(rewritten, article["source"], timestamp, bg_image)

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
        log.info("No new articles found this run.")

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("🚀 Samuga News Bot starting...")
    run_job()

    scheduler = BlockingScheduler()
    scheduler.add_job(run_job, "interval", minutes=30)
    log.info("⏰ Scheduler running — every 30 minutes")
    scheduler.start()
