"""
Samuga Travels Bot v1.2
Multi-tenant speedboat booking platform for the Maldives.
Single-file | asyncpg | Railway + PostgreSQL | Cloudinary
"""

import os, io, logging, asyncio, json, random, string, signal
import cloudinary, cloudinary.uploader, requests
import asyncpg
from datetime import datetime
from decimal import Decimal
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image as RLImage
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ── CONFIG ────────────────────────────────────────────────────────────────────
BOT_TOKEN        = os.environ.get("BOT_TOKEN", "")
if not BOT_TOKEN: raise RuntimeError("BOT_TOKEN env var not set!")
DATABASE_URL     = os.environ.get("DATABASE_URL", "")
ADMIN_GROUP_ID   = int(os.environ.get("ADMIN_GROUP_ID",  "-1004397030483"))
ADMIN_THREAD_ID  = int(os.environ.get("ADMIN_THREAD_ID", "2"))
GENERAL_THREAD_ID= int(os.environ.get("GENERAL_THREAD_ID","1"))
# Your personal Telegram ID — gets full admin access
SUPER_ADMINS    = [int(x) for x in os.environ.get("SUPER_ADMINS", "").split(",") if x.strip().isdigit()]

CLOUDINARY_CLOUD = os.environ.get("CLOUDINARY_CLOUD", "dfhj3clbh")
CLOUDINARY_KEY   = os.environ.get("CLOUDINARY_KEY",   "")
CLOUDINARY_SECRET= os.environ.get("CLOUDINARY_SECRET","")

# Configure Cloudinary immediately so uploads don't silently fail
if CLOUDINARY_KEY and CLOUDINARY_SECRET:
    cloudinary.config(
        cloud_name=CLOUDINARY_CLOUD,
        api_key=CLOUDINARY_KEY,
        api_secret=CLOUDINARY_SECRET,
        secure=True
    )
else:
    import warnings
    warnings.warn("⚠️ CLOUDINARY_KEY / CLOUDINARY_SECRET not set — image uploads will fail!")

# SamugaAI — Gemini free tier for customer/operator chat
GEMINI_API_KEY   = os.environ.get("GEMINI_API_KEY", "")

# ── STATES ────────────────────────────────────────────────────────────────────
OP_IDLE="op_idle"; OP_AWAIT_BUSINESS_NAME="op_await_business_name"
OP_AWAIT_LOGO="op_await_logo"; OP_AWAIT_BOAT_NAME="op_await_boat_name"
OP_AWAIT_SEATS="op_await_seats"; OP_AWAIT_TYPE="op_await_type"
OP_AWAIT_ROUTES="op_await_routes"; OP_AWAIT_OWNER_NAME="op_await_owner_name"
OP_AWAIT_OWNER_CONTACT="op_await_owner_contact"; OP_AWAIT_OWNER_ID_PHOTO="op_await_owner_id_photo"
OP_AWAIT_BML_ACCOUNT="op_await_bml_account"; OP_AWAIT_MIB_ACCOUNT="op_await_mib_account"; OP_REGISTERED="op_registered"
OP_AWAIT_SCHEDULE_ROUTE="op_await_schedule_route"; OP_AWAIT_SCHEDULE_TIME="op_await_schedule_time"
OP_AWAIT_SCHEDULE_PRICE="op_await_schedule_price"; OP_AWAIT_SCHEDULE_SEATS="op_await_schedule_seats"
CX_IDLE="cx_idle"; CX_AWAIT_DATE="cx_await_date"
CX_AWAIT_CONTACT="cx_await_contact"
CX_AWAIT_PASSENGER_COUNT="cx_await_passenger_count"
CX_COLLECTING_PASSENGERS="cx_collecting_passengers"; CX_AWAIT_PAYMENT_SLIP="cx_await_payment_slip"
CX_BOOKING_COMPLETE="cx_booking_complete"
# Fleet/boat states
OP_AWAIT_BOAT_ADD_NAME="op_await_boat_add_name"
OP_AWAIT_BOAT_ADD_CAPACITY="op_await_boat_add_capacity"
# Schedule extra states
OP_AWAIT_SCHEDULE_LOCATION="op_await_schedule_location"
OP_AWAIT_SCHEDULE_DAYS="op_await_schedule_days"
OP_AWAIT_CHANGE_NOTE="op_await_change_note"
# Bulk schedule setup
OP_BULK_LOCATION="op_bulk_location"
OP_BULK_PRICE="op_bulk_price"
OP_BULK_SEATS="op_bulk_seats"
OP_BULK_SATHU_DEPS="op_bulk_sathu_deps"
OP_BULK_FRI_DEPS="op_bulk_fri_deps"
# Admin states
ADMIN_AWAIT_BROADCAST="admin_await_broadcast"
# Refund states
CX_AWAIT_REFUND_ACCOUNT="cx_await_refund_account"
OP_AWAIT_REFUND_SLIP="op_await_refund_slip"
# AI chat state
CX_AI_CHAT="cx_ai_chat"
OP_AI_CHAT="op_ai_chat"
# Rate limit: max 10 AI questions per user per day (Gemini free tier)
_ai_usage: dict = {}  # {user_id: {"count": int, "date": str}}
ADMIN_AWAIT_LOGO="admin_await_logo"
ADMIN_AWAIT_REVIEW_TEXT="admin_await_review_text"
# Subscription states
OP_AWAIT_SUB_SLIP="op_await_sub_slip"

# ── SMART INPUT HELPERS ──────────────────────────────────────────────────────
def normalize_input(text: str) -> str:
    """Clean up common input variations"""
    return text.strip()

def parse_name_id(text: str) -> tuple[str, str] | None:
    """
    Flexibly parse 'Name, ID' from user input.
    Accepts: comma, dash, slash, pipe, space+ID as separators.
    Also handles: 'Ahmed Ali A123456' (space before ID starting with A/A0-9)
    """
    import re
    text = text.strip()
    # Try comma first (preferred)
    if "," in text:
        parts = text.split(",", 1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            return parts[0].strip(), parts[1].strip()
    # Try other separators: | / - (with spaces)
    for sep in [" | ", " / ", " - ", "|", "/"]:
        if sep in text:
            parts = text.split(sep, 1)
            if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                return parts[0].strip(), parts[1].strip()
    # Try: name followed by ID card pattern (A + digits or passport)
    match = re.search(r'^(.+?)\s+([A-Za-z]\d{5,}|[A-Z]{2}\d{6,})$', text)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return None

def is_cancel(text: str) -> bool:
    """Check if user wants to cancel"""
    return text.strip().lower() in ["cancel", "stop", "quit", "exit", "/cancel", "back", "nope", "no"]

def is_skip(text: str) -> bool:
    """Check if user wants to skip optional step"""
    return text.strip().lower() in ["skip", "no", "nope", "none", "-", "n/a", "na", "next"]

def parse_number(text: str) -> int | None:
    """Extract number from text like '2 seats', '2pax', '2 people'"""
    import re
    text = text.strip()
    match = re.search(r'\d+', text)
    if match:
        return int(match.group())
    return None

def parse_price(text: str) -> float | None:
    """Parse price from '250', '250MVR', 'MVR250', '250 mvr', '250.00'"""
    import re
    text = text.strip().upper().replace(",", "")
    text = text.replace("MVR", "").replace("RF", "").replace("MRF", "").strip()
    try:
        return float(text)
    except ValueError:
        match = re.search(r'[\d.]+', text)
        if match:
            try:
                return float(match.group())
            except ValueError:
                pass
    return None

def parse_time_24hr(text: str) -> str | None:
    """
    Parse and normalise departure time to 24hr HH:MM format.
    Accepts: 16:00, 04:00PM, 4:00pm, 16.00, 4pm, 16h00
    Rejects AM/PM and converts to 24hr.
    Returns None if unparseable.
    """
    import re as _re
    text = text.strip().upper().replace("H", ":").replace(".", ":")
    # Try HH:MM AM/PM
    m = _re.match(r"(\d{1,2}):(\d{2})\s*(AM|PM)?$", text)
    if m:
        h, mn, period = int(m.group(1)), int(m.group(2)), m.group(3)
        if period == "PM" and h != 12: h += 12
        if period == "AM" and h == 12: h = 0
        if 0 <= h <= 23 and 0 <= mn <= 59:
            return f"{h:02d}:{mn:02d}"
    # Try HH AM/PM (no minutes)
    m2 = _re.match(r"(\d{1,2})\s*(AM|PM)$", text)
    if m2:
        h, period = int(m2.group(1)), m2.group(2)
        if period == "PM" and h != 12: h += 12
        if period == "AM" and h == 12: h = 0
        if 0 <= h <= 23:
            return f"{h:02d}:00"
    return None

def parse_date_flexible(text: str):
    """Parse date from many formats"""
    from datetime import datetime as _dt
    text = text.strip()
    formats = [
        "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y",
        "%Y-%m-%d",
        "%d-%m-%y", "%d/%m/%y",
    ]
    for fmt in formats:
        try:
            return _dt.strptime(text, fmt).date()
        except ValueError:
            continue
    return None

def parse_bulk_departures(text: str):
    """
    Parse multiple departure lines like:
      10:15 Male to Airport to Thoddoo
      06:45 Thoddoo to Airport to Male
    Returns list of {
      "time": "10:15",
      "from": "Male",
      "to": "Thoddoo",
      "stops": ["Male", "Airport", "Thoddoo"],
      "full_route": "Male → Airport → Thoddoo"
    }
    or None if nothing parseable found.
    """
    import re
    results = []
    lines = [l.strip() for l in text.strip().split("\n") if l.strip()]
    time_pat = re.compile(r"^(\d{1,2}[:.]\d{2}\s*(?:AM|PM)?)", re.IGNORECASE)
    to_pat   = re.compile(r"\bto\b", re.IGNORECASE)

    for line in lines:
        line = re.sub(r"^\d+[.)\-\s]+", "", line).strip()
        m = time_pat.match(line)
        if not m:
            continue
        raw_time = m.group(1).strip()
        time_str = parse_time_24hr(raw_time) or raw_time.upper().replace(".", ":")
        rest = line[m.end():].strip()
        parts = [p.strip().title() for p in to_pat.split(rest) if p.strip()]
        if len(parts) >= 2:
            results.append({
                "time": time_str,
                "from": parts[0],
                "to":   parts[-1],
                "stops": parts,
                "full_route": " → ".join(parts)
            })
    return results if results else None

# ── DB POOL ───────────────────────────────────────────────────────────────────
_pool = None

async def get_pool():
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not set! Add it in Railway Variables.")
        db_url = DATABASE_URL.replace("postgres://", "postgresql://")
        for attempt in range(5):
            try:
                _pool = await asyncpg.create_pool(db_url, min_size=1, max_size=10)
                logger.info("✅ Database pool created")
                break
            except Exception as e:
                logger.error(f"DB pool attempt {attempt+1} failed: {e}")
                if attempt < 4:
                    await asyncio.sleep(3)
                else:
                    raise
    return _pool

async def init_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS operators (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT UNIQUE NOT NULL,
                telegram_username TEXT,
                business_name TEXT,
                boat_name TEXT,
                logo_url TEXT,
                seat_count INTEGER DEFAULT 0,
                boat_type TEXT DEFAULT 'ferry',
                routes TEXT[],
                owner_name TEXT,
                owner_contact TEXT,
                owner_id_photo_url TEXT,
                bml_account TEXT,
                payment_accounts TEXT DEFAULT '[]',
                status TEXT DEFAULT 'pending',
                is_recommended BOOLEAN DEFAULT FALSE,
                review_text TEXT,
                average_rating DECIMAL(3,2) DEFAULT 0,
                total_reviews INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS schedules (
                id SERIAL PRIMARY KEY,
                operator_id INTEGER REFERENCES operators(id) ON DELETE CASCADE,
                route_from TEXT NOT NULL,
                route_to TEXT NOT NULL,
                departure_time TEXT NOT NULL,
                price_per_seat DECIMAL(10,2) NOT NULL,
                total_seats INTEGER NOT NULL,
                available_seats INTEGER NOT NULL,
                is_active BOOLEAN DEFAULT TRUE,
                sched_stops TEXT DEFAULT '[]',
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                id SERIAL PRIMARY KEY,
                booking_ref TEXT UNIQUE NOT NULL,
                customer_telegram_id BIGINT NOT NULL,
                customer_name TEXT,
                operator_id INTEGER REFERENCES operators(id),
                schedule_id INTEGER REFERENCES schedules(id),
                travel_date DATE NOT NULL,
                passenger_count INTEGER NOT NULL,
                passengers TEXT DEFAULT '[]',
                total_amount DECIMAL(10,2) NOT NULL,
                status TEXT DEFAULT 'pending_payment',
                payment_slip_url TEXT,
                ticket_url TEXT,
                created_at TIMESTAMP DEFAULT NOW(),
                confirmed_at TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_states (
                telegram_id BIGINT PRIMARY KEY,
                role TEXT DEFAULT 'customer',
                state TEXT DEFAULT 'cx_idle',
                temp_data TEXT DEFAULT '{}',
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS reviews (
                id SERIAL PRIMARY KEY,
                operator_id INTEGER REFERENCES operators(id),
                customer_telegram_id BIGINT NOT NULL,
                booking_id INTEGER REFERENCES bookings(id),
                rating INTEGER CHECK (rating >= 1 AND rating <= 5),
                comment TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # Fleet: multiple boats per operator
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS boats (
                id SERIAL PRIMARY KEY,
                operator_id INTEGER REFERENCES operators(id) ON DELETE CASCADE,
                boat_name TEXT NOT NULL,
                capacity INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active',
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # Daily schedule overrides (boat swap, time change, cancellation)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS schedule_changes (
                id SERIAL PRIMARY KEY,
                schedule_id INTEGER REFERENCES schedules(id) ON DELETE CASCADE,
                change_date DATE NOT NULL,
                new_boat_name TEXT,
                new_time TEXT,
                note TEXT,
                status TEXT DEFAULT 'active',
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # Prevent duplicate daily overrides for the same schedule/date.
        # Clean old duplicates first so index creation will not fail on existing databases.
        await conn.execute("""
            DELETE FROM schedule_changes a
            USING schedule_changes b
            WHERE a.schedule_id=b.schedule_id
              AND a.change_date=b.change_date
              AND a.id > b.id
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS unique_schedule_change_per_day
            ON schedule_changes(schedule_id, change_date)
        """)
        # Add columns to schedules if missing
        await conn.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS location TEXT DEFAULT 'Jetty No. 1, Male'")
        await conn.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS run_days TEXT DEFAULT 'daily'")
        await conn.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS boat_name TEXT")
        # Add columns to bookings if missing
        await conn.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS reminder_sent BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS boarded_at TIMESTAMP")
        await conn.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS boarded_by BIGINT")
        await conn.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMP")
        await conn.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS cancelled_by TEXT")
        await conn.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS cancellation_reason TEXT")
        await conn.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS refund_account TEXT")
        await conn.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS refund_account_name TEXT")
        await conn.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS refund_slip_url TEXT")
        await conn.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS refund_status TEXT DEFAULT 'none'")
        await conn.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS refund_at TIMESTAMP")
        await conn.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS payment_alert_stage INTEGER DEFAULT 0")
        await conn.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS payment_alert_last_at TIMESTAMP")
        # AI usage tracking table (persistent — survives Railway restarts)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_usage (
                telegram_id BIGINT,
                usage_date  DATE,
                count       INTEGER DEFAULT 0,
                PRIMARY KEY (telegram_id, usage_date)
            )
        """)
        # Settings table for admin-configurable values
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # Insert defaults
        await conn.execute("""
            INSERT INTO settings (key, value) VALUES ('samuga_logo_url', '')
            ON CONFLICT (key) DO NOTHING
        """)
        await conn.execute("""
            INSERT INTO settings (key, value) VALUES ('subscription_fee', '500')
            ON CONFLICT (key) DO NOTHING
        """)
        await conn.execute("""
            INSERT INTO settings (key, value) VALUES ('subscription_accounts', '[]')
            ON CONFLICT (key) DO NOTHING
        """)
        await conn.execute("""
            INSERT INTO settings (key, value) VALUES ('commission_rate', '0')
            ON CONFLICT (key) DO NOTHING
        """)
        # Subscriptions table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id SERIAL PRIMARY KEY,
                operator_id INTEGER REFERENCES operators(id) ON DELETE CASCADE,
                plan TEXT DEFAULT 'trial',
                trial_started_at TIMESTAMP DEFAULT NOW(),
                trial_ends_at TIMESTAMP,
                paid_until TIMESTAMP,
                status TEXT DEFAULT 'trial',
                payment_slip_url TEXT,
                payment_amount DECIMAL(10,2),
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # Add trial columns to operators if missing
        await conn.execute("ALTER TABLE operators ADD COLUMN IF NOT EXISTS trial_started_at TIMESTAMP DEFAULT NOW()")
        await conn.execute("ALTER TABLE operators ADD COLUMN IF NOT EXISTS subscription_status TEXT DEFAULT 'trial'")
    logger.info("✅ Database initialized")

# ── DB HELPERS ────────────────────────────────────────────────────────────────
async def get_user_state(telegram_id: int) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM user_states WHERE telegram_id = $1", telegram_id)
    if row:
        d = dict(row)
        d["temp_data"] = json.loads(d.get("temp_data") or "{}")
        return d
    return {"telegram_id": telegram_id, "role": "customer", "state": CX_IDLE, "temp_data": {}}

async def set_user_state(telegram_id: int, state: str, temp_data: dict = None, role: str = None):
    pool = await get_pool()
    td = json.dumps(temp_data or {})
    async with pool.acquire() as conn:
        if role:
            await conn.execute("""
                INSERT INTO user_states (telegram_id, state, temp_data, role)
                VALUES ($1,$2,$3,$4)
                ON CONFLICT (telegram_id) DO UPDATE
                SET state=$2, temp_data=$3, role=$4, updated_at=NOW()
            """, telegram_id, state, td, role)
        else:
            await conn.execute("""
                INSERT INTO user_states (telegram_id, state, temp_data)
                VALUES ($1,$2,$3)
                ON CONFLICT (telegram_id) DO UPDATE
                SET state=$2, temp_data=$3, updated_at=NOW()
            """, telegram_id, state, td)

async def update_temp_key(telegram_id: int, key: str, value):
    sd = await get_user_state(telegram_id)
    temp = sd.get("temp_data", {}) or {}
    temp[key] = value
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE user_states SET temp_data=$1, updated_at=NOW() WHERE telegram_id=$2",
            json.dumps(temp), telegram_id)

async def get_setting(key: str, default: str = "") -> str:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM settings WHERE key=$1", key)
    return row["value"] if row and row["value"] else default

async def set_setting(key: str, value: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO settings (key, value, updated_at) VALUES ($1,$2,NOW())
            ON CONFLICT (key) DO UPDATE SET value=$2, updated_at=NOW()
        """, key, value)

async def safe_edit(query, text: str, parse_mode: str = "Markdown", reply_markup=None):
    """Edit either a caption message or a text message; fallback to reply if editing fails."""
    try:
        await query.edit_message_caption(caption=text, parse_mode=parse_mode, reply_markup=reply_markup)
        return
    except Exception:
        pass
    try:
        await query.edit_message_text(text=text, parse_mode=parse_mode, reply_markup=reply_markup)
        return
    except Exception:
        pass
    try:
        await query.message.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"safe_edit failed: {e}")

async def get_subscription(operator_id: int) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM subscriptions WHERE operator_id=$1 ORDER BY created_at DESC LIMIT 1",
            operator_id)
    return dict(row) if row else None

async def create_trial(operator_id: int):
    """Create a 2-month free trial when operator is first approved."""
    from datetime import timedelta
    trial_end = datetime.now() + timedelta(days=60)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO subscriptions (operator_id, plan, trial_ends_at, status)
            VALUES ($1, 'trial', $2, 'trial')
            ON CONFLICT DO NOTHING
        """, operator_id, trial_end)
        await conn.execute("""
            UPDATE operators SET subscription_status='trial', trial_started_at=NOW()
            WHERE id=$1
        """, operator_id)

async def get_sub_status(operator_id: int) -> dict:
    """
    Returns subscription status dict:
    {
      "status": "trial" | "active" | "expired" | "grace",
      "days_left": int,
      "trial": bool,
      "message": str
    }
    """
    from datetime import timedelta
    sub = await get_subscription(operator_id)
    now = datetime.now()

    if not sub:
        return {"status": "trial", "days_left": 60, "trial": True,
                "message": "Free trial active"}

    if sub["status"] == "trial":
        trial_end = sub["trial_ends_at"]
        if trial_end and now < trial_end:
            days_left = (trial_end - now).days
            return {"status": "trial", "days_left": days_left, "trial": True,
                    "message": f"Free trial — {days_left} days remaining"}
        else:
            return {"status": "expired", "days_left": 0, "trial": False,
                    "message": "Free trial ended — please subscribe to continue"}

    if sub["status"] == "active":
        paid_until = sub["paid_until"]
        if paid_until and now < paid_until:
            days_left = (paid_until - now).days
            if days_left <= 7:
                return {"status": "grace", "days_left": days_left, "trial": False,
                        "message": f"⚠️ Subscription expires in {days_left} days!"}
            return {"status": "active", "days_left": days_left, "trial": False,
                    "message": f"Subscription active — {days_left} days remaining"}
        else:
            return {"status": "expired", "days_left": 0, "trial": False,
                    "message": "Subscription expired — renew to stay listed"}

    return {"status": "expired", "days_left": 0, "trial": False,
            "message": "Subscription required"}

async def operator_is_active(operator_id: int) -> bool:
    """Check if operator can receive bookings (trial or paid)."""
    status = await get_sub_status(operator_id)
    return status["status"] in ["trial", "active", "grace"]

# ── SAMUGA AI ─────────────────────────────────────────────────────────────────
async def _ai_check_limit(user_id: int) -> tuple[bool, int]:
    """Returns (allowed, remaining). 10 messages/day — stored in DB, survives restarts."""
    from datetime import date
    today = date.today()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT count FROM ai_usage WHERE telegram_id=$1 AND usage_date=$2",
            user_id, today)
    count = row["count"] if row else 0
    remaining = 10 - count
    return remaining > 0, max(0, remaining)

async def _ai_increment(user_id: int):
    from datetime import date
    today = date.today()
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO ai_usage (telegram_id, usage_date, count)
            VALUES ($1, $2, 1)
            ON CONFLICT (telegram_id, usage_date)
            DO UPDATE SET count = ai_usage.count + 1
        """, user_id, today)

async def ask_samuga_ai(question: str, role: str, context: dict = None) -> str:
    """
    Ask Gemini a question with Samuga Travels context.
    role: "customer" or "operator"
    context: optional dict with operator name, route info etc.
    """
    if not GEMINI_API_KEY:
        return (
            "🤖 SamugaAI is not configured yet.\n\n"
            "Contact @SamugaTravels for help! 🙏"
        )

    if role == "operator":
        system_prompt = """You are SamugaAI, the helpful assistant for Samuga Travels operators in the Maldives.
You help speedboat and ferry operators with:
- How to add and manage schedules
- How to confirm bookings and send tickets
- How to manage their fleet
- Subscription and billing questions
- How to use bot commands
- General Maldives maritime travel questions

Key facts about Samuga Travels:
- Operators use /profile to see their dashboard
- Bulk schedule setup: tap Add Schedule → Bulk Setup → enter routes and times in 24hr format
- Subscription: MVR 500/month after 2-month free trial
- Booking confirmation: tap ✅ Confirm & Send Ticket when you receive payment slip
- All times must be in 24hr format (16:00 not 4:00PM)

Keep answers SHORT (max 3-4 sentences). Use simple English. Be friendly and helpful.
If you don't know something, say "Contact @SamugaTravels for this."
Never make up prices, policies, or features that aren't mentioned."""
    else:
        system_prompt = """You are SamugaAI, the helpful travel assistant for Samuga Travels customers in the Maldives.
You help passengers with:
- How to search and book speedboats
- What to bring on a boat trip
- Island information (Thoddoo, Maafushi, Dhigurah, etc.)
- What to expect during the journey
- Payment and ticket questions
- Travel tips for the Maldives

Key facts:
- Search boats by typing your route e.g. "Male to Thoddoo"
- Pay via BML or MIB bank transfer then upload screenshot
- Ticket arrives within 5-10 minutes after operator confirms
- 45-minute reminder is sent before departure
- Bring your National ID card (Maldivians) or Passport (foreigners)
- Use FollowMe app to track your boat

Keep answers SHORT (max 3-4 sentences). Be warm and friendly.
Never make up schedules, prices, or routes — say "Search in the bot to see current options."
If asked about refunds or complaints, say "Contact @SamugaTravels directly."
"""

    ctx_str = ""
    if context:
        if context.get("operator_name"):
            ctx_str = f"\n(Operator: {context['operator_name']})"
        if context.get("customer_route"):
            ctx_str = f"\n(Looking for: {context['customer_route']})"

    try:
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}",
            json={
                "contents": [{"parts": [{"text": f"{system_prompt}{ctx_str}\n\nUser: {question}"}]}],
                "generationConfig": {"maxOutputTokens": 200, "temperature": 0.7}
            },
            timeout=15
        )
        if resp.status_code == 200:
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            return f"🤖 *SamugaAI:*\n\n{text}"
        elif resp.status_code == 429:
            return "🤖 SamugaAI is a bit busy right now. Please try again in a minute! 🙏"
        else:
            logger.error(f"Gemini error: {resp.status_code} {resp.text[:200]}")
            return "🤖 Couldn't get an answer right now. Try again or contact @SamugaTravels! 🙏"
    except Exception as e:
        logger.error(f"SamugaAI error: {e}")
        return "🤖 Something went wrong. Please try again! 🙏"

# ── CANCELLATION ─────────────────────────────────────────────────────────────
async def cancel_booking(booking_id: int, cancelled_by: str, reason: str = "") -> tuple[dict | None, str]:
    """
    Atomically cancel a booking.
    - Locks the row to prevent double-cancel
    - Restores seats ONLY if booking was confirmed (no double-add)
    - Returns (result_dict, message)
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            booking = await conn.fetchrow("""
                SELECT * FROM bookings WHERE id=$1 FOR UPDATE
            """, booking_id)

            if not booking:
                return None, "Booking not found."
            if booking["status"] == "cancelled":
                return dict(booking), "already_cancelled"

            old_status = booking["status"]

            await conn.execute("""
                UPDATE bookings
                SET status='cancelled', cancelled_at=NOW(),
                    cancelled_by=$2, cancellation_reason=$3
                WHERE id=$1
            """, booking_id, cancelled_by, reason)

            # Restore seats ONLY if was confirmed (prevent double-restore)
            if old_status == "confirmed":
                await conn.execute("""
                    UPDATE schedules
                    SET available_seats = available_seats + $1
                    WHERE id=$2
                """, booking["passenger_count"], booking["schedule_id"])

            schedule = await conn.fetchrow(
                "SELECT * FROM schedules WHERE id=$1", booking["schedule_id"])
            operator = await conn.fetchrow(
                "SELECT * FROM operators WHERE id=$1", booking["operator_id"])

    return {
        "booking":    dict(booking),
        "schedule":   dict(schedule)  if schedule  else {},
        "operator":   dict(operator)  if operator  else {},
        "old_status": old_status,
    }, "cancelled"

# ── MONTHLY ANALYTICS ─────────────────────────────────────────────────────────
async def get_operator_monthly_report(operator_id: int, year: int, month: int) -> tuple[dict, dict | None]:
    """Pull monthly booking stats for an operator from the DB."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT
                COUNT(*) FILTER (
                    WHERE EXTRACT(YEAR FROM travel_date)=$2 AND EXTRACT(MONTH FROM travel_date)=$3
                ) AS total_bookings,
                COUNT(*) FILTER (
                    WHERE status='confirmed' AND EXTRACT(YEAR FROM travel_date)=$2 AND EXTRACT(MONTH FROM travel_date)=$3
                ) AS confirmed_bookings,
                COUNT(*) FILTER (
                    WHERE status='cancelled' AND EXTRACT(YEAR FROM travel_date)=$2 AND EXTRACT(MONTH FROM travel_date)=$3
                ) AS cancelled_bookings,
                COUNT(*) FILTER (
                    WHERE status IN ('pending_payment','pending_confirmation') AND EXTRACT(YEAR FROM travel_date)=$2 AND EXTRACT(MONTH FROM travel_date)=$3
                ) AS pending_bookings,
                COALESCE(SUM(passenger_count) FILTER (
                    WHERE status='confirmed' AND EXTRACT(YEAR FROM travel_date)=$2 AND EXTRACT(MONTH FROM travel_date)=$3
                ), 0) AS seats_sold,
                COALESCE(SUM(total_amount) FILTER (
                    WHERE status='confirmed' AND EXTRACT(YEAR FROM travel_date)=$2 AND EXTRACT(MONTH FROM travel_date)=$3
                ), 0) AS gross_sales,
                COALESCE(SUM(total_amount) FILTER (
                    WHERE status='cancelled' AND EXTRACT(YEAR FROM travel_date)=$2 AND EXTRACT(MONTH FROM travel_date)=$3
                ), 0) AS cancelled_value,
                COUNT(*) FILTER (
                    WHERE status='cancelled' AND cancelled_at IS NOT NULL AND EXTRACT(YEAR FROM cancelled_at)=$2 AND EXTRACT(MONTH FROM cancelled_at)=$3
                ) AS cancelled_this_month,
                COALESCE(SUM(total_amount) FILTER (
                    WHERE refund_status='completed' AND refund_at IS NOT NULL AND EXTRACT(YEAR FROM refund_at)=$2 AND EXTRACT(MONTH FROM refund_at)=$3
                ), 0) AS refunds_completed,
                COALESCE(SUM(total_amount) FILTER (
                    WHERE refund_status='requested'
                ), 0) AS refunds_pending
            FROM bookings
            WHERE operator_id=$1
        """, operator_id, year, month)

        top_route = await conn.fetchrow("""
            SELECT s.route_from, s.route_to, COUNT(*) AS trips
            FROM bookings b
            JOIN schedules s ON b.schedule_id=s.id
            WHERE b.operator_id=$1 AND b.status='confirmed'
              AND EXTRACT(YEAR  FROM b.travel_date)=$2
              AND EXTRACT(MONTH FROM b.travel_date)=$3
            GROUP BY s.route_from, s.route_to
            ORDER BY trips DESC LIMIT 1
        """, operator_id, year, month)

        # Rating
        op_row = await conn.fetchrow(
            "SELECT average_rating, total_reviews FROM operators WHERE id=$1", operator_id)

    return dict(row), dict(top_route) if top_route else None, dict(op_row) if op_row else {}

async def get_operator(telegram_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM operators WHERE telegram_id=$1", telegram_id)
    return dict(row) if row else None

def gen_ref():
    ts = datetime.now().strftime("%y%m%d")
    rand = ''.join(random.choices(string.digits, k=4))
    return f"ST-{ts}-{rand}"

# ── CLOUDINARY ────────────────────────────────────────────────────────────────
async def upload_image(file_bytes: bytes, folder: str, filename: str) -> str:
    result = cloudinary.uploader.upload(
        file_bytes, folder=f"samuga_travels/{folder}",
        public_id=filename, overwrite=True, resource_type="image")
    return result["secure_url"]

# ── PDF TICKET ────────────────────────────────────────────────────────────────
SAMUGA_LOGO_URL = "https://res.cloudinary.com/dfhj3clbh/image/upload/samuga_travels/logos/logo_{user_id}"

async def generate_ticket_pdf(booking: dict, operator: dict, schedule: dict) -> bytes:
    samuga_logo_url = await get_setting("samuga_logo_url", "")
    from reportlab.platypus import HRFlowable, KeepTogether
    from reportlab.lib.colors import HexColor
    from reportlab.pdfgen import canvas as rl_canvas

    buf = io.BytesIO()

    # Color palette — Samuga Travels ocean theme
    ST_NAVY   = HexColor('#0D2137')   # deep navy
    ST_BLUE   = HexColor('#1B6CA8')   # samuga blue
    ST_LIGHT  = HexColor('#E8F4FD')   # very light blue bg
    ST_ACCENT = HexColor('#00B4D8')   # bright accent
    ST_WHITE  = HexColor('#FFFFFF')
    ST_GRAY   = HexColor('#F5F8FA')
    ST_TEXT   = HexColor('#1A2733')
    ST_MUTED  = HexColor('#6B8A9E')

    doc = SimpleDocTemplate(buf, pagesize=A4,
                            rightMargin=15*mm, leftMargin=15*mm,
                            topMargin=15*mm, bottomMargin=15*mm)
    styles = getSampleStyleSheet()
    story = []

    # ── HEADER BAND ──────────────────────────────────────────────────────────
    from reportlab.platypus import KeepInFrame

    # Samuga Travels logo top-left — fixed width, maintain aspect ratio, no squeeze
    st_logo_img = None
    if samuga_logo_url:
        try:
            resp = requests.get(samuga_logo_url, timeout=5)
            img_data = io.BytesIO(resp.content)
            # Get natural size to preserve aspect ratio
            from PIL import Image as PILImage
            pil_img = PILImage.open(io.BytesIO(resp.content))
            nat_w, nat_h = pil_img.size
            logo_w = 45*mm
            logo_h = logo_w * nat_h / nat_w
            if logo_h > 18*mm:  # cap height
                logo_h = 18*mm
                logo_w = logo_h * nat_w / nat_h
            img_data.seek(0)
            st_logo_img = RLImage(img_data, width=logo_w, height=logo_h)
            st_logo_img.hAlign = 'LEFT'
        except: pass

    # Operator logo top-right
    op_logo_img = None
    if operator.get("logo_url"):
        try:
            resp_op = requests.get(operator["logo_url"], timeout=5)
            op_pil = __import__('PIL').Image.open(io.BytesIO(resp_op.content))
            ow, oh = op_pil.size
            ol_h = 24*mm
            ol_w = ol_h * ow / oh
            if ol_w > 30*mm:
                ol_w = 30*mm
                ol_h = ol_w * oh / ow
            op_logo_img = RLImage(io.BytesIO(resp_op.content), width=ol_w, height=ol_h)
            op_logo_img.hAlign = 'RIGHT'
        except: pass

    # Build header cells properly — no nested lists to prevent squeezing
    from reportlab.platypus import KeepInFrame
    left_cell_items  = ([st_logo_img] if st_logo_img else
                        [Paragraph('<font color="#1B6CA8"><b>Samuga Travels</b></font>',
                         ParagraphStyle('stfb', fontName='Helvetica-Bold', fontSize=11))])
    right_cell_items = []
    if op_logo_img:
        right_cell_items.append(op_logo_img)
    right_cell_items.append(Paragraph(
        f'<font color="#0D2137"><b>{operator.get("business_name","")}</b></font>',
        ParagraphStyle('opn2', fontName='Helvetica-Bold', fontSize=11, alignment=2)))
    right_cell_items.append(Paragraph(
        f'<font color="#6B8A9E" size="8">{operator.get("owner_contact","")}</font>',
        ParagraphStyle('opc2', fontName='Helvetica', fontSize=8, alignment=2)))

    header_table = Table([
        [KeepInFrame(85*mm, 25*mm, left_cell_items, hAlign='LEFT'),
         KeepInFrame(85*mm, 25*mm, right_cell_items, hAlign='RIGHT')]
    ], colWidths=[90*mm, 85*mm])
    header_table.setStyle(TableStyle([
        ('VALIGN',      (0,0), (-1,-1), 'MIDDLE'),
        ('ALIGN',       (0,0), (0,0),   'LEFT'),
        ('ALIGN',       (1,0), (1,0),   'RIGHT'),
        ('LEFTPADDING', (0,0), (-1,-1), 0),
        ('RIGHTPADDING',(0,0), (-1,-1), 0),
        ('TOPPADDING',  (0,0), (-1,-1), 2),
        ('BOTTOMPADDING',(0,0),(-1,-1), 2),
    ]))
    story.append(header_table)
    story.append(HRFlowable(width="100%", thickness=2, color=ST_ACCENT, spaceAfter=4*mm))

    # ── TICKET TITLE BAND ────────────────────────────────────────────────────
    title_data = [["  BOARDING TICKET  ·  " + booking["booking_ref"]]]
    title_t = Table(title_data, colWidths=[175*mm])
    title_t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), ST_NAVY),
        ('TEXTCOLOR', (0,0), (-1,-1), ST_WHITE),
        ('FONTNAME', (0,0), (-1,-1), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 13),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('PADDING', (0,0), (-1,-1), 8),
        ('TOPPADDING', (0,0), (-1,-1), 6),
    ]))
    story.append(title_t)
    story.append(Spacer(1, 4*mm))

    # ── JOURNEY DETAILS ──────────────────────────────────────────────────────
    route_str = f"{schedule.get('route_from','')} → {schedule.get('route_to','')}"
    travel_date = str(booking.get("travel_date",""))
    dep_time = schedule.get("departure_time","")
    location = schedule.get("location","Jetty No. 1, Male")
    boat_name = operator.get("boat_name","N/A")

    # Two-column journey card
    lbl = ParagraphStyle('lbl', fontName='Helvetica-Bold', fontSize=8, textColor=ST_MUTED)
    val = ParagraphStyle('val', fontName='Helvetica-Bold', fontSize=11, textColor=ST_TEXT)
    val_sm = ParagraphStyle('vsm', fontName='Helvetica', fontSize=10, textColor=ST_TEXT)

    journey_data = [
        [Paragraph("ROUTE", lbl), Paragraph("DATE", lbl),
         Paragraph("DEPARTURE", lbl), Paragraph("LOCATION", lbl)],
        [Paragraph(route_str, val), Paragraph(travel_date, val),
         Paragraph(dep_time, val), Paragraph(location, val_sm)],
    ]
    journey_t = Table(journey_data, colWidths=[52*mm, 38*mm, 35*mm, 50*mm])
    journey_t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), ST_LIGHT),
        ('ROWBACKGROUNDS', (0,0), (-1,-1), [ST_GRAY, ST_WHITE]),
        ('BOX', (0,0), (-1,-1), 1, ST_ACCENT),
        ('LINEABOVE', (0,1), (-1,1), 1, ST_ACCENT),
        ('PADDING', (0,0), (-1,-1), 8),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
    ]))
    story.append(journey_t)
    story.append(Spacer(1, 3*mm))

    # ── BOAT + PAYMENT ROW ───────────────────────────────────────────────────
    boat_payment_data = [
        [Paragraph("VESSEL", lbl), Paragraph("PASSENGERS", lbl), Paragraph("TOTAL PAID", lbl)],
        [Paragraph(f"🚤 {boat_name}", val_sm),
         Paragraph(str(booking.get("passenger_count",0)), val),
         Paragraph(f"MVR {booking.get('total_amount','0')}", val)],
    ]
    bp_t = Table(boat_payment_data, colWidths=[65*mm, 50*mm, 60*mm])
    bp_t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), ST_BLUE),
        ('TEXTCOLOR', (0,0), (-1,0), ST_WHITE),
        ('BACKGROUND', (0,1), (-1,1), ST_WHITE),
        ('BOX', (0,0), (-1,-1), 1, ST_BLUE),
        ('PADDING', (0,0), (-1,-1), 8),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
    ]))
    story.append(bp_t)
    story.append(Spacer(1, 4*mm))

    # ── PASSENGER TABLE ──────────────────────────────────────────────────────
    passengers = booking.get("passengers", [])
    if isinstance(passengers, str):
        try: passengers = json.loads(passengers)
        except: passengers = []

    if passengers:
        story.append(Paragraph("PASSENGER MANIFEST",
            ParagraphStyle('pmt', fontName='Helvetica-Bold', fontSize=9,
                           textColor=ST_NAVY, spaceBefore=2, spaceAfter=3)))
        pax_data = [[
            Paragraph("#", ParagraphStyle('ph', fontName='Helvetica-Bold', fontSize=8, textColor=ST_WHITE)),
            Paragraph("FULL NAME", ParagraphStyle('ph', fontName='Helvetica-Bold', fontSize=8, textColor=ST_WHITE)),
            Paragraph("ID / PASSPORT", ParagraphStyle('ph', fontName='Helvetica-Bold', fontSize=8, textColor=ST_WHITE)),
        ]]
        for i, p in enumerate(passengers, 1):
            row_bg = ST_LIGHT if i % 2 == 0 else ST_WHITE
            pax_data.append([
                Paragraph(str(i), ParagraphStyle('pv', fontName='Helvetica-Bold', fontSize=9, textColor=ST_BLUE)),
                Paragraph(p.get("name",""), ParagraphStyle('pv2', fontName='Helvetica', fontSize=9, textColor=ST_TEXT)),
                Paragraph(p.get("id_number",""), ParagraphStyle('pv3', fontName='Helvetica', fontSize=9, textColor=ST_TEXT)),
            ])
        pax_t = Table(pax_data, colWidths=[12*mm, 95*mm, 68*mm])
        pax_t.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), ST_NAVY),
            ('ROWBACKGROUNDS', (0,1), (-1,-1), [ST_WHITE, ST_LIGHT]),
            ('BOX', (0,0), (-1,-1), 0.5, ST_BLUE),
            ('INNERGRID', (0,0), (-1,-1), 0.3, HexColor('#D0E8F5')),
            ('PADDING', (0,0), (-1,-1), 7),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ]))
        story.append(pax_t)
        story.append(Spacer(1, 4*mm))

    # ── QR CODE ──────────────────────────────────────────────────────────────
    try:
        import qrcode as _qr
        passengers_list = booking.get("passengers", [])
        if isinstance(passengers_list, str):
            try: passengers_list = json.loads(passengers_list)
            except: passengers_list = []
        pax_names = ", ".join([p.get("name","") for p in passengers_list])
        # QR encodes a deep-link to verify via bot — staff scan to verify boarding
        qr_data = f"https://t.me/SamugaTravelsBot?start=verify_{booking['booking_ref']}"
        qr_img = _qr.make(qr_data)
        qr_buf = io.BytesIO()
        qr_img.save(qr_buf, format="PNG")
        qr_buf.seek(0)
        qr_rl = RLImage(qr_buf, width=28*mm, height=28*mm)

        # QR + footer text side by side
        qr_lbl = ParagraphStyle('qrl', fontName='Helvetica', fontSize=6.5, textColor=ST_MUTED, alignment=1)
        contact_text = (
            f"<b>Operator:</b> {operator.get('owner_contact','N/A')} &nbsp;|&nbsp; "
            f"<b>Business:</b> {operator.get('business_name','')} &nbsp;|&nbsp; "
            f"<b>Questions?</b> Contact your operator or Samuga Travels"
        )
        footer_left = [
            Paragraph(contact_text,
                ParagraphStyle('ctq', fontName='Helvetica', fontSize=7.5,
                               textColor=ST_MUTED, spaceAfter=3)),
            Paragraph(
                "■ <b>Present this ticket when boarding.</b> This is an official Samuga Travels booking ticket.",
                ParagraphStyle('f1q', fontName='Helvetica', fontSize=7.5,
                               textColor=ST_TEXT, spaceAfter=2)),
            Paragraph(
                f"<font color='#1B6CA8'><b>Samuga Travels</b></font> · Maldives · "
                f"Issued {datetime.now().strftime('%d %b %Y %H:%M')} MVT",
                ParagraphStyle('f2q', fontName='Helvetica', fontSize=7,
                               textColor=ST_MUTED)),
        ]
        footer_right = [
            qr_rl,
            Paragraph("Scan to verify", qr_lbl),
        ]
        story.append(HRFlowable(width="100%", thickness=1, color=ST_ACCENT, spaceBefore=2, spaceAfter=3*mm))
        footer_table = Table([[footer_left, footer_right]], colWidths=[140*mm, 35*mm])
        footer_table.setStyle(TableStyle([
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
            ('ALIGN',  (1,0), (1,0),   'CENTER'),
            ('LEFTPADDING',  (0,0), (-1,-1), 0),
            ('RIGHTPADDING', (0,0), (-1,-1), 0),
        ]))
        story.append(footer_table)
    except Exception as _qe:
        logger.error(f"QR code generation: {_qe}")
        # Fallback footer without QR
        story.append(HRFlowable(width="100%", thickness=1, color=ST_ACCENT, spaceBefore=2, spaceAfter=3*mm))
        story.append(Paragraph(
            f"<b>Operator Contact:</b> {operator.get('owner_contact','N/A')} | "
            f"<b>Business:</b> {operator.get('business_name','')}",
            ParagraphStyle('ctf', fontName='Helvetica', fontSize=8,
                           textColor=ST_MUTED, alignment=TA_CENTER)))
        story.append(Paragraph(
            "■ <b>Present this ticket when boarding.</b> This is an official Samuga Travels booking ticket.",
            ParagraphStyle('f1f', fontName='Helvetica', fontSize=8,
                           textColor=ST_TEXT, alignment=TA_CENTER)))

    doc.build(story)
    return buf.getvalue()

# ── KEYBOARDS ─────────────────────────────────────────────────────────────────
def main_kb(role="customer"):
    if role == "operator":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 My Profile",       callback_data="op_profile"),
             InlineKeyboardButton("🗓️ Add Schedule",     callback_data="op_schedules")],
            [InlineKeyboardButton("🚤 My Fleet",         callback_data="op_fleet"),
             InlineKeyboardButton("📦 Pending Bookings", callback_data="op_bookings")],
            [InlineKeyboardButton("📅 Today's Schedule", callback_data="op_today"),
             InlineKeyboardButton("📊 Monthly Report",   callback_data="op_monthly_report")],
            [InlineKeyboardButton("✏️ Edit Info",        callback_data="op_edit"),
             InlineKeyboardButton("🤖 Ask SamugaAI",    callback_data="op_ai_chat")],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚢 Book a Trip",            callback_data="cx_search"),
         InlineKeyboardButton("📋 My Bookings",            callback_data="cx_my_bookings")],
        [InlineKeyboardButton("🤖 Ask SamugaAI",          callback_data="cx_ai_chat")],
        [InlineKeyboardButton("🤝 Register as Operator",   callback_data="register_operator")],
    ])

def boat_type_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⛴️ Ferry Service",  callback_data="type_ferry")],
        [InlineKeyboardButton("🛥️ Private Hire",   callback_data="type_private")],
    ])

# ── COMMANDS ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    sd = await get_user_state(user.id)
    role = sd.get("role","customer")
    if role == "operator":
        op = await get_operator(user.id)
        if op and op.get("status") == "approved":
            role = "operator"
        else:
            role = "customer"
    await update.message.reply_text(
        f"🌊 *Welcome to Samuga Travels!*\n\n"
        f"Hi *{user.first_name}*! Book speedboats across the Maldives — fast, easy, trusted.\n\n"
        f"Just type your route to get started:\n"
        f"`Male to Thoddoo` · `Thoddoo to Male` · `Male to Maafushi`\n\n"
        f"Or tap a button below 👇",
        parse_mode="Markdown",
        reply_markup=main_kb(role))

async def cmd_verify(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Verify a booking — /verify ST-XXXXXX or via QR deep-link /start verify_ST-XXXXXX"""
    user = update.effective_user
    args = ctx.args or []
    ref = None
    if args and args[0].startswith("verify_"):
        ref = args[0].replace("verify_", "").strip().upper()
    elif args:
        ref = args[0].strip().upper()
    if not ref:
        await update.message.reply_text(
            "🔍 *Ticket Verification*\n\nUsage: `/verify ST-260629-1234`\n\nOr scan the QR code on the ticket.",
            parse_mode="Markdown")
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        bk = await conn.fetchrow("""
            SELECT b.*, o.business_name, o.owner_contact,
                   s.route_from, s.route_to, s.departure_time
            FROM bookings b
            JOIN operators o ON b.operator_id=o.id
            JOIN schedules s ON b.schedule_id=s.id
            WHERE b.booking_ref=$1
        """, ref)
    if not bk:
        await update.message.reply_text(
            f"❌ *Booking Not Found*\n\n`{ref}` does not exist.\n\nCheck the reference and try again.",
            parse_mode="Markdown")
        return
    passengers = bk["passengers"] or "[]"
    if isinstance(passengers, str):
        try: passengers = __import__("json").loads(passengers)
        except: passengers = []
    status_icons = {"confirmed":"✅","pending_confirmation":"⏳","pending_payment":"💳","cancelled":"❌"}
    icon = status_icons.get(bk["status"],"❓")
    boarded = bk.get("boarded_at")
    pax_list = "\n".join([f"  {i+1}. {p.get('name','')} ({p.get('id_number','')})"
                           for i,p in enumerate(passengers)])
    msg = (
        f"{icon} *Ticket Verification*\n\n"
        f"📋 Ref: `{bk['booking_ref']}`\n"
        f"📊 Status: *{bk['status'].upper().replace('_',' ')}*\n"
        f"🚤 {bk['business_name']}\n"
        f"📍 {bk['route_from']} → {bk['route_to']}\n"
        f"📅 {bk['travel_date']} @ {bk['departure_time']}\n"
        f"👥 Passengers:\n{pax_list}\n"
        f"💰 MVR {bk['total_amount']}\n"
    )
    if boarded:
        msg += f"\n🛳️ *Already boarded at {str(boarded)[:16]}* — ticket used."
        await update.message.reply_text(msg, parse_mode="Markdown")
    elif bk["status"] == "confirmed":
        msg += "\n✅ *Valid — not yet boarded.*"
        op = await get_operator(user.id)
        kb = None
        if user.id in SUPER_ADMINS or (op and op.get("id") == bk["operator_id"]):
            date_token = bk["travel_date"].strftime("%Y%m%d") if hasattr(bk["travel_date"], "strftime") else str(bk["travel_date"]).replace("-", "")
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🛳️ Mark as Boarded", callback_data=f"mark_boarded_{bk['id']}")],
                [InlineKeyboardButton("👥 Open Boarding Manifest", callback_data=f"op_manifest_{bk['schedule_id']}_{date_token}")]
            ])
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)
    else:
        msg += f"\n⚠️ Status is *{bk['status']}* — not yet confirmed."
        await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    sd = await get_user_state(user.id)
    role = sd.get("role","customer")
    await set_user_state(user.id, CX_IDLE if role != "operator" else OP_IDLE, {})
    await update.message.reply_text("❌ Cancelled. Back to main menu.", reply_markup=main_kb(role))

async def cmd_register(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await start_op_reg(update, ctx)

async def cmd_recommend(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /recommend <telegram_id> <review text>")
        return
    op_tid = int(args[0])
    review_text = " ".join(args[1:])
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE operators SET is_recommended=TRUE, review_text=$1 WHERE telegram_id=$2 RETURNING business_name, id",
            review_text, op_tid)
    if row:
        await update.message.reply_text(f"✅ *{row['business_name']}* is now Recommended!", parse_mode="Markdown")
        await ctx.bot.send_message(op_tid,
            f"🌟 *Congratulations!*\n\nYour business has been marked *Recommended by Samuga Travels!*\n\n💬 _{review_text}_",
            parse_mode="Markdown")

# ── ADMIN COMMANDS ────────────────────────────────────────────────────────────
def is_admin(user_id: int, chat_id: int) -> bool:
    return user_id in SUPER_ADMINS or chat_id == ADMIN_GROUP_ID

async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Main admin dashboard — /admin"""
    user = update.effective_user
    if not is_admin(user.id, update.effective_chat.id):
        await update.message.reply_text("⛔ Admin only.")
        return

    pool = await get_pool()
    async with pool.acquire() as conn:
        total_ops     = await conn.fetchval("SELECT COUNT(*) FROM operators WHERE status='approved'")
        pending_ops   = await conn.fetchval("SELECT COUNT(*) FROM operators WHERE status='pending'")
        total_bookings= await conn.fetchval("SELECT COUNT(*) FROM bookings")
        confirmed_bk  = await conn.fetchval("SELECT COUNT(*) FROM bookings WHERE status='confirmed'")
        total_revenue = await conn.fetchval("SELECT COALESCE(SUM(total_amount),0) FROM bookings WHERE status='confirmed'")
        total_customers = await conn.fetchval("SELECT COUNT(DISTINCT customer_telegram_id) FROM bookings")

    samuga_logo = await get_setting("samuga_logo_url", "")

    msg = (
        f"🛠️ *Samuga Travels — Admin Panel*\n\n"
        f"📊 *Platform Stats:*\n"
        f"  ✅ Approved Operators: *{total_ops}*\n"
        f"  ⏳ Pending Review: *{pending_ops}*\n"
        f"  🎫 Total Bookings: *{total_bookings}* ({confirmed_bk} confirmed)\n"
        f"  👥 Unique Customers: *{total_customers}*\n"
        f"  💰 Total Revenue: *MVR {total_revenue:.2f}*\n\n"
        f"🖼️ Samuga Logo: {'✅ Set' if samuga_logo else '❌ Not set'}\n\n"
        f"Choose an action below:"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Manage Operators",    callback_data="adm_operators"),
         InlineKeyboardButton("📦 All Bookings",        callback_data="adm_bookings")],
        [InlineKeyboardButton("📢 Broadcast Message",  callback_data="adm_broadcast"),
         InlineKeyboardButton("📊 Revenue Report",     callback_data="adm_revenue")],
        [InlineKeyboardButton("📡 Daily Control Room",  callback_data="adm_control_room")],
        [InlineKeyboardButton("🖼️ Upload Samuga Logo", callback_data="adm_upload_logo"),
         InlineKeyboardButton("⚙️ Settings",           callback_data="adm_settings")],
        [InlineKeyboardButton("🔍 Find Customer",      callback_data="adm_find_customer"),
         InlineKeyboardButton("🚤 All Schedules",      callback_data="adm_schedules")],
    ])
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)

async def admin_check(query, ctx) -> bool:
    """Check admin access for callbacks"""
    user = query.from_user
    if not is_admin(user.id, query.message.chat.id):
        await query.answer("⛔ Admin only.", show_alert=True)
        return False
    return True

async def cmd_urgent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Operator sends urgent review request"""
    user = update.effective_user
    op = await get_operator(user.id)
    if not op:
        await update.message.reply_text("⚠️ You don't have a pending application.")
        return
    if op["status"] == "approved":
        await update.message.reply_text("✅ Your account is already approved!")
        return
    if op["status"] == "rejected":
        await update.message.reply_text("❌ Your application was rejected. Contact @SamugaTravels.")
        return

    # Notify admin group with urgent flag
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id FROM operators WHERE telegram_id=$1", user.id)
    op_id = row["id"] if row else 0

    urgent_msg = (
        f"🚨 *URGENT REVIEW REQUEST*\n\n"
        f"👤 @{user.username or user.first_name} (`{user.id}`)\n"
        f"🏢 *{op['business_name']}*\n"
        f"🛥️ {op['boat_name']}\n\n"
        f"⚡ Operator is requesting urgent approval."
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve Now", callback_data=f"approve_op_{op_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"reject_op_{op_id}")
    ]])
    try:
        await ctx.bot.send_message(ADMIN_GROUP_ID, urgent_msg, parse_mode="Markdown",
                                   message_thread_id=ADMIN_THREAD_ID, reply_markup=kb)
        await update.message.reply_text(
            "🚨 *Urgent request sent!*\n\n"
            "Our team has been notified and will review your application as soon as possible.\n\n"
            "Thank you for your patience! 🙏",
            parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Urgent notify error: {e}")
        await update.message.reply_text("⚠️ Could not send request. Please contact @SamugaTravels directly.")

async def cmd_delete_my_data(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Allow operators/customers to request data deletion"""
    user = update.effective_user
    op = await get_operator(user.id)
    await update.message.reply_text(
        "🗑️ *Data Deletion Request*\n\n"
        "To delete your data from Samuga Travels, contact us directly:\n\n"
        "📩 @SamugaTravels\n\n"
        "We will remove:\n"
        "• Your operator profile and documents\n"
        "• Your booking history\n"
        "• Your uploaded photos and ID\n\n"
        "_Note: Confirmed booking records may be retained for up to 90 days for legal compliance._\n\n"
        "⚠️ This action is irreversible.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📩 Contact @SamugaTravels", url="https://t.me/SamugaTravels")
        ]]))

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Check application status"""
    user = update.effective_user
    op = await get_operator(user.id)
    if not op:
        await update.message.reply_text(
            "📋 You don't have an operator application.\n\nTap below to register!",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🤝 Register as Operator", callback_data="register_operator")
            ]]))
        return
    status_map = {
        "pending":  ("⏳", "Under Review", "Our team is reviewing your application."),
        "approved": ("✅", "Approved", "Your account is active. Use /start to manage."),
        "rejected": ("❌", "Rejected", "Contact @SamugaTravels for more info.")
    }
    icon, label, note = status_map.get(op["status"], ("❓","Unknown",""))
    rec = "🌟 *Recommended by Samuga Travels*\n" if op.get("is_recommended") else ""
    await update.message.reply_text(
        f"{icon} *Application Status: {label}*\n\n"
        f"🏢 {op['business_name']}\n"
        f"🛥️ {op['boat_name']}\n"
        f"{rec}"
        f"\n_{note}_\n\n"
        f"{'Type /urgent if you need urgent review.' if op['status'] == 'pending' else ''}",
        parse_mode="Markdown")

async def cmd_findcustomer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Find customer by booking ref or telegram ID"""
    user = update.effective_user
    if not is_admin(user.id, update.effective_chat.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    args = ctx.args
    if not args:
        await update.message.reply_text("Usage: `/findcustomer ST-260629-0389` or `/findcustomer 123456789`", parse_mode="Markdown")
        return
    query_str = args[0].strip()
    pool = await get_pool()
    async with pool.acquire() as conn:
        if query_str.startswith("ST-"):
            bk = await conn.fetchrow("""
                SELECT b.*, o.business_name FROM bookings b
                JOIN operators o ON b.operator_id=o.id
                WHERE b.booking_ref=$1
            """, query_str)
        else:
            try:
                tg_id = int(query_str)
                bk = await conn.fetchrow("""
                    SELECT b.*, o.business_name FROM bookings b
                    JOIN operators o ON b.operator_id=o.id
                    WHERE b.customer_telegram_id=$1 ORDER BY b.created_at DESC LIMIT 1
                """, tg_id)
            except ValueError:
                await update.message.reply_text("⚠️ Invalid format. Use booking ref or Telegram ID.")
                return
    if not bk:
        await update.message.reply_text("❌ No booking found.")
        return
    icons = {"pending_payment":"⏳","pending_confirmation":"🔄","confirmed":"✅","cancelled":"❌"}
    ic = icons.get(bk["status"],"❓")
    msg = (
        f"🔍 *Booking Found:*\n\n"
        f"{ic} `{bk['booking_ref']}`\n"
        f"👤 Customer: {bk['customer_name'] or 'N/A'}\n"
        f"🆔 Telegram ID: `{bk['customer_telegram_id']}`\n"
        f"🚤 Operator: {bk['business_name']}\n"
        f"📅 {bk['travel_date']} | 💰 MVR {bk['total_amount']}\n"
        f"📊 Status: *{bk['status'].upper()}*\n"
        f"🕐 Created: {str(bk['created_at'])[:16]}"
    )
    btns = [[InlineKeyboardButton("✉️ Message Customer", callback_data=f"msg_customer_{bk['customer_telegram_id']}")]]
    if bk["status"] == "pending_confirmation":
        btns.append([InlineKeyboardButton("✅ Force Confirm", callback_data=f"confirm_booking_{bk['id']}")])
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))

async def cmd_ops(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """List all approved operators"""
    user = update.effective_user
    if user.id not in SUPER_ADMINS and update.effective_chat.id != ADMIN_GROUP_ID:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        ops = await conn.fetch("SELECT id, business_name, boat_name, status, telegram_id FROM operators ORDER BY status, business_name")
    if not ops:
        await update.message.reply_text("No operators.")
        return
    msg = "📋 *All Operators:*\n\n"

    for op in ops:
        icon = {"pending":"⏳","approved":"✅","rejected":"❌"}.get(op["status"],"❓")
        msg += f"{icon} `{op['id']}` *{op['business_name']}* — {op['boat_name']}\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

# ── OPERATOR REGISTRATION ─────────────────────────────────────────────────────
async def start_op_reg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user if query else update.effective_user
    msg  = query.message   if query else update.message

    existing = await get_operator(user.id)
    if existing:
        s = existing.get("status")
        if s == "approved":
            await set_user_state(user.id, OP_IDLE, {}, role="operator")
            await msg.reply_text("✅ You're already a verified operator! Use /start to manage.")
            return
        elif s == "pending":
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🚨 Request Urgent Review", callback_data=f"urgent_review_{existing['id']}")
            ]])
            await msg.reply_text(
                "⏳ *Your application is under review.*\n\n"
                "Our team will notify you once approved.\n\n"
                "Need it urgently? Tap the button below to flag your application:",
                parse_mode="Markdown", reply_markup=kb)
            return
        elif s == "rejected":
            # Allow re-registration after rejection
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM operators WHERE telegram_id=$1", user.id)
            # Fall through to registration below

    await set_user_state(user.id, OP_AWAIT_BUSINESS_NAME, {}, role="operator_pending")
    await msg.reply_text(
        "🚤 *Operator Registration — Samuga Travels*\n\n"
        "*Step 1:* What is your *business/company name*?\n\n_Example: Thoddoo Express Travels_",
        parse_mode="Markdown")

# ── MESSAGE HANDLER ───────────────────────────────────────────────────────────
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    sd   = await get_user_state(user.id)
    state= sd.get("state", CX_IDLE)
    temp = sd.get("temp_data", {}) or {}
    text = (update.message.text or "").strip()

    # ── REFUND FLOW ──────────────────────────────────────────────────────────
    if state == CX_AWAIT_REFUND_ACCOUNT:
        if is_cancel(text):
            await set_user_state(user.id, CX_IDLE, {})
            await update.message.reply_text(
                "❌ Refund request cancelled.\n\n"
                "You can still contact the operator directly to arrange your refund.",
                reply_markup=main_kb("customer"))
            return
        # Parse account number + name
        parts = text.strip().split(" ", 1)
        if len(parts) < 2 or not parts[0].strip().isdigit():
            await update.message.reply_text(
                "⚠️ Please enter your *account number and name* in one line:\n\n"
                "_Example:_ `7770001234567 Ahmed Ali`\n\n"
                "Or type `cancel` to skip.",
                parse_mode="Markdown")
            return
        account_number = parts[0].strip()
        account_name   = parts[1].strip()
        bk_id   = temp.get("refund_booking_id")
        bk_ref  = temp.get("refund_booking_ref", "")
        amount  = temp.get("refund_amount", "0")
        op_tg   = temp.get("op_tg_id")
        op_name = temp.get("op_name","")
        op_cont = temp.get("op_contact","")

        # Save refund account to DB
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE bookings SET refund_account=$1, refund_account_name=$2, refund_status='requested'
                WHERE id=$3
            """, account_number, account_name, bk_id)

        await set_user_state(user.id, CX_IDLE, {})

        # Notify customer
        await update.message.reply_text(
            f"✅ *Refund Requested!*\n\n"
            f"📋 Booking: `{bk_ref}`\n"
            f"💰 Amount: *MVR {amount}*\n"
            f"🏦 Account: `{account_number}` — {account_name}\n\n"
            f"The operator has been notified and will process your refund.\n"
            f"You will receive a confirmation with the transfer slip once done.\n\n"
            f"📞 Operator: *{op_name}* | {op_cont}\n\n"
            f"_Refunds are typically processed within 1-3 business days._",
            parse_mode="Markdown",
            reply_markup=main_kb("customer"))

        # Notify operator with refund request + customer account details
        if op_tg:
            try:
                await ctx.bot.send_message(int(op_tg),
                    f"💸 *Refund Request*\n\n"
                    f"📋 Booking: `{bk_ref}`\n"
                    f"💰 Amount to refund: *MVR {amount}*\n\n"
                    f"*Customer bank account:*\n"
                    f"🏦 Account: `{account_number}`\n"
                    f"👤 Name: {account_name}\n\n"
                    f"Please transfer MVR {amount} to this account and upload the transfer slip below.\n"
                    f"The customer will receive the slip automatically. 🙏",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("📤 Upload Refund Slip", callback_data=f"upload_refund_{bk_id}")
                    ]]))
            except Exception as e:
                logger.error(f"Refund notify operator: {e}")
        return

    # ── ADMIN MESSAGE STATES ─────────────────────────────────────────────────
    if state == "admin_await_sub_fee":
        fee = parse_price(text)
        if not fee or fee <= 0:
            await update.message.reply_text("⚠️ Enter valid amount e.g. `500`", parse_mode="Markdown")
            return
        await set_setting("subscription_fee", str(int(fee)))
        await set_user_state(user.id, CX_IDLE, {})
        await update.message.reply_text(
            f"✅ Subscription fee set to *MVR {int(fee)}/month*", parse_mode="Markdown")
        return

    elif state == "admin_await_sub_accounts":
        import json as _j
        lines = [l.strip() for l in text.strip().split("\n") if l.strip()]
        accounts = []
        for line in lines:
            parts = line.split(" ", 2)
            if len(parts) >= 2:
                accounts.append({
                    "bank": parts[0].upper(),
                    "number": parts[1],
                    "name": parts[2] if len(parts) > 2 else ""
                })
        if not accounts:
            await update.message.reply_text("⚠️ Couldn't read accounts. Try:\n`BML 7770001234 Samuga Travels`", parse_mode="Markdown")
            return
        await set_setting("subscription_accounts", _j.dumps(accounts))
        await set_user_state(user.id, CX_IDLE, {})
        lines_out = "\n".join([f"🏦 {a['bank']}: {a['number']} — {a['name']}" for a in accounts])
        await update.message.reply_text(
            f"✅ *Payment accounts saved!*\n\n{lines_out}\n\n"
            f"Operators will see these when paying their subscription.",
            parse_mode="Markdown")
        return

    elif state == ADMIN_AWAIT_BROADCAST:
        if is_cancel(text):
            await set_user_state(user.id, CX_IDLE, {})
            await update.message.reply_text("❌ Broadcast cancelled.")
            return
        pool = await get_pool()
        async with pool.acquire() as conn:
            operators = await conn.fetch("SELECT telegram_id, business_name FROM operators WHERE status='approved'")
        sent = 0
        failed = 0
        for op in operators:
            try:
                await ctx.bot.send_message(op["telegram_id"],
                    f"📢 *Message from Samuga Travels:*\n\n{text}",
                    parse_mode="Markdown")
                sent += 1
            except Exception:
                failed += 1
        await set_user_state(user.id, CX_IDLE, {})
        await update.message.reply_text(
            f"📢 *Broadcast Complete!*\n\n✅ Sent: {sent}\n❌ Failed: {failed}",
            parse_mode="Markdown")
        return

    elif state == ADMIN_AWAIT_REVIEW_TEXT:
        op_id = (sd.get("temp_data") or {}).get("review_op_id")
        if op_id and not is_cancel(text):
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "UPDATE operators SET is_recommended=TRUE, review_text=$1 WHERE id=$2 RETURNING telegram_id, business_name",
                    text, op_id)
            if row:
                await ctx.bot.send_message(row["telegram_id"],
                    f"🌟 *Congratulations!*\n\nYour business is now *Recommended by Samuga Travels!*\n\n💬 _{text}_",
                    parse_mode="Markdown")
                await update.message.reply_text(f"🌟 *{row['business_name']}* is now Recommended!", parse_mode="Markdown")
        await set_user_state(user.id, CX_IDLE, {})
        return

    # ── SAMUGAAI CHAT STATES ─────────────────────────────────────────────────
    if state in [CX_AI_CHAT, OP_AI_CHAT]:
        if is_cancel(text):
            role = sd.get("role","customer")
            op = await get_operator(user.id)
            back_state = OP_IDLE if (op and op.get("status") == "approved") else CX_IDLE
            await set_user_state(user.id, back_state, {})
            await update.message.reply_text(
                "👋 Left SamugaAI. Back to main menu!",
                reply_markup=main_kb("operator" if (op and op.get("status") == "approved") else "customer"))
            return
        allowed, remaining = await _ai_check_limit(user.id)
        if not allowed:
            await update.message.reply_text(
                "🤖 You've used your 10 free SamugaAI questions for today.\n\n"
                "Come back tomorrow for more! 🙏",
                parse_mode="Markdown")
            return
        await _ai_increment(user.id)
        thinking = await update.message.reply_text("🤖 _SamugaAI is thinking..._", parse_mode="Markdown")
        role = "operator" if state == OP_AI_CHAT else "customer"
        op = await get_operator(user.id)
        ctx_data = {"operator_name": op.get("business_name") if op else None}
        answer = await ask_samuga_ai(text, role, ctx_data)
        try:
            await ctx.bot.delete_message(user.id, thinking.message_id)
        except: pass
        await update.message.reply_text(
            answer + f"\n\n_({remaining-1} questions left today)_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ End Chat", callback_data="ai_end_chat")
            ]]))
        return

    # ── GLOBAL CANCEL CHECK ──────────────────────────────────────────────────
    if is_cancel(text) and state not in [CX_IDLE, OP_IDLE]:
        role = sd.get("role","customer")
        if role == "operator":
            op = await get_operator(user.id)
            role = "operator" if (op and op.get("status")=="approved") else "customer"
        await set_user_state(user.id, CX_IDLE if role != "operator" else OP_IDLE, {})
        await update.message.reply_text(
            "❌ Cancelled. Back to main menu.",
            reply_markup=main_kb(role))
        return

    # ── OPERATOR REG FLOW ─────────────────────────────────────────────────────
    if state == OP_AWAIT_BUSINESS_NAME:
        await set_user_state(user.id, OP_AWAIT_BOAT_NAME, {**temp, "business_name": text})
        await update.message.reply_text(
            "✅ Got it!\n\n*Step 2:* What is your *boat name*?\n\n_Example: Ocean Star_",
            parse_mode="Markdown")

    elif state == OP_AWAIT_BOAT_NAME:
        await set_user_state(user.id, OP_AWAIT_SEATS, {**temp, "boat_name": text})
        await update.message.reply_text(
            "✅ Got it!\n\n*Step 3:* How many *seats* does your boat have?\n\n_Enter a number, e.g. 20_",
            parse_mode="Markdown")

    elif state == OP_AWAIT_SEATS:
        seat_num = parse_number(text)
        if not seat_num or seat_num < 1:
            await update.message.reply_text("⚠️ Please enter a valid number e.g. `20`", parse_mode="Markdown")
            return
        await set_user_state(user.id, OP_AWAIT_TYPE, {**temp, "seat_count": seat_num})
        await update.message.reply_text(
            "✅ Got it!\n\n*Step 4:* What type of service?",
            parse_mode="Markdown", reply_markup=boat_type_kb())
    elif state == OP_AWAIT_ROUTES:
        stops = [s.strip() for s in text.split(",") if s.strip()]
        if len(stops) < 2:
            await update.message.reply_text(
                "⚠️ Enter at least 2 stops separated by commas.\n\n"
                "_Example: `Male, Dhigurah, Thoddoo, Dhagethi`_",
                parse_mode="Markdown")
            return
        route_display = " → ".join(stops)
        await set_user_state(user.id, OP_AWAIT_OWNER_NAME, {**temp, "routes": stops, "route_display": route_display})
        await update.message.reply_text(
            f"✅ Route saved!\n\n📍 *{route_display}*\n\n*Step 6:* What is the *owner's full name*?",
            parse_mode="Markdown")

    elif state == OP_AWAIT_OWNER_NAME:
        await set_user_state(user.id, OP_AWAIT_OWNER_CONTACT, {**temp, "owner_name": text})
        await update.message.reply_text(
            "✅ Got it!\n\n*Step 7:* Owner's *contact number*?\n\n_Example: 7771234_",
            parse_mode="Markdown")

    elif state == OP_AWAIT_OWNER_CONTACT:
        await set_user_state(user.id, OP_AWAIT_OWNER_ID_PHOTO, {**temp, "owner_contact": text})
        await update.message.reply_text(
            "✅ Got it!\n\n*Step 8:* Please upload a *photo of the owner's ID card or passport*.\n\n"
            "🔒 *Privacy Notice:* Your ID is used only for operator verification by Samuga Travels admin. "
            "It will not be shown publicly or shared with other operators. Only Samuga Travels admin can use it for verification.\n\n"
            "_This is required by Samuga Travels to ensure all operators are legitimate._",
            parse_mode="Markdown")

    elif state == OP_AWAIT_BML_ACCOUNT:
        parts = text.strip().split(" ", 1)
        acct_num = parts[0].strip()
        acct_name = parts[1].strip() if len(parts) > 1 else ""
        bml_entry = f"{acct_num}|{acct_name}" if acct_name else acct_num
        await set_user_state(user.id, OP_AWAIT_MIB_ACCOUNT, {**temp, "bml_account": bml_entry})
        await update.message.reply_text(
            "✅ *BML account saved!*\n\n"
            "Do you also have an *MIB (Maldives Islamic Bank)* account?\n\n"
            "_Enter number and account name e.g:_\n`90101480050561001 Samuga Travels`\n\n"
            "_Or type_ *skip* _if not._",
            parse_mode="Markdown")

    elif state == OP_AWAIT_MIB_ACCOUNT:
        if is_skip(text):
            mib_entry = ""
        else:
            parts = text.strip().split(" ", 1)
            acct_num = parts[0].strip()
            acct_name = parts[1].strip() if len(parts) > 1 else ""
            mib_entry = f"{acct_num}|{acct_name}" if acct_name else acct_num
        final_temp = {**temp, "mib_account": mib_entry}
        op_id = await save_operator(user, final_temp)
        await notify_admin_new_op(ctx, user, final_temp, op_id=op_id)
        await set_user_state(user.id, OP_REGISTERED, {})
        await update.message.reply_text(
            "🎉 *Registration Complete!*\n\n"
            "Your application has been submitted to Samuga Travels for review.\n\n"
            "⏳ We\'ll verify your details and notify you here within 24 hours. Thank you! 🌊",
            parse_mode="Markdown")

    # ── SCHEDULE CHANGE HANDLERS ─────────────────────────────────────────────────
    elif state == OP_AWAIT_CHANGE_NOTE:
        t2 = temp or {}
        change_type = t2.get("change_type")
        sched_id = t2.get("change_sched_id")
        from datetime import timedelta as _td4
        tomorrow = datetime.now().date() + _td4(days=1)
        pool = await get_pool()

        if change_type == "time":
            new_time_val = parse_time_24hr(text.strip())
            if not new_time_val:
                await update.message.reply_text(
                    "⚠️ Use 24-hour format e.g. `16:00` or `06:45`",
                    parse_mode="Markdown")
                return
            async with pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO schedule_changes (schedule_id, change_date, new_time, note)
                    VALUES ($1,$2,$3,'Time changed by operator')
                    ON CONFLICT DO NOTHING
                """, sched_id, tomorrow, new_time_val)
                sched = await conn.fetchrow("SELECT * FROM schedules WHERE id=$1", sched_id)
                bookings = await conn.fetch("""
                    SELECT customer_telegram_id, booking_ref FROM bookings
                    WHERE schedule_id=$1 AND travel_date=$2 AND status='confirmed'
                """, sched_id, tomorrow)
            await set_user_state(user.id, OP_IDLE, {})
            await update.message.reply_text(
                f"✅ Departure time updated to *{new_time_val}* for tomorrow.",
                parse_mode="Markdown", reply_markup=main_kb("operator"))
            for bk in bookings:
                try:
                    await ctx.bot.send_message(bk["customer_telegram_id"],
                        f"⏰ *Schedule Update*\n\nYour booking `{bk['booking_ref']}` has a time change:\n\n"
                        f"New departure time: *{new_time_val}*\n"
                        f"📌 {sched.get('location','Jetty No. 1, Male')}\n\nSorry for any inconvenience! 🙏",
                        parse_mode="Markdown")
                except: pass

        elif change_type == "route":
            stops = [s.strip().title() for s in text.split(",") if s.strip()]
            if len(stops) < 2:
                await update.message.reply_text("⚠️ Enter at least 2 stops e.g. `Male, Gulhi, Maafushi`", parse_mode="Markdown")
                return
            route_display = " → ".join(stops)
            import json as _j
            async with pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO schedule_changes (schedule_id, change_date, note)
                    VALUES ($1,$2,$3)
                    ON CONFLICT DO NOTHING
                """, sched_id, tomorrow, f"Route changed: {route_display}")
                bookings = await conn.fetch("""
                    SELECT customer_telegram_id, booking_ref FROM bookings
                    WHERE schedule_id=$1 AND travel_date=$2 AND status='confirmed'
                """, sched_id, tomorrow)
            await set_user_state(user.id, OP_IDLE, {})
            await update.message.reply_text(
                f"✅ Route updated to *{route_display}* for tomorrow.",
                parse_mode="Markdown", reply_markup=main_kb("operator"))
            for bk in bookings:
                try:
                    await ctx.bot.send_message(bk["customer_telegram_id"],
                        f"🗺️ *Route Update*\n\nYour booking `{bk['booking_ref']}` route has changed:\n\n"
                        f"New route: *{route_display}*\n\nSorry for any inconvenience! 🙏",
                        parse_mode="Markdown")
                except: pass

    # ── BULK SCHEDULE SETUP FLOW ─────────────────────────────────────────────────
    elif state == OP_BULK_LOCATION:
        import re as _re2
        # Accept "Male to Airport to Thoddoo" format — one or multiple routes per line
        lines_raw = [l.strip() for l in text.strip().split("\n") if l.strip()]
        # Parse each line as a route with stops separated by "to"
        all_route_lines = []
        for line in lines_raw:
            stops_in_line = [s.strip().title() for s in _re2.split(r"\bto\b", line, flags=_re2.IGNORECASE) if s.strip()]
            if len(stops_in_line) >= 2:
                all_route_lines.append(stops_in_line)
        if not all_route_lines:
            await update.message.reply_text(
                "⚠️ Couldn\'t read routes. Try:\n\n"
                "`Male to Airport to Thoddoo`\n"
                "`Thoddoo to Airport to Male`",
                parse_mode="Markdown")
            return
        # Use all unique stops from first route as the stop list
        all_stops = all_route_lines[0]
        route_display = "\n".join([" → ".join(r) for r in all_route_lines])
        await set_user_state(user.id, OP_BULK_SATHU_DEPS,
                             {**temp, "bulk_stops": all_stops,
                              "bulk_routes": all_route_lines,
                              "bulk_route": route_display})
        await update.message.reply_text(
            f"✅ Routes saved!\n📍 {route_display}\n\n"
            f"*Step 2:* What is your *departure location/jetty*?\n\n"
            f"_Example: Jetty No. 1, Male_",
            parse_mode="Markdown")

    elif state == OP_BULK_SATHU_DEPS:
        # First message after route — might be location or departures
        if not temp.get("bulk_location"):
            # This is the location step
            location = text.strip()
            await set_user_state(user.id, OP_BULK_SATHU_DEPS,
                                 {**temp, "bulk_location": location})
            stops = temp.get("bulk_stops", [])
            route_display = temp.get("bulk_route", "")
            stops = temp.get("bulk_stops", [])
            bulk_routes = temp.get("bulk_routes", [[stops[0], stops[-1]], [stops[-1], stops[0]]])
            # Build example using full route strings
            def route_str(r): return " to ".join(r)
            example_lines = []
            example_lines.append(f"10:15 {route_str(bulk_routes[0])}")
            example_lines.append(f"16:00 {route_str(bulk_routes[0])}")
            if len(bulk_routes) > 1:
                example_lines.append(f"06:45 {route_str(bulk_routes[1])}")
                example_lines.append(f"13:00 {route_str(bulk_routes[1])}")
            example = "\n".join(example_lines)
            await update.message.reply_text(
                f"✅ Location: *{location}*\n\n"
                f"*Step 3:* Enter your *Saturday–Thursday departures*\n"
                f"_One per line: TIME then full route_\n\n"
                f"_Example:_\n`{example}`\n\n"
                f"_24hr or 12hr time both work!_",
                parse_mode="Markdown")
            return
        # This is the Sat-Thu departures
        deps = parse_bulk_departures(text)
        if not deps:
            await update.message.reply_text(
                "⚠️ Couldn't read departures. Use format:\n"
                "`10:15 Male to Thoddoo`\n"
                "`06:45 Thoddoo to Male`",
                parse_mode="Markdown")
            return
        await set_user_state(user.id, OP_BULK_FRI_DEPS,
                             {**temp, "bulk_sathu": deps})
        await update.message.reply_text(
            f"✅ Got *{len(deps)} Sat–Thu departures!*\n\n"
            f"*Step 4:* Do your *Friday departures differ*?\n\n"
            f"• Type your Friday departures if different\n"
            f"• Type `same` if Friday is the same\n"
            f"• Type `skip` if you don't operate on Fridays",
            parse_mode="Markdown")

    elif state == OP_BULK_FRI_DEPS:
        if not temp.get("bulk_price"):
            # This is Friday deps step
            fri_deps = None
            if is_skip(text):
                fri_deps = []
            elif text.strip().lower() == "same":
                fri_deps = temp.get("bulk_sathu", [])
            else:
                fri_deps = parse_bulk_departures(text)
                if fri_deps is None:
                    await update.message.reply_text(
                        "⚠️ Couldn't read Friday departures.\n"
                        "Type `same`, `skip`, or list times like:\n`10:00 Male to Thoddoo`",
                        parse_mode="Markdown")
                    return
            await set_user_state(user.id, OP_BULK_PRICE,
                                 {**temp, "bulk_fri": fri_deps})
            fri_msg = f"{len(fri_deps)} Friday departures" if fri_deps else "No Friday service"
            await update.message.reply_text(
                f"✅ *{fri_msg}*\n\n"
                f"*Step 5:* What is the *price per seat* (MVR)?\n\n"
                f"_Example: 535_",
                parse_mode="Markdown")
            return

    elif state == OP_BULK_PRICE:
        price = parse_price(text)
        if not price or price <= 0:
            await update.message.reply_text("⚠️ Enter valid price e.g. `535`", parse_mode="Markdown")
            return
        await set_user_state(user.id, OP_BULK_SEATS, {**temp, "bulk_price": price})
        await update.message.reply_text(
            f"✅ Price: *MVR {price}/seat*\n\n"
            f"*Step 6:* How many *seats per departure*?\n\n"
            f"_Example: 18_",
            parse_mode="Markdown")

    elif state == OP_BULK_SEATS:
        seats = parse_number(text)
        if not seats or seats < 1:
            await update.message.reply_text("⚠️ Enter valid number e.g. `18`", parse_mode="Markdown")
            return
        # Build all schedules
        op = await get_operator(user.id)
        stops       = temp.get("bulk_stops", [])
        location    = temp.get("bulk_location", "Jetty No. 1, Male")
        price       = temp.get("bulk_price", 0)
        sathu_deps  = temp.get("bulk_sathu", [])
        fri_deps    = temp.get("bulk_fri", [])
        import json as _j

        pool = await get_pool()
        created = 0
        async with pool.acquire() as conn:
            await conn.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS sched_stops TEXT DEFAULT '[]'")
            await conn.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS location TEXT DEFAULT 'Jetty No. 1, Male'")
            await conn.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS run_days TEXT DEFAULT 'daily'")
            await conn.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS boat_name TEXT")
            for dep in sathu_deps:
                dep_stops = dep.get("stops", [dep["from"], dep["to"]])
                await conn.execute("""
                    INSERT INTO schedules (operator_id, route_from, route_to, departure_time,
                                           price_per_seat, total_seats, available_seats,
                                           sched_stops, location, run_days)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'sat-thu')
                """, op["id"], dep["from"], dep["to"], dep["time"],
                    price, seats, seats, _j.dumps(dep_stops), location)
                created += 1
            for dep in fri_deps:
                dep_stops = dep.get("stops", [dep["from"], dep["to"]])
                await conn.execute("""
                    INSERT INTO schedules (operator_id, route_from, route_to, departure_time,
                                           price_per_seat, total_seats, available_seats,
                                           sched_stops, location, run_days)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'fri')
                """, op["id"], dep["from"], dep["to"], dep["time"],
                    price, seats, seats, _j.dumps(dep_stops), location)
                created += 1

        await set_user_state(user.id, OP_IDLE, {})

        # Build summary with full route per line
        route_display = temp.get("bulk_route","")
        sathu_lines = "\n".join([f"  ⏰ {d['time']} — {d.get('full_route', d['from']+' → '+d['to'])}" for d in sathu_deps])
        fri_lines   = "\n".join([f"  ⏰ {d['time']} — {d.get('full_route', d['from']+' → '+d['to'])}" for d in fri_deps]) if fri_deps else "  _No Friday service_"

        await update.message.reply_text(
            f"🎉 *{created} Schedules Created!*\n\n"
            f"📍 Route: *{route_display}*\n"
            f"📌 Location: *{location}*\n"
            f"💰 Price: *MVR {price}/seat*\n"
            f"💺 Seats: *{seats} per departure*\n\n"
            f"*Sat–Thu ({len(sathu_deps)} departures):*\n{sathu_lines}\n\n"
            f"*Friday ({len(fri_deps)} departures):*\n{fri_lines}\n\n"
            f"✅ All schedules are now live for customers!",
            parse_mode="Markdown", reply_markup=main_kb("operator"))

    # ── FLEET / BOAT ADD FLOW ────────────────────────────────────────────────────
    elif state == OP_AWAIT_BOAT_ADD_NAME:
        boat_name = text.strip()
        await set_user_state(user.id, OP_AWAIT_BOAT_ADD_CAPACITY, {**temp, "new_boat_name": boat_name})
        await update.message.reply_text(
            f"🚤 *{boat_name}*\n\nHow many passengers can this boat carry?",
            parse_mode="Markdown")

    elif state == OP_AWAIT_BOAT_ADD_CAPACITY:
        capacity = parse_number(text)
        if not capacity or capacity < 1:
            await update.message.reply_text("⚠️ Enter a valid number e.g. `20`", parse_mode="Markdown")
            return
        op = await get_operator(user.id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO boats (operator_id, boat_name, capacity) VALUES ($1,$2,$3)",
                op["id"], temp.get("new_boat_name"), capacity)
            boats = await conn.fetch("SELECT * FROM boats WHERE operator_id=$1 AND status='active'", op["id"])
        await set_user_state(user.id, OP_IDLE, {})
        fleet_list = "\n".join([f"  🚤 {b['boat_name']} ({b['capacity']} seats)" for b in boats])
        await update.message.reply_text(
            f"✅ *{temp.get('new_boat_name')}* added to your fleet!\n\n"
            f"*Your Fleet:*\n{fleet_list}",
            parse_mode="Markdown", reply_markup=main_kb("operator"))

    # ── SCHEDULE FLOW ─────────────────────────────────────────────────────────
    elif state == OP_AWAIT_SCHEDULE_ROUTE:
        stops = [s.strip().title() for s in text.split(",") if s.strip()]
        if len(stops) < 2:
            # Also support "Male to Thoddoo" format as 2-stop
            parts = [p.strip().title() for p in text.split("to", 1)]
            if len(parts) == 2 and parts[0] and parts[1]:
                stops = parts
            else:
                await update.message.reply_text(
                    "⚠️ Enter stops comma-separated or use 'from to destination'\n\n"
                    "_Single route: `Male, Thoddoo`_\n"
                    "_Multi-stop: `Male, Dhigurah, Thoddoo, Dhagethi`_",
                    parse_mode="Markdown")
                return
        route_display = " → ".join(stops)
        sched_from = stops[0]
        sched_to = stops[-1]
        await set_user_state(user.id, OP_AWAIT_SCHEDULE_TIME,
                             {**temp, "sched_from": sched_from, "sched_to": sched_to,
                              "sched_stops": stops, "route_display": route_display})
        await update.message.reply_text(
            f"✅ Route saved!\n\n📍 *{route_display}*\n\nWhat is the *departure time*? *(24hr format)*\n\n_Example: `16:00` or `06:45`_",
            parse_mode="Markdown")

    elif state == OP_AWAIT_SCHEDULE_TIME:
        parsed_time = parse_time_24hr(text)
        if not parsed_time:
            await update.message.reply_text(
                "⚠️ Please use *24-hour format*\n\n"
                "_Examples:_\n`16:00` not `4:00PM`\n`06:45` not `6:45am`\n`10:15`",
                parse_mode="Markdown")
            return
        await set_user_state(user.id, OP_AWAIT_SCHEDULE_PRICE, {**temp, "sched_time": parsed_time})
        await update.message.reply_text(
            "✅ Time saved!\n\nWhat is the *price per seat* (MVR)?",
            parse_mode="Markdown")

    elif state == OP_AWAIT_SCHEDULE_PRICE:
        price = parse_price(text)
        if price is None or price <= 0:
            await update.message.reply_text("⚠️ Enter a valid price e.g. `535` or `535MVR`", parse_mode="Markdown")
            return
        await set_user_state(user.id, OP_AWAIT_SCHEDULE_SEATS, {**temp, "sched_price": price})
        await update.message.reply_text("✅ Price saved!\n\nHow many *available seats* for this schedule?",
                                        parse_mode="Markdown")

    elif state == OP_AWAIT_SCHEDULE_SEATS:
        seats = parse_number(text)
        if not seats or seats < 1:
            await update.message.reply_text("⚠️ Enter a valid number e.g. `18`", parse_mode="Markdown")
            return
        sd2 = await get_user_state(user.id)
        t2  = sd2.get("temp_data", {}) or {}
        op  = await get_operator(user.id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            import json as _ji
            await conn.execute("""
                ALTER TABLE schedules ADD COLUMN IF NOT EXISTS sched_stops TEXT DEFAULT '[]'
            """)
            await conn.execute("""
                INSERT INTO schedules (operator_id, route_from, route_to, departure_time,
                                       price_per_seat, total_seats, available_seats, sched_stops)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            """, op["id"], t2.get("sched_from"), t2.get("sched_to"),
                t2.get("sched_time"), t2.get("sched_price"), seats, seats,
                _ji.dumps(t2.get("sched_stops", [])))
        # Ask for location next
        await set_user_state(user.id, OP_AWAIT_SCHEDULE_LOCATION,
                             {**t2, "sched_seats": seats})
        await update.message.reply_text(
            f"✅ {seats} seats saved!\n\n"
            f"📍 *What is the departure location/jetty?*\n\n"
            f"_Example: Jetty No. 1, Male_ or _Thoddoo Jetty_",
            parse_mode="Markdown")

    elif state == OP_AWAIT_SCHEDULE_LOCATION:
        location = text.strip() or "Jetty No. 1, Male"
        await set_user_state(user.id, OP_AWAIT_SCHEDULE_DAYS, {**temp, "sched_location": location})
        await update.message.reply_text(
            f"✅ Location: *{location}*\n\n"
            f"📅 *Which days does this schedule run?*\n\n"
            f"_Type one of:_\n"
            f"• `daily` — Every day\n"
            f"• `sat-thu` — Saturday to Thursday\n"
            f"• `fri` — Fridays only\n"
            f"• `weekdays` — Sunday to Thursday\n"
            f"• `weekend` — Friday & Saturday",
            parse_mode="Markdown")

    elif state == OP_AWAIT_SCHEDULE_DAYS:
        days_input = text.strip().lower()
        valid_days = ["daily","sat-thu","fri","weekdays","weekend","sun-thu","everyday"]
        run_days = days_input if days_input in valid_days else "daily"
        t2 = temp
        op = await get_operator(user.id)
        # Get operator's boats
        pool = await get_pool()
        async with pool.acquire() as conn:
            boats_list = await conn.fetch("SELECT * FROM boats WHERE operator_id=$1 AND status='active'", op["id"])
        if boats_list:
            # Let operator pick which boat runs this schedule
            boat_buttons = [[InlineKeyboardButton(f"🚤 {b['boat_name']} ({b['capacity']} seats)",
                callback_data=f"sched_boat_{b['id']}_{b['boat_name']}")] for b in boats_list]
            boat_buttons.append([InlineKeyboardButton("➕ Use Default (no specific boat)", callback_data="sched_boat_0_default")])
            # Save days in state first
            import json as _j
            await set_user_state(user.id, OP_AWAIT_SCHEDULE_DAYS,
                                 {**t2, "sched_location": t2.get("sched_location","Jetty No. 1, Male"),
                                  "run_days": run_days, "awaiting_boat_select": True})
            await update.message.reply_text(
                f"✅ Days: *{run_days}*\n\n🚤 *Which boat runs this schedule?*",
                parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(boat_buttons))
        else:
            # No boats added yet — save directly
            import json as _j
            seats = t2.get("sched_seats", t2.get("sched_price",0))
            async with pool.acquire() as conn:
                await conn.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS sched_stops TEXT DEFAULT '[]'")
                await conn.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS location TEXT DEFAULT 'Jetty No. 1, Male'")
                await conn.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS run_days TEXT DEFAULT 'daily'")
                await conn.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS boat_name TEXT")
                await conn.execute("""
                    INSERT INTO schedules (operator_id, route_from, route_to, departure_time,
                                           price_per_seat, total_seats, available_seats,
                                           sched_stops, location, run_days)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                """, op["id"], t2.get("sched_from"), t2.get("sched_to"),
                    t2.get("sched_time"), t2.get("sched_price"),
                    t2.get("sched_seats",0), t2.get("sched_seats",0),
                    _j.dumps(t2.get("sched_stops",[])),
                    t2.get("sched_location","Jetty No. 1, Male"), run_days)
            await set_user_state(user.id, OP_IDLE, {})
            await update.message.reply_text(
                f"✅ *Schedule Added!*\n\n"
                f"📍 {t2.get('sched_from')} → {t2.get('sched_to')}\n"
                f"⏰ {t2.get('sched_time')} | 📅 {run_days}\n"
                f"📌 {t2.get('sched_location','Jetty No. 1, Male')}\n"
                f"💰 MVR {t2.get('sched_price')}/seat | 👥 {t2.get('sched_seats',0)} seats\n\n"
                f"💡 Tip: Add your boats with the *🚤 My Fleet* button!",
                parse_mode="Markdown", reply_markup=main_kb("operator"))

    # ── CUSTOMER FLOW ─────────────────────────────────────────────────────────
    elif state == CX_AWAIT_DATE:
        travel_date = parse_date_flexible(text)
        if not travel_date:
            await update.message.reply_text(
                "⚠️ Couldn\'t read that date 😅\n\nTry formats like:\n`30-06-2026` or `30/06/2026`",
                parse_mode="Markdown")
            return
        if travel_date < datetime.now().date():
            await update.message.reply_text("⚠️ Date cannot be in the past.")
            return

        route_from = temp.get("route_from","")
        route_to   = temp.get("route_to","")
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT s.*, s.sched_stops, o.id as operator_id, o.business_name, o.boat_name, o.logo_url,
                       o.is_recommended, o.average_rating, o.total_reviews, o.owner_contact,
                       o.review_text, o.bml_account, o.payment_accounts, o.telegram_id as op_telegram_id
                FROM schedules s
                JOIN operators o ON s.operator_id = o.id
                WHERE LOWER(s.route_from) LIKE $1 AND LOWER(s.route_to) LIKE $2
                  AND o.status='approved' AND s.is_active=TRUE AND s.available_seats>0
                  AND COALESCE(o.subscription_status,'trial') != 'expired'
                  AND (
                    s.run_days = 'daily' OR s.run_days IS NULL
                    OR (s.run_days = 'fri'     AND EXTRACT(DOW FROM $3::date) = 5)
                    OR (s.run_days = 'sat-thu' AND EXTRACT(DOW FROM $3::date) != 5)
                    OR (s.run_days = 'weekdays' AND EXTRACT(DOW FROM $3::date) BETWEEN 0 AND 4)
                    OR (s.run_days = 'weekend'  AND EXTRACT(DOW FROM $3::date) IN (5,6))
                    OR (s.run_days = 'sun-thu'  AND EXTRACT(DOW FROM $3::date) BETWEEN 0 AND 4)
                    OR (s.run_days = 'everyday')
                  )
                ORDER BY o.is_recommended DESC, s.departure_time ASC
            """, f"%{route_from.lower()}%", f"%{route_to.lower()}%", travel_date)

        if not rows:
            await update.message.reply_text(
                f"😔 No boats found for *{route_from} → {route_to}* on *{text}*.\n\nTry a different date or route.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔍 Search Again", callback_data="cx_search")]]))
            await set_user_state(user.id, CX_IDLE, {})
            return

        schedules = [dict(r) for r in rows]
        # Store only IDs in state to avoid temp_data size limit; cache full data in context
        sched_ids = [s["id"] for s in schedules]
        ctx.user_data["schedules_cache"] = schedules
        await set_user_state(user.id, CX_AWAIT_PASSENGER_COUNT,
                             {**temp, "travel_date": str(travel_date), "sched_ids": sched_ids})

        # Sort options row
        sort_key = temp.get("sort_by", "recommended")
        if sort_key == "earliest":
            schedules = sorted(schedules, key=lambda x: x["departure_time"])
        elif sort_key == "cheapest":
            schedules = sorted(schedules, key=lambda x: float(x["price_per_seat"] or 0))
        elif sort_key == "seats":
            schedules = sorted(schedules, key=lambda x: x["available_seats"], reverse=True)
        else:  # recommended (default)
            schedules = sorted(schedules, key=lambda x: (not x.get("is_recommended"), x["departure_time"]))

        sort_labels = {"recommended":"⭐ Rec","earliest":"⏰ Early","cheapest":"💰 Cheap","seats":"💺 Seats"}
        sort_row = [
            InlineKeyboardButton(f"{'✓ ' if sort_key==k else ''}{v}",
                callback_data=f"srt_{k}")   # short callback — max 64 chars safe
            for k, v in sort_labels.items()
        ]

        msg = f"🚢 *Available Boats — {route_from} → {route_to}*\n📅 *{text}*\n\n"
        buttons = [sort_row]
        for i, s in enumerate(schedules):
            rating_val = float(s.get("average_rating") or 0)
            stars = "⭐" * int(rating_val) if rating_val else "No ratings yet"
            rec = "✨ *Recommended by Samuga Travels*\n" if s.get("is_recommended") else ""
            # Build stops line for multi-stop ferries
            import json as _j
            try:
                stops_list = _j.loads(s.get("sched_stops") or "[]")
                if stops_list and len(stops_list) > 2:
                    stops_line = "🛑 " + " → ".join(stops_list) + "\n"
                else:
                    stops_line = ""
            except Exception:
                stops_line = ""
            # Trust score line
            total_reviews = s.get("total_reviews", 0) or 0
            rating_val = float(s.get("average_rating") or 0)
            if total_reviews >= 20:
                trust = "🏆 Top Rated"
            elif total_reviews >= 5:
                trust = "✅ Verified"
            elif total_reviews >= 1:
                trust = "🆕 New Operator"
            else:
                trust = "🆕 New"

            # Completed bookings count
            msg += (
                f"{'─'*30}\n"
                f"🚤 *{s['business_name']}* — _{s['boat_name']}_\n"
                f"{rec}"
                f"📍 {s['route_from']} → {s['route_to']}\n"
                f"{stops_line}"
                f"⏰ Departure: *{s['departure_time']}*\n"
                f"💺 Available: *{s['available_seats']} seats* | 💰 *MVR {s['price_per_seat']}/seat*\n"
                f"{trust} · ⭐ {rating_val:.1f} ({total_reviews} reviews)\n"
            )
            if s.get("review_text"):
                msg += f"💬 _{s['review_text']}_\n"
            msg += "\n"
            buttons.append([InlineKeyboardButton(
                f"Book — {s['business_name']} ({s['departure_time']})",
                callback_data=f"book_sched_{i}")])

        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

    elif state == CX_AWAIT_CONTACT:
        parts = text.split(",", 1)
        if len(parts) != 2:
            await update.message.reply_text("⚠️ Format: `Full Name, Phone Number`\n\nExample: `Ahmed Ali, 7771234`", parse_mode="Markdown")
            return
        cx_name = parts[0].strip()
        cx_phone = parts[1].strip()
        await set_user_state(user.id, CX_AWAIT_PASSENGER_COUNT, {**temp, "cx_name": cx_name, "cx_phone": cx_phone})
        await update.message.reply_text(
            f"✅ *{cx_name}* saved!\n\n"
            f"If this contact is wrong, tap *Edit contact*.\n"
            f"If it is correct, just type the number of seats and continue.\n\n"
            f"💺 How many seats would you like to book?\n_(Max 10, available: {temp.get('sel_seats',0)})_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Edit contact", callback_data="cx_edit_contact")],
                [InlineKeyboardButton("📅 Change date / boat", callback_data="cx_edit_trip")]
            ]))

    elif state == CX_AWAIT_PASSENGER_COUNT:
        count = parse_number(text)
        if not count or count < 1:
            await update.message.reply_text("⚠️ Enter a valid number e.g. `2`", parse_mode="Markdown")
            return
        if count > 10:
            await update.message.reply_text("⚠️ Maximum 10 seats per booking.")
            return
        if count > int(temp.get("sel_seats", 0)):
            await update.message.reply_text(f"⚠️ Only *{temp.get('sel_seats')} seats* available.", parse_mode="Markdown")
            return
        # Build example format based on count
        cx_name = temp.get("cx_name", "You")
        # Build example with booker's name as passenger 1
        example_lines = []
        example_lines.append(f"1. {cx_name}, (your ID/passport number)")
        for i in range(1, count):
            example_lines.append(f"{i+1}. Full Name, ID/Passport Number")
        example_str = "\n".join(example_lines)

        await set_user_state(user.id, CX_COLLECTING_PASSENGERS,
                             {**temp, "passenger_count": count, "passengers_collected": [], "current_passenger": 1})
        await update.message.reply_text(
            f"👥 *Passenger count: {count}*\n\n"
            f"If this number is wrong, tap *Edit passenger count*.\n"
            f"If it is correct, just send the passenger details and continue.\n\n"
            f"*Enter all {count} passenger(s) at once:*\n\n"
            f"_One per line — Name, ID or Passport Number_\n\n"
            f"_Example:_\n`{example_str}`\n\n"
            f"📌 ID card for Maldivians, Passport number for foreigners\n\n"
            f"Send all {count} in one message 👇",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✏️ Edit passenger count", callback_data="cx_edit_pax_count")
            ]]))

    elif state == CX_COLLECTING_PASSENGERS:
        sd2 = await get_user_state(user.id)
        t2  = sd2.get("temp_data", {}) or {}
        total = t2.get("passenger_count", 1)

        # Parse all passengers from one message — one per line
        # Strip leading numbers like "1. Ahmed" or "1) Ahmed"
        import re as _re
        lines_raw = [_re.sub(r"^\d+[.)\-\s]+", "", l.strip()) for l in text.strip().split("\n") if l.strip()]
        passengers = []
        errors = []
        for i, line in enumerate(lines_raw):
            parsed = parse_name_id(line)
            if parsed:
                passengers.append({"name": parsed[0], "id_number": parsed[1]})
            else:
                errors.append(f"Line {i+1}: couldn\'t read `{line}`")

        if errors or len(passengers) != total:
            example = "\n".join([f"{i+1}. Ahmed Ali, A12345{i}" for i in range(total)])
            err_msg = "\n".join(errors) if errors else ""
            await update.message.reply_text(
                f"⚠️ Need exactly *{total} passenger(s)*, one per line.\n\n"
                f"{err_msg}\n\n"
                f"_Example for {total} passenger(s):_\n`{example}`\n\n"
                f"If the passenger count was a mistake, tap *Edit passenger count*. Otherwise send the correct list again.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✏️ Edit passenger count", callback_data="cx_edit_pax_count")
                ]]))
            return

        if True:  # always show summary now
            t2["passengers_collected"] = passengers
            # fall through to summary below
            pass
        if False:
            pass
        else:
            sd3 = await get_user_state(user.id)
            t3  = sd3.get("temp_data", {}) or {}
            t3["passengers_collected"] = passengers
            total_amt = float(t3.get("sel_price", 0)) * total
            pax_lines = "\n".join([f"  {i+1}. {p.get('name','N/A')} ({p.get('id_number','N/A')})" for i,p in enumerate(passengers)])
            import json as _json
            pay_str = ""
            try:
                accounts = _json.loads(t3.get("sel_payment_accounts") or "[]")
                if accounts:
                    for acc in accounts:
                        pay_str += f"🏦 *{acc['bank']}:* `{acc['number']}`"
                        if acc.get("name"): pay_str += f" — {acc['name']}"
                        pay_str += "\n"
                else:
                    pay_str = f"🏦 *BML:* `{t3.get('sel_bml','N/A')}`\n"
            except Exception:
                pay_str = f"🏦 *BML:* `{t3.get('sel_bml','N/A')}`\n"

            summary = (
                f"📝 *Booking Summary*\n\n"
                f"👤 *Booker:* {t3.get('cx_name','N/A')} | 📞 {t3.get('cx_phone','N/A')}\n"
                f"🚤 *Operator:* {t3.get('sel_business')}\n"
                f"🛥️ *Boat:* {t3.get('sel_boat')}\n"
                f"📍 *Route:* {t3.get('route_from')} → {t3.get('route_to')}\n"
                f"📅 *Date:* {t3.get('travel_date')}\n"
                f"⏰ *Departure:* {t3.get('sel_time')}\n"
                f"👥 *Passengers ({total}):*\n{pax_lines}\n\n"
                f"💰 *Total:* MVR {total_amt:.2f}\n\n"
                f"{'─'*30}\n"
                f"💳 *Payment Details:*\n\n"
                f"{pay_str}"
                f"💰 Amount: *MVR {total_amt:.2f}*\n\n"
                f"⚠️ *Cancellation / Refund Policy*\n\n"
                f"Please double-check your *route, date, time, passenger details, account number,* and *account name* before transfer.\n\n"
                f"If you send money to the wrong bank/account, this is not refundable by Samuga Travels or the operator. You must contact your bank.\n\n"
                f"Refunds/cancellations for valid payments depend on the operator's policy and trip timing.\n\n"
                f"👉 If everything is correct, transfer and *upload your payment screenshot here.*\n\n"
                f"Need to fix something? Use the edit buttons below before paying."
            )
            await set_user_state(user.id, CX_AWAIT_PAYMENT_SLIP,
                                 {**t3, "total_amount": str(total_amt), "passengers_collected": passengers})
            await update.message.reply_text(
                summary,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✏️ Edit contact", callback_data="cx_edit_contact"),
                     InlineKeyboardButton("✏️ Edit passengers", callback_data="cx_edit_pax_details")],
                    [InlineKeyboardButton("✏️ Edit passenger count", callback_data="cx_edit_pax_count")],
                    [InlineKeyboardButton("📅 Change date / boat", callback_data="cx_edit_trip")]
                ]))

    else:
        # Default — route search from text
        if " to " in text.lower():
            parts = text.lower().split(" to ", 1)
            rf = parts[0].strip().title()
            rt = parts[1].strip().title()
            await set_user_state(user.id, CX_AWAIT_DATE, {"route_from": rf, "route_to": rt})
            from datetime import timedelta
            today = datetime.now().date()
            dates = [today + timedelta(days=i) for i in range(4)]
            date_buttons = [[InlineKeyboardButton(
                f"{'Today' if i==0 else 'Tomorrow' if i==1 else d.strftime('%a %d %b')}",
                callback_data=f"date_select_{d.strftime('%d-%m-%Y')}"
            )] for i, d in enumerate(dates)]
            await update.message.reply_text(
                f"🔍 *{rf} → {rt}*\n\n📅 Select your *travel date* or type manually:\n_(DD-MM-YYYY or DD/MM/YYYY)_",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(date_buttons))
            return   # ← don't fall through to else block!
        else:
            sd2 = await get_user_state(user.id)
            role = sd2.get("role","customer")
            if role == "operator":
                op = await get_operator(user.id)
                role = "operator" if (op and op.get("status")=="approved") else "customer"
            # Show available routes as suggestions
            try:
                pool2 = await get_pool()
                async with pool2.acquire() as conn2:
                    avail = await conn2.fetch("""
                        SELECT DISTINCT s.route_from, s.route_to
                        FROM schedules s JOIN operators o ON s.operator_id=o.id
                        WHERE o.status='approved' AND s.is_active=TRUE AND s.available_seats>0
                        AND COALESCE(o.subscription_status,'trial') != 'expired'
                        ORDER BY s.route_from LIMIT 6
                    """)
                if avail:
                    route_list = "\n".join([f"  `{r['route_from']} to {r['route_to']}`" for r in avail])
                    await update.message.reply_text(
                        f"👋 Just type your route to search!\n\n"
                        f"*Available routes:*\n{route_list}\n\n"
                        f"_Or type any route you need_ 👇",
                        parse_mode="Markdown", reply_markup=main_kb(role))
                else:
                    await update.message.reply_text(
                        "👋 Type a route like *Male to Thoddoo* to search for boats!",
                        parse_mode="Markdown", reply_markup=main_kb(role))
            except Exception:
                await update.message.reply_text(
                    "👋 Type a route like *Male to Thoddoo* to search for boats!",
                    parse_mode="Markdown", reply_markup=main_kb(role))

# ── PHOTO HANDLER ─────────────────────────────────────────────────────────────
async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    sd   = await get_user_state(user.id)
    state= sd.get("state", CX_IDLE)
    temp = sd.get("temp_data", {}) or {}

    photo = update.message.photo[-1]
    f = await ctx.bot.get_file(photo.file_id)
    file_bytes = bytes(await f.download_as_bytearray())

    if state == OP_AWAIT_LOGO:
        await update.message.reply_text("⏳ Uploading logo...")
        url = await upload_image(file_bytes, "logos", f"logo_{user.id}")
        await set_user_state(user.id, OP_AWAIT_ROUTES, {**temp, "logo_url": url})
        await update.message.reply_text(
            "✅ Logo uploaded!\n\n*Step 5:* Enter your *route with all stops in order*\n\n"
            "_For a ferry with multiple stops:_\n"
            "`Male, Dhigurah, Thoddoo, Dhagethi`\n\n"
            "_For a direct route:_\n"
            "`Male, Thoddoo`\n\n"
            "_Separate each stop with a comma in travel order._",
            parse_mode="Markdown")

    elif state == OP_AWAIT_OWNER_ID_PHOTO:
        await update.message.reply_text("⏳ Uploading ID securely to Samuga Travels storage...")
        url = await upload_image(file_bytes, "private/id_photos", f"id_{user.id}")
        await set_user_state(user.id, OP_AWAIT_BML_ACCOUNT, {**temp, "owner_id_photo_url": url})
        await update.message.reply_text(
            "✅ ID uploaded!\n\n*Final step:* Your *BML bank account number and account name*?\n\n"
            "_Format: AccountNumber AccountName_\n_Example: 7770000234231 Samuga Art_",
            parse_mode="Markdown")

    elif state == CX_AWAIT_PAYMENT_SLIP:
        await update.message.reply_text("⏳ Processing your payment slip...")
        sd2 = await get_user_state(user.id)
        t2  = sd2.get("temp_data", {}) or {}
        op_contact_fb = t2.get("sel_op_contact","") or ""
        op_name_fb    = t2.get("sel_business","") or "the operator"
        booking_id = None
        ref = None

        # STEP 1: Save booking (critical)
        try:
            ref = gen_ref()
            url = await upload_image(file_bytes, "private/payment_slips", f"slip_{ref}")
            from datetime import date as _date
            travel_date_raw = t2.get("travel_date","")
            try:
                if isinstance(travel_date_raw, str) and travel_date_raw:
                    travel_date_val = datetime.strptime(travel_date_raw, "%Y-%m-%d").date()
                else:
                    travel_date_val = _date.today()
            except Exception:
                travel_date_val = _date.today()
            operator_id  = int(t2.get("sel_operator_id") or 0) or None
            schedule_id  = int(t2.get("sel_schedule_id") or 0) or None
            pax_count    = int(t2.get("passenger_count") or 1)
            total_amount = float(t2.get("total_amount") or 0)
            customer_name = f"{t2.get('cx_name','')} | {t2.get('cx_phone','')}"
            passengers_json = json.dumps(t2.get("passengers_collected",[]))
            logger.info(f"Booking insert: ref={ref} op={operator_id} sched={schedule_id} date={travel_date_val} pax={pax_count} amt={total_amount}")
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow("""
                    INSERT INTO bookings (booking_ref, customer_telegram_id, customer_name, operator_id, schedule_id,
                                          travel_date, passenger_count, passengers, total_amount,
                                          payment_slip_url, status)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'pending_confirmation')
                    RETURNING id
                """, ref, user.id, customer_name, operator_id, schedule_id,
                    travel_date_val, pax_count, passengers_json, total_amount, url)
            booking_id = row["id"]
            logger.info(f"✅ Booking {ref} saved with id={booking_id}")
        except Exception as e:
            logger.error(f"❌ Payment slip booking save error: {e}", exc_info=True)
            try:
                op_id_fb = int(t2.get("sel_operator_id") or 0) or None
                if op_id_fb:
                    pool2 = await get_pool()
                    async with pool2.acquire() as conn2:
                        opr = await conn2.fetchrow(
                            "SELECT business_name, owner_contact FROM operators WHERE id=$1", op_id_fb)
                    if opr:
                        op_name_fb = opr["business_name"]
                        op_contact_fb = opr["owner_contact"]
            except Exception:
                pass
            contact_line = ""
            kb_btns = []
            if op_contact_fb:
                contact_line = (
                    f"\n\n📞 *Contact the operator directly:*\n"
                    f"🚤 {op_name_fb}\n📱 {op_contact_fb}")
                tgh = op_contact_fb.replace('+','').replace(' ','')
                kb_btns.append([InlineKeyboardButton("📞 Contact Operator", url=f"https://t.me/{tgh}")])
            kb_btns.append([InlineKeyboardButton("📩 Contact Samuga Travels", url="https://t.me/SamugaTravels")])
            await update.message.reply_text(
                f"⚠️ *Booking Save Issue*\n\n"
                f"Your payment went through but we had trouble saving automatically.{contact_line}\n\n"
                f"Please send your slip directly to the operator and they will confirm manually. 🙏",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(kb_btns))
            return

        # STEP 2: Confirm to customer
        await set_user_state(user.id, CX_BOOKING_COMPLETE, {"booking_ref": ref, "booking_id": booking_id})
        await update.message.reply_text(
            f"✅ *Payment slip received!*\n\n"
            f"📋 Booking Ref: `{ref}`\n\n"
            f"Your booking is being reviewed by the operator. "
            f"You will receive your confirmed ticket within *5-10 minutes*. "
            f"Please do not resend your slip - we have received it!",
            parse_mode="Markdown")

        # STEP 3: Notify operator (best-effort)
        try:
            op_tg_raw = t2.get("sel_op_tg", 0)
            op_id_raw = t2.get("sel_operator_id")
            logger.info(f"STEP3 notify: booking_id={booking_id} sel_op_tg={op_tg_raw} sel_operator_id={op_id_raw}")
            sel = {
                "operator_id": int(op_id_raw or 0) or None,
                "id": int(t2.get("sel_schedule_id") or 0) or None,
                "departure_time": t2.get("sel_time",""),
                "op_telegram_id": int(op_tg_raw) if op_tg_raw else None,
            }
            await notify_operator_payment(ctx, booking_id, sel, t2, ref, user, photo.file_id)
        except Exception as e:
            logger.error(f"❌ Operator notify error (booking still saved): {e}", exc_info=True)



    elif state == OP_AWAIT_SUB_SLIP:
        op_id = (temp or {}).get("sub_operator_id")
        amount = (temp or {}).get("sub_amount", "500")
        if not op_id:
            await update.message.reply_text("⚠️ Session expired. Try again from My Subscription.")
            return
        await update.message.reply_text("⏳ Uploading payment slip...")
        slip_url = await upload_image(file_bytes, "subscription_slips", f"sub_{op_id}_{int(datetime.now().timestamp())}")
        pool = await get_pool()
        async with pool.acquire() as conn:
            sub_row = await conn.fetchrow("""
                INSERT INTO subscriptions (operator_id, plan, status, payment_slip_url, payment_amount)
                VALUES ($1, 'monthly', 'pending', $2, $3)
                RETURNING id
            """, op_id, slip_url, float(amount))
            op_row = await conn.fetchrow("SELECT business_name, telegram_id FROM operators WHERE id=$1", op_id)
        sub_id = sub_row["id"]
        await set_user_state(user.id, OP_IDLE, {})
        await update.message.reply_text(
            f"✅ *Payment slip received!*\n\n"
            f"Our team will verify and activate your subscription within a few hours. 🙏",
            parse_mode="Markdown")
        # Notify admin
        try:
            await ctx.bot.send_photo(ADMIN_GROUP_ID,
                photo=photo.file_id,
                caption=(
                    f"💳 *Subscription Payment*\n\n"
                    f"🏢 *{op_row['business_name']}*\n"
                    f"💰 Amount: MVR {amount}\n\n"
                    f"Approve to activate 30 days."
                ),
                parse_mode="Markdown",
                message_thread_id=ADMIN_THREAD_ID,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Approve — Activate 30 Days", callback_data=f"sub_approve_{sub_id}")],
                    [InlineKeyboardButton("❌ Reject Payment", callback_data=f"sub_reject_{sub_id}")]
                ]))
        except Exception as e:
            logger.error(f"Sub admin notify error: {e}")

    elif state == OP_AWAIT_REFUND_SLIP:
        op = await get_operator(user.id)
        if not op:
            await update.message.reply_text("⚠️ Operator account required.")
            return
        await update.message.reply_text("⏳ Uploading refund slip...")
        slip_url = await upload_image(file_bytes, "private/refund_slips",
                                      f"refund_{temp.get('refund_booking_id','0')}_{int(datetime.now().timestamp())}")

        bk_id         = temp.get("refund_booking_id")
        bk_ref        = temp.get("refund_booking_ref","")
        amount        = temp.get("refund_amount","0")
        account_num   = temp.get("refund_account","")
        account_name  = temp.get("refund_account_name","")
        customer_tg   = temp.get("customer_tg_id")
        op_name       = temp.get("op_name","")
        op_contact    = temp.get("op_contact","")

        # Update booking
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE bookings
                SET refund_slip_url=$1, refund_status='completed', refund_at=NOW()
                WHERE id=$2
            """, slip_url, bk_id)

        await set_user_state(user.id, OP_IDLE, {})

        # Notify operator — done
        await update.message.reply_text(
            f"✅ *Refund Slip Sent!*\n\n"
            f"📋 Booking: `{bk_ref}`\n"
            f"💰 MVR {amount} → {account_name}\n\n"
            f"The customer has been notified with the slip.",
            parse_mode="Markdown",
            reply_markup=main_kb("operator"))

        # Send slip + confirmation to customer
        if customer_tg:
            try:
                await ctx.bot.send_photo(
                    int(customer_tg),
                    photo=photo.file_id,
                    caption=(
                        f"✅ *Refund Processed!*\n\n"
                        f"📋 Booking: `{bk_ref}`\n"
                        f"💰 Amount: *MVR {amount}*\n"
                        f"🏦 To: `{account_num}` — {account_name}\n\n"
                        f"Your refund has been transferred. Please allow 1-2 business days for it to appear.\n\n"
                        f"📞 *Operator Contact:*\n"
                        f"🚤 {op_name}\n"
                        f"📱 {op_contact}\n\n"
                        f"Thank you for using Samuga Travels! 🌊"
                    ),
                    parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Refund slip send to customer: {e}")

    elif state == ADMIN_AWAIT_LOGO:
        if not is_admin(user.id, update.effective_chat.id):
            await update.message.reply_text("⛔ Admin only.")
            return
        await update.message.reply_text("⏳ Uploading Samuga Travels logo...")
        url = await upload_image(file_bytes, "branding", "samuga_travels_logo")
        await set_setting("samuga_logo_url", url)
        await set_user_state(user.id, CX_IDLE, {})
        await update.message.reply_text(
            f"✅ *Samuga Travels logo updated!*\n\n"
            f"It will now appear on every ticket. 🎫\n\n"
            f"URL: `{url}`",
            parse_mode="Markdown")

    else:
        # Allow cancel via text — but if they sent a photo we just guide them
        await update.message.reply_text(
            "⚠️ Wasn't expecting an image right now.\n\n"
            "Type `cancel` to go back to the main menu, or /start to restart.",
            parse_mode="Markdown")

# ── CALLBACK HANDLER ──────────────────────────────────────────────────────────
async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    data = query.data
    sd   = await get_user_state(user.id)
    temp = sd.get("temp_data", {}) or {}

    if data == "register_operator":
        await start_op_reg(update, ctx)

    elif data.startswith("verify_ticket_"):
        bk_id = int(data.split("_")[-1])
        pool = await get_pool()
        async with pool.acquire() as conn:
            bk = await conn.fetchrow("SELECT booking_ref FROM bookings WHERE id=$1", bk_id)
        if bk:
            ctx.args = [f"verify_{bk['booking_ref']}"]
            await cmd_verify(update, ctx)

    elif data == "cx_edit_contact":
        # Let customer correct booker name/phone without restarting.
        if sd.get("state") not in [CX_AWAIT_PASSENGER_COUNT, CX_COLLECTING_PASSENGERS, CX_AWAIT_PAYMENT_SLIP, CX_AWAIT_CONTACT]:
            await query.answer("This booking step is no longer active.", show_alert=True)
            return
        await set_user_state(user.id, CX_AWAIT_CONTACT, temp)
        await query.message.reply_text(
            "✏️ *Edit contact details*\n\n"
            "Enter *Full Name* and *Phone Number* again:\n\n"
            "_Format: Ahmed Ali, 7771234_",
            parse_mode="Markdown")

    elif data == "cx_edit_trip":
        # Let customer change date/boat while keeping the route search.
        if sd.get("state") not in [CX_AWAIT_CONTACT, CX_AWAIT_PASSENGER_COUNT, CX_COLLECTING_PASSENGERS, CX_AWAIT_PAYMENT_SLIP, CX_AWAIT_DATE]:
            await query.answer("This booking step is no longer active.", show_alert=True)
            return
        route_from = temp.get("route_from", "")
        route_to = temp.get("route_to", "")
        if not route_from or not route_to:
            await query.message.reply_text(
                "🔍 Please type your route again, for example:\n`Male to Thoddoo`",
                parse_mode="Markdown")
            await set_user_state(user.id, CX_IDLE, {})
            return
        from datetime import timedelta
        today = datetime.now().date()
        dates = [today + timedelta(days=i) for i in range(4)]
        date_buttons = [[InlineKeyboardButton(
            f"{'Today' if i==0 else 'Tomorrow' if i==1 else d.strftime('%a %d %b')}",
            callback_data=f"date_select_{d.strftime('%d-%m-%Y')}"
        )] for i, d in enumerate(dates)]
        await set_user_state(user.id, CX_AWAIT_DATE, temp)
        await query.message.reply_text(
            f"📅 *Change date / boat*\n\n"
            f"Route: *{route_from} → {route_to}*\n\n"
            f"Select a new travel date, or type manually:\n_(DD-MM-YYYY or DD/MM/YYYY)_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(date_buttons))

    elif data == "cx_edit_pax_details":
        # Let customer resend passenger names/IDs without changing count.
        if sd.get("state") not in [CX_COLLECTING_PASSENGERS, CX_AWAIT_PAYMENT_SLIP]:
            await query.answer("This booking step is no longer active.", show_alert=True)
            return
        total = int(temp.get("passenger_count", 1) or 1)
        cx_name = temp.get("cx_name", "You")
        example_lines = [f"1. {cx_name}, (your ID/passport number)"]
        for i in range(1, total):
            example_lines.append(f"{i+1}. Full Name, ID/Passport Number")
        example_str = "\n".join(example_lines)
        await set_user_state(user.id, CX_COLLECTING_PASSENGERS, {**temp, "passengers_collected": []})
        await query.message.reply_text(
            f"✏️ *Edit passenger details*\n\n"
            f"Passenger count: *{total}*\n\n"
            f"Send all {total} passenger(s) again, one per line:\n\n"
            f"_Example:_\n`{example_str}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✏️ Edit passenger count", callback_data="cx_edit_pax_count")
            ]]))

    elif data == "cx_edit_pax_count":
        # Customer may have typed the wrong number of seats/passengers.
        # Let them correct it without restarting the whole booking flow.
        if sd.get("state") not in [CX_COLLECTING_PASSENGERS, CX_AWAIT_PASSENGER_COUNT]:
            await query.answer("This step is no longer active.", show_alert=True)
            return
        await set_user_state(user.id, CX_AWAIT_PASSENGER_COUNT, {**temp, "passengers_collected": []})
        await query.message.reply_text(
            f"✏️ *Edit passenger count*\n\n"
            f"How many seats/passengers do you want to book?\n"
            f"_Max 10, available: {temp.get('sel_seats', 0)}_\n\n"
            f"Example: `1`",
            parse_mode="Markdown")

    elif data.startswith("cx_cancel_booking_"):
        bk_id = int(data.split("_")[-1])
        pool = await get_pool()
        async with pool.acquire() as conn:
            bk = await conn.fetchrow("""
                SELECT b.*, s.departure_time, s.route_from, s.route_to,
                       o.telegram_id as op_tg_id, o.business_name as op_name,
                       o.owner_contact as op_contact
                FROM bookings b
                JOIN schedules s ON b.schedule_id=s.id
                JOIN operators o ON b.operator_id=o.id
                WHERE b.id=$1 AND b.customer_telegram_id=$2
            """, bk_id, user.id)
        if not bk:
            await query.answer("Booking not found.", show_alert=True)
            return
        if bk["status"] == "cancelled":
            await query.answer("This booking is already cancelled.", show_alert=True)
            return

        # ── Check 24hr rule ──────────────────────────────────────────────────
        from datetime import timedelta as _td24
        now = datetime.now()
        # Combine travel_date + departure_time to get departure datetime
        try:
            dep_str = f"{bk['travel_date']} {bk['departure_time']}"
            dep_dt  = datetime.strptime(dep_str, "%Y-%m-%d %H:%M")
        except Exception:
            dep_dt = None

        hours_until = ((dep_dt - now).total_seconds() / 3600) if dep_dt else 999

        if hours_until <= 0:
            # Already departed
            await query.message.reply_text(
                f"⚠️ *Cannot Cancel*\n\n"
                f"This trip has already departed.\n\n"
                f"For any issues, contact the operator directly:\n"
                f"📞 {bk['op_contact']}\n"
                f"🚤 {bk['op_name']}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📞 Contact Operator",
                        url=f"https://t.me/{bk['op_contact'].replace('+','').replace(' ','')}")
                ]]))
            return

        elif hours_until < 24:
            # Less than 24 hours — no automatic refund, must contact operator
            await query.message.reply_text(
                f"⚠️ *Less Than 24 Hours Before Departure*\n\n"
                f"Your trip departs in *{int(hours_until)}h {int((hours_until%1)*60)}min*.\n\n"
                f"Per Samuga Travels policy:\n"
                f"• Cancellations within 24hrs of departure require contacting the operator directly\n"
                f"• Refunds are at the operator's discretion\n\n"
                f"*Contact your operator:*\n"
                f"🚤 {bk['op_name']}\n"
                f"📞 {bk['op_contact']}\n\n"
                f"_If the operator agrees to a refund, they will process it directly._",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📞 Contact Operator",
                        url=f"https://t.me/{bk['op_contact'].replace('+','').replace(' ','')}")],
                    [InlineKeyboardButton("🔙 Keep Booking", callback_data="cx_my_bookings")]
                ]))
            return

        else:
            # More than 24 hours — eligible for cancellation + refund
            was_confirmed = bk["status"] == "confirmed"
            refund_note = (
                f"\n\n💰 *Refund Eligible*\n"
                f"Since you paid MVR {bk['total_amount']}, you can request a refund after cancelling."
                if was_confirmed else ""
            )
            await query.message.reply_text(
                f"❌ *Cancel Booking?*\n\n"
                f"📋 Ref: `{bk['booking_ref']}`\n"
                f"📍 {bk['route_from']} → {bk['route_to']}\n"
                f"📅 {bk['travel_date']} @ {bk['departure_time']}\n"
                f"💰 MVR {bk['total_amount']}"
                f"{refund_note}\n\n"
                f"⏰ Departure in *{int(hours_until)}hrs* — eligible for cancellation.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Cancel & Request Refund" if was_confirmed else "✅ Yes, Cancel",
                        callback_data=f"confirm_cancel_{bk_id}"),
                     InlineKeyboardButton("🔙 Keep Booking", callback_data="cx_my_bookings")]
                ]))

    elif data.startswith("confirm_cancel_"):
        bk_id = int(data.split("_")[-1])

        # Check this booking belongs to the user
        pool = await get_pool()
        async with pool.acquire() as conn:
            ownership = await conn.fetchrow(
                "SELECT id FROM bookings WHERE id=$1 AND customer_telegram_id=$2",
                bk_id, user.id)
        if not ownership:
            await query.answer("⛔ Not your booking.", show_alert=True)
            return

        # Use atomic cancel_booking function
        result, msg_code = await cancel_booking(
            bk_id,
            cancelled_by=f"customer_{user.id}",
            reason="Customer requested cancellation"
        )

        if msg_code == "already_cancelled":
            await query.answer("This booking was already cancelled.", show_alert=True)
            return
        if not result:
            await query.answer("Booking not found.", show_alert=True)
            return

        bk       = result["booking"]
        schedule = result["schedule"]
        operator = result["operator"]
        was_confirmed = result["old_status"] == "confirmed"

        # Notify operator
        if operator.get("telegram_id"):
            seats_note = "\n💺 Seats have been automatically added back." if was_confirmed else ""
            try:
                await ctx.bot.send_message(operator["telegram_id"],
                    f"❌ *Booking Cancelled by Customer*\n\n"
                    f"📋 Ref: `{bk['booking_ref']}`\n"
                    f"📍 Route: {schedule.get('route_from','')} → {schedule.get('route_to','')}\n"
                    f"📅 Date: {bk['travel_date']}\n"
                    f"👥 Passengers: {bk['passenger_count']}\n"
                    f"💰 Amount: MVR {bk['total_amount']}{seats_note}",
                    parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Cancel notify operator: {e}")

        if was_confirmed:
            # Store booking id in user state so refund flow can use it
            await set_user_state(user.id, CX_AWAIT_REFUND_ACCOUNT,
                                 {"refund_booking_id": bk_id, "refund_booking_ref": bk["booking_ref"],
                                  "refund_amount": str(bk["total_amount"]),
                                  "op_tg_id": result["operator"].get("telegram_id",""),
                                  "op_name":  result["operator"].get("business_name",""),
                                  "op_contact": result["operator"].get("owner_contact","")})
            await query.edit_message_text(
                f"✅ Booking `{bk['booking_ref']}` cancelled.\n\n"
                f"💰 *Refund: MVR {bk['total_amount']}*\n\n"
                f"Please enter your *bank account number and account name* to receive your refund:\n\n"
                f"_Format: `7770001234567 Ahmed Ali`_\n"
                f"_(BML or MIB account number followed by account name)_",
                parse_mode="Markdown")
        else:
            await query.edit_message_text(
                f"✅ Booking `{bk['booking_ref']}` cancelled successfully.",
                parse_mode="Markdown")

    elif data.startswith("report_issue_"):
        bk_id = int(data.split("_")[-1])
        pool = await get_pool()
        async with pool.acquire() as conn:
            bk = await conn.fetchrow("SELECT booking_ref FROM bookings WHERE id=$1", bk_id)
        ref = bk["booking_ref"] if bk else "N/A"
        await query.message.reply_text(
            f"⚠️ *Report an Issue*\n\n"
            f"Booking: `{ref}`\n\n"
            f"Please contact Samuga Travels directly with your booking reference:\n\n"
            f"📩 @SamugaTravels\n\n"
            f"We will investigate and respond within 24 hours.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📩 Contact @SamugaTravels", url="https://t.me/SamugaTravels")
            ]]))

    elif data == "cx_search":
        await set_user_state(user.id, CX_IDLE, {})
        pool = await get_pool()
        async with pool.acquire() as conn:
            ar = await conn.fetch("""
                SELECT DISTINCT s.route_from, s.route_to
                FROM schedules s JOIN operators o ON s.operator_id=o.id
                WHERE o.status='approved' AND s.is_active=TRUE AND s.available_seats>0
                AND COALESCE(o.subscription_status,'trial') != 'expired'
                ORDER BY s.route_from, s.route_to LIMIT 8
            """)
        if ar:
            rlines = "\n".join([f"  `{r['route_from']} to {r['route_to']}`" for r in ar])
            msg = f"🔍 *Search for Boats*\n\n*Available routes right now:*\n{rlines}\n\n_Just type your route below_ 👇"
        else:
            msg = "🔍 *Search for Boats*\n\nType your route — example:\n`Male to Thoddoo`\n`Thoddoo to Male`\n`Male to Maafushi`\n\n_Just type naturally_ 👇"
        await query.message.reply_text(msg, parse_mode="Markdown")

    elif data.startswith("srt_"):
        # Sort preference — re-run the last search with new sort
        sort_key = data.replace("srt_", "")
        sd2 = await get_user_state(user.id)
        t2 = sd2.get("temp_data", {}) or {}
        # Store sort preference and re-trigger search with cached schedules
        schedules = ctx.user_data.get("schedules_cache", [])
        if not schedules:
            await query.answer("Session expired — please search again.", show_alert=True)
            return
        await set_user_state(user.id, sd2.get("state", CX_IDLE), {**t2, "sort_by": sort_key})
        await query.answer(f"Sorted!")
        # Rebuild the message with new sort
        route_from = t2.get("route_from","")
        route_to   = t2.get("route_to","")
        travel_date = t2.get("travel_date","")
        sort_labels = {"recommended":"⭐ Rec","earliest":"⏰ Early","cheapest":"💰 Cheap","seats":"💺 Seats"}

        if sort_key == "earliest":
            schedules = sorted(schedules, key=lambda x: x["departure_time"])
        elif sort_key == "cheapest":
            schedules = sorted(schedules, key=lambda x: float(x["price_per_seat"] or 0))
        elif sort_key == "seats":
            schedules = sorted(schedules, key=lambda x: x["available_seats"], reverse=True)
        else:
            schedules = sorted(schedules, key=lambda x: (not x.get("is_recommended"), x["departure_time"]))

        ctx.user_data["schedules_cache"] = schedules
        sort_row = [
            InlineKeyboardButton(f"{'✓ ' if sort_key==k else ''}{v}", callback_data=f"srt_{k}")
            for k, v in sort_labels.items()
        ]
        import json as _j2
        msg = f"🚢 *Available Boats — {route_from} → {route_to}*\n📅 *{travel_date}*\n\n"
        buttons = [sort_row]
        for i, s in enumerate(schedules):
            rating_val = float(s.get("average_rating") or 0)
            rec = "✨ *Recommended*\n" if s.get("is_recommended") else ""
            total_reviews = s.get("total_reviews", 0) or 0
            trust = "🏆 Top Rated" if total_reviews >= 20 else ("✅ Verified" if total_reviews >= 5 else "🆕 New")
            try:
                stops_list = _j2.loads(s.get("sched_stops") or "[]")
                stops_line = "🛑 " + " → ".join(stops_list) + "\n" if stops_list and len(stops_list) > 2 else ""
            except: stops_line = ""
            msg += (
                f"{'─'*28}\n"
                f"🚤 *{s['business_name']}* — _{s['boat_name']}_\n"
                f"{rec}"
                f"📍 {s['route_from']} → {s['route_to']}\n"
                f"{stops_line}"
                f"⏰ *{s['departure_time']}* | 💺 {s['available_seats']} seats | 💰 MVR {s['price_per_seat']}/seat\n"
                f"{trust} · ⭐ {rating_val:.1f} ({total_reviews} reviews)\n\n"
            )
            buttons.append([InlineKeyboardButton(
                f"Book — {s['business_name']} ({s['departure_time']})",
                callback_data=f"book_sched_{i}")])
        try:
            await query.edit_message_text(msg[:4000], parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(buttons))
        except Exception:
            await query.message.reply_text(msg[:4000], parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("date_select_"):
        selected_date_str = data.replace("date_select_", "")
        # Inject as if user typed the date
        sd2 = await get_user_state(user.id)
        t2 = sd2.get("temp_data", {}) or {}
        # Fake a message with this date into the state handler
        travel_date = datetime.strptime(selected_date_str, "%d-%m-%Y").date()
        route_from = t2.get("route_from","")
        route_to   = t2.get("route_to","")
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT s.*, s.sched_stops, o.id as operator_id, o.business_name, o.boat_name, o.logo_url,
                       o.is_recommended, o.average_rating, o.total_reviews, o.owner_contact,
                       o.review_text, o.bml_account, o.payment_accounts, o.telegram_id as op_telegram_id
                FROM schedules s
                JOIN operators o ON s.operator_id = o.id
                WHERE LOWER(s.route_from) LIKE $1 AND LOWER(s.route_to) LIKE $2
                  AND o.status='approved' AND s.is_active=TRUE AND s.available_seats>0
                  AND COALESCE(o.subscription_status,'trial') != 'expired'
                  AND (
                    s.run_days = 'daily' OR s.run_days IS NULL
                    OR (s.run_days = 'fri'     AND EXTRACT(DOW FROM $3::date) = 5)
                    OR (s.run_days = 'sat-thu' AND EXTRACT(DOW FROM $3::date) != 5)
                    OR (s.run_days = 'weekdays' AND EXTRACT(DOW FROM $3::date) BETWEEN 0 AND 4)
                    OR (s.run_days = 'weekend'  AND EXTRACT(DOW FROM $3::date) IN (5,6))
                    OR (s.run_days = 'sun-thu'  AND EXTRACT(DOW FROM $3::date) BETWEEN 0 AND 4)
                    OR (s.run_days = 'everyday')
                  )
                ORDER BY o.is_recommended DESC, s.departure_time ASC
            """, f"%{route_from.lower()}%", f"%{route_to.lower()}%", travel_date)
        if not rows:
            await query.message.reply_text(
                f"😔 No boats for *{route_from} → {route_to}* on *{selected_date_str}*. Try another date.",
                parse_mode="Markdown")
            return
        schedules = [dict(r) for r in rows]
        ctx.user_data["schedules_cache"] = schedules
        sched_ids = [s["id"] for s in schedules]
        await set_user_state(user.id, CX_AWAIT_PASSENGER_COUNT,
                             {**t2, "travel_date": str(travel_date), "sched_ids": sched_ids})
        import json as _j
        msg = f"🚢 *Available Boats — {route_from} → {route_to}*\n📅 *{selected_date_str}*\n\n"
        buttons = []
        for i, s in enumerate(schedules):
            rating_val = float(s.get("average_rating") or 0)
            stars = "⭐" * int(rating_val) if rating_val else "No ratings yet"
            rec = "✨ *Recommended by Samuga Travels*\n" if s.get("is_recommended") else ""
            try:
                stops_list = _j.loads(s.get("sched_stops") or "[]")
                stops_line = "🛑 " + " → ".join(stops_list) + "\n" if stops_list and len(stops_list) > 2 else ""
            except: stops_line = ""
            msg += (
                f"{'─'*30}\n"
                f"🚤 *{s['business_name']}* — _{s['boat_name']}_\n"
                f"{rec}"
                f"📍 {s['route_from']} → {s['route_to']}\n"
                f"{stops_line}"
                f"⏰ *{s['departure_time']}*\n"
                f"💺 {s['available_seats']} seats | 💰 MVR {s['price_per_seat']}/seat\n"
                f"⭐ {stars}\n\n"
            )
            buttons.append([InlineKeyboardButton(
                f"Book — {s['business_name']} ({s['departure_time']})",
                callback_data=f"book_sched_{i}")])
        await query.message.reply_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

    elif data == "cx_my_bookings":
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT b.*, o.business_name, o.owner_contact FROM bookings b
                JOIN operators o ON b.operator_id = o.id
                WHERE b.customer_telegram_id=$1 ORDER BY b.created_at DESC LIMIT 5
            """, user.id)
        if not rows:
            await query.message.reply_text("📋 No bookings yet.\n\nSearch for boats to make your first booking! 🚤")
            return
        icons = {"pending_payment":"⏳","pending_confirmation":"🔄","confirmed":"✅","cancelled":"❌"}
        for b in rows:
            ic = icons.get(b["status"],"❓")
            msg = (
                f"{ic} `{b['booking_ref']}`\n"
                f"🚤 {b['business_name']}\n"
                f"📅 {b['travel_date']} | 💰 MVR {b['total_amount']}\n"
                f"📊 {b['status'].upper().replace('_',' ')}"
            )
            btns = []
            if b["status"] == "confirmed":
                btns.append([InlineKeyboardButton("🔍 Verify Ticket",
                    callback_data=f"verify_ticket_{b['id']}")])
                btns.append([
                    InlineKeyboardButton("📞 Contact Operator",
                        url=f"https://t.me/{b['owner_contact'].replace('+','')}"),
                    InlineKeyboardButton("⚠️ Report Issue",
                        callback_data=f"report_issue_{b['id']}")
                ])
            elif b["status"] == "pending_confirmation":
                btns.append([InlineKeyboardButton("❌ Cancel Request",
                    callback_data=f"cx_cancel_booking_{b['id']}")])
            await query.message.reply_text(msg, parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(btns) if btns else None)

    elif data.startswith("book_sched_"):
        idx = int(data.split("_")[-1])
        schedules = ctx.user_data.get("schedules_cache", [])
        if not schedules:
            await query.message.reply_text("⚠️ Session expired. Please search again.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔍 Search Again", callback_data="cx_search")]]))
            return
        if idx >= len(schedules):
            await query.message.reply_text("⚠️ Invalid selection.")
            return
        sel = schedules[idx]
        # Store flat keys to avoid large JSON in temp_data
        await set_user_state(user.id, CX_AWAIT_CONTACT, {
            **temp,
            "sel_operator_id": sel.get("operator_id"),
            "sel_schedule_id": sel.get("id"),
            "sel_business": sel.get("business_name"),
            "sel_boat": sel.get("boat_name"),
            "sel_time": sel.get("departure_time"),
            "sel_price": str(sel.get("price_per_seat", 0)),
            "sel_seats": int(sel.get("available_seats", 0)),
            "sel_bml": sel.get("bml_account", ""),
            "sel_payment_accounts": sel.get("payment_accounts", "[]"),
            "sel_op_tg": sel.get("op_telegram_id", 0),
            "sel_op_contact": sel.get("owner_contact", ""),
            "route_from": temp.get("route_from", ""),
            "route_to": temp.get("route_to", ""),
            "travel_date": temp.get("travel_date", ""),
        })
        await query.message.reply_text(
            f"✅ *{sel['business_name']}* selected!\n\n"
            f"📍 {temp.get('route_from')} → {temp.get('route_to')}\n"
            f"⏰ {sel['departure_time']} | 💺 {sel['available_seats']} seats\n\n"
            f"If this trip/boat is wrong, tap *Change date / boat*.\n"
            f"If it is correct, enter your contact details and continue.\n\n"
            f"👤 *Your contact details:*\nEnter *Full Name* and *Phone Number*:\n\n_Format: Ahmed Ali, 7771234_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📅 Change date / boat", callback_data="cx_edit_trip")
            ]]))

    elif data.startswith("type_"):
        boat_type = data.split("_")[1]
        await set_user_state(user.id, OP_AWAIT_LOGO, {**temp, "boat_type": boat_type})
        await query.message.reply_text(
            f"✅ *{'Ferry' if boat_type=='ferry' else 'Private Hire'}* selected!\n\n"
            f"*Step 5:* Please upload your *boat/company logo*.",
            parse_mode="Markdown")

    elif data.startswith("approve_op_"):
        op_id = int(data.split("_")[-1])
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE operators SET status='approved' WHERE id=$1 RETURNING telegram_id, business_name", op_id)
        if row:
            await set_user_state(row["telegram_id"], OP_IDLE, {}, role="operator")
            # Start 2-month free trial
            await create_trial(op_id)
            from datetime import timedelta
            trial_end = (datetime.now() + timedelta(days=60)).strftime("%d %b %Y")
            await ctx.bot.send_message(row["telegram_id"],
                f"🎉 *Congratulations! You're approved!*\n\n"
                f"*{row['business_name']}* is now live on Samuga Travels!\n\n"
                f"🎁 *Free Trial: 2 Months*\n"
                f"Your free trial runs until *{trial_end}* — no payment needed now.\n\n"
                f"✅ *Welcome to Samuga Travels Operator Panel*\n\n"
                f"1. Add your schedules\n"
                f"2. Check pending bookings\n"
                f"3. Confirm payment only after checking your bank/account\n"
                f"4. Use Today's Schedule for passengers\n"
                f"5. Scan ticket QR or mark boarded\n"
                f"6. Check Monthly Report for earnings\n\n"
                f"After the free trial, a small monthly fee of *MVR 500* keeps you listed.\n"
                f"Need help? Contact @SamugaTravels\n\n"
                f"Use /start to add your schedules and start receiving bookings! 🌊",
                parse_mode="Markdown")
            await query.edit_message_text(
                f"✅ Operator *{row['business_name']}* approved! 2-month trial started.",
                parse_mode="Markdown")

    elif data.startswith("reject_op_"):
        op_id = int(data.split("_")[-1])
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE operators SET status='rejected' WHERE id=$1 RETURNING telegram_id, business_name", op_id)
        if row:
            await ctx.bot.send_message(row["telegram_id"],
                f"❌ Your application for *{row['business_name']}* was not approved.\nContact @SamugaTravels for info.",
                parse_mode="Markdown")
            await query.edit_message_text(f"❌ Operator *{row['business_name']}* rejected.", parse_mode="Markdown")

    elif data.startswith("sched_boat_"):
        # Format: sched_boat_{boat_id}_{boat_name}
        parts_data = data.split("_", 3)
        boat_id = int(parts_data[2])
        boat_name_sel = parts_data[3] if len(parts_data) > 3 else "default"
        sd2 = await get_user_state(user.id)
        t2 = sd2.get("temp_data", {}) or {}
        import json as _j
        op = await get_operator(user.id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS sched_stops TEXT DEFAULT '[]'")
            await conn.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS location TEXT DEFAULT 'Jetty No. 1, Male'")
            await conn.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS run_days TEXT DEFAULT 'daily'")
            await conn.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS boat_name TEXT")
            await conn.execute("""
                INSERT INTO schedules (operator_id, route_from, route_to, departure_time,
                                       price_per_seat, total_seats, available_seats,
                                       sched_stops, location, run_days, boat_name)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            """, op["id"], t2.get("sched_from"), t2.get("sched_to"),
                t2.get("sched_time"), t2.get("sched_price"),
                t2.get("sched_seats",0), t2.get("sched_seats",0),
                _j.dumps(t2.get("sched_stops",[])),
                t2.get("sched_location","Jetty No. 1, Male"),
                t2.get("run_days","daily"),
                None if boat_name_sel == "default" else boat_name_sel)
        await set_user_state(user.id, OP_IDLE, {})
        boat_display = boat_name_sel if boat_name_sel != "default" else "Default"
        await query.edit_message_text(
            f"✅ *Schedule Added!*\n\n"
            f"📍 {t2.get('sched_from')} → {t2.get('sched_to')}\n"
            f"⏰ {t2.get('sched_time')} | 📅 {t2.get('run_days','daily')}\n"
            f"📌 {t2.get('sched_location','Jetty No. 1, Male')}\n"
            f"🚤 Boat: {boat_display}\n"
            f"💰 MVR {t2.get('sched_price')}/seat | 👥 {t2.get('sched_seats',0)} seats",
            parse_mode="Markdown")

    elif data == "op_fleet":
        op = await get_operator(user.id)
        if not op:
            await query.message.reply_text("⚠️ No operator profile.")
            return
        pool = await get_pool()
        async with pool.acquire() as conn:
            boats = await conn.fetch("SELECT * FROM boats WHERE operator_id=$1 ORDER BY created_at", op["id"])
        if not boats:
            await query.message.reply_text(
                "🚤 *Your Fleet*\n\nNo boats added yet.\n\nAdd your first boat:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("➕ Add a Boat", callback_data="op_add_boat")
                ]]))
            return
        msg = "🚤 *Your Fleet:*\n\n"
        buttons = []
        for b in boats:
            status_icon = "✅" if b["status"] == "active" else "🔧"
            msg += f"{status_icon} *{b['boat_name']}* — {b['capacity']} seats\n"
            buttons.append([
                InlineKeyboardButton(f"🔧 Maintenance — {b['boat_name']}", callback_data=f"boat_maintenance_{b['id']}"),
                InlineKeyboardButton(f"✅ Active", callback_data=f"boat_active_{b['id']}")
            ])
        buttons.append([InlineKeyboardButton("➕ Add Another Boat", callback_data="op_add_boat")])
        await query.message.reply_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

    elif data == "op_add_boat":
        await set_user_state(user.id, OP_AWAIT_BOAT_ADD_NAME, {})
        await query.message.reply_text(
            "🚤 *Add a Boat*\n\nWhat is this boat's name?\n\n_Example: SamugaTravels 1, Ocean Star_",
            parse_mode="Markdown")

    elif data.startswith("boat_maintenance_"):
        boat_id = int(data.split("_")[-1])
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("UPDATE boats SET status='maintenance' WHERE id=$1 RETURNING boat_name", boat_id)
        if row:
            await query.answer(f"🔧 {row['boat_name']} set to maintenance.", show_alert=True)
            await query.edit_message_text(f"🔧 *{row['boat_name']}* is now under maintenance.\nCustomers won't see it in available boats.", parse_mode="Markdown")

    elif data.startswith("boat_active_"):
        boat_id = int(data.split("_")[-1])
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("UPDATE boats SET status='active' WHERE id=$1 RETURNING boat_name", boat_id)
        if row:
            await query.answer(f"✅ {row['boat_name']} is now active!", show_alert=True)
            await query.edit_message_text(f"✅ *{row['boat_name']}* is now active.", parse_mode="Markdown")

    elif data == "op_today":
        op = await get_operator(user.id)
        if not op:
            return
        from datetime import timedelta as _td
        today = datetime.now().date()
        tomorrow = today + _td(days=1)
        pool = await get_pool()
        async with pool.acquire() as conn:
            # Today — view only
            scheds_today = await conn.fetch("""
                SELECT s.*, COALESCE(sc.new_boat_name, s.boat_name) as active_boat,
                       COALESCE(sc.new_time, s.departure_time) as active_time,
                       sc.note as change_note
                FROM schedules s
                LEFT JOIN schedule_changes sc ON sc.schedule_id=s.id AND sc.change_date=$1
                WHERE s.operator_id=$2 AND s.is_active=TRUE
                ORDER BY s.departure_time
            """, today, op["id"])
            bookings_today = await conn.fetch("""
                SELECT schedule_id, COUNT(*) as cnt, SUM(passenger_count) as pax
                FROM bookings WHERE travel_date=$1 AND status='confirmed' AND operator_id=$2
                GROUP BY schedule_id
            """, today, op["id"])
            # Tomorrow — with change buttons
            scheds_tmr = await conn.fetch("""
                SELECT s.*, COALESCE(sc.new_boat_name, s.boat_name) as active_boat,
                       COALESCE(sc.new_time, s.departure_time) as active_time,
                       sc.note as change_note
                FROM schedules s
                LEFT JOIN schedule_changes sc ON sc.schedule_id=s.id AND sc.change_date=$1
                WHERE s.operator_id=$2 AND s.is_active=TRUE
                ORDER BY s.departure_time
            """, tomorrow, op["id"])
            bookings_tmr = await conn.fetch("""
                SELECT schedule_id, COUNT(*) as cnt, SUM(passenger_count) as pax
                FROM bookings WHERE travel_date=$1 AND status='confirmed' AND operator_id=$2
                GROUP BY schedule_id
            """, tomorrow, op["id"])

        bk_map_today = {b["schedule_id"]: b for b in bookings_today}
        bk_map_tmr   = {b["schedule_id"]: b for b in bookings_tmr}

        msg = f"📅 *Today — {today.strftime('%A, %d %b')}*\n\n"
        buttons = []
        if not scheds_today:
            msg += "_No schedules today._\n"
        for s in scheds_today:
            bk = bk_map_today.get(s["id"])
            pax   = bk["pax"]  if bk else 0
            cnt   = bk["cnt"]  if bk else 0
            chng  = f" ⚠️ {s['change_note']}" if s.get("change_note") else ""
            msg += (
                f"⏰ *{s['active_time']}* — {s['route_from']} → {s['route_to']}\n"
                f"🚤 {s['active_boat'] or 'Default'} | 📌 {s.get('location','Jetty No. 1, Male')}\n"
                f"🎫 {cnt} bookings | 👥 {pax} pax{chng}\n\n"
            )
            if cnt:
                buttons.append([InlineKeyboardButton(
                    f"👥 Manifest {s['active_time']} — {s['route_from']} → {s['route_to']}",
                    callback_data=f"op_manifest_{s['id']}_{today.strftime('%Y%m%d')}")])

        msg += f"\n📅 *Tomorrow — {tomorrow.strftime('%A, %d %b')}* _(tap to manage)_\n\n"
        if not scheds_tmr:
            msg += "_No schedules tomorrow._\n"
        for s in scheds_tmr:
            bk = bk_map_tmr.get(s["id"])
            pax   = bk["pax"]  if bk else 0
            cnt   = bk["cnt"]  if bk else 0
            chng  = f" ⚠️ {s['change_note']}" if s.get("change_note") else ""
            msg += (
                f"⏰ *{s['active_time']}* — {s['route_from']} → {s['route_to']}\n"
                f"🚤 {s['active_boat'] or 'Default'} | 📌 {s.get('location','Jetty No. 1, Male')}\n"
                f"🎫 {cnt} bookings | 👥 {pax} pax{chng}\n\n"
            )
            buttons.append([InlineKeyboardButton(
                f"✏️ Manage {s['active_time']} — {s['route_from']} → {s['route_to']}",
                callback_data=f"change_sched_{s['id']}")])

        await query.message.reply_text(msg, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)

    elif data.startswith("op_manifest_"):
        op = await get_operator(user.id)
        if not op:
            await query.answer("Operator account required.", show_alert=True)
            return
        parts = data.split("_")
        sched_id = int(parts[2])
        date_token = parts[3] if len(parts) > 3 else datetime.now().strftime("%Y%m%d")
        manifest_date = datetime.strptime(date_token, "%Y%m%d").date()
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT b.id, b.booking_ref, b.passengers, b.passenger_count,
                       b.boarded_at, b.status, b.customer_name,
                       s.route_from, s.route_to, s.departure_time, s.location
                FROM bookings b
                JOIN schedules s ON b.schedule_id=s.id
                WHERE b.schedule_id=$1
                  AND b.operator_id=$2
                  AND b.travel_date=$3
                  AND b.status='confirmed'
                ORDER BY b.boarded_at NULLS FIRST, b.created_at
            """, sched_id, op["id"], manifest_date)
        if not rows:
            await query.message.reply_text(
                f"👥 *Boarding Manifest*\n\nNo confirmed passengers for {manifest_date}.",
                parse_mode="Markdown")
            return
        first = rows[0]
        total_pax = sum(int(r["passenger_count"] or 0) for r in rows)
        boarded_pax = sum(int(r["passenger_count"] or 0) for r in rows if r["boarded_at"])
        msg = (
            f"👥 *Boarding Manifest*\n\n"
            f"📍 {first['route_from']} → {first['route_to']}\n"
            f"📅 {manifest_date} @ {first['departure_time']}\n"
            f"📌 {first.get('location') or 'Jetty No. 1, Male'}\n\n"
            f"🛳️ Boarded: *{boarded_pax}/{total_pax}*\n\n"
        )
        buttons = []
        for r in rows:
            icon = "✅" if r["boarded_at"] else "⬜"
            passengers = r["passengers"] or "[]"
            if isinstance(passengers, str):
                try:
                    passengers = json.loads(passengers)
                except Exception:
                    passengers = []
            pax_lines = []
            for psg in passengers:
                pax_lines.append(f"{psg.get('name','N/A')} — {psg.get('id_number','N/A')}")
            if not pax_lines:
                pax_lines = [r["customer_name"] or "Passenger details on file"]
            msg += f"{icon} `{r['booking_ref']}`\n"
            for line in pax_lines:
                msg += f"   {line}\n"
            if r["boarded_at"]:
                msg += f"   Boarded: {str(r['boarded_at'])[:16]}\n\n"
            else:
                msg += "\n"
                buttons.append([InlineKeyboardButton(
                    f"✅ Mark boarded — {r['booking_ref']}",
                    callback_data=f"mark_boarded_{r['id']}")])
        await query.message.reply_text(
            msg[:3900],
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)

    elif data.startswith("change_sched_"):
        sched_id = int(data.split("_")[-1])
        op = await get_operator(user.id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            sched = await conn.fetchrow("SELECT * FROM schedules WHERE id=$1", sched_id)
            boats = await conn.fetch("SELECT * FROM boats WHERE operator_id=$1 AND status='active'", op["id"])
        if not sched:
            await query.answer("Schedule not found.", show_alert=True)
            return
        buttons = []
        for b in boats:
            buttons.append([InlineKeyboardButton(
                f"🚤 Swap to {b['boat_name']}",
                callback_data=f"swap_boat_{sched_id}_{b['boat_name']}")])
        buttons.append([InlineKeyboardButton("⏰ Change Time", callback_data=f"swap_time_{sched_id}")])
        buttons.append([InlineKeyboardButton("🗺️ Change Route", callback_data=f"swap_route_{sched_id}")])
        buttons.append([InlineKeyboardButton("❌ Cancel Tomorrow's Departure", callback_data=f"cancel_today_{sched_id}")])
        await query.message.reply_text(
            f"✏️ *Manage Tomorrow's Schedule*\n\n"
            f"⏰ {sched['departure_time']} — {sched['route_from']} → {sched['route_to']}\n"
            f"📌 {sched.get('location','Jetty No. 1, Male')}\n\n"
            f"What would you like to change for tomorrow?",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("swap_time_"):
        sched_id = int(data.split("_")[-1])
        await set_user_state(user.id, OP_AWAIT_CHANGE_NOTE, {"change_type": "time", "change_sched_id": sched_id})
        await query.message.reply_text(
            "⏰ *Change Tomorrow's Departure Time*\n\nEnter the new time:\n_Example: 05:00 PM_",
            parse_mode="Markdown")

    elif data.startswith("swap_route_"):
        sched_id = int(data.split("_")[-1])
        await set_user_state(user.id, OP_AWAIT_CHANGE_NOTE, {"change_type": "route", "change_sched_id": sched_id})
        await query.message.reply_text(
            "🗺️ *Change Tomorrow's Route*\n\nEnter new stops comma-separated:\n_Example: Male, Gulhi, Maafushi_",
            parse_mode="Markdown")

    elif data.startswith("swap_boat_"):
        parts_s = data.split("_", 3)
        sched_id = int(parts_s[2])
        new_boat = parts_s[3]
        from datetime import timedelta as _td2
        tomorrow = datetime.now().date() + _td2(days=1)
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO schedule_changes (schedule_id, change_date, new_boat_name, note)
                VALUES ($1,$2,$3,'Boat swapped by operator')
                ON CONFLICT DO NOTHING
            """, sched_id, tomorrow, new_boat)
            sched = await conn.fetchrow("SELECT * FROM schedules WHERE id=$1", sched_id)
            bookings = await conn.fetch("""
                SELECT customer_telegram_id, booking_ref FROM bookings
                WHERE schedule_id=$1 AND travel_date=$2 AND status='confirmed'
            """, sched_id, tomorrow)
        await query.edit_message_text(
            f"✅ Tomorrow's {sched['departure_time']} departure now uses *{new_boat}*.",
            parse_mode="Markdown")
        # Notify customers
        for bk in bookings:
            try:
                await ctx.bot.send_message(bk["customer_telegram_id"],
                    f"🚤 *Schedule Update*\n\n"
                    f"Your booking `{bk['booking_ref']}` has a small update:\n\n"
                    f"The boat for your *{sched['departure_time']}* departure has been changed to *{new_boat}*.\n"
                    f"📌 Location: {sched.get('location','Jetty No. 1, Male')}\n\n"
                    f"All other details remain the same. Safe travels! 🌊",
                    parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Customer notify error: {e}")

    elif data.startswith("cancel_today_"):
        sched_id = int(data.split("_")[-1])
        from datetime import timedelta as _td3
        tomorrow = datetime.now().date() + _td3(days=1)
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO schedule_changes (schedule_id, change_date, note, status)
                VALUES ($1,$2,'Departure cancelled for tomorrow','cancelled')
                ON CONFLICT DO NOTHING
            """, sched_id, tomorrow)
            sched = await conn.fetchrow("SELECT * FROM schedules WHERE id=$1", sched_id)
            bookings = await conn.fetch("""
                SELECT customer_telegram_id, booking_ref FROM bookings
                WHERE schedule_id=$1 AND travel_date=$2 AND status='confirmed'
            """, sched_id, tomorrow)
        await query.edit_message_text(f"✅ Tomorrow's {sched['departure_time']} departure marked as cancelled.")
        for bk in bookings:
            try:
                await ctx.bot.send_message(bk["customer_telegram_id"],
                    f"❌ *Departure Cancelled*\n\n"
                    f"We regret to inform you that your *{sched['departure_time']}* departure\n"
                    f"{sched['route_from']} → {sched['route_to']} has been cancelled today.\n\n"
                    f"Booking `{bk['booking_ref']}`\n\n"
                    f"Please contact the operator for rebooking or refund. Sorry for the inconvenience. 🙏",
                    parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Cancel notify error: {e}")

    # ── ADMIN PANEL CALLBACKS ──────────────────────────────────────────────────
    elif data == "adm_operators":
        if not await admin_check(query, ctx): return
        pool = await get_pool()
        async with pool.acquire() as conn:
            ops = await conn.fetch("SELECT * FROM operators ORDER BY status, created_at DESC LIMIT 20")
        if not ops:
            await query.message.reply_text("No operators found.")
            return
        for op in ops:
            status_icon = {"pending":"⏳","approved":"✅","rejected":"❌"}.get(op["status"],"❓")
            rec = "🌟 " if op["is_recommended"] else ""
            msg = (
                f"{status_icon} {rec}*{op['business_name']}*\n"
                f"🛥️ {op['boat_name']} | 💺 {op['seat_count']} seats\n"
                f"👤 @{op['telegram_username'] or 'N/A'} (`{op['telegram_id']}`)\n"
                f"📞 {op['owner_contact'] or 'N/A'}\n"
                f"📅 {str(op['created_at'])[:10]}"
            )
            btns = []
            if op["status"] != "approved":
                btns.append([InlineKeyboardButton("✅ Approve", callback_data=f"approve_op_{op['id']}"),
                             InlineKeyboardButton("❌ Reject",  callback_data=f"reject_op_{op['id']}")])
            btns.append([
                InlineKeyboardButton("🌟 Recommend" if not op["is_recommended"] else "⭐ Un-recommend",
                    callback_data=f"admin_recommend_{op['id']}" if not op["is_recommended"] else f"admin_unrecommend_{op['id']}"),
                InlineKeyboardButton("🔄 Reset", callback_data=f"admin_reset_{op['id']}"),
                InlineKeyboardButton("🗑️ Delete", callback_data=f"admin_delete_{op['id']}")
            ])
            await query.message.reply_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))

    elif data == "adm_bookings":
        if not await admin_check(query, ctx): return
        pool = await get_pool()
        async with pool.acquire() as conn:
            bks = await conn.fetch("""
                SELECT b.*, o.business_name FROM bookings b
                JOIN operators o ON b.operator_id=o.id
                ORDER BY b.created_at DESC LIMIT 15
            """)
        if not bks:
            await query.message.reply_text("No bookings yet.")
            return
        icons = {"pending_payment":"⏳","pending_confirmation":"🔄","confirmed":"✅","cancelled":"❌"}
        msg = "📦 *Recent Bookings:*\n\n"
        for b in bks:
            ic = icons.get(b["status"],"❓")
            msg += (f"{ic} `{b['booking_ref']}` — {b['business_name']}\n"
                   f"   👤 {b['customer_name'] or 'N/A'} | 📅 {b['travel_date']} | MVR {b['total_amount']}\n\n")
        await query.message.reply_text(msg, parse_mode="Markdown")

    elif data == "adm_revenue" or data.startswith("adm_revenue_"):
        if not await admin_check(query, ctx): return
        now = datetime.now()
        if data.startswith("adm_revenue_"):
            parts = data.split("_")
            year, month = int(parts[2]), int(parts[3])
        else:
            year, month = now.year, now.month
        month_name = datetime(year, month, 1).strftime("%B %Y")

        pool = await get_pool()
        async with pool.acquire() as conn:
            # Platform-wide monthly stats
            mstats = await conn.fetchrow("""
                SELECT
                    COUNT(*) FILTER (WHERE status='confirmed')   AS confirmed,
                    COUNT(*) FILTER (WHERE status='cancelled')   AS cancelled,
                    COUNT(*) FILTER (WHERE status IN ('pending_payment','pending_confirmation')) AS pending,
                    COUNT(*) AS total,
                    COALESCE(SUM(total_amount) FILTER (WHERE status='confirmed'),0) AS revenue,
                    COALESCE(SUM(passenger_count) FILTER (WHERE status='confirmed'),0) AS seats
                FROM bookings
                WHERE EXTRACT(YEAR FROM travel_date)=$1
                  AND EXTRACT(MONTH FROM travel_date)=$2
            """, year, month)

            # Subscription income this month
            sub_income_ops = await conn.fetchval("""
                SELECT COUNT(*) FROM subscriptions
                WHERE status='active'
                AND EXTRACT(YEAR FROM updated_at)=$1
                AND EXTRACT(MONTH FROM updated_at)=$2
            """, year, month)
            sub_fee = float(await get_setting("subscription_fee","500"))
            sub_income = (sub_income_ops or 0) * sub_fee

            # Top operators
            top_ops = await conn.fetch("""
                SELECT o.business_name, COUNT(*) as bookings,
                       COALESCE(SUM(b.total_amount),0) as revenue
                FROM bookings b JOIN operators o ON b.operator_id=o.id
                WHERE b.status='confirmed'
                  AND EXTRACT(YEAR FROM b.travel_date)=$1
                  AND EXTRACT(MONTH FROM b.travel_date)=$2
                GROUP BY o.business_name ORDER BY revenue DESC LIMIT 5
            """, year, month)

            # Inactive operators (no bookings in 30 days)
            inactive = await conn.fetchval("""
                SELECT COUNT(*) FROM operators o
                WHERE o.status='approved'
                AND NOT EXISTS (
                    SELECT 1 FROM bookings b
                    WHERE b.operator_id=o.id
                    AND b.created_at > NOW() - INTERVAL '30 days'
                )
            """)

            canc_rate = 0
            if mstats["total"] > 0:
                canc_rate = round(mstats["cancelled"] / mstats["total"] * 100, 1)

        msg = (
            f"📊 *Platform Report — {month_name}*\n\n"
            f"📦 *Bookings:*\n"
            f"  ✅ Confirmed: *{mstats['confirmed']}*\n"
            f"  ❌ Cancelled: *{mstats['cancelled']}* ({canc_rate}%)\n"
            f"  ⏳ Pending: *{mstats['pending']}*\n"
            f"  📋 Total: *{mstats['total']}*\n\n"
            f"💺 Seats sold: *{mstats['seats']}*\n"
            f"💰 Platform revenue: *MVR {float(mstats['revenue']):,.2f}*\n\n"
            f"💳 *Samuga Income:*\n"
            f"  Subscriptions renewed: *{sub_income_ops}*\n"
            f"  Subscription income: *MVR {sub_income:,.2f}*\n\n"
            f"⚠️ Inactive operators (30d): *{inactive}*\n\n"
            f"🏆 *Top Operators:*\n"
        )
        for i, op in enumerate(top_ops, 1):
            msg += f"  {i}. {op['business_name']} — {op['bookings']} bookings | MVR {float(op['revenue']):.2f}\n"

        # Month navigation
        from datetime import timedelta
        prev_m = (datetime(year, month, 1) - timedelta(days=1))
        next_m_dt = datetime(year, month, 28) + timedelta(days=4)
        next_m = next_m_dt.replace(day=1)
        nav = [InlineKeyboardButton(f"◀ {prev_m.strftime('%b')}", callback_data=f"adm_revenue_{prev_m.year}_{prev_m.month}")]
        if (next_m.year, next_m.month) <= (now.year, now.month):
            nav.append(InlineKeyboardButton(f"{next_m.strftime('%b')} ▶", callback_data=f"adm_revenue_{next_m.year}_{next_m.month}"))

        await query.message.reply_text(msg, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([nav]))

    elif data == "adm_broadcast":
        if not await admin_check(query, ctx): return
        await set_user_state(user.id, ADMIN_AWAIT_BROADCAST, {})
        await query.message.reply_text(
            "📢 *Broadcast Message*\n\n"
            "Type the message to send to *all approved operators*:\n\n"
            "_Type_ `cancel` _to abort._",
            parse_mode="Markdown")

    elif data == "adm_upload_logo":
        if not await admin_check(query, ctx): return
        await set_user_state(user.id, ADMIN_AWAIT_LOGO, {})
        await query.message.reply_text(
            "🖼️ *Upload Samuga Travels Logo*\n\n"
            "Send the logo image now and it will appear on every ticket! 🎫",
            parse_mode="Markdown")

    elif data == "adm_settings":
        if not await admin_check(query, ctx): return
        samuga_logo = await get_setting("samuga_logo_url", "Not set")
        sub_fee = await get_setting("subscription_fee", "500")
        sub_accounts = await get_setting("subscription_accounts", "[]")
        msg = (
            f"⚙️ *Settings*\n\n"
            f"🖼️ Samuga Logo: {'✅ Set' if samuga_logo else '❌ Not set'}\n\n"
            f"💳 *Subscription:*\n"
            f"  Monthly fee: *MVR {sub_fee}*\n"
            f"  Payment accounts: {'✅ Set' if sub_accounts != '[]' else '❌ Not set'}\n"
        )
        await query.message.reply_text(msg, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🖼️ Update Logo", callback_data="adm_upload_logo")],
                [InlineKeyboardButton("💳 Subscriptions", callback_data="adm_subscriptions")],
                [InlineKeyboardButton("🔙 Back to Admin", callback_data="adm_back")]
            ]))

    elif data == "adm_subscriptions":
        if not await admin_check(query, ctx): return
        pool = await get_pool()
        async with pool.acquire() as conn:
            subs = await conn.fetch("""
                SELECT s.*, o.business_name, o.telegram_id
                FROM subscriptions s JOIN operators o ON s.operator_id=o.id
                ORDER BY s.created_at DESC LIMIT 20
            """)
        fee = await get_setting("subscription_fee", "500")
        sub_icons = {"trial":"🎁","active":"✅","expired":"❌","pending":"⏳","grace":"⚠️"}
        msg = f"💳 *Subscriptions* | Fee: MVR {fee}/month\n\n"
        for s in subs:
            ic = sub_icons.get(s["status"],"❓")
            end = s["trial_ends_at"] or s["paid_until"]
            end_str = end.strftime("%d %b %Y") if end else "N/A"
            msg += f"{ic} *{s['business_name']}* — {s['status'].upper()} until {end_str}\n"
        if not subs: msg += "_No subscriptions yet._"
        await query.message.reply_text(msg, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💰 Set Fee", callback_data="adm_set_fee")],
                [InlineKeyboardButton("🏦 Set Payment Accounts", callback_data="adm_set_sub_accounts")],
            ]))

    elif data == "adm_set_fee":
        if not await admin_check(query, ctx): return
        await set_user_state(user.id, "admin_await_sub_fee", {})
        await query.message.reply_text(
            "💰 *Set Subscription Fee*\n\nEnter the monthly fee in MVR:\n_Example: 500_",
            parse_mode="Markdown")

    elif data == "adm_set_sub_accounts":
        if not await admin_check(query, ctx): return
        await set_user_state(user.id, "admin_await_sub_accounts", {})
        await query.message.reply_text(
            "🏦 *Set Samuga Travels Payment Accounts*\n\n"
            "Enter one per line: BANK NUMBER NAME\n\n"
            "_Example:_\n"
            "`BML 7770001234567 Samuga Travels`\n"
            "`MIB 90101234567890 Samuga Travels`",
            parse_mode="Markdown")

    elif data == "adm_schedules":
        if not await admin_check(query, ctx): return
        pool = await get_pool()
        async with pool.acquire() as conn:
            scheds = await conn.fetch("""
                SELECT s.*, o.business_name FROM schedules s
                JOIN operators o ON s.operator_id=o.id
                WHERE s.is_active=TRUE ORDER BY o.business_name, s.departure_time
            """)
        if not scheds:
            await query.message.reply_text("No active schedules.")
            return
        msg = "🚤 *All Active Schedules:*\n\n"
        for s in scheds:
            msg += (f"🏢 *{s['business_name']}*\n"
                   f"  ⏰ {s['departure_time']} | {s['route_from']} → {s['route_to']}\n"
                   f"  📌 {s.get('location','N/A')} | 💺 {s['available_seats']} seats | MVR {s['price_per_seat']}\n\n")
        await query.message.reply_text(msg[:4000], parse_mode="Markdown")

    elif data == "adm_find_customer":
        if not await admin_check(query, ctx): return
        await query.message.reply_text(
            "🔍 Use: `/findcustomer <booking_ref or telegram_id>`\n\nExample: `/findcustomer ST-260629-0389`",
            parse_mode="Markdown")

    elif data == "adm_control_room":
        if not await admin_check(query, ctx): return
        from datetime import timedelta
        pool = await get_pool()
        now = datetime.now()
        today = now.date()
        async with pool.acquire() as conn:
            # Bookings today
            bk_today = await conn.fetchval(
                "SELECT COUNT(*) FROM bookings WHERE created_at::date=$1", today)
            bk_confirmed = await conn.fetchval(
                "SELECT COUNT(*) FROM bookings WHERE created_at::date=$1 AND status='confirmed'", today)
            bk_pending_pay = await conn.fetchval(
                "SELECT COUNT(*) FROM bookings WHERE created_at::date=$1 AND status='pending_payment'", today)
            bk_pending_conf = await conn.fetchval(
                "SELECT COUNT(*) FROM bookings WHERE status='pending_confirmation'")
            # Revenue today
            rev_today = await conn.fetchval(
                "SELECT COALESCE(SUM(total_amount),0) FROM bookings WHERE created_at::date=$1 AND status='confirmed'", today)
            seats_today = await conn.fetchval(
                "SELECT COALESCE(SUM(passenger_count),0) FROM bookings WHERE created_at::date=$1 AND status='confirmed'", today)
            # Departures today
            departures = await conn.fetchval(
                "SELECT COUNT(*) FROM schedules WHERE is_active=TRUE")
            # Stale pending confirmations (>20 min)
            stale = await conn.fetch("""
                SELECT b.booking_ref, o.business_name,
                       EXTRACT(EPOCH FROM (NOW()-b.created_at))/60 as mins_ago
                FROM bookings b JOIN operators o ON b.operator_id=o.id
                WHERE b.status='pending_confirmation'
                AND b.created_at < NOW() - INTERVAL '20 minutes'
                ORDER BY b.created_at
                LIMIT 5
            """)
            # Subscriptions expiring in 7 days
            expiring = await conn.fetchval("""
                SELECT COUNT(*) FROM subscriptions
                WHERE (trial_ends_at BETWEEN NOW() AND NOW()+INTERVAL '7 days'
                       OR paid_until BETWEEN NOW() AND NOW()+INTERVAL '7 days')
                AND status IN ('trial','active')
            """)
            # Departures in next 2 hours
            soon = await conn.fetchval("""
                SELECT COUNT(DISTINCT b.id) FROM bookings b
                JOIN schedules s ON b.schedule_id=s.id
                WHERE b.travel_date=$1 AND b.status='confirmed'
                AND s.departure_time::time BETWEEN NOW()::time AND (NOW()+INTERVAL '2 hours')::time
            """, today)

        msg = (
            f"📡 *Daily Control Room — {today.strftime('%d %b %Y')}*\n\n"
            f"📦 *Bookings Today:* {bk_today}\n"
            f"  ✅ Confirmed: {bk_confirmed}\n"
            f"  💳 Pending payment: {bk_pending_pay}\n"
            f"  🔄 Pending operator: {bk_pending_conf}\n\n"
            f"🚤 Active schedules: {departures}\n"
            f"👥 Seats sold today: {seats_today}\n"
            f"💰 Revenue today: MVR {rev_today:.2f}\n\n"
        )
        # Needs attention
        attention = []
        if stale:
            for s in stale:
                attention.append(f"⏰ `{s['booking_ref']}` — {s['business_name']} waiting {int(s['mins_ago'])}min")
        if expiring:
            attention.append(f"💳 {expiring} subscription(s) expiring in 7 days")
        if soon:
            attention.append(f"🚢 {soon} confirmed bookings departing in next 2 hours")

        if attention:
            msg += "🚨 *Needs Attention:*\n"
            for a in attention:
                msg += f"  • {a}\n"
        else:
            msg += "✅ *All clear — nothing needs attention!*"

        await query.message.reply_text(msg, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="adm_control_room")],
                [InlineKeyboardButton("🔙 Admin Panel", callback_data="adm_back")]
            ]))

    elif data == "adm_back":
        if not await admin_check(query, ctx): return
        await query.message.reply_text("Back to admin — type /admin", parse_mode="Markdown")

    elif data.startswith("urgent_review_"):
        op_id = int(data.split("_")[-1])
        pool = await get_pool()
        async with pool.acquire() as conn:
            op = await conn.fetchrow("SELECT * FROM operators WHERE id=$1", op_id)
        if op:
            urgency_msg = (
                f"🚨 *URGENT REVIEW REQUEST*\n\n"
                f"👤 @{op['telegram_username'] or op['telegram_id']} (`{op['telegram_id']}`)\n"
                f"🏢 *{op['business_name']}*\n"
                f"🛥️ {op['boat_name']}\n\n"
                f"Operator is requesting urgent approval."
            )
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Approve Now", callback_data=f"approve_op_{op_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject_op_{op_id}")
            ]])
            try:
                await ctx.bot.send_message(ADMIN_GROUP_ID, urgency_msg,
                    parse_mode="Markdown", message_thread_id=ADMIN_THREAD_ID, reply_markup=kb)
                await query.answer("🚨 Urgent request sent to admin!", show_alert=True)
                await query.edit_message_text(
                    "🚨 *Urgent review request sent!*\n\n"
                    "Our team has been notified. You will hear back shortly.",
                    parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Urgent notify error: {e}")
                await query.answer("Failed to send. Try again.", show_alert=True)

    elif data == "op_schedules":
        op = await get_operator(user.id)
        if not op or op.get("status") != "approved":
            await query.message.reply_text("⚠️ Account not yet approved.")
            return
        await query.message.reply_text(
            "🗓️ *Add Schedules*\n\nHow would you like to add?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 Bulk Setup (recommended)", callback_data="op_bulk_setup")],
                [InlineKeyboardButton("➕ Add Single Schedule", callback_data="op_single_schedule")],
            ]))

    elif data == "op_single_schedule":
        op = await get_operator(user.id)
        if not op or op.get("status") != "approved":
            return
        await set_user_state(user.id, OP_AWAIT_SCHEDULE_ROUTE, {})
        await query.message.reply_text(
            "🗓️ *Add a Single Schedule*\n\n"
            "Enter stops comma-separated:\n_Male, Airport, Thoddoo_",
            parse_mode="Markdown")

    elif data == "op_bulk_setup":
        op = await get_operator(user.id)
        if not op or op.get("status") != "approved":
            return
        await set_user_state(user.id, OP_BULK_LOCATION, {})
        await query.message.reply_text(
            "📋 *Bulk Schedule Setup*\n\n"
            "This will create all your weekly schedules at once!\n\n"
            "*Step 1:* What are your route stops?\n"
            "_Enter comma-separated in order:_\n"
            "`Male, Airport, Thoddoo`",
            parse_mode="Markdown")

    elif data == "op_profile":
        op = await get_operator(user.id)
        if not op:
            await query.message.reply_text("⚠️ No operator profile found.")
            return
        routes = ", ".join(op.get("routes") or [])
        await query.message.reply_text(
            f"🚤 *Your Operator Profile*\n\n"
            f"🏢 *Business:* {op['business_name']}\n"
            f"🛥️ *Boat:* {op['boat_name']}\n"
            f"💺 *Seats:* {op['seat_count']}\n"
            f"📍 *Routes:* {routes}\n"
            f"📊 *Status:* {op['status'].upper()}\n"
            f"⭐ *Rating:* {op['average_rating']} ({op['total_reviews']} reviews)\n"
            f"✨ *Recommended:* {'Yes 🌟' if op['is_recommended'] else 'No'}\n\n"
            f"{'─'*30}\n"
            f"💡 *Quick Guide:*\n"
            f"• 📌 Pin this message for quick access\n"
            f"• Type `/profile` anytime to see your profile\n"
            f"• Type `/schedules` to manage your routes\n"
            f"• Type `/bookings` to see pending bookings\n"
            f"• Type `/fleet` to manage your boats\n"
            f"• Type `/today` to view today\'s schedule\n"
            f"• Type `/help` for all commands\n\n"
            f"_Commands are flexible — just type naturally!_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 Bulk Schedule Setup", callback_data="op_bulk_setup"),
                 InlineKeyboardButton("➕ Add Single Schedule", callback_data="op_single_schedule")],
                [InlineKeyboardButton("🚤 My Fleet",            callback_data="op_fleet"),
                 InlineKeyboardButton("📦 Pending Bookings",    callback_data="op_bookings")],
                [InlineKeyboardButton("📅 Today & Tomorrow",    callback_data="op_today"),
                 InlineKeyboardButton("✏️ Edit Info",           callback_data="op_edit")],
                [InlineKeyboardButton("📊 Monthly Report",      callback_data="op_monthly_report"),
                 InlineKeyboardButton("💳 My Subscription",     callback_data="op_subscription")],
            ]))

    elif data == "op_bookings":
        op = await get_operator(user.id)
        if not op:
            return
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT b.*, s.route_from, s.route_to, s.departure_time
                FROM bookings b JOIN schedules s ON b.schedule_id=s.id
                WHERE b.operator_id=$1 AND b.status='pending_confirmation'
                ORDER BY b.created_at DESC LIMIT 10
            """, op["id"])
        if not rows:
            await query.message.reply_text("📦 No pending bookings.")
            return
        for b in rows:
            await query.message.reply_text(
                f"📦 *Pending Booking*\n\n🔖 `{b['booking_ref']}`\n"
                f"📍 {b['route_from']} → {b['route_to']}\n"
                f"📅 {b['travel_date']} @ {b['departure_time']}\n"
                f"👥 {b['passenger_count']} passengers | 💰 MVR {b['total_amount']}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Confirm & Send Ticket", callback_data=f"confirm_booking_{b['id']}")
                ]]))




    elif data == "op_monthly_report" or data.startswith("op_report_"):
        op = await get_operator(user.id)
        if not op:
            await query.message.reply_text("⚠️ No operator profile found.")
            return
        now = datetime.now()
        if data.startswith("op_report_"):
            parts = data.split("_")
            year, month = int(parts[2]), int(parts[3])
        else:
            year, month = now.year, now.month
        month_name = datetime(year, month, 1).strftime("%B %Y")
        stats, top_route, op_meta = await get_operator_monthly_report(op["id"], year, month)

        total     = int(stats.get("total_bookings", 0) or 0)
        confirmed = int(stats.get("confirmed_bookings", 0) or 0)
        cancelled = int(stats.get("cancelled_bookings", 0) or 0)
        pending   = int(stats.get("pending_bookings", 0) or 0)
        seats     = int(stats.get("seats_sold", 0) or 0)
        gross     = float(stats.get("gross_sales", 0) or 0)
        canc_val  = float(stats.get("cancelled_value", 0) or 0)
        cancelled_actions = int(stats.get("cancelled_this_month", 0) or 0)
        refunds_completed = float(stats.get("refunds_completed", 0) or 0)
        refunds_pending   = float(stats.get("refunds_pending", 0) or 0)
        try:
            commission_rate = float(await get_setting("commission_rate", "0") or 0)
        except Exception:
            commission_rate = 0.0
        commission = gross * commission_rate / 100
        net_earning = max(0, gross - refunds_completed - commission)
        rating    = float(op_meta.get("average_rating", 0) or 0)
        reviews   = int(op_meta.get("total_reviews", 0) or 0)

        route_line = ""
        if top_route:
            route_line = f"\n🧭 Top route: *{top_route['route_from']} → {top_route['route_to']}* ({top_route['trips']} trips)"

        if total == 0:
            perf = "📭 No bookings yet this month."
        elif confirmed / max(total, 1) >= 0.9:
            perf = "🏆 Excellent — 90%+ confirmation rate!"
        elif confirmed / max(total, 1) >= 0.7:
            perf = "✅ Good month — strong confirmation rate."
        else:
            perf = "⚠️ Some bookings went unconfirmed — check pending."

        msg = (
            f"📊 *Monthly Report — {month_name}*\n\n"
            f"🚤 *{op['business_name']}*\n\n"
            f"📦 *Bookings:*\n"
            f"  🎫 Total: *{total}*\n"
            f"  ✅ Confirmed: *{confirmed}*\n"
            f"  ❌ Cancelled: *{cancelled}*\n"
            f"  ⏳ Pending: *{pending}*\n\n"
            f"💺 Seats sold: *{seats}*\n\n"
            f"💰 *Money:*\n"
            f"  Gross confirmed sales: *MVR {gross:,.2f}*\n"
            f"  Refunds completed: *MVR {refunds_completed:,.2f}*\n"
            f"  Refunds pending: *MVR {refunds_pending:,.2f}*\n"
            f"  Samuga commission ({commission_rate:g}%): *MVR {commission:,.2f}*\n"
            f"  💵 Estimated net earning: *MVR {net_earning:,.2f}*\n\n"
            f"📉 Cancelled trip value: *MVR {canc_val:,.2f}*\n"
            f"🗓 Cancelled this month: *{cancelled_actions}*\n\n"
            f"⭐ Rating: *{rating:.1f}* ({reviews} reviews){route_line}\n\n"
            f"_{perf}_"
        )

        from datetime import timedelta as _tdr
        prev_m = datetime(year, month, 1) - _tdr(days=1)
        next_m = (datetime(year, month, 28) + _tdr(days=4)).replace(day=1)
        nav = [InlineKeyboardButton(f"◀ {prev_m.strftime('%b %Y')}", callback_data=f"op_report_{prev_m.year}_{prev_m.month}")]
        if (next_m.year, next_m.month) <= (now.year, now.month):
            nav.append(InlineKeyboardButton(f"{next_m.strftime('%b %Y')} ▶", callback_data=f"op_report_{next_m.year}_{next_m.month}"))

        await query.message.reply_text(msg, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                nav,
                [InlineKeyboardButton("❌ Cancelled Bookings", callback_data=f"op_cancelled_{year}_{month}")],
                [InlineKeyboardButton("🔙 Back", callback_data="op_profile")]
            ]))

    elif data.startswith("op_cancelled_"):
        op = await get_operator(user.id)
        if not op: return
        parts = data.split("_")
        year, month = int(parts[2]), int(parts[3])
        month_name = datetime(year, month, 1).strftime("%B %Y")
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT booking_ref, customer_name, travel_date, passenger_count,
                       total_amount, cancelled_at, cancellation_reason
                FROM bookings
                WHERE operator_id=$1 AND status='cancelled'
                  AND EXTRACT(YEAR FROM travel_date)=$2
                  AND EXTRACT(MONTH FROM travel_date)=$3
                ORDER BY cancelled_at DESC LIMIT 10
            """, op["id"], year, month)
        if not rows:
            await query.message.reply_text(f"✅ No cancelled bookings in {month_name}!", parse_mode="Markdown")
            return
        msg = f"❌ *Cancelled — {month_name}*\n\n"
        for r in rows:
            msg += (
                f"📋 `{r['booking_ref']}`\n"
                f"  👤 {r['customer_name'] or 'N/A'} | 👥 {r['passenger_count']} pax\n"
                f"  📅 {r['travel_date']} | MVR {r['total_amount']}\n"
                f"  🕐 {str(r['cancelled_at'])[:16] if r['cancelled_at'] else 'N/A'}\n\n"
            )
        await query.message.reply_text(msg[:4000], parse_mode="Markdown")

    elif data.startswith("sub_approve_"):
        if not await admin_check(query, ctx): return
        sub_id = int(data.split("_")[-1])
        from datetime import timedelta as _td_sub
        pool = await get_pool()
        async with pool.acquire() as conn:
            sub = await conn.fetchrow("SELECT * FROM subscriptions WHERE id=$1", sub_id)
        if not sub:
            await query.answer("Subscription not found.", show_alert=True)
            return
        now = datetime.now()
        base = sub["paid_until"] if sub["paid_until"] and sub["paid_until"] > now else now
        new_until = base + _td_sub(days=30)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE subscriptions SET status='active', paid_until=$1, updated_at=NOW() WHERE id=$2",
                new_until, sub_id)
            await conn.execute(
                "UPDATE operators SET subscription_status='active' WHERE id=$1", sub["operator_id"])
            op_row = await conn.fetchrow(
                "SELECT telegram_id, business_name FROM operators WHERE id=$1", sub["operator_id"])
        biz = op_row["business_name"] if op_row else "Operator"
        until_str = new_until.strftime("%d %b %Y")
        await query.edit_message_text(
            f"✅ Subscription approved for *{biz}*\nActive until: *{until_str}*",
            parse_mode="Markdown")
        if op_row:
            try:
                await ctx.bot.send_message(op_row["telegram_id"],
                    f"✅ *Subscription Activated!*\n\n"
                    f"Thank you! *{biz}* is live on Samuga Travels.\n\n"
                    f"📅 Active until: *{until_str}*\n\n"
                    f"Your schedules are live and customers can book. 🌊",
                    parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Sub approve notify: {e}")

    elif data.startswith("sub_reject_"):
        if not await admin_check(query, ctx): return
        sub_id = int(data.split("_")[-1])
        pool = await get_pool()
        async with pool.acquire() as conn:
            sub = await conn.fetchrow("SELECT * FROM subscriptions WHERE id=$1", sub_id)
        if not sub:
            await query.answer("Subscription not found.", show_alert=True)
            return
        async with pool.acquire() as conn:
            op_row = await conn.fetchrow(
                "SELECT telegram_id, business_name FROM operators WHERE id=$1", sub["operator_id"])
        await query.edit_message_text("❌ Subscription payment rejected.")
        if op_row:
            try:
                await ctx.bot.send_message(op_row["telegram_id"],
                    f"❌ *Payment Not Confirmed*\n\n"
                    f"We could not verify your payment. Please check the amount and account, "
                    f"or contact @SamugaTravels. 🙏",
                    parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Sub reject notify: {e}")

    elif data.startswith("upload_refund_"):
        bk_id = int(data.split("_")[-1])
        op = await get_operator(user.id)
        if not op:
            await query.answer("Operator account required.", show_alert=True)
            return
        pool = await get_pool()
        async with pool.acquire() as conn:
            bk = await conn.fetchrow("SELECT * FROM bookings WHERE id=$1", bk_id)
        if not bk or str(bk["operator_id"]) != str(op["id"]):
            await query.answer("Not your booking.", show_alert=True)
            return
        await set_user_state(user.id, OP_AWAIT_REFUND_SLIP,
                             {"refund_booking_id": bk_id,
                              "refund_booking_ref": bk["booking_ref"],
                              "refund_amount": str(bk["total_amount"]),
                              "refund_account": bk.get("refund_account",""),
                              "refund_account_name": bk.get("refund_account_name",""),
                              "customer_tg_id": bk["customer_telegram_id"],
                              "op_name": op["business_name"],
                              "op_contact": op["owner_contact"]})
        await query.message.reply_text(
            f"📤 *Upload Refund Slip*\n\n"
            f"Booking: `{bk['booking_ref']}`\n"
            f"Amount: MVR {bk['total_amount']}\n"
            f"To: `{bk.get('refund_account','')}` — {bk.get('refund_account_name','')}\n\n"
            f"Please send the *transfer screenshot* now 👇",
            parse_mode="Markdown")

    elif data.startswith("mark_boarded_"):
        booking_id = int(data.split("_")[-1])
        op = await get_operator(user.id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            bk = await conn.fetchrow("SELECT * FROM bookings WHERE id=$1", booking_id)
        if not bk:
            await query.answer("Booking not found.", show_alert=True)
            return
        # Only the operator who owns it or super admin can mark boarded
        if user.id not in SUPER_ADMINS and (not op or op.get("id") != bk["operator_id"]):
            await query.answer("⛔ Not authorised.", show_alert=True)
            return
        if bk.get("boarded_at"):
            await query.answer("Already marked as boarded!", show_alert=True)
            return
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE bookings SET boarded_at=NOW(), boarded_by=$1 WHERE id=$2
            """, user.id, booking_id)
        passengers = bk.get("passengers") or "[]"
        if isinstance(passengers, str):
            try:
                passengers = json.loads(passengers)
            except Exception:
                passengers = []
        pax_names = ", ".join([psg.get("name", "") for psg in passengers if psg.get("name")]) or (bk.get("customer_name") or "Passenger")
        await safe_edit(query,
            f"🛳️ *Passenger boarded!*\n\n"
            f"Ref: `{bk['booking_ref']}`\n"
            f"Passenger: *{pax_names}*\n"
            f"Marked at: {datetime.now().strftime('%d %b %Y %H:%M')} MVT\n\n"
            f"✅ Ticket used — cannot be reused.",
            parse_mode="Markdown")

    elif data.startswith("not_received_"):
        booking_id = int(data.split("_")[-1])
        pool = await get_pool()
        async with pool.acquire() as conn:
            bk = await conn.fetchrow("""
                SELECT b.*, o.telegram_username AS operator_username,
                       o.telegram_id AS operator_telegram_id,
                       o.owner_contact AS operator_contact,
                       o.business_name AS operator_business_name
                FROM bookings b
                JOIN operators o ON b.operator_id=o.id
                WHERE b.id=$1
            """, booking_id)
        if not bk:
            await query.answer("Booking not found.", show_alert=True)
            return

        # Customer contact button: prefer operator username, fallback to Telegram user ID deep-link.
        # This lets the customer and operator talk directly, then operator can confirm or reject from the same admin message.
        contact_buttons = []
        op_username = (bk.get("operator_username") or "").strip()
        if op_username:
            contact_buttons.append([InlineKeyboardButton(
                "📩 Contact Operator",
                url=f"https://t.me/{op_username.lstrip('@')}"
            )])
        elif bk.get("operator_telegram_id"):
            contact_buttons.append([InlineKeyboardButton(
                "📩 Contact Operator",
                url=f"tg://user?id={bk['operator_telegram_id']}"
            )])

        try:
            await ctx.bot.send_message(bk["customer_telegram_id"],
                f"⚠️ *Payment Not Confirmed*\n\n"
                f"Hi! The operator could not verify your payment for booking `{bk['booking_ref']}`.\n\n"
                f"This could be because:\n"
                f"• Transfer sent to wrong account\n"
                f"• Amount was incorrect\n"
                f"• Screenshot was unclear\n\n"
                f"⚠️ If money was sent to the wrong bank/account, Samuga Travels and the operator cannot refund it. You must contact your bank.\n\n"
                f"Please double-check and resend your slip, or contact the operator. 🙏",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(contact_buttons) if contact_buttons else None)
        except Exception as e:
            logger.error(f"Not received notify: {e}")

        customer_contact = bk.get("customer_name") or "Customer"
        operator_buttons = [
            [InlineKeyboardButton("📩 Contact Customer", url=f"tg://user?id={bk['customer_telegram_id']}")],
            [InlineKeyboardButton("✅ Confirm & Send Ticket", callback_data=f"confirm_booking_{booking_id}")],
            [InlineKeyboardButton("❌ Keep Not Confirmed", callback_data=f"reject_booking_{booking_id}")],
        ]
        await safe_edit(query,
            f"❌ *Payment Not Confirmed*\n\n"
            f"Customer has been notified for booking `{bk['booking_ref']}`.\n\n"
            f"📩 We forwarded your contact button to the customer.\n"
            f"👤 *Customer contact:* {customer_contact}\n\n"
            f"After you talk and solve the issue, you can confirm the booking below. "
            f"If the payment is still wrong, keep it not confirmed.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(operator_buttons))

    elif data.startswith("reject_booking_"):
        booking_id = int(data.split("_")[-1])
        pool = await get_pool()
        async with pool.acquire() as conn:
            bk = await conn.fetchrow("SELECT booking_ref, status FROM bookings WHERE id=$1", booking_id)
        if not bk:
            await query.answer("Booking not found.", show_alert=True)
            return
        if bk["status"] == "confirmed":
            await query.answer("Already confirmed — cannot reject here.", show_alert=True)
            return
        await safe_edit(query,
            f"❌ *Payment Still Not Confirmed*\n\n"
            f"Booking `{bk['booking_ref']}` is left pending/not confirmed.\n\n"
            f"Customer can resend a clearer slip or contact the operator again.",
            parse_mode="Markdown")

    elif data.startswith("confirm_booking_"):
        booking_id = int(data.split("_")[-1])
        await do_confirm_booking(ctx, booking_id, query)

# ── HELPERS ───────────────────────────────────────────────────────────────────
async def save_operator(user, temp: dict):
    import json as _json
    # Build payment accounts list
    accounts = []
    if temp.get("bml_account"):
        parts = temp["bml_account"].split("|", 1)
        accounts.append({"bank": "BML", "number": parts[0].strip(), "name": parts[1].strip() if len(parts) > 1 else ""})
    if temp.get("mib_account"):
        parts = temp["mib_account"].split("|", 1)
        accounts.append({"bank": "MIB", "number": parts[0].strip(), "name": parts[1].strip() if len(parts) > 1 else ""})
    payment_accounts_json = _json.dumps(accounts)

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Add payment_accounts column if it doesn't exist
        await conn.execute("""
            ALTER TABLE operators ADD COLUMN IF NOT EXISTS payment_accounts TEXT DEFAULT '[]'
        """)
        await conn.execute("""
            INSERT INTO operators (telegram_id, telegram_username, business_name, boat_name,
                                   logo_url, seat_count, boat_type, routes, owner_name,
                                   owner_contact, owner_id_photo_url, bml_account,
                                   payment_accounts, status)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,'pending')
            ON CONFLICT (telegram_id) DO UPDATE SET
                business_name=EXCLUDED.business_name,
                boat_name=EXCLUDED.boat_name,
                logo_url=EXCLUDED.logo_url,
                owner_name=EXCLUDED.owner_name,
                owner_contact=EXCLUDED.owner_contact,
                owner_id_photo_url=EXCLUDED.owner_id_photo_url,
                bml_account=EXCLUDED.bml_account,
                payment_accounts=EXCLUDED.payment_accounts,
                status='pending'
        """, user.id, user.username, temp.get("business_name"), temp.get("boat_name"),
            temp.get("logo_url"), int(temp.get("seat_count") or 0), temp.get("boat_type"),
            temp.get("routes",[]), temp.get("owner_name"), temp.get("owner_contact"),
            temp.get("owner_id_photo_url"), temp.get("bml_account",""),
            payment_accounts_json)
        # Fetch the saved operator id
        row = await conn.fetchrow("SELECT id FROM operators WHERE telegram_id=$1", user.id)
        return row["id"] if row else 0

async def notify_admin_new_op(ctx, user, temp: dict, op_id: int = 0):
    if op_id == 0:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT id FROM operators WHERE telegram_id=$1", user.id)
        op_id = row["id"] if row else 0

    msg = (
        f"🆕 *New Operator Application*\n\n"
        f"👤 @{user.username or user.first_name} (`{user.id}`)\n"
        f"🏢 *{temp.get('business_name')}*\n"
        f"🛥️ {temp.get('boat_name')} — {temp.get('seat_count')} seats\n"
        f"📍 {temp.get('boat_type','ferry').title()}\n"
        f"🗺️ {', '.join(temp.get('routes',[]))}\n"
        f"👤 {temp.get('owner_name')} | 📞 {temp.get('owner_contact')}\n"
        f"🏦 BML: `{temp.get('bml_account','N/A')}`\n"
        f"🏦 MIB: `{temp.get('mib_account','—')}`"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve_op_{op_id}"),
        InlineKeyboardButton("❌ Reject",  callback_data=f"reject_op_{op_id}")
    ]])
    try:
        logger.info(f"Sending admin notification to group {ADMIN_GROUP_ID} thread {ADMIN_THREAD_ID}")
        await ctx.bot.send_message(ADMIN_GROUP_ID, msg, parse_mode="Markdown",
                                   message_thread_id=ADMIN_THREAD_ID, reply_markup=kb)
        logger.info("✅ Admin notification sent")
    except Exception as e:
        logger.error(f"❌ Admin notify FAILED: {e}")
        # Try without thread ID as fallback
        try:
            await ctx.bot.send_message(ADMIN_GROUP_ID, msg, parse_mode="Markdown", reply_markup=kb)
            logger.info("✅ Admin notification sent (no thread)")
        except Exception as e2:
            logger.error(f"❌ Admin notify fallback FAILED: {e2}")
    try:
        if temp.get("logo_url"):
            await ctx.bot.send_photo(ADMIN_GROUP_ID, photo=temp["logo_url"],
                                     caption="🖼️ Operator Logo", message_thread_id=ADMIN_THREAD_ID)
        if temp.get("owner_id_photo_url"):
            await ctx.bot.send_photo(ADMIN_GROUP_ID, photo=temp["owner_id_photo_url"],
                                     caption="🪪 Owner ID", message_thread_id=ADMIN_THREAD_ID)
    except Exception as e:
        logger.error(f"❌ Admin photo send FAILED: {e}")

async def notify_operator_payment(ctx, booking_id, sel, temp, ref, customer, slip_file_id):
    # Get operator telegram ID — try all possible sources
    op_tg_id = (sel.get("op_telegram_id") or temp.get("sel_op_tg") or
                temp.get("op_tg_id") or temp.get("op_telegram_id"))

    # Convert 0 to None (0 is falsy but stored as int)
    if op_tg_id == 0 or op_tg_id == "0":
        op_tg_id = None

    logger.info(f"notify_operator: booking={booking_id} op_tg_id={op_tg_id} "
                f"sel_op_tg={temp.get('sel_op_tg')} sel_operator_id={temp.get('sel_operator_id')}")

    if not op_tg_id:
        # Fallback: look up from DB using operator_id
        op_id = (sel.get("operator_id") or sel.get("id") or
                 temp.get("sel_operator_id") or temp.get("operator_id"))
        if not op_id:
            # Last resort: look up from the booking itself
            pool = await get_pool()
            async with pool.acquire() as conn:
                bk_row = await conn.fetchrow(
                    "SELECT operator_id FROM bookings WHERE id=$1", booking_id)
            op_id = bk_row["operator_id"] if bk_row else None

        if op_id:
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT telegram_id FROM operators WHERE id=$1", int(op_id))
            if row:
                op_tg_id = row["telegram_id"]
                logger.info(f"notify_operator: resolved op_tg_id={op_tg_id} from DB")

    if not op_tg_id:
        logger.error(f"❌ Cannot notify operator for booking {booking_id} — no telegram_id found. "
                     f"sel={sel} temp_keys={list(temp.keys())}")
        return

    pax = temp.get("passengers_collected",[])
    pax_lines = "\n".join([f"  {i+1}. {p.get('name','N/A')} ({p.get('id_number','N/A')})" for i,p in enumerate(pax)]) or "  (details on file)"
    dep_time = sel.get("departure_time") or temp.get("sel_time","")
    msg = (
        f"💳 *New Payment Received!*\n\n"
        f"🔖 Ref: `{ref}`\n"
        f"👤 *Customer:* {temp.get('cx_name','N/A')} | 📞 {temp.get('cx_phone','N/A')}\n"
        f"📍 {temp.get('route_from')} → {temp.get('route_to')}\n"
        f"📅 {temp.get('travel_date')} @ {dep_time}\n"
        f"👥 {temp.get('passenger_count')} passengers:\n{pax_lines}\n"
        f"💰 MVR {temp.get('total_amount')}\n\n"
        f"Please confirm or mark not received as soon as possible. The bot will remind you if this waits too long.\n\n"
        f"Review the slip and confirm below 👇"
    )
    try:
        await ctx.bot.send_photo(op_tg_id, photo=slip_file_id, caption=msg, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirm & Send Ticket", callback_data=f"confirm_booking_{booking_id}")],
                [InlineKeyboardButton("❌ Not Received / Wrong Transfer", callback_data=f"not_received_{booking_id}")]
            ]))
        logger.info(f"✅ Operator {op_tg_id} notified for booking {booking_id}")
    except Exception as e:
        logger.error(f"Operator notify error: {e}")

async def do_confirm_booking(ctx, booking_id: int, query):
    pool = await get_pool()
    async with pool.acquire() as conn:
        # ── ATOMIC TRANSACTION: lock booking + deduct seats in one operation ──
        async with conn.transaction():
            # Lock the booking row — prevents double-confirm race condition
            booking = await conn.fetchrow("""
                SELECT b.*, o.business_name, o.boat_name, o.logo_url,
                       o.owner_contact, o.telegram_id as op_telegram_id,
                       s.route_from, s.route_to, s.departure_time, s.price_per_seat
                FROM bookings b
                JOIN operators o ON b.operator_id=o.id
                JOIN schedules s ON b.schedule_id=s.id
                WHERE b.id=$1 AND b.status='pending_confirmation'
                FOR UPDATE
            """, booking_id)

            if not booking:
                # Already confirmed, cancelled, or doesn't exist
                try:
                    await query.answer("⚠️ This booking is already confirmed or no longer pending.", show_alert=True)
                    await safe_edit(query,
                        "⚠️ Already processed — no action needed.",
                        parse_mode="Markdown")
                except: pass
                return

            # Atomically deduct seats — only succeeds if enough seats available
            seat_update = await conn.fetchrow("""
                UPDATE schedules
                SET available_seats = available_seats - $1
                WHERE id=$2 AND available_seats >= $1
                RETURNING available_seats
            """, booking["passenger_count"], booking["schedule_id"])

            if not seat_update:
                try:
                    await query.answer("❌ Not enough seats available to confirm!", show_alert=True)
                    await safe_edit(query,
                        f"❌ Cannot confirm — not enough seats available for `{booking['booking_ref']}`.",
                        parse_mode="Markdown")
                except: pass
                return

            # Both checks passed — confirm the booking
            await conn.execute("""
                UPDATE bookings SET status='confirmed', confirmed_at=NOW() WHERE id=$1
            """, booking_id)

        logger.info(f"✅ Booking {booking['booking_ref']} confirmed atomically. Seats left: {seat_update['available_seats']}")

    booking_dict = dict(booking)
    passengers = booking_dict.get("passengers", "[]")
    if isinstance(passengers, str):
        try: booking_dict["passengers"] = json.loads(passengers)
        except: booking_dict["passengers"] = []

    # Fetch full operator info including contact
    pool2 = await get_pool()
    async with pool2.acquire() as conn2:
        full_op = await conn2.fetchrow("SELECT * FROM operators WHERE id=$1", booking["operator_id"])
        sched_full = await conn2.fetchrow("SELECT * FROM schedules WHERE id=$1", booking["schedule_id"])

    op_dict = {
        "business_name": booking["business_name"],
        "boat_name": booking["boat_name"],
        "logo_url": booking["logo_url"],
        "owner_contact": full_op["owner_contact"] if full_op else "",
        "telegram_id": full_op["telegram_id"] if full_op else 0,
    }
    sched_dict = {
        "route_from": booking["route_from"],
        "route_to": booking["route_to"],
        "departure_time": booking["departure_time"],
        "price_per_seat": booking["price_per_seat"],
        "location": sched_full["location"] if sched_full and "location" in sched_full.keys() else "Jetty No. 1, Male",
    }

    # Generate + send ticket (booking already confirmed — best-effort)
    ticket_sent = False
    try:
        pdf_bytes = await generate_ticket_pdf(booking_dict, op_dict, sched_dict)
        pdf_file  = io.BytesIO(pdf_bytes)
        pdf_file.name = f"ticket_{booking['booking_ref']}.pdf"
        await ctx.bot.send_document(
            booking["customer_telegram_id"], document=pdf_file,
            caption=(
                f"✅ *Booking Confirmed!*\n\n"
                f"🎫 Your ticket is attached.\n"
                f"🔖 Ref: `{booking['booking_ref']}`\n"
                f"🚤 {booking['business_name']}\n"
                f"📍 {booking['route_from']} → {booking['route_to']}\n"
                f"📅 {booking['travel_date']} @ {booking['departure_time']}\n\n"
                f"Present this ticket when boarding. Safe travels! 🌊"
            ),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚢 Book Your Next Trip", callback_data="cx_search")],
                [InlineKeyboardButton("📋 My Bookings", callback_data="cx_my_bookings")]
            ]))
        ticket_sent = True
    except Exception as e:
        logger.error(f"❌ Ticket PDF/send error for {booking['booking_ref']}: {e}", exc_info=True)
        try:
            await ctx.bot.send_message(
                booking["customer_telegram_id"],
                f"✅ *Booking Confirmed!*\n\n"
                f"🔖 Ref: `{booking['booking_ref']}`\n"
                f"🚤 {booking['business_name']}\n"
                f"📍 {booking['route_from']} → {booking['route_to']}\n"
                f"📅 {booking['travel_date']} @ {booking['departure_time']}\n"
                f"👥 {booking['passenger_count']} passengers | 💰 MVR {booking['total_amount']}\n\n"
                f"📞 Operator: {op_dict.get('owner_contact','')}\n\n"
                f"Show this confirmation when boarding. Safe travels! 🌊",
                parse_mode="Markdown")
            ticket_sent = True
        except Exception as e2:
            logger.error(f"❌ Text confirmation also failed: {e2}")
    try:
        if ticket_sent:
            await safe_edit(query,
                f"✅ Booking `{booking['booking_ref']}` confirmed! Ticket sent.", parse_mode="Markdown")
        else:
            await safe_edit(query,
                f"✅ Booking `{booking['booking_ref']}` confirmed! ⚠️ Could not auto-send ticket — contact the customer.", parse_mode="Markdown")
    except Exception:
        pass


# ── ERROR HANDLER ─────────────────────────────────────────────────────────────
async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception while handling update: {ctx.error}", exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong. Please try again or send /start")
        except Exception:
            pass

# ── SCHEDULED JOBS ───────────────────────────────────────────────────────────

async def job_payment_confirmation_watchdog(ctx: ContextTypes.DEFAULT_TYPE):
    """Remind operator/admin when a paid booking is waiting too long for confirmation."""
    pool = await get_pool()

    async def _send_admin_alert(text: str, reply_markup=None):
        try:
            await ctx.bot.send_message(
                ADMIN_GROUP_ID, text, parse_mode="Markdown",
                message_thread_id=ADMIN_THREAD_ID, reply_markup=reply_markup
            )
        except Exception as e:
            logger.error(f"Payment watchdog admin thread send failed: {e}")
            try:
                await ctx.bot.send_message(ADMIN_GROUP_ID, text, parse_mode="Markdown", reply_markup=reply_markup)
            except Exception as e2:
                logger.error(f"Payment watchdog admin fallback failed: {e2}")

    async with pool.acquire() as conn:
        first_alerts = await conn.fetch("""
            UPDATE bookings b
            SET payment_alert_stage=1, payment_alert_last_at=NOW()
            FROM operators o, schedules s
            WHERE b.operator_id=o.id AND b.schedule_id=s.id
              AND b.status='pending_confirmation'
              AND COALESCE(b.payment_alert_stage,0)=0
              AND b.created_at <= NOW() - INTERVAL '15 minutes'
            RETURNING b.id, b.booking_ref, b.customer_telegram_id, b.customer_name,
                      b.travel_date, b.passenger_count, b.total_amount,
                      o.telegram_id AS operator_telegram_id, o.business_name AS operator_name,
                      o.owner_contact AS operator_contact,
                      s.route_from, s.route_to, s.departure_time
        """)
        second_alerts = await conn.fetch("""
            UPDATE bookings b
            SET payment_alert_stage=2, payment_alert_last_at=NOW()
            FROM operators o, schedules s
            WHERE b.operator_id=o.id AND b.schedule_id=s.id
              AND b.status='pending_confirmation'
              AND COALESCE(b.payment_alert_stage,0)=1
              AND b.created_at <= NOW() - INTERVAL '20 minutes'
            RETURNING b.id, b.booking_ref, b.customer_telegram_id, b.customer_name,
                      b.travel_date, b.passenger_count, b.total_amount,
                      o.telegram_id AS operator_telegram_id, o.business_name AS operator_name,
                      o.owner_contact AS operator_contact,
                      s.route_from, s.route_to, s.departure_time
        """)

    for b in first_alerts:
        op_buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("📩 Contact Customer", url=f"tg://user?id={b['customer_telegram_id']}")],
            [InlineKeyboardButton("✅ Confirm & Send Ticket", callback_data=f"confirm_booking_{b['id']}")],
            [InlineKeyboardButton("❌ Not Received / Wrong Transfer", callback_data=f"not_received_{b['id']}")]
        ])
        try:
            await ctx.bot.send_message(
                b["operator_telegram_id"],
                f"⏳ *Payment confirmation pending*\n\n"
                f"Booking `{b['booking_ref']}` has been waiting about *15 minutes*.\n\n"
                f"👤 Customer: {b['customer_name'] or 'N/A'}\n"
                f"📍 {b['route_from']} → {b['route_to']}\n"
                f"📅 {b['travel_date']} @ {b['departure_time']}\n"
                f"👥 {b['passenger_count']} pax | 💰 MVR {b['total_amount']}\n\n"
                f"Please check your bank and confirm, or mark not received.",
                parse_mode="Markdown", reply_markup=op_buttons
            )
        except Exception as e:
            logger.error(f"Payment watchdog first operator ping failed: {e}")

        await _send_admin_alert(
            f"⚠️ *Payment waiting too long*\n\n"
            f"Booking: `{b['booking_ref']}`\n"
            f"Operator: *{b['operator_name']}* ({b['operator_contact'] or 'no contact'})\n"
            f"Customer: {b['customer_name'] or 'N/A'} (`{b['customer_telegram_id']}`)\n"
            f"Trip: {b['route_from']} → {b['route_to']}\n"
            f"Date: {b['travel_date']} @ {b['departure_time']}\n"
            f"Pax: {b['passenger_count']} | Amount: MVR {b['total_amount']}\n\n"
            f"Bot pinged the operator. If not approved in another 5 minutes, it will ping again.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📩 Contact Operator", url=f"tg://user?id={b['operator_telegram_id']}"),
                InlineKeyboardButton("📩 Contact Customer", url=f"tg://user?id={b['customer_telegram_id']}")
            ]])
        )

    for b in second_alerts:
        op_buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("📩 Contact Customer", url=f"tg://user?id={b['customer_telegram_id']}")],
            [InlineKeyboardButton("✅ Confirm & Send Ticket", callback_data=f"confirm_booking_{b['id']}")],
            [InlineKeyboardButton("❌ Not Received / Wrong Transfer", callback_data=f"not_received_{b['id']}")]
        ])
        try:
            await ctx.bot.send_message(
                b["operator_telegram_id"],
                f"🚨 *Second reminder — customer is waiting*\n\n"
                f"Booking `{b['booking_ref']}` is still not approved.\n\n"
                f"👤 Customer: {b['customer_name'] or 'N/A'}\n"
                f"📍 {b['route_from']} → {b['route_to']}\n"
                f"📅 {b['travel_date']} @ {b['departure_time']}\n"
                f"👥 {b['passenger_count']} pax | 💰 MVR {b['total_amount']}\n\n"
                f"Please confirm now or mark not received. Samuga Travels may contact you directly.",
                parse_mode="Markdown", reply_markup=op_buttons
            )
        except Exception as e:
            logger.error(f"Payment watchdog second operator ping failed: {e}")

        await _send_admin_alert(
            f"🚨 *Second ping — operator still has not approved*\n\n"
            f"Booking: `{b['booking_ref']}`\n"
            f"Operator: *{b['operator_name']}* ({b['operator_contact'] or 'no contact'})\n"
            f"Customer: {b['customer_name'] or 'N/A'} (`{b['customer_telegram_id']}`)\n"
            f"Trip: {b['route_from']} → {b['route_to']}\n"
            f"Date: {b['travel_date']} @ {b['departure_time']}\n"
            f"Pax: {b['passenger_count']} | Amount: MVR {b['total_amount']}\n\n"
            f"Please reach out to the operator directly.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📩 Contact Operator", url=f"tg://user?id={b['operator_telegram_id']}"),
                InlineKeyboardButton("📩 Contact Customer", url=f"tg://user?id={b['customer_telegram_id']}")
            ]])
        )

async def job_morning_ping(ctx: ContextTypes.DEFAULT_TYPE):
    """20:00 MVT — ping all operators to prepare tomorrow's schedules"""
    from datetime import timedelta as _td
    tomorrow = datetime.now().date() + _td(days=1)
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Get all approved operators with active schedules
        operators = await conn.fetch("""
            SELECT DISTINCT o.telegram_id, o.business_name, o.id as op_id
            FROM operators o
            JOIN schedules s ON s.operator_id = o.id
            WHERE o.status = 'approved' AND s.is_active = TRUE
        """)
        for op in operators:
            scheds = await conn.fetch("""
                SELECT * FROM schedules WHERE operator_id=$1 AND is_active=TRUE
                ORDER BY departure_time
            """, op["op_id"])
            if not scheds:
                continue
            sched_lines = "\n".join([
                f"  ⏰ {s['departure_time']} — {s['route_from']} → {s['route_to']} | 📌 {s.get('location','Jetty No. 1, Male')}"
                for s in scheds
            ])
            buttons = [[InlineKeyboardButton("📅 View & Manage Schedule", callback_data="op_today")]]
            try:
                from datetime import timezone, timedelta as _tdtz
                mvt_hour = (datetime.now(timezone.utc) + _tdtz(hours=5)).hour
                if 5 <= mvt_hour < 12:
                    greeting = "🌅 Good morning"
                    note = "Have a great day on the water! 🌊"
                elif 12 <= mvt_hour < 17:
                    greeting = "☀️ Good afternoon"
                    note = "Hope the afternoon trips are going smoothly! 🚤"
                elif 17 <= mvt_hour < 21:
                    greeting = "🌇 Good evening"
                    note = "Please review tomorrow's trips before the day starts. 🌊"
                else:
                    greeting = "🌙 Good night"
                    note = "Please review tomorrow's trips before the day starts. 🌊"
                await ctx.bot.send_message(op["telegram_id"],
                    f"{greeting}, *{op['business_name']}!*\n\n"
                    f"Tomorrow's schedule check — *{tomorrow}*:\n{sched_lines}\n\n"
                    f"⚠️ Any changes? Tap below to manage departures before customers arrive.\n\n"
                    f"_{note}_",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(buttons))
            except Exception as e:
                logger.error(f"Daily ping failed for {op['telegram_id']}: {e}")

async def job_subscription_check(ctx: ContextTypes.DEFAULT_TYPE):
    """
    Runs daily at 9AM MVT.
    - Warns operators 7 days before trial/subscription ends
    - Suspends expired operators (hides schedules from customers)
    """
    from datetime import timedelta
    now = datetime.now()
    warn_threshold = now + timedelta(days=7)
    pool = await get_pool()
    async with pool.acquire() as conn:
        subs = await conn.fetch("""
            SELECT s.*, o.telegram_id, o.business_name, o.id as op_id
            FROM subscriptions s
            JOIN operators o ON s.operator_id = o.id
            WHERE o.status = 'approved'
        """)
    for sub in subs:
        tg_id = sub["telegram_id"]
        name  = sub["business_name"]
        fee   = await get_setting("subscription_fee", "500")

        if sub["status"] == "trial":
            trial_end = sub["trial_ends_at"]
            if not trial_end: continue
            if now >= trial_end:
                # Trial expired — suspend
                pool2 = await get_pool()
                async with pool2.acquire() as conn2:
                    await conn2.execute(
                        "UPDATE subscriptions SET status='expired' WHERE id=$1", sub["id"])
                    await conn2.execute(
                        "UPDATE operators SET subscription_status='expired' WHERE id=$1", sub["op_id"])
                try:
                    await ctx.bot.send_message(tg_id,
                        f"❌ *Trial Ended — {name}*\n\n"
                        f"Your 2-month free trial has ended.\n\n"
                        f"Your schedules are currently hidden from customers.\n\n"
                        f"Subscribe for *MVR {fee}/month* to reactivate.\n"
                        f"Tap /start → 💳 My Subscription to pay.",
                        parse_mode="Markdown")
                except: pass
            elif trial_end <= warn_threshold:
                days_left = (trial_end - now).days
                try:
                    await ctx.bot.send_message(tg_id,
                        f"⚠️ *Trial Ending Soon — {name}*\n\n"
                        f"Your free trial ends in *{days_left} days* "
                        f"({trial_end.strftime('%d %b %Y')}).\n\n"
                        f"Subscribe for *MVR {fee}/month* to keep your listings active.\n"
                        f"Tap /start → 💳 My Subscription to pay now.",
                        parse_mode="Markdown")
                except: pass

        elif sub["status"] == "active":
            paid_until = sub["paid_until"]
            if not paid_until: continue
            if now >= paid_until:
                # Subscription expired
                pool2 = await get_pool()
                async with pool2.acquire() as conn2:
                    await conn2.execute(
                        "UPDATE subscriptions SET status='expired' WHERE id=$1", sub["id"])
                    await conn2.execute(
                        "UPDATE operators SET subscription_status='expired' WHERE id=$1", sub["op_id"])
                try:
                    await ctx.bot.send_message(tg_id,
                        f"❌ *Subscription Expired — {name}*\n\n"
                        f"Your schedules are now hidden from customers.\n\n"
                        f"Renew for *MVR {fee}/month* to reactivate.\n"
                        f"Tap /start → 💳 My Subscription to pay.",
                        parse_mode="Markdown")
                except: pass
            elif paid_until <= warn_threshold:
                days_left = (paid_until - now).days
                try:
                    await ctx.bot.send_message(tg_id,
                        f"⚠️ *Subscription Expiring — {name}*\n\n"
                        f"Your subscription expires in *{days_left} days* "
                        f"({paid_until.strftime('%d %b %Y')}).\n\n"
                        f"Renew now to avoid interruption! *MVR {fee}/month*\n"
                        f"Tap /start → 💳 My Subscription.",
                        parse_mode="Markdown")
                except: pass

async def job_departure_reminders(ctx: ContextTypes.DEFAULT_TYPE):
    """Run every 5 minutes — send 45-min reminders to confirmed customers"""
    from datetime import timedelta
    now = datetime.now()
    today = now.date()
    # Target: departures happening in 40-50 minutes from now
    remind_from = (now.replace(second=0, microsecond=0) + timedelta(minutes=40)).strftime("%H:%M")
    remind_to   = (now.replace(second=0, microsecond=0) + timedelta(minutes=50)).strftime("%H:%M")

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Find bookings with departures in ~45 min, not yet reminded
        bookings = await conn.fetch("""
            SELECT b.customer_telegram_id, b.booking_ref, b.passenger_count,
                   COALESCE(sc.new_time, s.departure_time) as dep_time,
                   COALESCE(sc.new_boat_name, s.boat_name, o.boat_name) as boat_name,
                   s.location, s.route_from, s.route_to, o.business_name
            FROM bookings b
            JOIN schedules s ON b.schedule_id = s.id
            JOIN operators o ON b.operator_id = o.id
            LEFT JOIN schedule_changes sc ON sc.schedule_id=s.id AND sc.change_date=$1 AND sc.status='active'
            WHERE b.travel_date = $1
              AND b.status = 'confirmed'
              AND b.reminder_sent = FALSE
              AND COALESCE(sc.new_time, s.departure_time) >= $2
              AND COALESCE(sc.new_time, s.departure_time) <= $3
        """, today, remind_from, remind_to)

        for bk in bookings:
            try:
                await ctx.bot.send_message(bk["customer_telegram_id"],
                    f"🌊 *Almost time to set sail!*\n\n"
                    f"Hey there! Just a friendly reminder that your boat departs in about *45 minutes*. "
                    f"Please make your way to the jetty soon! 😊\n\n"
                    f"🚤 *{bk['boat_name'] or bk['business_name']}*\n"
                    f"📍 *{bk['route_from']} → {bk['route_to']}*\n"
                    f"⏰ Departure: *{bk['dep_time']}*\n"
                    f"📌 Location: *{bk.get('location') or 'Jetty No. 1, Male'}*\n"
                    f"🎫 Booking: `{bk['booking_ref']}` | 👥 {bk['passenger_count']} pax\n\n"
                    f"📱 You can use the *FollowMe* app to track your boat in real time.\n\n"
                    f"Wishing you a safe, smooth and wonderful journey! 🌟\n"
                    f"Safe travels from all of us at *Samuga Travels* 🌊🤝",
                    parse_mode="Markdown")
                # Mark as reminded
                await conn.execute(
                    "UPDATE bookings SET reminder_sent=TRUE WHERE booking_ref=$1",
                    bk["booking_ref"])
                logger.info(f"✅ Reminder sent to {bk['customer_telegram_id']} for {bk['booking_ref']}")
            except Exception as e:
                logger.error(f"Reminder failed for {bk['customer_telegram_id']}: {e}")

# ── MAIN ──────────────────────────────────────────────────────────────────────
async def main():
    # Init DB first before anything else
    logger.info("🌊 Starting Samuga Travels Bot v1.2...")
    await init_db()
    logger.info("✅ DB ready — building bot...")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )
    async def cmd_start_with_verify(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Handle /start — if it has verify_ payload, run ticket verify instead"""
        args = ctx.args or []
        if args and args[0].startswith("verify_"):
            await cmd_verify(update, ctx)
        else:
            await cmd_start(update, ctx)
    app.add_handler(CommandHandler("start",  cmd_start_with_verify))
    app.add_handler(CommandHandler("cancel",    cmd_cancel))
    app.add_handler(CommandHandler("verify",    cmd_verify))
    app.add_handler(CommandHandler("register",  cmd_register))
    app.add_handler(CommandHandler("recommend", cmd_recommend))
    app.add_handler(CommandHandler("admin",     cmd_admin))
    app.add_handler(CommandHandler("ops",       cmd_ops))
    app.add_handler(CommandHandler("urgent",       cmd_urgent))
    app.add_handler(CommandHandler("deletemydata", cmd_delete_my_data))
    app.add_handler(CommandHandler("status",       cmd_status))
    app.add_handler(CommandHandler("findcustomer", cmd_findcustomer))

    # ── Flexible operator/customer shortcuts ──
    class FakeCallbackQuery:
        def __init__(self, update, data):
            self.message = update.message
            self.from_user = update.effective_user
            self.data = data
        async def answer(self, *args, **kwargs):
            return None
        async def edit_message_text(self, text=None, parse_mode=None, reply_markup=None, **kwargs):
            await self.message.reply_text(text or "", parse_mode=parse_mode, reply_markup=reply_markup)
        async def edit_message_caption(self, caption=None, parse_mode=None, reply_markup=None, **kwargs):
            await self.message.reply_text(caption or "", parse_mode=parse_mode, reply_markup=reply_markup)

    class FakeCallbackUpdate:
        def __init__(self, update, data):
            self.callback_query = FakeCallbackQuery(update, data)
            self.effective_user = update.effective_user
            self.effective_chat = update.effective_chat
            self.effective_message = update.message

    async def run_callback_shortcut(update, context, data: str):
        await handle_callback(FakeCallbackUpdate(update, data), context)

    async def cmd_profile(u, c):
        sd = await get_user_state(u.effective_user.id)
        if sd.get("role") == "operator":
            await run_callback_shortcut(u, c, "op_profile")
        else:
            await cmd_start(u, c)
    async def cmd_schedules_shortcut(u, c):
        op = await get_operator(u.effective_user.id)
        if op and op.get("status") == "approved":
            await set_user_state(u.effective_user.id, OP_AWAIT_SCHEDULE_ROUTE, {})
            await u.message.reply_text("🗓️ *Add a Schedule*\n\nEnter the route stops comma-separated:\n_Example: Male, Thoddoo_", parse_mode="Markdown")
        else:
            await u.message.reply_text("⚠️ Operator account required.")
    async def cmd_bookings_shortcut(u, c):
        await run_callback_shortcut(u, c, "op_bookings")
    async def cmd_fleet_shortcut(u, c):
        await run_callback_shortcut(u, c, "op_fleet")
    async def cmd_today_shortcut(u, c):
        await run_callback_shortcut(u, c, "op_today")
    async def cmd_search_shortcut(u, c):
        await u.message.reply_text("🔍 Type your route to search:\n_Example: Male to Thoddoo_", parse_mode="Markdown")
    async def cmd_mybookings_shortcut(u, c):
        await run_callback_shortcut(u, c, "cx_my_bookings")
    async def cmd_help_full(u, c):
        sd = await get_user_state(u.effective_user.id)
        role = sd.get("role","customer")
        op = await get_operator(u.effective_user.id)
        is_op = op and op.get("status") == "approved"
        if is_op:
            await u.message.reply_text(
                "🌊 *Samuga Travels — Operator Commands*\n\n"
                "Just type naturally or use any of these:\n\n"
                "/profile — View your profile\n"
                "/schedules — Add a schedule\n"
                "/bookings — Pending bookings\n"
                "/fleet — Manage your boats\n"
                "/today — Today\'s schedule\n"
                "/status — Your account status\n"
                "/urgent — Request urgent review\n"
                "/cancel — Cancel current action\n"
                "/start — Main menu\n\n"
                "_Commands are flexible — close enough works!_",
                parse_mode="Markdown")
        else:
            await u.message.reply_text(
                "🌊 *Samuga Travels — Help*\n\n"
                "Just type a route like *Male to Thoddoo* to start!\n\n"
                "/start — Main menu\n"
                "/mybookings — Your bookings\n"
                "/search — Search boats\n"
                "/status — Application status\n"
                "/cancel — Cancel current action\n\n"
                "_You can also just type naturally — the bot understands!_",
                parse_mode="Markdown")

    for cmd in ["profile", "myprofile"]:
        app.add_handler(CommandHandler(cmd, cmd_profile))
    for cmd in ["schedules", "addschedule", "schedule"]:
        app.add_handler(CommandHandler(cmd, cmd_schedules_shortcut))
    for cmd in ["bookings", "mybookings", "pending"]:
        app.add_handler(CommandHandler(cmd, cmd_bookings_shortcut))
    for cmd in ["fleet", "boats", "myfleet"]:
        app.add_handler(CommandHandler(cmd, cmd_fleet_shortcut))
    for cmd in ["today", "todayschedule"]:
        app.add_handler(CommandHandler(cmd, cmd_today_shortcut))
    for cmd in ["report", "monthly", "earnings"]:
        app.add_handler(CommandHandler(cmd, lambda u, c: run_callback_shortcut(u, c, "op_monthly_report")))
    for cmd in ["search", "searchboats", "book"]:
        app.add_handler(CommandHandler(cmd, cmd_search_shortcut))
    for cmd in ["help", "commands"]:
        app.add_handler(CommandHandler(cmd, cmd_help_full))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    # ── Scheduled jobs ──
    from datetime import time as dt_time
    jq = app.job_queue
    # Evening schedule-prep ping: 20:00 MVT = 15:00 UTC
    jq.run_daily(job_morning_ping, time=dt_time(15, 0, 0), name="evening_schedule_ping")
    # Departure reminders: every 5 minutes
    jq.run_repeating(job_departure_reminders, interval=300, first=30, name="departure_reminders")
    # Payment confirmation watchdog: first ping after 15 min, second ping 5 min later
    jq.run_repeating(job_payment_confirmation_watchdog, interval=300, first=120, name="payment_confirmation_watchdog")
    # Subscription expiry check: daily 9AM MVT = 04:00 UTC
    jq.run_daily(job_subscription_check, time=dt_time(4, 0, 0), name="subscription_check")
    logger.info("✅ Scheduled jobs registered")

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    logger.info("🌊 Samuga Travels Bot v1.2 LIVE!")

    # Graceful shutdown on SIGTERM (Railway stop signal)
    stop_event = asyncio.Event()
    def _handle_sigterm(*_):
        logger.info("🛑 SIGTERM received — shutting down gracefully...")
        stop_event.set()
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    await stop_event.wait()
    logger.info("👋 Stopping bot...")
    await app.updater.stop()
    await app.stop()
    await app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
