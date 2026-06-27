"""Pillow card generation."""
import io
import os
import textwrap
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from config import CAT_CONFIG, SAMUGA_PUBLIC_SOURCE, log, mvt_now

FONT_REGULAR_PATHS = ["NotoSansThaana-Regular.ttf", "/app/NotoSansThaana-Regular.ttf", "/mnt/data/NotoSansThaana-Regular.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
FONT_BOLD_PATHS = ["NotoSansThaana-Bold.ttf", "/app/NotoSansThaana-Bold.ttf", "/mnt/data/NotoSansThaana-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
LOGO_PATHS = ["logo.png", "/app/logo.png", "SamugaNewsBot_Profile.png"]


def _font(paths, size):
    for p in paths:
        if os.path.exists(p):
            try: return ImageFont.truetype(p, size)
            except Exception: pass
    return ImageFont.load_default()


def _logo(size=120):
    for p in LOGO_PATHS:
        if os.path.exists(p):
            try:
                img = Image.open(p).convert("RGBA")
                img.thumbnail((size, size))
                return img
            except Exception:
                pass
    return None


def wrap_text(text, font, max_width, draw):
    words = str(text or "").split()
    lines, line = [], ""
    for w in words:
        test = (line + " " + w).strip()
        if draw.textbbox((0,0), test, font=font)[2] <= max_width:
            line = test
        else:
            if line: lines.append(line)
            line = w
    if line: lines.append(line)
    return lines


def generate_card(title, source=SAMUGA_PUBLIC_SOURCE, timestamp=None, cat="LOCAL", bg=None):
    W, H = 1080, 1080
    img = Image.new("RGB", (W, H), (3, 12, 18))
    draw = ImageDraw.Draw(img)
    # Gradient / glow
    for y in range(H):
        c = int(8 + y/H*18)
        draw.line([(0,y),(W,y)], fill=(2, c, c+6))
    cat_conf = CAT_CONFIG.get(cat, CAT_CONFIG["LOCAL"])
    accent = cat_conf["color"]
    draw.rounded_rectangle((50, 50, W-50, H-50), radius=45, outline=(25, 55, 70), width=3, fill=(5, 12, 21))
    draw.rounded_rectangle((80, 95, 315, 155), radius=28, fill=(8, 52, 72))
    label_font = _font(FONT_BOLD_PATHS, 34)
    draw.text((105, 107), cat_conf["label"].replace("  ", " ")[:18], font=label_font, fill=accent)

    logo = _logo(90)
    if logo:
        img.paste(logo, (82, 850), logo)
    small = _font(FONT_BOLD_PATHS, 30)
    draw.text((190, 875), source, font=small, fill=(180, 200, 215))
    ts = timestamp or mvt_now().strftime("%d %b %Y • %H:%M")
    draw.text((650, 875), ts, font=_font(FONT_REGULAR_PATHS, 28), fill=(135, 155, 170))

    title_font = _font(FONT_BOLD_PATHS, 64)
    lines = wrap_text(title, title_font, 900, draw)[:7]
    y = 245
    for line in lines:
        draw.text((90, y), line, font=title_font, fill=(245, 250, 255))
        y += 78
    draw.text((90, 965), "@samugacommunity", font=_font(FONT_REGULAR_PATHS, 28), fill=accent)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def check_fonts():
    ok = any(os.path.exists(p) for p in FONT_REGULAR_PATHS)
    log.info("✅ Thaana fonts available" if ok else "⚠️ Thaana font not found, using fallback")
    return ok
