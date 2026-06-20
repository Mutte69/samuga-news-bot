import os
import time
import logging
import hashlib
import json
import feedparser
import requests
import anthropic
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance
from datetime import datetime, timezone
from apscheduler.schedulers.blocking import BlockingScheduler
from io import BytesIO

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL_ID  = os.environ.get("TELEGRAM_CHANNEL_ID", "@samugacommunity")
ANTHROPIC_API_KEY    = os.environ["ANTHROPIC_API_KEY"]
UNSPLASH_ACCESS_KEY  = os.environ.get("UNSPLASH_ACCESS_KEY", "")

ai = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── RSS Feeds ─────────────────────────────────────────────────────────────────
RSS_FEEDS = [
    "https://news.google.com/rss/search?q=maldives&hl=en-MV&gl=MV&ceid=MV:en",
    "https://news.google.com/rss/search?q=maldives+news&hl=en&gl=US&ceid=US:en",
]

# ── Persistent seen cache ─────────────────────────────────────────────────────
DATA_DIR  = "/data"
os.makedirs(DATA_DIR, exist_ok=True)
SEEN_FILE = os.path.join(DATA_DIR, "seen_articles.json")

def load_seen():
    try:
        if os.path.exists(SEEN_FILE):
            with open(SEEN_FILE) as f:
                return set(json.load(f))
    except Exception as e:
        log.warning(f"Load seen error: {e}")
    return set()

def save_seen(seen):
    try:
        with open(SEEN_FILE, "w") as f:
            json.dump(list(seen)[-500:], f)
    except Exception as e:
        log.warning(f"Save seen error: {e}")

# ── Fetch news (today only) ───────────────────────────────────────────────────
def fetch_news():
    articles = []
    seen_titles = set()
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:10]:
                title = entry.get("title", "")
                # Deduplicate by title
                title_key = title.lower()[:50]
                if title_key in seen_titles:
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
            log.error(f"Feed error {url}: {e}")
    return articles

# ── Rewrite + image keyword ───────────────────────────────────────────────────
def rewrite_news(title, summary):
    prompt = f"""You are a news writer for Samuga Media, a Maldivian digital media outlet.

Rewrite the following news into a short, punchy, engaging English post for a Telegram channel.
- Max 3 sentences
- Clear and direct  
- No hashtags, no emojis
- Professional but easy to read

Also provide a 2-3 word Unsplash image search keyword relevant to the topic (e.g. "tropical ocean", "government building", "coral reef diving").

Title: {title}
Summary: {summary}

Respond in EXACTLY this format (two lines only):
TEXT: [rewritten news]
IMAGE: [2-3 word keyword]"""

    try:
        msg = ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )
        response = msg.content[0].text.strip()
        text, keyword = "", "maldives tropical"
        for line in response.split('\n'):
            if line.startswith("TEXT:"):
                text = line[5:].strip()
            elif line.startswith("IMAGE:"):
                keyword = line[6:].strip()
        return (text or title), keyword
    except Exception as e:
        log.error(f"Claude error: {e}")
        return title, "maldives"

# ── Fetch Unsplash image ──────────────────────────────────────────────────────
def fetch_background_image(keyword):
    if not UNSPLASH_ACCESS_KEY:
        log.warning("No Unsplash key set")
        return None
    try:
        url = f"https://api.unsplash.com/photos/random?query={keyword}&client_id={UNSPLASH_ACCESS_KEY}"
        log.info(f"Calling Unsplash: {url[:80]}...")
        resp = requests.get(url, timeout=15)
        log.info(f"Unsplash status: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            img_url = data["urls"]["regular"]
            log.info(f"Image URL: {img_url[:60]}...")
            img_resp = requests.get(img_url, timeout=20)
            if img_resp.status_code == 200:
                log.info(f"✅ Got Unsplash image for: {keyword}")
                return Image.open(BytesIO(img_resp.content)).convert("RGB")
            else:
                log.error(f"Image download failed: {img_resp.status_code}")
        else:
            log.error(f"Unsplash API failed: {resp.status_code} — {resp.text[:200]}")
    except Exception as e:
        log.error(f"Unsplash exception: {e}")
    return None

# ── Colors ────────────────────────────────────────────────────────────────────
BG_TOP     = (10, 40, 75)
BG_BOTTOM  = (5, 20, 45)
ACCENT     = (41, 171, 226)
WHITE      = (255, 255, 255)
LIGHT_GRAY = (200, 215, 230)

# ── Generate card ─────────────────────────────────────────────────────────────
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
        bg = bg.crop(((nw - W) // 2, (nh - H) // 2, (nw - W) // 2 + W, (nh - H) // 2 + H))
        bg = ImageEnhance.Brightness(bg).enhance(0.22)
        overlay = Image.new("RGB", (W, H), (8, 30, 65))
        img = Image.blend(bg, overlay, 0.55)
    else:
        d = ImageDraw.Draw(img)
        for y in range(H):
            t = y / H
            d.line([(0, y), (W, y)], fill=(
                int(BG_TOP[0] + (BG_BOTTOM[0] - BG_TOP[0]) * t),
                int(BG_TOP[1] + (BG_BOTTOM[1] - BG_TOP[1]) * t),
                int(BG_TOP[2] + (BG_BOTTOM[2] - BG_TOP[2]) * t),
            ))

    # Bottom dark gradient
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    for y in range(H // 2, H):
        t = (y - H // 2) / (H // 2)
        od.line([(0, y), (W, y)], fill=(5, 20, 50, int(210 * t)))
    img = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")

    # Top dark gradient
    ov2 = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od2 = ImageDraw.Draw(ov2)
    for y in range(0, 170):
        t = 1 - y / 170
        od2.line([(0, y), (W, y)], fill=(5, 20, 50, int(190 * t)))
    img = Image.alpha_composite(img.convert("RGBA"), ov2).convert("RGB")

    draw = ImageDraw.Draw(img)

    # Top accent line
    draw.rectangle([(0, 0), (W, 5)], fill=ACCENT)

    # Logo
    try:
        logo = Image.open("logo.png").convert("RGBA")
        lh = 72
        lw = int(logo.width * lh / logo.height)
        logo = logo.resize((lw, lh), Image.LANCZOS)
        img.paste(logo, (50, 38), logo)
    except Exception as e:
        log.warning(f"Logo: {e}")

    # Fonts
    try:
        f_tag   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22)
        f_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 46)
        f_body  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 27)
        f_sm    = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 21)
    except:
        f_tag = f_title = f_body = f_sm = ImageFont.load_default()

    # t.me/samugacommunity top right
    draw.text((W - 310, 50), "t.me/samugacommunity", font=f_sm, fill=(200, 230, 255))

    # LATEST NEWS tag
    tag_y = 590
    draw.rectangle([(50, tag_y), (222, tag_y + 34)], fill=ACCENT)
    draw.text((63, tag_y + 6), "● LATEST NEWS", font=f_tag, fill=WHITE)

    # Word wrap
    def wrap(text, font, max_w):
        words = text.split()
        lines, cur = [], ""
        for w in words:
            test = (cur + " " + w).strip()
            if draw.textbbox((0, 0), test, font=font)[2] <= max_w:
                cur = test
            else:
                if cur: lines.append(cur)
                cur = w
        if cur: lines.append(cur)
        return lines

    max_w = W - 100
    sentences = text.split('. ')
    headline  = sentences[0] + ('.' if len(sentences) > 1 else '')
    body      = '. '.join(sentences[1:]) if len(sentences) > 1 else ''

    y = tag_y + 48
    for line in wrap(headline, f_title, max_w)[:4]:
        draw.text((50, y), line, font=f_title, fill=WHITE)
        y += 56

    if body:
        y += 4
        for line in wrap(body, f_body, max_w)[:3]:
            draw.text((50, y), line, font=f_body, fill=LIGHT_GRAY)
            y += 36

    # Bottom bar
    draw.rectangle([(0, H - 78), (W, H)], fill=(3, 12, 30))
    draw.rectangle([(0, H - 78), (W, H - 75)], fill=ACCENT)
    draw.text((50, H - 53), f"Source: {source}", font=f_sm, fill=LIGHT_GRAY)
    draw.text((W - 260, H - 53), timestamp, font=f_sm, fill=LIGHT_GRAY)

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

# ── Breaking news keywords ───────────────────────────────────────────────────
BREAKING_KEYWORDS = [
    "breaking", "urgent", "alert", "killed", "dead", "explosion", "crash",
    "attack", "arrested", "emergency", "disaster", "flood", "fire", "missing",
    "tsunami", "earthquake", "accident", "murder", "terror", "bomb", "coup",
    "resign", "resign", "crisis", "leaked", "scandal", "raid"
]

def is_breaking(title, summary):
    text = (title + " " + summary).lower()
    return any(kw in text for kw in BREAKING_KEYWORDS)

def get_maldives_hour():
    """Get current hour in Maldives Time (UTC+5)"""
    utc_now = datetime.utcnow()
    mvt_hour = (utc_now.hour + 5) % 24
    return mvt_hour

def is_active_hours():
    """7:00 AM to 6:00 PM MVT = active (every 30 min)"""
    h = get_maldives_hour()
    return 7 <= h < 18

# ── Main job ──────────────────────────────────────────────────────────────────
def run_job():
    mvt_hour = get_maldives_hour()
    active   = is_active_hours()
    log.info(f"🕐 MVT hour: {mvt_hour:02d}:xx | Mode: {'ACTIVE (30min)' if active else 'NIGHT (3hr, breaking only)'}")
    log.info("🔍 Fetching news...")

    seen     = load_seen()
    articles = fetch_news()
    posted   = 0

    for article in articles:
        if article["id"] in seen:
            continue

        # Night mode: only post breaking news
        if not active and not is_breaking(article["title"], article["summary"]):
            log.info(f"⏭️ Skipping (night mode, not breaking): {article['title'][:50]}")
            continue

        log.info(f"📰 {article['title'][:70]}...")
        rewritten, keyword = rewrite_news(article["title"], article["summary"])

        log.info(f"🖼️ Image keyword: {keyword}")
        bg = fetch_background_image(keyword)

        ts   = datetime.now().strftime("%d %b %Y • %H:%M")
        card = generate_card(rewritten, article["source"], ts, bg)

        # Add 🚨 tag for breaking news at night
        breaking_tag = "🚨 <b>BREAKING</b>\n\n" if not active else ""
        caption = (
            f"{breaking_tag}<b>{article['title']}</b>\n\n"
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
        log.info("No new articles this run.")

# ── Smart scheduler ───────────────────────────────────────────────────────────
def scheduled_check():
    """Runs every 30 mins but skips night hours unless breaking news"""
    mvt_hour = get_maldives_hour()
    active   = is_active_hours()

    # Night mode: only run every 3 hours (at 18, 21, 00, 03, 06)
    if not active and mvt_hour not in [18, 21, 0, 3, 6]:
        log.info(f"💤 Night mode — skipping this 30min tick (MVT {mvt_hour:02d}:xx)")
        return

    run_job()

# ── Entry ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("🚀 Samuga News Bot starting...")
    log.info("📅 Schedule: 7AM-6PM MVT every 30min | 6PM-7AM MVT every 3hrs (breaking only)")
    run_job()
    scheduler = BlockingScheduler()
    scheduler.add_job(scheduled_check, "interval", minutes=30)
    log.info("⏰ Scheduler started")
    scheduler.start()
