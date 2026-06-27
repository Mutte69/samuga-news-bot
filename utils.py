"""Common helpers used across modules."""
import re
import html
import unicodedata
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin
from config import SAMUGA_PUBLIC_LINK

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)
_HTML_A_RE = re.compile(r"<a\s+[^>]*href=[\"'][^\"']+[\"'][^>]*>(.*?)</a>", re.I | re.S)


def strip_source_links(text: str) -> str:
    s = html.unescape(str(text or ""))
    s = _HTML_A_RE.sub(r"\1", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _URL_RE.sub("", s)
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def samuga_public_caption(caption: str) -> str:
    clean = strip_source_links(caption)
    if "@samugacommunity" not in clean and SAMUGA_PUBLIC_LINK not in clean:
        clean = clean.rstrip() + "\n\n📡 <b>Samuga Media</b> | @samugacommunity"
    return clean


def clean_text(text: str, limit: int = 2000) -> str:
    s = strip_source_links(text)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


def has_thaana(text: str) -> bool:
    return any("\u0780" <= ch <= "\u07BF" for ch in str(text or ""))


def thaana_ratio(text: str) -> float:
    letters = [ch for ch in str(text or "") if ch.isalpha() or ("\u0780" <= ch <= "\u07BF")]
    if not letters:
        return 0.0
    return sum(1 for ch in letters if "\u0780" <= ch <= "\u07BF") / len(letters)


def looks_latin_thaana(text: str) -> bool:
    t = str(text or "").lower()
    if has_thaana(t):
        return False
    words = ["raajje", "mihaaru", "dhivehi", "dharivarun", "sarukaaru", "majlis", "addu", "fuvahmulah", "furusathu", "dhathuru", "guraathulun", "thauleem", "sihhee", "raees", "fuluhun"]
    return sum(1 for w in words if w in t) >= 2


def parse_dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    try:
        dt = parsedate_to_datetime(str(value))
        if dt.tzinfo:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        pass
    for fmt in ["%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d %b %Y %H:%M"]:
        try:
            dt = datetime.strptime(str(value).strip(), fmt)
            return dt.replace(tzinfo=None)
        except Exception:
            continue
    return None


def match_key(text: str) -> str:
    t = str(text or "").lower()
    out = []
    for ch in t:
        if "\u0780" <= ch <= "\u07bf":
            out.append(ch)
        else:
            out.append(unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode("ascii"))
    t = "".join(out)
    t = re.sub(r"[^a-z0-9\u0780-\u07bf ]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:90]


def absolute_url(base: str, href: str) -> str:
    try:
        return urljoin(base, href)
    except Exception:
        return href or base
