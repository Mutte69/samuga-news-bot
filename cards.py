"""
cards.py — Samuga AI Card Generation Module
Extracted from bot.py v7.0

Contains:
  - generate_card()          PIL-based news card (English + Thaana fallback)
  - generate_dhivehi_card()  Pango/Cairo Thaana card (proper RTL shaping)
  - fetch_background_image() Pexels background image fetcher
  - _safe_bg_keyword()       Smart keyword extractor for backgrounds
  - draw_weather_icon()      Vector weather icon renderer

Import in bot.py:
  from cards import generate_card, generate_dhivehi_card, fetch_background_image
"""

import os, io, logging, re, requests
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont, ImageEnhance

log = logging.getLogger(__name__)

# ── These come from bot.py config — passed in or read from env ────────────────
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")

# ── Card color constants ───────────────────────────────────────────────────────
WHITE      = (255, 255, 255)
LIGHT_GRAY = (200, 215, 230)
BG_TOP     = (10, 40, 75)
BG_BOTTOM  = (5, 20, 45)

# ── Category config (colors + labels) ─────────────────────────────────────────
CAT_CONFIG = {
    "BREAKING":  {"label": "🚨  BREAKING NEWS", "color": (220, 50, 50)},
    "LOCAL":     {"label": "🇲🇻  LOCAL NEWS",    "color": (41, 171, 226)},
    "POLITICAL": {"label": "🏛️  POLITICAL",      "color": (180, 140, 40)},
    "LIFESTYLE": {"label": "🌴  LIFESTYLE",      "color": (160, 80, 220)},
    "SPORTS":    {"label": "🏅  SPORTS",         "color": (34, 180, 80)},
    "DISASTER":  {"label": "🚨  BREAKING NEWS",  "color": (220, 50, 50)},
    "WORLD":     {"label": "🌍  WORLD NEWS",     "color": (220, 80, 60)},
    "WEATHER":   {"label": "🌴  LIFESTYLE",      "color": (160, 80, 220)},
    "TOURISM":   {"label": "🌴  LIFESTYLE",      "color": (160, 80, 220)},
    "FOOTBALL":  {"label": "🏅  SPORTS",         "color": (34, 180, 80)},
}

# ── Background keyword maps ───────────────────────────────────────────────────
CAT_BG_KEYWORDS = {
    "BREAKING":  ["emergency lights night", "police lights dark", "crisis dark dramatic", "rescue operation dark"],
    "LOCAL":     ["maldives aerial ocean", "male city maldives", "maldives island drone", "maldives lagoon blue"],
    "POLITICAL": ["parliament building architecture", "government building dark", "official hall columns"],
    "LIFESTYLE": ["tropical beach sunset", "maldives lagoon aerial", "resort pool tropical", "island sunrise"],
    "SPORTS":    ["football stadium lights night", "soccer field green aerial", "sport arena lights"],
    "DISASTER":  ["emergency lights night", "rescue operation dark", "crisis scene dramatic", "disaster response"],
    "WORLD":     ["world globe dark", "city skyline night", "international airport", "global city lights"],
    "TOURISM":   ["maldives resort luxury", "tropical beach aerial", "maldives overwater villa", "island paradise blue"],
    "WEATHER":   ["storm clouds dramatic", "tropical rain dark", "monsoon ocean waves", "dark clouds sea"],
    "FOOTBALL":  ["football stadium lights night", "soccer field green aerial", "football match crowd"],
}
DEFAULT_BG_KEYWORDS = [
    "maldives ocean aerial", "island blue lagoon",
    "tropical dark dramatic", "maldives night city", "ocean waves dark"
]


# ═══════════════════════════════════════════════════════════════════════════════
# Background image helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _safe_bg_keyword(title, cat):
    """
    Extract a safe, visually appropriate Pexels search keyword from the article title.
    Never shows wrong flags, wrong faces, or misleading visuals.
    """
    import random as _r
    t = title.lower()

    if any(k in t for k in ["maldives", "male", "hulhumale", "addu", "atoll",
                             "mndf", "mps", "police", "coast guard"]):
        return _r.choice(["maldives aerial ocean", "male city maldives", "maldives island drone",
                          "maldives lagoon blue", "tropical island aerial"])
    if any(k in t for k in ["parliament", "majlis", "government", "minister",
                             "president", "cabinet", "policy", "law", "bill"]):
        return _r.choice(["parliament building architecture", "government building dark",
                          "official hall columns", "legislative building aerial"])
    if any(k in t for k in ["court", "judge", "verdict", "sentence", "criminal", "trial"]):
        return _r.choice(["court building architecture", "justice scales dark",
                          "legal building exterior", "court hall dramatic"])
    if any(k in t for k in ["fire", "blaze", "burned", "flames"]):
        return _r.choice(["fire night dark dramatic", "emergency lights night",
                          "fire rescue dark", "flames dark dramatic"])
    if any(k in t for k in ["accident", "crash", "collision", "vehicle"]):
        return _r.choice(["emergency lights night", "accident scene dark",
                          "road night dramatic", "rescue operation night"])
    if any(k in t for k in ["boat", "ferry", "ship", "vessel", "sea", "ocean", "coast"]):
        return _r.choice(["boat ocean maldives", "sea vessel dramatic", "ocean dark waves",
                          "maldives boat lagoon", "ferry ocean dark"])
    if any(k in t for k in ["hospital", "health", "medical", "disease", "drug", "dengue"]):
        return _r.choice(["hospital building exterior", "medical blue dark",
                          "healthcare building", "medical technology dark"])
    if any(k in t for k in ["school", "education", "student", "university", "exam"]):
        return _r.choice(["school building exterior", "education building",
                          "university campus aerial", "classroom empty dramatic"])
    if any(k in t for k in ["economy", "finance", "bank", "budget", "mvr", "usd", "money"]):
        return _r.choice(["finance building city", "economy dark dramatic",
                          "bank building architecture", "business district night"])
    if any(k in t for k in ["weather", "storm", "rain", "flood", "wind"]):
        return _r.choice(["storm clouds ocean", "dark rain dramatic",
                          "monsoon waves tropical", "storm lightning sea"])
    if any(k in t for k in ["football", "soccer", "sport", "game", "match", "tournament"]):
        return _r.choice(["football stadium night", "soccer pitch aerial",
                          "sport arena lights", "football match crowd"])
    if any(k in t for k in ["tourism", "resort", "tourist", "hotel", "visit"]):
        return _r.choice(["maldives resort luxury", "overwater villa tropical",
                          "maldives beach sunset", "tropical resort aerial"])
    if any(k in t for k in ["arrest", "murder", "kill", "crime", "robbery", "theft"]):
        return _r.choice(["police lights night", "crime scene dark dramatic",
                          "investigation dark city", "night city dramatic dark"])
    if any(k in t for k in ["earthquake", "tsunami", "disaster", "emergency"]):
        return _r.choice(["disaster rescue dramatic", "emergency response night",
                          "crisis dark dramatic", "emergency lights dark"])

    fallbacks = CAT_BG_KEYWORDS.get(cat, DEFAULT_BG_KEYWORDS)
    return _r.choice(fallbacks)


def fetch_background_image(keyword, cat=None, title=None):
    """
    Fetch a background image from Pexels using smart keyword extraction.
    Returns a PIL Image or None.
    """
    if not PEXELS_API_KEY:
        return None
    import random as _rand
    try:
        if title:
            search_kw = _safe_bg_keyword(title, cat or "LOCAL")
        elif cat and cat in CAT_BG_KEYWORDS:
            search_kw = _rand.choice(CAT_BG_KEYWORDS[cat])
        elif not keyword or keyword in ["maldives news", "news", "local"]:
            search_kw = _rand.choice(DEFAULT_BG_KEYWORDS)
        else:
            dangerous = ["president", "minister", "india", "china", "pakistan",
                         "israel", "america", "flag", "person", "man", "woman"]
            if any(d in keyword.lower() for d in dangerous):
                search_kw = _rand.choice(DEFAULT_BG_KEYWORDS)
            else:
                search_kw = keyword

        resp = requests.get(
            f"https://api.pexels.com/v1/search?query={search_kw}&per_page=10&orientation=square",
            headers={"Authorization": PEXELS_API_KEY}, timeout=15)
        if resp.status_code == 200:
            photos = resp.json().get("photos", [])
            if photos:
                photo = _rand.choice(photos)
                r = requests.get(photo["src"]["large"], timeout=20)
                if r.status_code == 200:
                    log.info(f"✅ Pexels bg: '{search_kw}'")
                    return Image.open(BytesIO(r.content)).convert("RGB")
    except Exception as e:
        log.error(f"Pexels: {e}")
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Dhivehi card — Pango/Cairo (proper Thaana RTL shaping)
# ═══════════════════════════════════════════════════════════════════════════════

def generate_dhivehi_card(text, source, timestamp, cat, bg_image=None):
    """Generate a card with proper Thaana shaping using Pango/Cairo."""
    try:
        import gi
        gi.require_version("Pango", "1.0")
        gi.require_version("PangoCairo", "1.0")
        from gi.repository import Pango, PangoCairo
        import cairo
    except Exception as e:
        log.error(f"Pango not available (falling back to PIL): {e}")
        return generate_card(text, source, timestamp, cat, bg_image, _skip_dhivehi=True)

    import numpy as np

    W, H = 1080, 1080
    DV_CAT = {
        "BREAKING": {"label": "ބްރޭކިން ނިއުސް", "color": (220, 50, 50)},
        "LOCAL":    {"label": "ލޯކަލް ނިއުސް",   "color": (0, 180, 255)},
        "POLITICAL":{"label": "ސިޔާސީ",          "color": (180, 140, 40)},
        "LIFESTYLE":{"label": "ލައިފްސްޓައިލް",  "color": (160, 80, 220)},
        "SPORTS":   {"label": "ކުޅިވަރު",        "color": (34, 180, 80)},
        "DISASTER": {"label": "ބްރޭކިން ނިއުސް", "color": (220, 50, 50)},
        "WORLD":    {"label": "ދުނިޔެ",          "color": (50, 180, 100)},
        "FOOTBALL": {"label": "ކުޅިވަރު",        "color": (34, 180, 80)},
        "TOURISM":  {"label": "ލައިފްސްޓައިލް",  "color": (160, 80, 220)},
        "WEATHER":  {"label": "ލައިފްސްޓައިލް",  "color": (160, 80, 220)},
    }
    cfg    = DV_CAT.get(cat, DV_CAT["LOCAL"])
    accent = cfg["color"]
    label_dv = cfg["label"]

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    ctx     = cairo.Context(surface)

    # Background
    if bg_image:
        try:
            bg  = bg_image.copy().convert("RGB")
            r   = bg.width / bg.height
            nh, nw = (H, int(H * r)) if r > 1 else (int(W / r), W)
            bg  = bg.resize((nw, nh), Image.LANCZOS)
            bg  = bg.crop(((nw - W) // 2, (nh - H) // 2,
                           (nw - W) // 2 + W, (nh - H) // 2 + H))
            bg  = ImageEnhance.Brightness(bg).enhance(0.32)
            navy = Image.new("RGB", (W, H), (8, 30, 65))
            bg  = Image.blend(bg, navy, 0.45).convert("RGBA")
            bg_arr  = np.array(bg)
            bg_bgra = np.ascontiguousarray(bg_arr[:, :, [2, 1, 0, 3]])
            bg_surf = cairo.ImageSurface.create_for_data(bg_bgra, cairo.FORMAT_ARGB32, W, H)
            ctx.set_source_surface(bg_surf, 0, 0)
            ctx.paint()
        except Exception as e:
            log.error(f"DV card BG paste: {e}")
            ctx.set_source_rgb(0.008, 0.047, 0.107)
            ctx.paint()
    else:
        ctx.set_source_rgb(0.008, 0.047, 0.107)
        ctx.paint()

    # Bottom gradient
    grad = cairo.LinearGradient(0, H // 2, 0, H)
    grad.add_color_stop_rgba(0, 0.02, 0.08, 0.2, 0)
    grad.add_color_stop_rgba(1, 0.02, 0.08, 0.2, 0.85)
    ctx.set_source(grad); ctx.rectangle(0, 0, W, H); ctx.fill()

    # Top gradient
    grad2 = cairo.LinearGradient(0, 0, 0, 170)
    grad2.add_color_stop_rgba(0, 0.02, 0.08, 0.2, 0.75)
    grad2.add_color_stop_rgba(1, 0, 0, 0, 0)
    ctx.set_source(grad2); ctx.rectangle(0, 0, W, H); ctx.fill()

    # Accent bar
    ctx.set_source_rgb(accent[0] / 255, accent[1] / 255, accent[2] / 255)
    ctx.rectangle(0, 0, W, 5); ctx.fill()

    # PIL overlay for logo + footer text
    from PIL import ImageDraw as _ID, ImageFont as _IF
    ov  = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od  = _ID.Draw(ov)
    try:
        logo = Image.open("logo.png").convert("RGBA")
        lh   = 72; lw = int(logo.width * lh / logo.height)
        logo = logo.resize((lw, lh), Image.LANCZOS)
        ov.paste(logo, (50, 38), logo)
    except Exception as e:
        log.debug(f"DV logo overlay: {e}")
    try:
        f_sm = _IF.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 21)
        od.text((W - 310, 50), "t.me/samugacommunity", font=f_sm, fill=(200, 230, 255, 220))
        od.text((50, H - 52), f"Source: {source}", font=f_sm, fill=(180, 200, 220, 220))
        tw = od.textlength(timestamp, font=f_sm)
        od.text((W - 50 - int(tw), H - 52), timestamp, font=f_sm, fill=(180, 200, 220, 220))
        od.line([(0, H - 65), (W, H - 65)], fill=(255, 255, 255, 50), width=1)
    except Exception as e:
        log.debug(f"DV footer draw: {e}")

    ov_arr  = np.array(ov)
    ov_bgra = np.ascontiguousarray(ov_arr[:, :, [2, 1, 0, 3]])
    ov_surf = cairo.ImageSurface.create_for_data(ov_bgra, cairo.FORMAT_ARGB32, W, H)
    ctx.set_source_surface(ov_surf, 0, 0); ctx.paint()

    # Category label (Dhivehi Pango)
    tag_y  = 580
    cat_lo = PangoCairo.create_layout(ctx)
    cat_lo.set_text(label_dv, -1)
    cat_lo.set_font_description(Pango.FontDescription("Noto Sans Thaana Bold 20"))
    tw, _ = cat_lo.get_pixel_size()
    ctx.set_source_rgb(accent[0] / 255, accent[1] / 255, accent[2] / 255)
    ctx.rectangle(50, tag_y, tw + 26, 36); ctx.fill()
    ctx.set_source_rgb(1, 1, 1)
    ctx.move_to(63, tag_y + 6); PangoCairo.show_layout(ctx, cat_lo)

    # Headline + body
    # Rule: an explicit blank line (\n\n) is an intentional headline/subhead
    # separator and is ALWAYS honored. Part 1 = headline, the rest = subhead.
    # If there is NO blank line, the whole text stays as headline and simply
    # wraps to more lines — UNLESS it is very long (no blank line, >160 chars),
    # in which case we fall back to the old 80-char auto-split so auto-generated
    # cards still look balanced.
    _blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    if len(_blocks) >= 2:
        headline = _blocks[0].replace("\n", " ").strip()
        body     = " ".join(b.replace("\n", " ") for b in _blocks[1:]).strip()
    else:
        single = (_blocks[0] if _blocks else text).replace("\n", " ").strip()
        if len(single) <= 160:
            headline = single
            body     = ""
        else:
            words = single.split()
            hw, bw, cc = [], [], 0
            for i, w in enumerate(words):
                if cc < 80:
                    hw.append(w); cc += len(w) + 1
                else:
                    bw = words[i:]; break
            headline = " ".join(hw)
            body     = " ".join(bw)

    def to_arabic_nums(t):
        return t.translate(str.maketrans("0123456789", "\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669"))

    h_lo = PangoCairo.create_layout(ctx)
    h_lo.set_width(980 * Pango.SCALE)
    h_lo.set_alignment(Pango.Alignment.RIGHT)
    h_fd = Pango.FontDescription("Noto Sans Thaana 50")
    h_fd.set_weight(Pango.Weight.ULTRABOLD)
    h_lo.set_font_description(h_fd)
    h_lo.set_text(to_arabic_nums(headline), -1)
    ctx.set_source_rgb(1, 1, 1)
    ctx.move_to(50, tag_y + 44); PangoCairo.show_layout(ctx, h_lo)

    if body:
        _, hh = h_lo.get_pixel_size()
        b_lo  = PangoCairo.create_layout(ctx)
        b_lo.set_width(980 * Pango.SCALE)
        b_lo.set_alignment(Pango.Alignment.RIGHT)
        b_lo.set_font_description(Pango.FontDescription("Noto Sans Thaana 26"))
        b_lo.set_text(to_arabic_nums(body), -1)
        ctx.set_source_rgba(0.78, 0.86, 1, 0.85)
        ctx.move_to(50, tag_y + 44 + hh + 8); PangoCairo.show_layout(ctx, b_lo)

    png_buf = io.BytesIO()
    surface.write_to_png(png_buf)
    png_buf.seek(0)
    return png_buf


# ═══════════════════════════════════════════════════════════════════════════════
# English card — PIL/Pillow
# ═══════════════════════════════════════════════════════════════════════════════

def generate_card(text, source, timestamp, cat, bg_image=None, morning=False, _skip_dhivehi=False):
    """
    Generate a 1080x1080 news card.
    - Dhivehi text (Thaana chars) → routed to generate_dhivehi_card() automatically
    - morning=True → golden accent + morning brief style
    - _skip_dhivehi=True → force PIL path (used as Pango fallback)
    """
    # Route Dhivehi text to Pango-based card generator
    if not morning and not _skip_dhivehi and any('\u0780' <= ch <= '\u07BF' for ch in text):
        return generate_dhivehi_card(text, source, timestamp, cat, bg_image)

    W, H   = 1080, 1080
    accent = (255, 180, 0) if morning else CAT_CONFIG.get(cat, CAT_CONFIG["LOCAL"])["color"]
    label  = "🌅  MORNING BRIEF" if morning else CAT_CONFIG.get(cat, CAT_CONFIG["LOCAL"])["label"]

    img = Image.new("RGB", (W, H), BG_TOP)
    if bg_image:
        bg  = bg_image.copy()
        r   = bg.width / bg.height
        nh, nw = (H, int(H * r)) if r > 1 else (int(W / r), W)
        bg  = bg.resize((nw, nh), Image.LANCZOS).crop(
            ((nw - W) // 2, (nh - H) // 2, (nw - W) // 2 + W, (nh - H) // 2 + H))
        bg  = ImageEnhance.Brightness(bg).enhance(0.32)
        img = Image.blend(bg, Image.new("RGB", (W, H), (8, 30, 65)), 0.45)
    else:
        d = ImageDraw.Draw(img)
        for y in range(H):
            t = y / H
            d.line([(0, y), (W, y)], fill=(
                int(BG_TOP[0] + (BG_BOTTOM[0] - BG_TOP[0]) * t),
                int(BG_TOP[1] + (BG_BOTTOM[1] - BG_TOP[1]) * t),
                int(BG_TOP[2] + (BG_BOTTOM[2] - BG_TOP[2]) * t),
            ))

    # Bottom dark vignette
    ov  = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od  = ImageDraw.Draw(ov)
    for y in range(H // 2, H):
        t = (y - H // 2) / (H // 2)
        od.line([(0, y), (W, y)], fill=(5, 20, 50, int(185 * t)))
    img = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")

    # Top dark vignette
    ov2 = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od2 = ImageDraw.Draw(ov2)
    for y in range(0, 170):
        t = 1 - y / 170
        od2.line([(0, y), (W, y)], fill=(5, 20, 50, int(190 * t)))
    img = Image.alpha_composite(img.convert("RGBA"), ov2).convert("RGB")

    draw = ImageDraw.Draw(img)
    draw.rectangle([(0, 0), (W, 5)], fill=accent)

    # Logo
    try:
        logo = Image.open("logo.png").convert("RGBA")
        lh   = 72; lw = int(logo.width * lh / logo.height)
        logo = logo.resize((lw, lh), Image.LANCZOS)
        img.paste(logo, (50, 38), logo)
    except Exception as e:
        log.debug(f"logo paste: {e}")

    # Font loading — Thaana or DejaVu
    has_thaana = any('\u0780' <= ch <= '\u07BF' for ch in text)

    def find_thaana_font(name):
        for path in [f"/app/{name}", f"/data/{name}",
                     f"/usr/share/fonts/truetype/noto/{name}"]:
            if os.path.exists(path): return path
        return None

    THAANA_BOLD = find_thaana_font("NotoSansThaana-Bold.ttf")
    THAANA_REG  = find_thaana_font("NotoSansThaana-Regular.ttf")

    try:
        if has_thaana and THAANA_BOLD:
            f_tag   = ImageFont.truetype(THAANA_BOLD, 22)
            f_title = ImageFont.truetype(THAANA_BOLD, 46)
            f_body  = ImageFont.truetype(THAANA_REG or THAANA_BOLD, 27)
            f_sm    = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 21)
        else:
            f_tag   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22)
            f_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 46)
            f_body  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 27)
            f_sm    = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 21)
    except Exception as e:
        log.debug(f"font load fallback: {e}")
        f_tag = f_title = f_body = f_sm = ImageFont.load_default()

    draw.text((W - 310, 50), "t.me/samugacommunity", font=f_sm, fill=(200, 230, 255))

    # Category tag
    tag_label = {
        "BREAKING": "BREAKING NEWS", "LOCAL": "LOCAL NEWS", "POLITICAL": "POLITICAL",
        "LIFESTYLE": "LIFESTYLE", "SPORTS": "SPORTS", "DISASTER": "BREAKING NEWS",
        "WORLD": "WORLD NEWS", "WEATHER": "LIFESTYLE", "TOURISM": "LIFESTYLE",
        "FOOTBALL": "SPORTS"
    }.get(cat, cat) if has_thaana else label

    f_tag_en = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22)
    tag_y    = 590
    tw       = draw.textbbox((0, 0), tag_label, font=f_tag_en)[2] + 26
    draw.rectangle([(50, tag_y), (50 + tw, tag_y + 34)], fill=accent)
    draw.text((63, tag_y + 6), tag_label, font=f_tag_en,
              fill=WHITE if not morning else (0, 0, 0))

    # Text wrap helper
    def wrap(t, f, mw):
        words = t.split(); lines, cur = [], ""
        for w in words:
            test = (cur + " " + w).strip()
            if draw.textbbox((0, 0), test, font=f)[2] <= mw:
                cur = test
            else:
                if cur: lines.append(cur)
                cur = w
        if cur: lines.append(cur)
        return lines

    # Convert digits for Thaana RTL
    if has_thaana:
        text = text.translate(str.maketrans(
            "0123456789", "\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669"))

    # Split headline vs body
    # Rule: explicit blank line (\n\n) = intentional headline/subhead separator,
    # always honored. No blank line = whole text stays headline (wraps), unless
    # very long, where we fall back to the original auto-split behaviour.
    _blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    if len(_blocks) >= 2:
        headline = _blocks[0].replace("\n", " ").strip()
        body     = " ".join(b.replace("\n", " ") for b in _blocks[1:]).strip()
    elif has_thaana:
        single = (_blocks[0] if _blocks else text).replace("\n", " ").strip()
        if len(single) <= 160:
            headline = single; body = ""
        else:
            words = single.split(); hw, bw, cc = [], [], 0
            for i, w in enumerate(words):
                if cc < 80: hw.append(w); cc += len(w) + 1
                else:       bw = words[i:]; break
            headline = " ".join(hw)
            body     = " ".join(bw)
    else:
        single = (_blocks[0] if _blocks else text).replace("\n", " ").strip()
        if len(single) <= 120 and ". " not in single:
            headline = single; body = ""
        else:
            sentences = single.split(". ")
            headline  = sentences[0] + ("." if len(sentences) > 1 else "")
            body      = ". ".join(sentences[1:]) if len(sentences) > 1 else ""

    y = tag_y + 48
    for line in wrap(headline, f_title, W - 100)[:4]:
        draw.text((50, y), line, font=f_title, fill=WHITE); y += 56
    if body:
        y += 4
        for line in wrap(body, f_body, W - 100)[:3]:
            draw.text((50, y), line, font=f_body, fill=LIGHT_GRAY); y += 36

    # Footer
    draw.rectangle([(0, H - 78), (W, H)], fill=(3, 12, 30))
    draw.rectangle([(0, H - 78), (W, H - 75)], fill=accent)
    draw.text((50, H - 53), f"Source: {source}", font=f_sm, fill=LIGHT_GRAY)
    draw.text((W - 260, H - 53), timestamp, font=f_sm, fill=LIGHT_GRAY)

    buf = BytesIO()
    img.save(buf, format="PNG", quality=95)
    buf.seek(0)
    return buf


# ═══════════════════════════════════════════════════════════════════════════════
# Weather icon renderer (vector, scales at any size)
# ═══════════════════════════════════════════════════════════════════════════════

def draw_weather_icon(draw, code, x, y, size=40):
    """Draw a vector weather icon. Scales cleanly at any size."""
    import math
    cx, cy = x, y
    s  = size
    lw = max(2, s // 18)

    if code == 0:  # Sun
        draw.ellipse([cx - s // 3, cy - s // 3, cx + s // 3, cy + s // 3],
                     fill=(255, 210, 40, 255))
        for angle in range(0, 360, 30):
            rad = math.radians(angle)
            x1  = cx + int((s // 3 + s // 12) * math.cos(rad))
            y1  = cy + int((s // 3 + s // 12) * math.sin(rad))
            x2  = cx + int((s // 2 + s // 10) * math.cos(rad))
            y2  = cy + int((s // 2 + s // 10) * math.sin(rad))
            draw.line([x1, y1, x2, y2], fill=(255, 210, 40, 230), width=lw)

    elif code in [1, 2]:  # Partly cloudy
        draw.ellipse([cx - s // 6, cy - s // 2, cx + s // 2, cy + s // 8],
                     fill=(255, 210, 40, 235))
        draw.ellipse([cx - s // 2, cy - s // 8, cx + s // 6, cy + s // 2],
                     fill=(225, 235, 250, 255))
        draw.ellipse([cx - s // 8, cy - s // 5, cx + s // 2, cy + s // 3],
                     fill=(225, 235, 250, 255))
        draw.ellipse([cx - s // 2, cy, cx + s // 4, cy + s // 2],
                     fill=(225, 235, 250, 255))

    elif code == 3:  # Cloud
        draw.ellipse([cx - s // 2, cy - s // 8, cx + s // 2, cy + s // 2],
                     fill=(210, 220, 245, 255))
        draw.ellipse([cx - s // 3, cy - s // 3, cx + s // 6, cy + s // 4],
                     fill=(210, 220, 245, 255))
        draw.ellipse([cx - s // 12, cy - s // 4, cx + s // 2, cy + s // 3],
                     fill=(210, 220, 245, 255))

    elif code in [51, 53, 55, 61, 63, 65, 80, 81, 82]:  # Rain
        draw.ellipse([cx - s // 2, cy - s // 5, cx + s // 2, cy + s // 3],
                     fill=(175, 190, 225, 255))
        draw.ellipse([cx - s // 3, cy - s // 3, cx + s // 6, cy + s // 5],
                     fill=(175, 190, 225, 255))
        draw.ellipse([cx - s // 12, cy - s // 4, cx + s // 2, cy + s // 4],
                     fill=(175, 190, 225, 255))
        for rx in [-s // 3, 0, s // 3]:
            draw.line([cx + rx, cy + s // 3, cx + rx - s // 12, cy + s // 2 + s // 8],
                      fill=(90, 160, 255, 235), width=lw)

    elif code in [95, 96, 99]:  # Thunderstorm
        draw.ellipse([cx - s // 2, cy - s // 5, cx + s // 2, cy + s // 3],
                     fill=(90, 90, 115, 255))
        draw.ellipse([cx - s // 3, cy - s // 3, cx + s // 6, cy + s // 5],
                     fill=(90, 90, 115, 255))
        draw.ellipse([cx - s // 12, cy - s // 4, cx + s // 2, cy + s // 4],
                     fill=(90, 90, 115, 255))
        bolt = [cx + s // 12, cy + s // 4, cx - s // 12, cy + s // 4,
                cx, cy + s // 2, cx - s // 6, cy + s // 2, cx + s // 5, cy + s * 3 // 4]
        draw.line(bolt, fill=(255, 215, 0, 255), width=lw + 1)

    else:  # Default cloud
        draw.ellipse([cx - s // 2, cy - s // 8, cx + s // 2, cy + s // 2],
                     fill=(190, 200, 230, 255))
        draw.ellipse([cx - s // 3, cy - s // 3, cx + s // 6, cy + s // 4],
                     fill=(190, 200, 230, 255))
