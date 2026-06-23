import os, io, threading, time, logging, hashlib, json, feedparser, requests, anthropic, re
from PIL import Image, ImageDraw, ImageFont, ImageEnhance
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from apscheduler.schedulers.blocking import BlockingScheduler
from io import BytesIO

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "@samugacommunity")
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
PEXELS_API_KEY      = os.environ.get("PEXELS_API_KEY", "")
GEMINI_API_KEY      = os.environ.get("GEMINI_API_KEY", "")
TAVILY_API_KEY      = os.environ.get("TAVILY_API_KEY", "")
BOT_USERNAME        = os.environ.get("BOT_USERNAME", "SamugaNewsBot")
IMGBB_API_KEY       = os.environ.get("IMGBB_API_KEY", "")
BUFFER_TOKEN        = os.environ.get("BUFFER_ACCESS_TOKEN", "")
BUFFER_FB_ID        = os.environ.get("BUFFER_FACEBOOK_ID", "")
BUFFER_IG_ID        = os.environ.get("BUFFER_INSTAGRAM_ID", "")
BUFFER_TW_ID        = os.environ.get("BUFFER_TWITTER_ID", "")

ai = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── RSS Feeds ─────────────────────────────────────────────────────────────────
RSS_FEEDS = [
    {"url": "https://news.google.com/rss/search?q=maldives&hl=en-MV&gl=MV&ceid=MV:en", "cat": "LOCAL", "lang": "en"},
    {"url": "https://see.mv/feed", "cat": "LOCAL", "lang": "en"},
    {"url": "https://english.sun.mv/feed", "cat": "LOCAL", "lang": "en"},
    {"url": "https://edition.mv/feed", "cat": "LOCAL", "lang": "en"},
    {"url": "https://maldivesindependent.com/feed", "cat": "LOCAL", "lang": "en"},
    {"url": "https://oneonline.mv/en/feed", "cat": "LOCAL", "lang": "en"},
    {"url": "https://psmnews.mv/en/feed", "cat": "LOCAL", "lang": "en"},
    {"url": "https://mihaaru.com/rss", "cat": "LOCAL", "lang": "dv"},
    {"url": "https://avas.mv/feed", "cat": "LOCAL", "lang": "dv"},
    {"url": "https://news.google.com/rss/search?q=world+cup+2026+football&hl=en&gl=US&ceid=US:en", "cat": "FOOTBALL", "lang": "en"},
    {"url": "https://news.google.com/rss/search?q=champions+league+football&hl=en&gl=US&ceid=US:en", "cat": "FOOTBALL", "lang": "en"},
    {"url": "https://news.google.com/rss/search?q=war+conflict+breaking&hl=en&gl=US&ceid=US:en", "cat": "WORLD", "lang": "en"},
    {"url": "https://news.google.com/rss/search?q=earthquake+tsunami+volcanic+eruption&hl=en&gl=US&ceid=US:en", "cat": "DISASTER", "lang": "en"},
    {"url": "https://news.google.com/rss/search?q=maldives+accident+incident+breaking&hl=en&gl=MV&ceid=MV:en", "cat": "DISASTER", "lang": "en"},
    {"url": "https://news.google.com/rss/search?q=maldives+weather+storm&hl=en&gl=US&ceid=US:en", "cat": "WEATHER", "lang": "en"},
    {"url": "https://news.google.com/rss/search?q=maldives+tourism+travel&hl=en&gl=US&ceid=US:en", "cat": "TOURISM", "lang": "en"},
]

CAT_CONFIG = {
    "LOCAL":   {"label": "🇲🇻  LOCAL NEWS",   "color": (41,171,226)},
    "FOOTBALL":{"label": "⚽  FOOTBALL",       "color": (34,180,80)},
    "WORLD":   {"label": "🌍  WORLD NEWS",     "color": (220,80,60)},
    "DISASTER":{"label": "🚨  DISASTER ALERT", "color": (220,120,0)},
    "WEATHER": {"label": "🌤️  WEATHER",        "color": (100,180,240)},
    "TOURISM": {"label": "✈️  TOURISM",        "color": (160,80,220)},
}

# ── Core Team Session Context (in-memory only, clears on restart) ────────────
core_team_session_context = {}  # user_id -> stored context

# ── Dhivehi Approval Queue (in-memory) ───────────────────────────────────────
dhivehi_pending = {}  # key -> {card_buf, caption, article_title, link}
dhivehi_pending_counter = [0]

def store_pending_dhivehi(card_buf, dv_text, title, link, keyword="maldives news", source="LOCAL", cat="LOCAL"):
    dhivehi_pending_counter[0] += 1
    key = f"dv{dhivehi_pending_counter[0]}"
    dhivehi_pending[key] = {
        "dv_text": dv_text,
        "title": title,
        "link": link,
        "keyword": keyword,
        "source": source,
        "cat": cat,
    }
    if len(dhivehi_pending) > 20:
        oldest = list(dhivehi_pending.keys())[0]
        del dhivehi_pending[oldest]
    return key

# ── Core Team Config ──────────────────────────────────────────────────────────
CORE_TEAM_CHAT_ID = "-1002829230299"

CORE_TEAM_MEMBERS = {
    "manchii": {"name": "Manchii", "full": "Abdul Muhsin", "role": "Founder & MD", "notes": "Big ideas, entrepreneur, boss, loves to push boundaries"},
    "mutte":   {"name": "Manchii", "full": "Abdul Muhsin", "role": "Founder & MD", "notes": "Big ideas, entrepreneur, boss, loves to push boundaries"},
    "uly":     {"name": "Uly", "full": "Mariyam Ulya", "role": "Co-Founder & Editor-in-Chief", "notes": "Journalist brain, editorial standards, keeps content sharp"},
    "ulya":    {"name": "Uly", "full": "Mariyam Ulya", "role": "Co-Founder & Editor-in-Chief", "notes": "Journalist brain, editorial standards, keeps content sharp"},
    "thooma":  {"name": "Thooma", "full": "Aminath Thooma", "role": "Presenter & Marketing Assistant", "notes": "Content face, presenter energy, needs confidence boosts sometimes"},
    "kit":     {"name": "Kity", "full": "Kit", "role": "Manchii's wife & idea contributor", "notes": "Creative, boosts team morale, great at boosting Thooma, shares fresh ideas"},
    "kity":    {"name": "Kity", "full": "Kit", "role": "Manchii's wife & idea contributor", "notes": "Creative, boosts team morale, great at boosting Thooma, shares fresh ideas"},
}

CORE_TEAM_PROACTIVE_TRIGGERS = [
    "?", "idea", "what do you think", "thoughts", "suggest", "brainstorm",
    "samuga", "content", "post", "story", "plan", "strategy", "marketing",
    "tiktok", "instagram", "facebook", "caption", "script", "video", "reel",
    "haha", "lol", "😂", "anyone", "guys", "let's", "lets", "what if", "how about"
]

BREAKING_KEYWORDS = [
    "breaking","urgent","alert","killed","dead","dies","explosion","crash","attack",
    "arrested","emergency","disaster","flood","fire","missing","tsunami","earthquake",
    "accident","murder","bomb","resign","crisis","leaked","scandal","raid","collapse",
    "shot","war","strike","invasion","hostage","trapped","sinking"
]

# ── Storage ───────────────────────────────────────────────────────────────────
DATA_DIR  = "/data"
os.makedirs(DATA_DIR, exist_ok=True)
SEEN_FILE = os.path.join(DATA_DIR, "seen_articles.json")

def load_seen():
    try:
        if os.path.exists(SEEN_FILE):
            with open(SEEN_FILE) as f: return set(json.load(f))
    except: pass
    return set()

def save_seen(seen):
    try:
        with open(SEEN_FILE,"w") as f: json.dump(list(seen)[-1000:], f)
    except: pass

# ── Memory ────────────────────────────────────────────────────────────────────
recent_posts = []
user_conversations = {}

# Analytics counters (reset weekly)
analytics = {"posts_by_cat": {}, "breaking_count": 0, "social_success": 0, "social_fail": 0, "week_start": None}

def track_analytics(cat, is_breaking=False, social_ok=None):
    global analytics
    from datetime import timezone as _tz
    week = (datetime.now(_tz.utc) + timedelta(hours=5)).isocalendar()[1]
    if analytics["week_start"] != week:
        analytics = {"posts_by_cat": {}, "breaking_count": 0, "social_success": 0, "social_fail": 0, "week_start": week}
    if cat != "SOCIAL":
        analytics["posts_by_cat"][cat] = analytics["posts_by_cat"].get(cat, 0) + 1
    if is_breaking: analytics["breaking_count"] += 1
    if social_ok is True: analytics["social_success"] += 1
    if social_ok is False: analytics["social_fail"] += 1

def remember_post(title, cat, timestamp):
    recent_posts.append({"title":title,"cat":cat,"time":timestamp})
    if len(recent_posts) > 50: recent_posts.pop(0)
    track_analytics(cat)

def get_conversation(uid):
    if uid not in user_conversations: user_conversations[uid] = []
    return user_conversations[uid]

def add_to_conversation(uid, role, content):
    conv = get_conversation(uid)
    conv.append({"role":role,"content":content})
    if len(conv) > 10: user_conversations[uid] = conv[-10:]

# ── Helpers ───────────────────────────────────────────────────────────────────
def get_mvt_hour(): return (datetime.utcnow().hour + 5) % 24
def is_day_mode(): return 7 <= get_mvt_hour() < 18

def is_fresh(entry, hours=24):
    try:
        pub = entry.get("published","")
        if pub:
            dt = parsedate_to_datetime(pub)
            if dt.tzinfo: dt = dt.replace(tzinfo=None)
            return datetime.utcnow() - dt < timedelta(hours=hours)
    except: pass
    return True

def is_breaking(title, summary="", cat=""):
    return any(kw in (title+" "+summary).lower() for kw in BREAKING_KEYWORDS) or cat=="DISASTER"

last_regular_post_time = None
def can_post_regular():
    global last_regular_post_time
    if not last_regular_post_time: return True
    return (datetime.utcnow()-last_regular_post_time).total_seconds() >= 7200

# ── Social post daily counter (MVT based) ─────────────────────────────────────
social_post_counts = {"date": None, "count": 0}

def mvt_now():
    """Current time in Maldives Time (UTC+5)"""
    from datetime import timezone
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=5)

def is_day_social():
    """6AM to 10PM MVT = day mode for socials"""
    h = mvt_now().hour
    return 6 <= h < 22

def can_post_social():
    """Check if social daily limit not reached: 20 posts 6AM-10PM, 5 posts night"""
    global social_post_counts
    today = mvt_now().date()
    if social_post_counts["date"] != today:
        social_post_counts = {"date": today, "count": 0}
    limit = 30 if is_day_social() else 5
    return social_post_counts["count"] < limit

def increment_social_count():
    global social_post_counts
    today = mvt_now().date()
    if social_post_counts["date"] != today:
        social_post_counts = {"date": today, "count": 0}
    social_post_counts["count"] += 1
    log.info(f"📊 Social posts today: {social_post_counts['count']} ({'day' if is_day_social() else 'night'} limit: {30 if is_day_social() else 5})")

# ── Gemini Translate ──────────────────────────────────────────────────────────
def gemini_translate(text):
    if not GEMINI_API_KEY: return text
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={GEMINI_API_KEY}"
        resp = requests.post(url, json={"contents":[{"parts":[{"text":f"Translate this Dhivehi text to English. Return ONLY the English translation:\n\n{text}"}]}]}, timeout=15)
        if resp.status_code == 200:
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e: log.error(f"Gemini: {e}")
    return text

# ── Fetch News ────────────────────────────────────────────────────────────────
def fetch_news():
    articles, seen_titles = [], set()
    for fc in RSS_FEEDS:
        try:
            feed = feedparser.parse(fc["url"])
            for entry in feed.entries[:10]:
                title   = entry.get("title","")
                summary = entry.get("summary", title)
                if fc["lang"] == "dv":
                    title   = gemini_translate(title)
                    summary = gemini_translate(summary[:300])
                key = title.lower()[:50]
                if key in seen_titles or not is_fresh(entry): continue
                seen_titles.add(key)
                articles.append({
                    "id": hashlib.md5(entry.get("link",title).encode()).hexdigest(),
                    "title": title, "summary": summary,
                    "link": entry.get("link",""), "cat": fc["cat"],
                    "source": entry.get("source",{}).get("title", fc["cat"]),
                })
        except Exception as e: log.error(f"Feed error: {e}")
    log.info(f"Found {len(articles)} fresh articles")
    return articles

def get_local_headlines():
    headlines = []
    try:
        for fc in RSS_FEEDS[:5]:
            feed = feedparser.parse(fc["url"])
            for entry in feed.entries[:3]:
                title = entry.get("title","")
                if title and is_fresh(entry, hours=12):
                    headlines.append(f"• [{fc['cat']}] {title}")
            if len(headlines) >= 10: break
    except: pass
    return headlines[:10]

# ── Rewrite with Claude ───────────────────────────────────────────────────────
DEFAULT_KEYWORDS = {"LOCAL":"maldives government","FOOTBALL":"football stadium","WORLD":"world politics","DISASTER":"emergency rescue","WEATHER":"tropical weather","TOURISM":"maldives resort beach"}

def rewrite_news(title, summary, cat):
    cat_ctx = {"LOCAL":"local Maldivian news","FOOTBALL":"football news","WORLD":"world news","DISASTER":"disaster/emergency","WEATHER":"weather news","TOURISM":"tourism news"}.get(cat,"news")
    extra = "Note: Only headline available. Expand with relevant context." if not summary or summary.strip()==title.strip() or len(summary)<30 else ""
    prompt = f"""You are a news writer for Samuga Media, a Maldivian digital media outlet.
Rewrite this {cat_ctx} into a short punchy engaging English Telegram post.
- Max 3 sentences, clear and direct, no hashtags, no emojis, professional
- IMPORTANT: Use gender-neutral terms (they/their, "the accused", "the suspect", "the individual") unless the original text explicitly states gender. Do not assume gender from names.
{extra}
Also give a specific 2-3 word Pexels image keyword for this topic.

Title: {title}
Summary: {summary}

Respond EXACTLY:
TEXT: [rewritten news]
IMAGE: [specific keyword]"""
    try:
        msg = ai.messages.create(model="claude-haiku-4-5-20251001", max_tokens=400, messages=[{"role":"user","content":prompt}])
        text, kw = "", DEFAULT_KEYWORDS.get(cat,"maldives")
        for line in msg.content[0].text.strip().split('\n'):
            if line.startswith("TEXT:"): text = line[5:].strip()
            elif line.startswith("IMAGE:"): kw = line[6:].strip()
        return (text or title), kw
    except Exception as e:
        log.error(f"Claude rewrite: {e}")
        return title, DEFAULT_KEYWORDS.get(cat,"maldives")

# ── Pexels ────────────────────────────────────────────────────────────────────
def fetch_background_image(keyword):
    if not PEXELS_API_KEY: return None
    try:
        resp = requests.get(f"https://api.pexels.com/v1/search?query={keyword}&per_page=5&orientation=square", headers={"Authorization":PEXELS_API_KEY}, timeout=15)
        if resp.status_code == 200:
            photos = resp.json().get("photos",[])
            if photos:
                r = requests.get(photos[0]["src"]["large"], timeout=20)
                if r.status_code == 200:
                    log.info(f"✅ Pexels: {keyword}")
                    return Image.open(BytesIO(r.content)).convert("RGB")
    except Exception as e: log.error(f"Pexels: {e}")
    return None

# ── Generate Card ─────────────────────────────────────────────────────────────
WHITE=(255,255,255); LIGHT_GRAY=(200,215,230); BG_TOP=(10,40,75); BG_BOTTOM=(5,20,45)

def generate_card(text, source, timestamp, cat, bg_image=None, morning=False):
    W, H = 1080, 1080
    accent = (255,180,0) if morning else CAT_CONFIG.get(cat,CAT_CONFIG["LOCAL"])["color"]
    label  = "🌅  MORNING BRIEF" if morning else CAT_CONFIG.get(cat,CAT_CONFIG["LOCAL"])["label"]

    img = Image.new("RGB",(W,H),BG_TOP)
    if bg_image:
        bg = bg_image.copy()
        r = bg.width/bg.height
        nh,nw = (H,int(H*r)) if r>1 else (int(W/r),W)
        bg = bg.resize((nw,nh),Image.LANCZOS).crop(((nw-W)//2,(nh-H)//2,(nw-W)//2+W,(nh-H)//2+H))
        bg = ImageEnhance.Brightness(bg).enhance(0.22)
        img = Image.blend(bg, Image.new("RGB",(W,H),(8,30,65)), 0.55)
    else:
        d = ImageDraw.Draw(img)
        for y in range(H):
            t=y/H
            d.line([(0,y),(W,y)],fill=(int(BG_TOP[0]+(BG_BOTTOM[0]-BG_TOP[0])*t),int(BG_TOP[1]+(BG_BOTTOM[1]-BG_TOP[1])*t),int(BG_TOP[2]+(BG_BOTTOM[2]-BG_TOP[2])*t)))

    ov=Image.new("RGBA",(W,H),(0,0,0,0)); od=ImageDraw.Draw(ov)
    for y in range(H//2,H):
        t=(y-H//2)/(H//2); od.line([(0,y),(W,y)],fill=(5,20,50,int(215*t)))
    img=Image.alpha_composite(img.convert("RGBA"),ov).convert("RGB")

    ov2=Image.new("RGBA",(W,H),(0,0,0,0)); od2=ImageDraw.Draw(ov2)
    for y in range(0,170):
        t=1-y/170; od2.line([(0,y),(W,y)],fill=(5,20,50,int(190*t)))
    img=Image.alpha_composite(img.convert("RGBA"),ov2).convert("RGB")

    draw=ImageDraw.Draw(img)
    draw.rectangle([(0,0),(W,5)],fill=accent)

    try:
        logo=Image.open("logo.png").convert("RGBA")
        lh=72; lw=int(logo.width*lh/logo.height)
        logo=logo.resize((lw,lh),Image.LANCZOS)
        img.paste(logo,(50,38),logo)
    except: pass

    try:
        f_tag  =ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",22)
        f_title=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",46)
        f_body =ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",27)
        f_sm   =ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",21)
    except: f_tag=f_title=f_body=f_sm=ImageFont.load_default()

    draw.text((W-310,50),"t.me/samugacommunity",font=f_sm,fill=(200,230,255))
    tag_y=590; tw=draw.textbbox((0,0),label,font=f_tag)[2]+26
    draw.rectangle([(50,tag_y),(50+tw,tag_y+34)],fill=accent)
    draw.text((63,tag_y+6),label,font=f_tag,fill=WHITE if not morning else (0,0,0))

    def wrap(t,f,mw):
        words=t.split(); lines,cur=[],""
        for w in words:
            test=(cur+" "+w).strip()
            if draw.textbbox((0,0),test,font=f)[2]<=mw: cur=test
            else:
                if cur: lines.append(cur)
                cur=w
        if cur: lines.append(cur)
        return lines

    sentences=text.split('. ')
    headline=sentences[0]+('.' if len(sentences)>1 else '')
    body='. '.join(sentences[1:]) if len(sentences)>1 else ''

    y=tag_y+48
    for line in wrap(headline,f_title,W-100)[:4]:
        draw.text((50,y),line,font=f_title,fill=WHITE); y+=56
    if body:
        y+=4
        for line in wrap(body,f_body,W-100)[:3]:
            draw.text((50,y),line,font=f_body,fill=LIGHT_GRAY); y+=36

    draw.rectangle([(0,H-78),(W,H)],fill=(3,12,30))
    draw.rectangle([(0,H-78),(W,H-75)],fill=accent)
    draw.text((50,H-53),f"Source: {source}",font=f_sm,fill=LIGHT_GRAY)
    draw.text((W-260,H-53),timestamp,font=f_sm,fill=LIGHT_GRAY)

    buf=BytesIO(); img.save(buf,format="PNG",quality=95); buf.seek(0)
    return buf

# ── Telegram ──────────────────────────────────────────────────────────────────
def send_to_telegram(buf, caption):
    try:
        resp=requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
            data={"chat_id":TELEGRAM_CHANNEL_ID,"caption":caption,"parse_mode":"HTML"},
            files={"photo":("card.png",buf,"image/png")},timeout=30)
        resp.raise_for_status(); log.info("✅ Posted to Telegram"); return True
    except Exception as e: log.error(f"Telegram: {e}"); return False

def download_telegram_photo(photo_list):
    """Download the highest quality photo from a Telegram photo array"""
    try:
        # Get largest photo (last in list)
        file_id = photo_list[-1]["file_id"]
        # Get file path
        resp = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile",
            params={"file_id": file_id}, timeout=10
        )
        file_path = resp.json()["result"]["file_path"]
        # Download the file
        img_resp = requests.get(
            f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}",
            timeout=20
        )
        from PIL import Image
        img = Image.open(io.BytesIO(img_resp.content)).convert("RGB")
        log.info("✅ Telegram photo downloaded — returning PIL Image for card generation")
        return img  # generate_card expects PIL Image, not BytesIO
    except Exception as e:
        log.error(f"Photo download: {e}")
        return None

def send_photo(chat_id, buf, caption):
    """Send a photo to any Telegram chat/channel"""
    try:
        buf.seek(0)
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
            data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
            files={"photo": ("weather.png", buf, "image/png")},
            timeout=30
        )
        resp.raise_for_status()
        log.info("✅ Photo sent to Telegram")
        return True
    except Exception as e:
        log.error(f"send_photo: {e}")
        return False

def send_text(chat_id, text, reply_to=None, thread_id=None):
    payload={"chat_id":chat_id,"text":text,"parse_mode":"HTML"}
    if reply_to: payload["reply_to_message_id"]=reply_to
    if thread_id: payload["message_thread_id"]=thread_id
    try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",json=payload,timeout=15)
    except Exception as e: log.error(f"Send text: {e}")

# ── Gemini Dhivehi Caption ────────────────────────────────────────────────────
def make_dhivehi_caption(english_text, title):
    """Convert English news caption to Dhivehi using Gemini"""
    if not GEMINI_API_KEY:
        return None
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={GEMINI_API_KEY}"
        prompt = f"""You are a Maldivian news writer. Write a short news caption in Dhivehi (Thaana script) for this news.

English title: {title}
English summary: {english_text}

Write 2-3 sentences in natural Dhivehi as it would appear in a Maldivian news channel.
Return ONLY the Dhivehi text, nothing else."""

        resp = requests.post(url, json={"contents":[{"parts":[{"text":prompt}]}]}, timeout=15)
        if resp.status_code == 200:
            dv_text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            log.info(f"✅ Gemini Dhivehi caption done")
            return dv_text
    except Exception as e:
        log.error(f"Gemini Dhivehi caption: {e}")
    return None

# ── Auto Poll ─────────────────────────────────────────────────────────────────
POLL_KEYWORDS = [
    "government","president","parliament","minister","policy","law","vote","election",
    "decision","budget","tax","fee","regulation","announce","reform","appointed",
    "resign","fired","arrested","court","judge","sentence","verdict","accused",
    "protest","rally","strike","ban","approve","reject","pass","failed"
]

def should_create_poll(title, summary, cat):
    """Check if news warrants a poll"""
    if cat not in ["LOCAL", "WORLD"]: return False
    text = (title + " " + summary).lower()
    return any(kw in text for kw in POLL_KEYWORDS)

def generate_poll_question(title, rewritten):
    """Use Claude to generate a relevant poll question"""
    try:
        msg = ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role":"user","content":f"""Based on this news, create a simple Telegram poll.

News: {title}
Summary: {rewritten}

Return EXACTLY in this format (nothing else):
QUESTION: [one short poll question in English]
OPT1: [option 1, max 4 words]
OPT2: [option 2, max 4 words]
OPT3: [option 3, max 4 words]

Keep it simple, neutral and relevant to the news."""}]
        )
        text = msg.content[0].text.strip()
        question, options = "", []
        for line in text.split('\n'):
            if line.startswith("QUESTION:"): question = line[9:].strip()
            elif line.startswith("OPT"): options.append(line.split(":",1)[1].strip())
        return question, options[:3]
    except Exception as e:
        log.error(f"Poll generation: {e}")
        return None, []

def send_poll(question, options):
    """Send a Telegram poll to the channel"""
    if not question or len(options) < 2:
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPoll",
            json={
                "chat_id": TELEGRAM_CHANNEL_ID,
                "question": f"🗳️ {question}",
                "options": options,
                "is_anonymous": True,
            },
            timeout=15
        )
        if resp.status_code == 200:
            log.info(f"✅ Poll sent: {question[:50]}")
        else:
            log.error(f"Poll error: {resp.status_code}")
    except Exception as e:
        log.error(f"Poll send: {e}")

# ── Buffer / Social ───────────────────────────────────────────────────────────
def upload_to_imgbb(img_bytes):
    if not IMGBB_API_KEY: return None
    try:
        import base64
        resp=requests.post("https://api.imgbb.com/1/upload",data={"key":IMGBB_API_KEY,"image":base64.b64encode(img_bytes).decode()},timeout=20)
        if resp.status_code==200:
            url=resp.json()["data"]["url"]; log.info(f"✅ imgbb: {url[:50]}"); return url
    except Exception as e: log.error(f"imgbb: {e}")
    return None

def resolve_url(url):
    """Follow redirects to get real URL (fixes Google News RSS links)"""
    if not url: return url
    try:
        if "news.google.com" in url or "feedproxy" in url:
            r = requests.get(url, allow_redirects=True, timeout=10)
            log.info(f"🔗 Resolved: {r.url[:80]}")
            return r.url
    except Exception as e:
        log.warning(f"URL resolve failed: {e}")
    return url

def post_to_buffer(image_url, caption, channel_id, metadata=None):
    """Post to a single Buffer channel. Returns True on success."""
    if not BUFFER_TOKEN or not channel_id: return False
    clean = re.sub(r'<[^>]+>', '', caption).replace('&amp;', '&').strip()

    query = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    ... on PostActionSuccess { post { id text } }
    ... on MutationError { message }
  }
}"""

    post_input = {
        "text": clean,
        "channelId": channel_id,
        "schedulingType": "automatic",
        "mode": "shareNow",
        "assets": [{"image": {"url": image_url}}],
    }
    if metadata:
        post_input["metadata"] = metadata

    try:
        resp = requests.post(
            "https://api.buffer.com",
            json={"query": query, "variables": {"input": post_input}},
            headers={"Authorization": f"Bearer {BUFFER_TOKEN}", "Content-Type": "application/json"},
            timeout=20
        )
        log.info(f"Buffer raw [{channel_id[:8]}]: {resp.status_code} | {resp.text[:400]}")
        if resp.status_code == 200:
            data = resp.json()
            if "errors" in data:
                log.error(f"Buffer GraphQL error [{channel_id[:8]}]: {data['errors']}")
                return False
            result = data.get("data", {}).get("createPost", {})
            err_msg = result.get("message", "")
            if err_msg:
                log.error(f"Buffer mutation error [{channel_id[:8]}]: {err_msg}")
                return False
            post_id = result.get("post", {}).get("id", "?")
            log.info(f"✅ Buffer posted [{channel_id[:8]}] id={post_id}")
            return True
        else:
            log.error(f"Buffer HTTP {resp.status_code}: {resp.text[:300]}")
    except Exception as e:
        log.error(f"Buffer exception [{channel_id[:8]}]: {e}")
    return False

def post_to_social(img_buf, caption):
    if not BUFFER_TOKEN:
        log.warning("Social: no BUFFER_TOKEN, skipping")
        return
    if not can_post_social():
        limit = 30 if is_day_social() else 5
        log.info(f"📵 Social limit reached ({30 if is_day_social() else 5} posts {'day' if is_day_social() else 'night'}) — skipping")
        return
    try:
        img_bytes = img_buf.getvalue()
        image_url = upload_to_imgbb(img_bytes)
        if not image_url:
            log.error("Social: imgbb upload failed, skipping")
            return

        # Strip HTML for all social platforms
        clean = re.sub(r'<[^>]+>', '', caption).replace('&amp;', '&').strip()

        # Extract and resolve article URL (fixes Google News redirects)
        link_match = re.search(r"href='([^']+)'", caption)
        raw_url = link_match.group(1) if link_match else ""
        article_url = resolve_url(raw_url) if raw_url else ""

        # FB/IG: full text + real link
        fb_ig = clean
        if article_url and article_url not in fb_ig:
            fb_ig = fb_ig + "\n\n" + article_url
        fb_ig = fb_ig[:2200]

        # Twitter: first line + link (280 char hard limit)
        lines = [l.strip() for l in clean.split('\n') if l.strip()]
        tw = (lines[0] if lines else clean)[:220]
        if article_url:
            tw = tw + "\n\n" + article_url
        tw = tw[:280]

        results = {}
        for cid, cap, name, meta in [
            (BUFFER_FB_ID, fb_ig, "Facebook",  {"facebook":  {"type": "post"}}),
            (BUFFER_IG_ID, fb_ig, "Instagram", {"instagram": {"type": "post", "shouldShareToFeed": True}}),
            (BUFFER_TW_ID, tw,    "Twitter",   None),
        ]:
            if not cid:
                log.warning(f"Social: skipping {name} — no channel ID set")
                continue
            results[name] = post_to_buffer(image_url, cap, cid, metadata=meta)
            time.sleep(2)

        ok = [k for k, v in results.items() if v]
        fail = [k for k, v in results.items() if not v]
        if ok:
            log.info(f"✅ Social posted to: {', '.join(ok)}")
            increment_social_count()
            track_analytics("SOCIAL", social_ok=True)
        if fail:
            log.error(f"❌ Social failed for: {', '.join(fail)}")
            track_analytics("SOCIAL", social_ok=False)
    except Exception as e:
        log.error(f"Social: {e}")

# ── Post Article ──────────────────────────────────────────────────────────────
def post_article(article, seen, social_only=False, allow_social=True):
    global last_regular_post_time
    cat=article["cat"]
    breaking=is_breaking(article["title"],article["summary"],cat)

    rewritten, keyword = rewrite_news(article["title"],article["summary"],cat)
    bg = fetch_background_image(keyword)
    ts = datetime.now().strftime("%d %b %Y • %H:%M")
    card = generate_card(rewritten, article["source"], ts, cat, bg)

    cat_emoji={"LOCAL":"🇲🇻","FOOTBALL":"⚽","WORLD":"🌍","DISASTER":"🚨","WEATHER":"🌤️","TOURISM":"✈️"}.get(cat,"📰")
    breaking_tag="🚨 <b>BREAKING NEWS</b>\n\n" if breaking else ""

    caption = (f"{breaking_tag}{cat_emoji} <b>{article['title']}</b>\n\n"
               f"{rewritten}\n\n"
               f"🔗 <a href='{article['link']}'>Read more</a>\n\n"
               f"📡 <b>Samuga Media</b> | @samugacommunity")

    # Dhivehi workflow — for dv sources, generate Dhivehi TEXT and send to core team for review
    # NO card created yet — card only made after team approves
    is_dv_source = article.get("lang") == "dv"
    if is_dv_source and CORE_TEAM_CHAT_ID:
        try:
            dv_text = make_dhivehi_caption(rewritten, article["title"])
            if dv_text:
                key = store_pending_dhivehi(None, dv_text, article["title"], article["link"], keyword=keyword, source=article.get("source","LOCAL"), cat=cat)
                approval_msg = (
                    "🇲🇻 <b>Dhivehi Writing — Review Needed</b>\n\n"
                    f"<b>Article:</b> {article['title']}\n\n"
                    "<b>Bot wrote:</b>\n"
                    f"{dv_text}\n\n"
                    f"<b>Key:</b> <code>{key}</code>\n\n"
                    f"✅ Approve: <code>@SamugaNewsBot post {key}</code>\n"
                    f"✏️ Correct: <code>@SamugaNewsBot post {key} ތިޔަ ތަން ރަނގަޅު ކޮށްފައި...</code>\n"
                    f"❌ Reject: <code>@SamugaNewsBot reject {key}</code>"
                )
                send_text(CORE_TEAM_CHAT_ID, approval_msg)
                log.info(f"📨 Dhivehi text sent to core team for review: {key}")
        except Exception as e:
            log.error(f"Dhivehi workflow: {e}")

    social_caption = caption

    # Social only (night mode) — always post to socials, skip Telegram
    if social_only:
        if allow_social:
            card.seek(0)
            threading.Thread(target=post_to_social, args=(card, social_caption), daemon=True).start()
        seen.add(article["id"]); save_seen(seen); remember_post(article["title"], cat, ts)
        log.info(f"📱 Social only [{cat}]"); return True

    # Telegram throttle for regular posts — still post to socials every 30min
    if not breaking and not can_post_regular():
        mins = int((7200 - (datetime.utcnow() - last_regular_post_time).total_seconds()) / 60)
        log.info(f"⏳ [{cat}] Telegram throttled {mins}m — posting to socials only")
        card.seek(0)
        threading.Thread(target=post_to_social, args=(card, social_caption), daemon=True).start()
        seen.add(article["id"]); save_seen(seen); remember_post(article["title"], cat, ts)
        return False

    # Day mode — post to Telegram + socials
    log.info(f"📰 [{'🔴BREAKING' if breaking else '🟡REGULAR'}][{cat}] {article['title'][:60]}...")
    if send_to_telegram(card, caption):
        seen.add(article["id"]); save_seen(seen); remember_post(article["title"], cat, ts)
        if not breaking:
            last_regular_post_time = datetime.utcnow()
            log.info("🕐 Regular timer reset — next in 2hrs")
        else:
            log.info("🔴 Breaking posted!")
        card.seek(0)
        threading.Thread(target=post_to_social, args=(card, social_caption), daemon=True).start()

        # Auto poll for political/government news
        if should_create_poll(article["title"], article["summary"], cat):
            log.info("🗳️ Generating poll...")
            question, options = generate_poll_question(article["title"], rewritten)
            if question and options:
                time.sleep(3)
                send_poll(question, options)

        return True
    # Telegram failed — still post to socials
    log.warning(f"Telegram failed for [{cat}] — posting to socials anyway")
    card.seek(0)
    threading.Thread(target=post_to_social, args=(card, social_caption), daemon=True).start()
    seen.add(article["id"]); save_seen(seen)
    return False

# ── Run Job ───────────────────────────────────────────────────────────────────
def score_article(a):
    """Score article by Maldives relevance + breaking priority"""
    score = 0
    title_lower = a["title"].lower()
    summary_lower = a.get("summary","").lower()
    cat = a["cat"]
    # Category priority
    if cat == "LOCAL": score += 50
    elif cat == "DISASTER": score += 40
    elif cat == "WEATHER": score += 30
    elif cat == "TOURISM": score += 20
    elif cat == "WORLD": score += 10
    elif cat == "FOOTBALL": score += 5
    # Maldives keywords boost
    mv_kws = ["maldives","male","dhivehi","raajje","mvr","atoll","island","resort","gaa",
              "parliament","majlis","president","minister","police","court","malé"]
    for kw in mv_kws:
        if kw in title_lower or kw in summary_lower: score += 15
    # Breaking boost
    if is_breaking(a["title"], a.get("summary",""), cat): score += 60
    return score

def run_job(social_only=False):
    h=get_mvt_hour()
    log.info(f"🕐 MVT {h:02d}:xx | {'SOCIAL ONLY' if social_only else 'DAY' if is_day_mode() else 'NIGHT'}")
    seen=load_seen(); articles=fetch_news()

    # Filter unseen and score by Maldives relevance
    fresh = [a for a in articles if a["id"] not in seen]
    if not fresh:
        log.info("No fresh articles."); return

    # Sort by score descending
    fresh.sort(key=score_article, reverse=True)

    breaking_articles = [a for a in fresh if is_breaking(a["title"], a.get("summary",""), a["cat"])]
    regular_articles  = [a for a in fresh if not is_breaking(a["title"], a.get("summary",""), a["cat"])]

    log.info(f"🔴 {len(breaking_articles)} breaking | 🟡 {len(regular_articles)} regular")

    posted = 0
    social_posted = 0
    MAX_SOCIAL_PER_RUN = 2  # max 2 social posts per 30min check

    # Post breaking first
    for a in breaking_articles:
        can_social = social_posted < MAX_SOCIAL_PER_RUN
        if post_article(a, seen, social_only, allow_social=can_social):
            posted += 1
            social_posted += 1
        time.sleep(10)

    # Then best regular articles (dedupe by category — one per cat)
    seen_cats = set()
    for a in regular_articles:
        if a["cat"] in seen_cats: continue
        seen_cats.add(a["cat"])
        can_social = social_posted < MAX_SOCIAL_PER_RUN
        if post_article(a, seen, social_only, allow_social=can_social):
            posted += 1
            social_posted += 1
        if posted > 1: time.sleep(300)

    log.info(f"✅ Posted {posted} articles ({social_posted} to socials).")

def breaking_news_check():
    """Fast check every 5 min — Maldives breaking news only, no throttle"""
    try:
        seen = load_seen()
        articles = fetch_news()
        for a in articles:
            if a["id"] in seen: continue
            if a["cat"] not in ["LOCAL", "DISASTER"]: continue
            if not is_breaking(a["title"], a.get("summary",""), a["cat"]): continue
            # Score for Maldives relevance
            if score_article(a) < 60: continue
            log.info(f"🔴 BREAKING FAST: {a['title'][:60]}")
            post_article(a, seen, social_only=False, allow_social=True)
            break  # one at a time
    except Exception as e:
        log.error(f"Breaking check: {e}")

def scheduled_check():
    h=get_mvt_hour()
    if not is_day_mode() and h not in [18,21,0,3,6]:
        log.info(f"💤 Night (MVT {h:02d}:xx) — social only")
        run_job(social_only=True); return
    run_job()

# ── Morning Brief (7AM MVT) ───────────────────────────────────────────────────
def send_morning_brief():
    log.info("🌅 Morning brief...")
    try:
        headlines=get_local_headlines()
        if not headlines: return
        # Inject actual MVT date so Claude never hallucinates it
        from datetime import timezone, timedelta
        mvt = datetime.now(timezone.utc) + timedelta(hours=5)
        today_str = mvt.strftime("%A, %d %B %Y")
        prompt = f"""Create a warm "Good Morning Maldives 🌅" news brief for @samugacommunity.
Today's date is {today_str} (Maldives Time). Use this exact date in your greeting.
Headlines: {chr(10).join(headlines[:8])}
- Friendly greeting mentioning today's date exactly as given above
- Top 3-5 stories in 1 sentence each with emoji  
- Upbeat closing
- Max 180 words, English"""
        msg=ai.messages.create(model="claude-haiku-4-5-20251001",max_tokens=400,messages=[{"role":"user","content":prompt}])
        brief=msg.content[0].text.strip()
        caption=f"🌅 <b>Good Morning Maldives!</b>\n\n{brief}\n\n📡 <b>Samuga Media</b> | @samugacommunity"
        send_text(TELEGRAM_CHANNEL_ID, caption)
        log.info("✅ Morning brief sent!")
    except Exception as e: log.error(f"Morning brief: {e}")

# ── Weather Card ──────────────────────────────────────────────────────────────
def get_weather_data():
    """Fetch real-time weather for Male, Maldives via Open-Meteo (free, no key needed)"""
    try:
        url = ("https://api.open-meteo.com/v1/forecast"
               "?latitude=4.1755&longitude=73.5093"
               "&current=temperature_2m,weathercode,windspeed_10m,relativehumidity_2m,apparent_temperature"
               "&hourly=temperature_2m,weathercode,precipitation_probability"
               "&daily=temperature_2m_max,temperature_2m_min,sunrise,sunset,weathercode"
               "&timezone=Indian%2FMaldives&forecast_days=1")
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        log.error(f"Weather fetch: {e}")
    return None

def weather_code_to_info(code):
    """Convert WMO weather code to emoji + description"""
    if code == 0:   return "☀️", "Clear Sky"
    if code in [1,2]: return "🌤️", "Partly Cloudy"
    if code == 3:   return "☁️", "Overcast"
    if code in [45,48]: return "🌫️", "Foggy"
    if code in [51,53,55]: return "🌦️", "Drizzle"
    if code in [61,63,65]: return "🌧️", "Rain"
    if code in [71,73,75]: return "🌨️", "Snow"
    if code in [80,81,82]: return "🌧️", "Rain Showers"
    if code in [95,96,99]: return "⛈️", "Thunderstorm"
    return "🌡️", "Unknown"

def draw_weather_icon(draw, code, x, y, size=40):
    """Draw vector weather icon instead of emoji (fixes font rendering issues)"""
    cx, cy = x, y
    s = size
    if code == 0:  # Sun
        draw.ellipse([cx-s//3, cy-s//3, cx+s//3, cy+s//3], fill=(255,220,50,255))
        for angle in range(0, 360, 45):
            import math
            rad = math.radians(angle)
            x1 = cx + int((s//3+4)*math.cos(rad))
            y1 = cy + int((s//3+4)*math.sin(rad))
            x2 = cx + int((s//2+2)*math.cos(rad))
            y2 = cy + int((s//2+2)*math.sin(rad))
            draw.line([x1,y1,x2,y2], fill=(255,220,50,220), width=2)
    elif code in [1,2]:  # Part cloud
        draw.ellipse([cx-s//3, cy-s//3, cx+s//3, cy+s//3], fill=(255,220,50,200))
        draw.ellipse([cx-s//2, cy, cx+s//2, cy+s//2], fill=(220,230,255,240))
        draw.ellipse([cx-s//4, cy-s//6, cx+s//4+6, cy+s//3+4], fill=(220,230,255,240))
    elif code == 3:  # Cloud
        draw.ellipse([cx-s//2, cy-s//6, cx+s//2, cy+s//2], fill=(200,210,240,240))
        draw.ellipse([cx-s//4, cy-s//3, cx+s//4+4, cy+s//4], fill=(200,210,240,240))
    elif code in [51,53,55,61,63,65,80,81,82]:  # Rain
        draw.ellipse([cx-s//2, cy-s//4, cx+s//2, cy+s//3], fill=(160,180,220,240))
        draw.ellipse([cx-s//4, cy-s//2, cx+s//4+4, cy+s//6], fill=(160,180,220,240))
        for rx in [-s//3, 0, s//3]:
            draw.line([cx+rx, cy+s//3, cx+rx-4, cy+s//2+4], fill=(100,160,255,220), width=2)
    elif code in [95,96,99]:  # Thunder
        draw.ellipse([cx-s//2, cy-s//4, cx+s//2, cy+s//3], fill=(80,80,100,240))
        draw.ellipse([cx-s//4, cy-s//2, cx+s//4+4, cy+s//6], fill=(80,80,100,240))
        pts = [cx+4,cy+s//4, cx-4,cy+s//4, cx,cy+s//2, cx-8,cy+s//2, cx+12,cy+s*3//4]
        draw.line(pts, fill=(255,220,0,255), width=3)
    else:  # Default cloud
        draw.ellipse([cx-s//2, cy-s//6, cx+s//2, cy+s//2], fill=(180,190,220,240))

def generate_weather_card(weather_data):
    """Generate iPhone-style weather card with drawn icons"""
    from PIL import Image, ImageDraw, ImageFont
    import math

    W, H = 1080, 1080
    img = Image.new("RGB", (W, H), (0,0,0))
    draw = ImageDraw.Draw(img, "RGBA")

    current = weather_data.get("current", {})
    hourly  = weather_data.get("hourly", {})
    daily   = weather_data.get("daily", {})

    temp     = round(current.get("temperature_2m", 29))
    feels    = round(current.get("apparent_temperature", 29))
    humidity = current.get("relativehumidity_2m", 80)
    wind     = round(current.get("windspeed_10m", 10))
    code     = current.get("weathercode", 0)
    _, condition = weather_code_to_info(code)

    # Daily H/L
    temp_max = round(daily.get("temperature_2m_max", [temp])[0])
    temp_min = round(daily.get("temperature_2m_min", [temp])[0])

    # Sunrise/sunset
    sunrise_raw = daily.get("sunrise", [""])[0]
    sunset_raw  = daily.get("sunset", [""])[0]
    sunrise_str = sunrise_raw.split("T")[1][:5] if "T" in sunrise_raw else "06:00"
    sunset_str  = sunset_raw.split("T")[1][:5] if "T" in sunset_raw else "18:00"

    # Hourly
    hours  = hourly.get("time", [])
    temps  = hourly.get("temperature_2m", [])
    codes  = hourly.get("weathercode", [])
    precip = hourly.get("precipitation_probability", [])

    # Background gradient
    for y in range(H):
        t = y / H
        if code in [95,96,99]:
            r,g,b = int(15+t*25), int(8+t*15), int(35+t*50)
        elif code in [61,63,65,80,81,82,51,53,55]:
            r,g,b = int(30+t*25), int(50+t*35), int(90+t*55)
        elif code == 0:
            r,g,b = int(15+t*8), int(70+t*35), int(170+t*35)
        else:
            r,g,b = int(50+t*25), int(70+t*35), int(110+t*55)
        draw.line([(0,y),(W,y)], fill=(r,g,b,255))

    try:
        font_huge  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 180)
        font_large = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 52)
        font_med   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 36)
        font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
        font_tiny  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 22)
        font_xs    = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 18)
    except:
        font_huge = font_large = font_med = font_small = font_tiny = font_xs = ImageFont.load_default()

    from datetime import timezone
    mvt = datetime.now(timezone.utc) + timedelta(hours=5)

    # Location
    loc = "Malé, Maldives"
    lw = draw.textlength(loc, font=font_large)
    draw.text(((W-lw)//2, 55), loc, font=font_large, fill=(255,255,255,230))

    # Big weather icon (drawn)
    draw_weather_icon(draw, code, W//2, 210, size=100)

    # Temperature
    temp_str = f"{temp}°"
    tw = draw.textlength(temp_str, font=font_huge)
    draw.text(((W-tw)//2, 320), temp_str, font=font_huge, fill=(255,255,255,255))

    # Condition
    cw = draw.textlength(condition, font=font_large)
    draw.text(((W-cw)//2, 520), condition, font=font_large, fill=(255,255,255,200))

    # H / L
    hl_str = f"H:{temp_max}°  L:{temp_min}°"
    hlw = draw.textlength(hl_str, font=font_med)
    draw.text(((W-hlw)//2, 585), hl_str, font=font_med, fill=(255,255,255,190))

    # Details row: feels, humidity, wind
    details = f"Feels {feels}°   Humidity {humidity}%   Wind {wind} km/h"
    dw = draw.textlength(details, font=font_tiny)
    draw.text(((W-dw)//2, 635), details, font=font_tiny, fill=(255,255,255,170))

    # Sunrise / Sunset row
    sun_str = f"Sunrise {sunrise_str}   Sunset {sunset_str}"
    sw = draw.textlength(sun_str, font=font_tiny)
    draw.text(((W-sw)//2, 665), sun_str, font=font_tiny, fill=(255,220,100,200))

    # Divider
    draw.line([(60, 705), (W-60, 705)], fill=(255,255,255,50), width=1)

    # Hourly forecast — next 8 hours with drawn icons
    now_hour = mvt.hour
    slot_w = (W - 120) // 8
    displayed = 0

    for i, (h_str, t, c, p) in enumerate(zip(hours, temps, codes, precip)):
        try:
            h_hour = int(h_str.split("T")[1][:2])
        except: continue
        if h_hour < now_hour: continue
        if displayed >= 8: break

        x = 60 + displayed * slot_w + slot_w // 2
        y_base = 725

        # Hour label
        h_label = "Now" if displayed == 0 else f"{h_hour:02d}:00"
        hw = draw.textlength(h_label, font=font_xs)
        draw.text((x - hw//2, y_base), h_label, font=font_xs, fill=(255,255,255,170))

        # Drawn weather icon
        draw_weather_icon(draw, c, x, y_base + 45, size=28)

        # Temp
        t_str = f"{round(t)}°"
        tw2 = draw.textlength(t_str, font=font_small)
        draw.text((x - tw2//2, y_base + 75), t_str, font=font_small, fill=(255,255,255,255))

        # Rain %
        if p and p > 0:
            p_str = f"{p}%"
            pw = draw.textlength(p_str, font=font_xs)
            draw.text((x - pw//2, y_base + 108), p_str, font=font_xs, fill=(120,200,255,210))

        displayed += 1

    # Bottom divider
    draw.line([(60, 895), (W-60, 895)], fill=(255,255,255,50), width=1)

    # Date + time
    time_str = mvt.strftime("%A, %d %B %Y  •  %H:%M MVT")
    fw = draw.textlength(time_str, font=font_xs)
    draw.text(((W-fw)//2, 910), time_str, font=font_xs, fill=(255,255,255,130))

    # Brand
    brand = "Samuga Media  |  @samugacommunity"
    bw = draw.textlength(brand, font=font_small)
    draw.text(((W-bw)//2, 940), brand, font=font_small, fill=(255,255,255,200))

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf

def send_weather_update(time_of_day="morning"):
    """Send weather card to Telegram"""
    log.info(f"🌤️ Weather update ({time_of_day})...")
    try:
        data = get_weather_data()
        if not data:
            log.error("Weather: no data"); return
        card = generate_weather_card(data)
        current = data.get("current", {})
        temp = round(current.get("temperature_2m", 29))
        code = current.get("weathercode", 0)
        emoji, condition = weather_code_to_info(code)
        greeting = "🌅 Good Morning Maldives!" if time_of_day == "morning" else "🌙 Good Evening Maldives!"
        caption = (f"{greeting}\n\n"
                   f"{emoji} <b>Current Weather \u2014 Mal\u00e9</b>\n"
                   f"\U0001f321\ufe0f <b>{temp}\u00b0C</b> \u2014 {condition}\n\n"
                   f"\U0001f4e1 <b>Samuga Media</b> | @samugacommunity")
        send_photo(TELEGRAM_CHANNEL_ID, card, caption)
        log.info(f"✅ Weather card sent ({time_of_day})")
    except Exception as e:
        log.error(f"Weather update: {e}")

# ── Night Summary (12AM MVT) ──────────────────────────────────────────────────
def send_night_summary():
    log.info("🌙 Night summary...")
    try:
        if not recent_posts: log.info("No posts for summary"); return
        posts_text="\n".join([f"• [{p['cat']}] {p['title']}" for p in recent_posts[-15:]])
        prompt=f"""Create a "Tonight's Top Stories 🌙" summary for @samugacommunity.
Today's posts: {posts_text}
- Warm good evening greeting
- Top 5 stories in 1 sentence each with emoji
- Good night closing
- Max 180 words, English"""
        msg=ai.messages.create(model="claude-haiku-4-5-20251001",max_tokens=400,messages=[{"role":"user","content":prompt}])
        summary=msg.content[0].text.strip()
        caption=f"🌙 <b>Tonight's Top Stories</b>\n\n{summary}\n\n📡 <b>Samuga Media</b> | @samugacommunity"
        send_text(TELEGRAM_CHANNEL_ID, caption)
        log.info("✅ Night summary sent!")
    except Exception as e: log.error(f"Night summary: {e}")

# ── Weekly Analytics Report to Core Team ─────────────────────────────────────
def send_weekly_analytics():
    log.info("📊 Weekly analytics report...")
    try:
        from datetime import timezone
        mvt = datetime.now(timezone.utc) + timedelta(hours=5)
        week_str = mvt.strftime("Week of %d %B %Y")

        total = sum(v for k, v in analytics["posts_by_cat"].items() if k != "SOCIAL")
        by_cat = analytics["posts_by_cat"]

        lines = []
        for cat in ["LOCAL","WORLD","FOOTBALL","TOURISM","WEATHER","DISASTER"]:
            if cat in by_cat:
                lines.append(f"  • {cat}: {by_cat[cat]} posts")

        cat_lines = chr(10).join([f"  - {c}: {by_cat[c]} posts" for c in ["LOCAL","WORLD","FOOTBALL","TOURISM","WEATHER","DISASTER"] if c in by_cat])
        report = (
            "<b>Samuga Media Weekly Report</b>" + chr(10)
            + week_str + chr(10) + chr(10)
            + "<b>Total Articles:</b> " + str(total) + chr(10)
            + (cat_lines if cat_lines else "  No posts yet") + chr(10) + chr(10)
            + "<b>Breaking News:</b> " + str(analytics["breaking_count"]) + chr(10) + chr(10)
            + "<b>Social Posting:</b>" + chr(10)
            + "  Success: " + str(analytics["social_success"]) + chr(10)
            + "  Failed: " + str(analytics["social_fail"]) + chr(10) + chr(10)
            + "<b>Bot:</b> Samuga News Bot v3.2" + chr(10)
            + "Samuga Media | @samugacommunity"
        )
        send_text(CORE_TEAM_CHAT_ID, report)
        log.info("✅ Analytics report sent to core team")
    except Exception as e:
        log.error(f"Analytics report: {e}")

# ── Weekly Digest (Friday 6PM MVT) ───────────────────────────────────────────
def send_weekly_digest():
    log.info("📊 Weekly digest...")
    try:
        if not recent_posts: return
        posts_text="\n".join([f"• [{p['cat']}] {p['title']}" for p in recent_posts])
        prompt=f"""Create a "This Week in Maldives 🇲🇻" weekly digest for @samugacommunity.
This week: {posts_text}
- Top 5 most important stories
- 2 sentences each with emoji
- Encouraging closing
- Max 280 words"""
        msg=ai.messages.create(model="claude-haiku-4-5-20251001",max_tokens=500,messages=[{"role":"user","content":prompt}])
        digest=msg.content[0].text.strip()
        caption=f"📊 <b>This Week in Maldives 🇲🇻</b>\n\n{digest}\n\n📡 <b>Samuga Media</b> | @samugacommunity"
        send_text(TELEGRAM_CHANNEL_ID, caption)
        log.info("✅ Weekly digest sent!")
    except Exception as e: log.error(f"Weekly digest: {e}")

# ── Tavily Search ─────────────────────────────────────────────────────────────
def tavily_search(query):
    if not TAVILY_API_KEY: return ""
    try:
        resp=requests.post("https://api.tavily.com/search",
            json={"api_key":TAVILY_API_KEY,"query":query,"search_depth":"basic","max_results":4,"include_answer":True},timeout=15)
        if resp.status_code==200:
            data=resp.json()
            answer=data.get("answer","")
            snippets=[r.get("content","")[:200] for r in data.get("results",[])[:3]]
            log.info(f"✅ Tavily: {query[:40]}")
            return (answer+"\n"+"\n".join(snippets)).strip()
    except Exception as e: log.error(f"Tavily: {e}")
    return ""

def needs_web_search(msg):
    # Skip search only for simple greetings / meta questions
    # Skip search for short messages or greetings
    if len(msg.strip()) <= 4: return False
    skip_kws = ["hello", "hi", "who are you", "what is samuga", "about you",
                "thank", "okay", "ok", "bye", "good morning", "good night",
                "good evening", "assalam", "hey", "sup", "wassup"]
    if any(k in msg.lower() for k in skip_kws): return False
    return True  # Default: always search for current info

# ── Smart Chat ────────────────────────────────────────────────────────────────
def is_dhivehi(text):
    """Check if text contains Thaana script (Dhivehi)"""
    return any('\u0780' <= c <= '\u07BF' for c in text)

def chat_with_gemini_dhivehi(user_message, context="", conversation_history=None):
    """Handle Dhivehi chat using actual Gemini API (native Dhivehi support)"""
    if not GEMINI_API_KEY:
        log.warning("No GEMINI_API_KEY — falling back to Claude for Dhivehi")
        return None
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={GEMINI_API_KEY}"

        # Try web search for Dhivehi queries too
        web_context = ""
        try:
            if needs_web_search(user_message) or not context:
                web_context = tavily_search(f"maldives news today 2026")
                if web_context:
                    log.info("🌐 Dhivehi path: web search done")
        except Exception as e:
            log.error(f"Dhivehi web search: {e}")

        if web_context:
            news_section = "LIVE WEB SEARCH (use this for answers, never repeat same info):\n" + web_context[:600]
        elif context:
            news_section = "LATEST NEWS CONTEXT:\n" + context
        else:
            news_section = ""

        system_prompt = (
            "You are Samuga AI, a Maldivian news assistant. Always reply in natural Dhivehi (Thaana script) only.\n\n"
            "ABOUT SAMUGA:\n"
            "- Samuga Media: Maldivian digital news outlet\n"
            "- Channel: @samugacommunity\n"
            "- Founder: Abdul Muhsin (Manchii) | Co-Founder: Mariyam Ulya (Uly)\n\n"
            + (news_section + "\n\n" if news_section else "") +
            "RULES:\n"
            "- Reply ONLY in Dhivehi Thaana script\n"
            "- Natural, conversational tone like a friendly Maldivian\n"
            "- Max 3-4 sentences\n"
            "- NEVER repeat the same news you already mentioned in this conversation\n"
            "- If asked for more — give DIFFERENT stories\n"
            "- Mention @samugacommunity when relevant\n"
            "- Never write in English or Latin script\n"
            "- Never say you cannot search or lack real-time info"
        )

        # Build contents array with history for multi-turn
        contents = []
        if conversation_history:
            for turn in conversation_history[-6:]:
                role = "user" if turn["role"] == "user" else "model"
                contents.append({"role": role, "parts": [{"text": turn["content"]}]})
        contents.append({"role": "user", "parts": [{"text": user_message}]})

        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": contents,
            "generationConfig": {"maxOutputTokens": 400, "temperature": 0.7}
        }

        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code == 200:
            reply = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            log.info("✅ Gemini Dhivehi chat reply done")
            return reply
        else:
            log.error(f"Gemini Dhivehi chat HTTP {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        log.error(f"Gemini Dhivehi chat error: {e}")
    return None

def chat_with_claude(user_message, user_id=None):
    try:
        # Run headlines + web search in parallel to cut latency
        results = {}

        def fetch_headlines():
            try: results["headlines"] = get_local_headlines()
            except: results["headlines"] = []

        def fetch_web():
            try:
                if needs_web_search(user_message):
                    q = user_message
                    local_kws = ["weather","news","update","what happened","anything","latest","today"]
                    if any(w in user_message.lower() for w in local_kws) and "maldives" not in user_message.lower():
                        q = f"maldives {user_message} 2026"
                    elif any(w in user_message.lower() for w in ["world cup","match","score","won","win"]):
                        q = f"{user_message} 2026 latest"
                    results["web"] = tavily_search(q)
                    if results["web"]: log.info(f"🌐 Web: {results['web'][:60]}...")
            except Exception as e:
                log.error(f"Web search: {e}")
                results["web"] = ""

        t1 = threading.Thread(target=fetch_headlines)
        t2 = threading.Thread(target=fetch_web)
        t1.start(); t2.start()
        t1.join(timeout=5); t2.join(timeout=5)

        headlines = results.get("headlines", [])
        web_context = results.get("web", "") or ""
        headlines_text = "\n".join(headlines[:8]) if headlines else "No recent headlines."

        memory_text = ""
        if recent_posts:
            memory_text = "Recently posted:\n" + "".join([f"• [{p['cat']}] {p['title']}\n" for p in recent_posts[-5:]])

        if web_context:
            context = f"LIVE WEB SEARCH (use this for your answer):\n{web_context[:800]}"
            if memory_text: context += f"\n\n{memory_text}"
        else:
            context = f"LATEST NEWS:\n{headlines_text}"
            if memory_text: context += f"\n\n{memory_text}"

        system=f"""You are Samuga AI — smart friendly Maldivian news assistant for Samuga Media.

ABOUT SAMUGA:
Samuga Media delivers trusted Maldivian news. @samugacommunity is our Telegram channel.
Founder & MD: Abdul Muhsin (Manchii/Mutte) — Maldivian entrepreneur
Co-Founder & Editor: Mariyam Ulya (Uly) — journalist & editorial lead

CONTEXT:
{context}

PERSONALITY:
- Warm, friendly, like a knowledgeable Maldivian friend
- Max 4 sentences per reply
- Use context for accurate answers
- Guide to @samugacommunity for more
- If user writes Dhivehi — reply in Dhivehi
- Never say you lack real-time data"""

        messages=get_conversation(user_id).copy() if user_id else []
        messages.append({"role":"user","content":user_message})

        msg=ai.messages.create(model="claude-haiku-4-5-20251001",max_tokens=600,system=system,messages=messages)
        reply=msg.content[0].text.strip()

        if user_id:
            add_to_conversation(user_id,"user",user_message)
            add_to_conversation(user_id,"assistant",reply)
        return reply
    except Exception as e:
        log.error(f"Chat: {e}")
        return "Hey! Something went wrong 😅 Check @samugacommunity for the latest!"

# ── Core Team Smart Chat ──────────────────────────────────────────────────────
def get_sender_info(user_name, first_name):
    """Identify core team member from username or first name"""
    check = (user_name or "").lower()
    fname = (first_name or "").lower()
    for key, info in CORE_TEAM_MEMBERS.items():
        if key in check or key in fname or info["name"].lower() in fname:
            return info
    return None

def should_respond_proactively(text):
    """Check if bot should jump in without being tagged"""
    t = text.lower()
    return any(trigger in t for trigger in CORE_TEAM_PROACTIVE_TRIGGERS)

def chat_with_coreteam(message, sender_name, sender_info=None, conversation_history=None, session_ctx=""):
    """Smart core team chat — creative, funny, knows the team"""
    try:
        # Build sender context
        if sender_info:
            sender_ctx = f"{sender_info['name']} ({sender_info['role']}) — {sender_info['notes']}"
        else:
            sender_ctx = sender_name or "a team member"

        # Get recent headlines for context
        headlines = []
        try: headlines = get_local_headlines()
        except: pass
        news_ctx = "\n".join(headlines[:5]) if headlines else ""

        news_line = ("LATEST MALDIVES NEWS:\n" + news_ctx) if news_ctx else ""
        system = (
            "You are Samuga AI — witty, sharp, creative team assistant for Samuga Media core team.\n\n"
            "ABOUT SAMUGA MEDIA:\n"
            "- Maldivian digital news & media outlet\n"
            "- Mission: unfiltered truth, real stories, people voice\n"
            "- Telegram: @samugacommunity | Website: en.samugamedia.com\n\n"
            "THE CORE TEAM (you know them personally):\n"
            "- Manchii (Abdul Muhsin) — Founder & MD. Big vision, entrepreneur, always thinking next move. Roast him gently\n"
            "- Uly (Mariyam Ulya) — Co-Founder & Editor-in-Chief. Sharp journalist brain, keeps content accurate\n"
            "- Thooma (Aminath Thooma) — Presenter & Marketing. Face of content, full of energy, needs confidence boost sometimes\n"
            "- Kity (Kit) — Manchii wife, creative contributor, team heart, great at hyping Thooma, brings fresh ideas\n\n"
            f"YOU ARE SPEAKING WITH: {sender_ctx}\n\n"
            + (news_line + "\n\n" if news_line else "")
            + (("SESSION CONTEXT (team shared this for reference):\n" + session_ctx + "\n\n") if session_ctx else "")
            + "YOUR PERSONALITY IN THIS GROUP:\n"
            "- Casual, warm, feel like a real team member not a bot\n"
            "- Funny and witty — crack jokes when vibe calls for it, especially with Manchii\n"
            "- Hype Thooma when she needs it, she has got it in her\n"
            "- Support Kity ideas, she brings great creative energy\n"
            "- Help with content ideas, scripts, captions, strategies instantly\n"
            "- When brainstorming — give 3 specific ideas not generic ones\n"
            "- Keep replies SHORT unless asked for detail — max 3-4 sentences casual\n"
            "- Use occasional emoji but do not overdo it\n"
            "- Never sound corporate or formal\n"
            "- Speak like you are part of the team, not serving the team"
        )

        messages = []
        if conversation_history:
            messages = conversation_history[-8:]  # last 4 exchanges
        messages.append({"role": "user", "content": message})

        msg = ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            system=system,
            messages=messages
        )
        return msg.content[0].text.strip()
    except Exception as e:
        log.error(f"Core team chat: {e}")
        return None

# ── Chat Handler ──────────────────────────────────────────────────────────────
def handle_updates():
    offset=0; bot_mention=f"@{BOT_USERNAME}".lower()
    log.info(f"💬 Chat listening for @{BOT_USERNAME}...")
    while True:
        try:
            resp=requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                params={"offset":offset,"timeout":30},timeout=40)
            if resp.status_code!=200: time.sleep(5); continue
            for update in resp.json().get("result",[]):
                offset=update["update_id"]+1
                msg=update.get("message",{})
                if not msg: continue
                text=msg.get("text","") or msg.get("caption","")
                photo=msg.get("photo")  # list of photo sizes if message has photo
                video=msg.get("video") or msg.get("video_note")
                if not text and not photo and not video: continue
                if not text: text=""
                # Skip videos for card creation — only photos supported
                if video and not photo: photo = None
                chat_id=msg["chat"]["id"]
                msg_id=msg["message_id"]
                thread_id=msg.get("message_thread_id")  # for forum/topic groups
                chat_type=msg["chat"]["type"]
                user_name=msg.get("from",{}).get("username","")
                first_name=msg.get("from",{}).get("first_name","there")
                display_name=user_name or first_name
                user_id=str(msg.get("from",{}).get("id",""))

                if chat_type=="private":
                    if text.startswith("/start"):
                        send_text(chat_id,
                            f"👋 Hey {first_name}! I'm <b>Samuga AI</b> — your Maldives news assistant!\n\n"
                            f"Ask me anything about Maldives news, politics, tourism, football or world news.\n\n"
                            f"ދިވެހިން ވެސް ވާހަކަ ދެއްކިދާނެ! 🇲🇻\n\n"
                            f"📡 Follow <b>@samugacommunity</b> for live news updates!",reply_to=msg_id)
                    elif text.startswith("/search "):
                        query=text[8:].strip()
                        log.info(f"🔍 Search: {query}")
                        results=tavily_search(f"{query} maldives")
                        reply=chat_with_claude(f"Tell me about: {query}. Use this info: {results[:400]}", user_id)
                        send_text(chat_id, reply, reply_to=msg_id, thread_id=thread_id)
                    else:
                        log.info(f"💬 DM {display_name}: {text[:50]}")
                        # Route Dhivehi to Gemini
                        if is_dhivehi(text):
                            log.info("🇲🇻 Dhivehi detected — using Gemini")
                            headlines = get_local_headlines()
                            context = "\n".join(headlines[:5]) if headlines else ""
                            history = get_conversation(user_id)
                            reply = chat_with_gemini_dhivehi(text, context, history)
                            if reply:
                                add_to_conversation(user_id, "user", text)
                                add_to_conversation(user_id, "assistant", reply)
                            else:
                                reply = chat_with_claude(text, user_id)
                        else:
                            reply = chat_with_claude(text, user_id)
                        send_text(chat_id, reply, reply_to=msg_id, thread_id=thread_id)

                elif chat_type in ["group","supergroup"]:
                    is_core_team = str(chat_id) == CORE_TEAM_CHAT_ID
                    tagged = bot_mention in text.lower()
                    clean = text.replace(bot_mention, "").strip() if tagged else text.strip()

                    # Core team group — smarter behavior
                    if is_core_team:
                        sender_info = get_sender_info(display_name, first_name)
                        history = get_conversation(user_id)

                        # @SamugaNewsBot post <key> [optional corrected dhivehi text]
                        if tagged and clean.lower().startswith("post "):
                            parts = clean[5:].strip().split(" ", 1)
                            key = parts[0].strip()
                            corrected = parts[1].strip() if len(parts) > 1 else None
                            if key in dhivehi_pending:
                                pending = dhivehi_pending.pop(key)
                                final_dv_text = corrected if corrected else pending["dv_text"]
                                # NOW generate the card with approved Dhivehi text
                                try:
                                    send_text(chat_id, f"⏳ Creating card for {key}...", thread_id=thread_id)
                                    kw = pending.get("keyword", pending["cat"].lower())
                                    bg = fetch_background_image(kw)
                                    ts_now = datetime.now().strftime("%d %b %Y • %H:%M")
                                    card = generate_card(final_dv_text, pending["source"], ts_now, pending["cat"], bg)
                                    full_caption = (
                                        f"🇲🇻 <b>{pending['title']}</b>\n\n"
                                        f"{final_dv_text}\n\n"
                                        f"🔗 <a href='{pending['link']}'>ތަފްސީލް ކިޔާ</a>\n\n"
                                        f"📡 <b>ސަމޫގާ މީޑިއާ</b> | @samugacommunity"
                                    )
                                    if send_to_telegram(card, full_caption):
                                        send_text(chat_id, f"✅ Posted to community! ({key})", reply_to=msg_id, thread_id=thread_id)
                                        log.info(f"✅ Dhivehi card {key} posted to community")
                                    else:
                                        send_text(chat_id, f"❌ Telegram post failed.", reply_to=msg_id, thread_id=thread_id)
                                except Exception as e:
                                    log.error(f"Dhivehi card post error: {e}")
                                    send_text(chat_id, f"❌ Error creating card: {e}", reply_to=msg_id, thread_id=thread_id)
                            else:
                                send_text(chat_id, f"Key {key} not found or already posted.", reply_to=msg_id, thread_id=thread_id)

                        # @SamugaNewsBot reject <key>
                        elif tagged and clean.lower().startswith("reject "):
                            key = clean[7:].strip()
                            if key in dhivehi_pending:
                                title = dhivehi_pending[key]["title"]
                                del dhivehi_pending[key]
                                send_text(chat_id, f"🗑️ Rejected: {key} ({title[:40]})", reply_to=msg_id, thread_id=thread_id)
                            else:
                                send_text(chat_id, f"Key {key} not found.", reply_to=msg_id, thread_id=thread_id)

                        # @SamugaNewsBot card [dhivehi text] — manual card creation
                        elif tagged and (
                            "create card and post" in clean.lower() or
                            "create card and send to community" in clean.lower() or
                            "create card and send to core team" in clean.lower() or
                            "create card and post to core team" in clean.lower() or
                            "create card and post to community" in clean.lower()
                        ):
                            cl = clean.lower()
                            if "to core team" in cl:
                                destination = "coreteam"
                            elif "to community" in cl or "send to community" in cl:
                                destination = "community"
                            else:
                                destination = "all"

                            # Extract the content text (everything before @SamugaNewsBot)
                            # The text comes from the photo caption or message, minus the command
                            raw_text = text  # original full text including caption
                            # Remove the bot mention and command suffix
                            for cmd in ["create card and post", "create card and send to community", "create card and send to core team"]:
                                raw_text = raw_text.replace(f"@{BOT_USERNAME} {cmd}", "").replace(f"@{BOT_USERNAME.lower()} {cmd}", "")
                            raw_text = raw_text.replace(f"@{BOT_USERNAME}", "").strip()

                            if video and not photo:
                                send_text(chat_id, "Videos are not supported for cards — please send a photo instead 📸", reply_to=msg_id, thread_id=thread_id)
                            elif not raw_text and not photo:
                                send_text(chat_id, "Send a photo with caption text, or just text, then add the command at the end.", reply_to=msg_id, thread_id=thread_id)
                            else:
                                content_text = raw_text or "Samuga Media"
                                try:
                                    send_text(chat_id, "⏳ Creating card...", thread_id=thread_id)

                                    # Use uploaded photo as background if available
                                    if photo:
                                        bg = download_telegram_photo(photo)
                                        log.info("🖼️ Using uploaded photo as card background")
                                    else:
                                        bg = fetch_background_image("maldives news")

                                    ts_now = datetime.now().strftime("%d %b %Y • %H:%M")
                                    card = generate_card(content_text, "Samuga Media", ts_now, "LOCAL", bg)
                                    full_caption = (
                                        "🇲🇻 " + content_text + "\n\n"
                                        "📡 <b>Samuga Media</b> | @samugacommunity"
                                    )

                                    posted = []

                                    if destination in ["community", "all"]:
                                        card.seek(0)
                                        if send_to_telegram(card, full_caption):
                                            posted.append("Community")

                                    if destination == "coreteam":
                                        card.seek(0)
                                        if send_photo(CORE_TEAM_CHAT_ID, card, full_caption):
                                            posted.append("Core Team")

                                    if destination == "all":
                                        card_bytes = card.getvalue()
                                        buf_social = io.BytesIO(card_bytes)
                                        threading.Thread(target=post_to_social, args=(buf_social, full_caption), daemon=True).start()
                                        posted.append("FB + IG + Twitter")

                                    if posted:
                                        send_text(chat_id, "✅ Posted to: " + ", ".join(posted), reply_to=msg_id, thread_id=thread_id)
                                        log.info(f"✅ Manual card posted to: {posted}")
                                    else:
                                        send_text(chat_id, "❌ Failed to post.", reply_to=msg_id, thread_id=thread_id)

                                except Exception as e:
                                    log.error(f"Manual card: {e}")
                                    send_text(chat_id, f"❌ Error: {e}", reply_to=msg_id, thread_id=thread_id)

                        # /read command — store context for this session
                        elif text.strip().lower().startswith("/read"):
                            context_text = text.strip()[5:].strip()
                            if context_text:
                                core_team_session_context[chat_id] = context_text
                                send_text(chat_id, "Got it! I have read that and will use it as context for this session 📖", reply_to=msg_id, thread_id=thread_id)
                                log.info(f"📖 Session context stored: {context_text[:60]}...")
                            else:
                                send_text(chat_id, "Send it like this: /read [paste your content here]", reply_to=msg_id, thread_id=thread_id)

                        # Respond if tagged OR proactive trigger detected
                        elif (not (tagged and (clean.lower().startswith("post ") or clean.lower().startswith("reject ")))) and (tagged or should_respond_proactively(text)):
                            if not clean: clean = text.strip()
                            log.info(f"🧠 Core team {'[tagged]' if tagged else '[proactive]'} {display_name}: {clean[:50]}")
                            session_ctx = core_team_session_context.get(chat_id, "")

                            if is_dhivehi(clean):
                                headlines = get_local_headlines()
                                ctx = "\n".join(headlines[:5]) if headlines else ""
                                reply = chat_with_gemini_dhivehi(clean, ctx, history)
                                if not reply:
                                    reply = chat_with_coreteam(clean, display_name, sender_info, history, session_ctx)
                            else:
                                reply = chat_with_coreteam(clean, display_name, sender_info, history, session_ctx)

                            if reply:
                                add_to_conversation(user_id, "user", clean)
                                add_to_conversation(user_id, "assistant", reply)
                                send_text(chat_id, reply, reply_to=msg_id if tagged else None, thread_id=thread_id)

                    # Regular group — only respond when tagged
                    elif tagged and clean:
                        log.info(f"💬 Group {display_name}: {clean[:50]}")
                        if is_dhivehi(clean):
                            log.info("🇲🇻 Dhivehi group mention — using Gemini")
                            headlines = get_local_headlines()
                            context = "\n".join(headlines[:5]) if headlines else ""
                            history = get_conversation(user_id)
                            reply = chat_with_gemini_dhivehi(clean, context, history)
                            if reply:
                                add_to_conversation(user_id, "user", clean)
                                add_to_conversation(user_id, "assistant", reply)
                            else:
                                reply = chat_with_claude(clean, user_id)
                        else:
                            reply = chat_with_claude(clean, user_id)
                        send_text(chat_id, reply, reply_to=msg_id, thread_id=thread_id)
        except Exception as e:
            log.error(f"Update loop: {e}"); time.sleep(5)

# ── Entry ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("🚀 Samuga News Bot v3.2 starting...")
    log.info("📅 7AM-6PM: every 30min | Night: social only")
    log.info("🌅 7AM Brief | 🌙 12AM Summary | 🌤️ 8AM/8PM Weather | 📊 Friday Digest")
    log.info("💬 Smart chat with history, Tavily search, Dhivehi support")

    seen_on_start=load_seen()
    log.info(f"📚 Loaded {len(seen_on_start)} seen articles")

    threading.Thread(target=handle_updates, daemon=True).start()

    scheduler=BlockingScheduler(timezone="UTC")
    scheduler.add_job(scheduled_check, "interval", minutes=30)
    # Breaking news fast check every 5 min (LOCAL/DISASTER only)
    scheduler.add_job(breaking_news_check, "interval", minutes=5)
    # Morning brief 7AM MVT = 2AM UTC
    scheduler.add_job(send_morning_brief, "cron", hour=2, minute=0)
    # Night summary 12AM MVT = 7PM UTC
    scheduler.add_job(send_night_summary, "cron", hour=19, minute=0)
    # Weekly digest Friday 6PM MVT = 1PM UTC Friday
    scheduler.add_job(send_weekly_digest, "cron", day_of_week="fri", hour=13, minute=0)
    # Weekly analytics report Friday 6:30PM MVT = 1:30PM UTC Friday
    scheduler.add_job(send_weekly_analytics, "cron", day_of_week="fri", hour=13, minute=30)
    # Weather update 8AM MVT = 3AM UTC
    scheduler.add_job(lambda: send_weather_update("morning"), "cron", hour=3, minute=0)
    # Weather update 8PM MVT = 3PM UTC
    scheduler.add_job(lambda: send_weather_update("evening"), "cron", hour=15, minute=0)

    log.info("⏰ Scheduler started!")
    scheduler.start()
