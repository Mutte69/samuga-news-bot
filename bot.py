import os, threading, time, logging, hashlib, json, feedparser, requests, anthropic, re
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

def remember_post(title, cat, timestamp):
    recent_posts.append({"title":title,"cat":cat,"time":timestamp})
    if len(recent_posts) > 50: recent_posts.pop(0)

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

# ── Gemini Translate ──────────────────────────────────────────────────────────
def gemini_translate(text):
    if not GEMINI_API_KEY: return text
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
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

def send_text(chat_id, text, reply_to=None):
    payload={"chat_id":chat_id,"text":text,"parse_mode":"HTML"}
    if reply_to: payload["reply_to_message_id"]=reply_to
    try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",json=payload,timeout=15)
    except Exception as e: log.error(f"Send text: {e}")

# ── Gemini Dhivehi Caption ────────────────────────────────────────────────────
def make_dhivehi_caption(english_text, title):
    """Convert English news caption to Dhivehi using Gemini"""
    if not GEMINI_API_KEY:
        return None
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
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

def post_to_buffer(image_url, caption, channel_ids):
    if not BUFFER_TOKEN: return False
    clean=re.sub(r'<[^>]+>','',caption).replace('&amp;','&').strip()
    query="""
    mutation CreatePost($channelId: String!, $text: String!, $imageUrl: String!) {
        createPost(input: {channelId: $channelId, text: $text,
            assets: [{imageUrl: $imageUrl}], schedulingType: automatic, mode: addToQueue}) {
            ... on PostActionSuccess { post { id } }
            ... on MutationError { message }
        }
    }"""
    for cid in channel_ids:
        if not cid: continue
        try:
            resp=requests.post("https://api.buffer.com",
                json={"query":query,"variables":{"channelId":cid,"text":clean[:2200],"imageUrl":image_url}},
                headers={"Authorization":f"Bearer {BUFFER_TOKEN}","Content-Type":"application/json"},timeout=20)
            if resp.status_code==200:
                data=resp.json()
                err=data.get("data",{}).get("createPost",{}).get("message","")
                if err: log.error(f"Buffer [{cid[:8]}]: {err}")
                elif "errors" in data: log.error(f"Buffer [{cid[:8]}]: {data['errors']}")
                else: log.info(f"✅ Buffer: {cid[:8]}...")
            else: log.error(f"Buffer HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e: log.error(f"Buffer: {e}")
        time.sleep(2)
    return True

def post_to_social(img_buf, caption):
    if not BUFFER_TOKEN: return
    try:
        img_bytes=img_buf.getvalue()
        url=upload_to_imgbb(img_bytes)
        if url:
            post_to_buffer(url, caption, [BUFFER_FB_ID, BUFFER_IG_ID, BUFFER_TW_ID])
            log.info("✅ Social posting done!")
    except Exception as e: log.error(f"Social: {e}")

# ── Post Article ──────────────────────────────────────────────────────────────
def post_article(article, seen, social_only=False):
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

    social_caption = caption

    # Social only (night mode)
    if social_only:
        card.seek(0)
        threading.Thread(target=post_to_social,args=(card,social_caption),daemon=True).start()
        seen.add(article["id"]); save_seen(seen); remember_post(article["title"],cat,ts)
        log.info(f"📱 Social only [{cat}]"); return True

    # Telegram throttle for regular posts
    if not breaking and not can_post_regular():
        mins=int((7200-(datetime.utcnow()-last_regular_post_time).total_seconds())/60)
        log.info(f"⏳ [{cat}] Telegram throttled {mins}m — social only")
        card.seek(0)
        threading.Thread(target=post_to_social,args=(card,social_caption),daemon=True).start()
        seen.add(article["id"]); save_seen(seen); return False

    log.info(f"📰 [{'🔴BREAKING' if breaking else '🟡REGULAR'}][{cat}] {article['title'][:60]}...")
    if send_to_telegram(card, caption):
        seen.add(article["id"]); save_seen(seen); remember_post(article["title"],cat,ts)
        if not breaking:
            last_regular_post_time=datetime.utcnow()
            log.info("🕐 Regular timer reset — next in 2hrs")
        else: log.info("🔴 Breaking posted!")
        card.seek(0)
        threading.Thread(target=post_to_social,args=(card,social_caption),daemon=True).start()

        # Auto poll for political/government news
        if should_create_poll(article["title"], article["summary"], cat):
            log.info("🗳️ Generating poll...")
            question, options = generate_poll_question(article["title"], rewritten)
            if question and options:
                time.sleep(3)
                send_poll(question, options)

        return True
    return False

# ── Run Job ───────────────────────────────────────────────────────────────────
def run_job(social_only=False):
    h=get_mvt_hour()
    log.info(f"🕐 MVT {h:02d}:xx | {'SOCIAL ONLY' if social_only else 'DAY' if is_day_mode() else 'NIGHT'}")
    seen=load_seen(); articles=fetch_news()
    breaking_articles=[]; by_cat={}
    for a in articles:
        if a["id"] in seen: continue
        cat=a["cat"]
        if is_breaking(a["title"],a["summary"],cat): breaking_articles.append(a)
        elif cat not in by_cat: by_cat[cat]=a
    if not breaking_articles and not by_cat:
        log.info("No fresh articles."); return
    log.info(f"🔴 {len(breaking_articles)} breaking | 🟡 {len(by_cat)} regular")
    posted=0
    for a in breaking_articles:
        if post_article(a,seen,social_only): posted+=1
        time.sleep(10)
    for cat in ["LOCAL","WORLD","FOOTBALL","TOURISM","WEATHER"]:
        if cat not in by_cat: continue
        if post_article(by_cat[cat],seen,social_only): posted+=1
        if posted<len(by_cat): time.sleep(300)
    log.info(f"✅ Posted {posted} articles.")

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
        prompt=f"""Create a warm "Good Morning Maldives 🌅" news brief for @samugacommunity.
Headlines: {chr(10).join(headlines[:8])}
- Friendly greeting with today's date
- Top 3-5 stories in 1 sentence each with emoji  
- Upbeat closing
- Max 180 words, English"""
        msg=ai.messages.create(model="claude-haiku-4-5-20251001",max_tokens=400,messages=[{"role":"user","content":prompt}])
        brief=msg.content[0].text.strip()
        caption=f"🌅 <b>Good Morning Maldives!</b>\n\n{brief}\n\n📡 <b>Samuga Media</b> | @samugacommunity"
        send_text(TELEGRAM_CHANNEL_ID, caption)
        log.info("✅ Morning brief sent!")
    except Exception as e: log.error(f"Morning brief: {e}")

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
    kws=["latest","today","now","current","happening","news","score","match","result",
         "win","won","lost","goal","weather","storm","earthquake","tsunami","war","attack",
         "update","recently","breaking","who","when","champion","cup","killed","dead",
         "crash","accident","fire","flood","yesterday","election","vote"]
    return any(k in msg.lower() for k in kws)

# ── Smart Chat ────────────────────────────────────────────────────────────────
def is_dhivehi(text):
    """Check if text contains Thaana script (Dhivehi)"""
    return any('\u0780' <= c <= '\u07BF' for c in text)

def chat_with_gemini_dhivehi(user_message, context=""):
    """Handle Dhivehi chat using Claude with strong Dhivehi prompt"""
    try:
        system = f"""You are Samuga AI, a Maldivian news assistant. The user is writing in Dhivehi.

YOU MUST reply ONLY in Dhivehi (Thaana script). Do not write any English words at all.

{"Latest news: " + context if context else ""}

Samuga Media is a Maldivian news outlet. Channel: @samugacommunity
Founder: Abdul Muhsin (Manchii). Co-Founder: Mariyam Ulya (Uly).

Rules:
- Reply in natural conversational Dhivehi only
- Max 3 sentences
- Mention @samugacommunity if relevant"""

        msg = ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=system,
            messages=[{"role":"user","content":user_message}]
        )
        reply = msg.content[0].text.strip()
        log.info("✅ Dhivehi reply done")
        return reply
    except Exception as e:
        log.error(f"Dhivehi chat error: {e}")
    return None

def chat_with_claude(user_message, user_id=None):
    try:
        headlines=[]
        try: headlines=get_local_headlines()
        except: pass
        headlines_text="\n".join(headlines[:8]) if headlines else "No recent headlines."

        memory_text=""
        if recent_posts:
            memory_text="Recently posted:\n"+"".join([f"• [{p['cat']}] {p['title']}\n" for p in recent_posts[-5:]])

        web_context=""
        try:
            if needs_web_search(user_message):
                q=user_message
                if any(w in user_message.lower() for w in ["world cup","match","score","won","win"]):
                    q=f"{user_message} 2026 latest"
                web_context=tavily_search(q)
        except: pass

        context=f"LATEST NEWS:\n{headlines_text}"
        if memory_text: context+=f"\n\n{memory_text}"
        if web_context: context+=f"\n\nWEB SEARCH:\n{web_context[:600]}"

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
                text=msg.get("text","")
                if not text: continue
                chat_id=msg["chat"]["id"]
                msg_id=msg["message_id"]
                chat_type=msg["chat"]["type"]
                user_name=msg.get("from",{}).get("first_name","there")
                user_id=str(msg.get("from",{}).get("id",""))

                if chat_type=="private":
                    if text.startswith("/start"):
                        send_text(chat_id,
                            f"👋 Hey {user_name}! I'm <b>Samuga AI</b> — your Maldives news assistant!\n\n"
                            f"Ask me anything about Maldives news, politics, tourism, football or world news.\n\n"
                            f"ދިވެހިން ވެސް ވާހަކަ ދެއްކިދާނެ! 🇲🇻\n\n"
                            f"📡 Follow <b>@samugacommunity</b> for live news updates!",reply_to=msg_id)
                    elif text.startswith("/search "):
                        query=text[8:].strip()
                        log.info(f"🔍 Search: {query}")
                        results=tavily_search(f"{query} maldives")
                        reply=chat_with_claude(f"Tell me about: {query}. Use this info: {results[:400]}", user_id)
                        send_text(chat_id, reply, reply_to=msg_id)
                    else:
                        log.info(f"💬 DM {user_name}: {text[:50]}")
                        # Route Dhivehi to Gemini
                        if is_dhivehi(text):
                            log.info("🇲🇻 Dhivehi detected — using Gemini")
                            headlines = get_local_headlines()
                            context = "\n".join(headlines[:5]) if headlines else ""
                            reply = chat_with_gemini_dhivehi(text, context)
                            if not reply:
                                reply = chat_with_claude(text, user_id)
                        else:
                            reply = chat_with_claude(text, user_id)
                        send_text(chat_id, reply, reply_to=msg_id)

                elif chat_type in ["group","supergroup"]:
                    if bot_mention in text.lower():
                        clean=text.lower().replace(bot_mention,"").strip()
                        if clean:
                            log.info(f"💬 Group {user_name}: {clean[:50]}")
                            if is_dhivehi(clean):
                                log.info("🇲🇻 Dhivehi group mention — using Gemini")
                                headlines = get_local_headlines()
                                context = "\n".join(headlines[:5]) if headlines else ""
                                reply = chat_with_gemini_dhivehi(clean, context) or chat_with_claude(clean, user_id)
                            else:
                                reply = chat_with_claude(clean, user_id)
                            send_text(chat_id, reply, reply_to=msg_id)
        except Exception as e:
            log.error(f"Update loop: {e}"); time.sleep(5)

# ── Entry ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("🚀 Samuga News Bot v3.0 starting...")
    log.info("📅 7AM-6PM: every 30min | Night: social only")
    log.info("🌅 7AM Morning Brief | 🌙 12AM Night Summary | 📊 Friday Weekly Digest")
    log.info("💬 Smart chat with history, Tavily search, Dhivehi support")

    seen_on_start=load_seen()
    log.info(f"📚 Loaded {len(seen_on_start)} seen articles")

    threading.Thread(target=handle_updates, daemon=True).start()

    scheduler=BlockingScheduler(timezone="UTC")
    scheduler.add_job(scheduled_check, "interval", minutes=30)
    # Morning brief 7AM MVT = 2AM UTC
    scheduler.add_job(send_morning_brief, "cron", hour=2, minute=0)
    # Night summary 12AM MVT = 7PM UTC (previous day)
    scheduler.add_job(send_night_summary, "cron", hour=19, minute=0)
    # Weekly digest Friday 6PM MVT = 1PM UTC Friday
    scheduler.add_job(send_weekly_digest, "cron", day_of_week="fri", hour=13, minute=0)

    log.info("⏰ Scheduler started!")
    scheduler.start()
