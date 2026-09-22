import asyncio
import hashlib
import hmac
import html
import io
import json
import logging
import os
import re
import secrets
import string
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional, Literal
from urllib.parse import parse_qsl, urlparse

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
from openpyxl.utils import get_column_letter

import psycopg
from dotenv import load_dotenv
from fastapi import (
    FastAPI,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from PIL import Image
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field, field_validator
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================================================
# NABA - Main Backend
# =========================================================

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | NABA | %(message)s",
)

logger = logging.getLogger("NABA")

# =========================================================
# Environment
# =========================================================

def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip().strip("'\"").strip()

def env_int(name: str, default: int = 0) -> int:
    raw = env(name, str(default))
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be an integer")

def origin_of(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise RuntimeError(f"Invalid URL: {url}")
    return f"{parsed.scheme}://{parsed.netloc}"

# =========================================================
# Configuration
# =========================================================

BOT_TOKEN = env("BOT_TOKEN")
CHANNEL_ID = env("CHANNEL_ID")
WEB_APP_URL = env("WEB_APP_URL")
BACKEND_URL = env("BACKEND_URL").rstrip("/")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = env("WEBHOOK_URL").rstrip("/")
DATABASE_URL = env("DATABASE_URL")

ADMIN_TELEGRAM_ID = env_int("ADMIN_TELEGRAM_ID", 6931187332)
ADMIN_USER_ID = ADMIN_TELEGRAM_ID

MAX_ATTACHMENT_MB = env_int("MAX_ATTACHMENT_MB", 5)
MAX_ATTACHMENT_BYTES = MAX_ATTACHMENT_MB * 1024 * 1024
MAX_PENDING_ATTACHMENTS = env_int("MAX_PENDING_ATTACHMENTS", 5)
INIT_DATA_MAX_AGE = env_int("INIT_DATA_MAX_AGE", 3600)

CAMERA_URL = env("CAMERA_URL") or f"{WEB_APP_URL.rstrip('/')}/camera.html"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not configured")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not configured")
if not WEB_APP_URL or not WEB_APP_URL.startswith("https://"):
    raise RuntimeError("WEB_APP_URL must use HTTPS")
if not BACKEND_URL or not BACKEND_URL.startswith("https://"):
    raise RuntimeError("BACKEND_URL must use HTTPS")

if not WEBHOOK_URL:
    WEBHOOK_URL = f"{BACKEND_URL}{WEBHOOK_PATH}"

if not WEBHOOK_URL.startswith("https://"):
    raise RuntimeError("WEBHOOK_URL must use HTTPS")

configured_webhook_secret = env("WEBHOOK_SECRET")
if re.fullmatch(r"[A-Za-z0-9_-]{16,256}", configured_webhook_secret or ""):
    WEBHOOK_SECRET = configured_webhook_secret
else:
    WEBHOOK_SECRET = hashlib.sha256(f"naba-webhook:{BOT_TOKEN}".encode()).hexdigest()

PHOTO_ALLOWED_IDS = {ADMIN_TELEGRAM_ID}
for raw_id in env("PHOTO_ALLOWED_IDS").split(","):
    raw_id = raw_id.strip()
    if raw_id.lstrip("-").isdigit():
        PHOTO_ALLOWED_IDS.add(int(raw_id))

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index.html"

# =========================================================
# Services
# =========================================================

SERVICES = {
    "research": "مشاريع • تقارير • بحوث",
    "autocad": "رسم وتصميم AutoCAD",
    "minitab": "تحليل البيانات Minitab",
    "formatting": "تنسيق PowerPoint / Word / PDF",
    "cv": "سيرة ذاتية CV احترافية",
    "cv_ats": "سيرة ذاتية CV بنظام ATS",
    "logo": "تصميم Logo",
    "identity": "Logo + هوية بصرية",
}

SERVICE_DETAIL_LABELS = {
    "cv_type": "نوع السيرة الذاتية",
    "photo": "الصورة",
    "language": "لغة السيرة الذاتية",
    "target_job": "الوظيفة / المجال المستهدف",
    "design_type": "نوع التصميم",
    "brand_name": "اسم المشروع / العلامة",
    "business_field": "مجال النشاط",
    "design_idea": "فكرة التصميم والتفاصيل المطلوبة",
    "preferred_colors": "الألوان / النمط المفضل",
    "usage": "أماكن استخدام التصميم",
    "extra_notes": "تعليمات إضافية",
}

VALID_STATUSES = [
    "NEW", "UNDER_REVIEW", "WAITING_CUSTOMER", "ACCEPTED",
    "READY_TO_START", "IN_PROGRESS", "READY_FOR_DELIVERY",
    "COMPLETED", "REJECTED", "CANCELLED",
]

ALLOWED_STATUS_TRANSITIONS = {
    "NEW": ["UNDER_REVIEW", "REJECTED", "CANCELLED"],
    "UNDER_REVIEW": ["WAITING_CUSTOMER", "READY_TO_START", "REJECTED", "CANCELLED"],
    "WAITING_CUSTOMER": ["ACCEPTED", "REJECTED", "CANCELLED"],
    "ACCEPTED": ["READY_TO_START", "CANCELLED"],
    "READY_TO_START": ["IN_PROGRESS", "CANCELLED"],
    "IN_PROGRESS": ["UNDER_REVIEW", "READY_FOR_DELIVERY", "CANCELLED"],
    "READY_FOR_DELIVERY": ["CANCELLED"],
    "COMPLETED": [],
    "REJECTED": [],
    "CANCELLED": [],
}

# =========================================================
# Database
# =========================================================

@contextmanager
def db():
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                username TEXT NOT NULL DEFAULT '',
                service_type TEXT NOT NULL DEFAULT '',
                service_name TEXT NOT NULL DEFAULT '',
                department TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                page_count TEXT NOT NULL DEFAULT '',
                language TEXT NOT NULL DEFAULT '',
                autocad_type TEXT NOT NULL DEFAULT '',
                deadline TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                order_status TEXT NOT NULL DEFAULT 'NEW',
                payment_status TEXT NOT NULL DEFAULT 'UNPAID',
                price NUMERIC(12,2) NOT NULL DEFAULT 0,
                deposit NUMERIC(12,2) NOT NULL DEFAULT 0,
                remaining_balance NUMERIC(12,2) NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                service_details JSONB NOT NULL DEFAULT '{}'::jsonb,
                admin_note TEXT NOT NULL DEFAULT '',
                delivery_date TEXT NOT NULL DEFAULT '',
                delivery_time TEXT NOT NULL DEFAULT '',
                quotation_price NUMERIC(12,2) NOT NULL DEFAULT 0,
                quotation_currency TEXT NOT NULL DEFAULT 'IQD',
                quotation_notes TEXT NOT NULL DEFAULT '',
                customer_decision TEXT NOT NULL DEFAULT 'PENDING',
                customer_decision_at TIMESTAMPTZ,
                idempotency_key TEXT,
                idempotency_fingerprint TEXT,
                delivery_channel_status TEXT NOT NULL DEFAULT 'PENDING',
                channel_message_id BIGINT,
                channel_attachment_message_id BIGINT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS channel_customer_messages (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                order_id TEXT REFERENCES orders(order_id) ON DELETE CASCADE,
                user_id BIGINT NOT NULL,
                channel_message_id BIGINT NOT NULL UNIQUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                order_id TEXT NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
                amount NUMERIC(12,2) NOT NULL,
                currency TEXT NOT NULL DEFAULT 'IQD',
                payment_method TEXT NOT NULL DEFAULT 'CASH',
                recorded_by TEXT NOT NULL DEFAULT 'Admin',
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_logs (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                order_id TEXT NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
                action TEXT NOT NULL,
                performed_by TEXT NOT NULL DEFAULT 'System',
                details TEXT NOT NULL DEFAULT '',
                timestamp TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS attachments (
                token TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                filename TEXT NOT NULL,
                content_type TEXT NOT NULL,
                data BYTEA NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                order_id TEXT
            )
        """)

async def init_db_with_retry(attempts: int = 5, delay: int = 3):
    for attempt in range(1, attempts + 1):
        try:
            await asyncio.to_thread(init_db)
            logger.info("Database initialized successfully")
            return
        except psycopg.OperationalError:
            logger.warning("Database unavailable - attempt %s/%s", attempt, attempts, exc_info=True)
            if attempt == attempts:
                raise
            await asyncio.sleep(delay * attempt)

def add_audit_log_conn(conn, order_id: str, action: str, performed_by: str = "System", details: str = ""):
    conn.execute("""
        INSERT INTO audit_logs(order_id, action, performed_by, details)
        VALUES(%s, %s, %s, %s)
    """, (order_id, action, performed_by, details))

def add_audit_log(order_id: str, action: str, performed_by: str = "System", details: str = ""):
    with db() as conn:
        add_audit_log_conn(conn, order_id, action, performed_by, details)

# =========================================================
# Auth
# =========================================================

def verify_init_data(init_data: str) -> dict:
    if not init_data:
        raise HTTPException(status_code=401, detail="افتح تطبيق النبع من داخل Telegram")
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        raise HTTPException(status_code=401, detail="بيانات Telegram غير صالحة")

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="تعذر التحقق من جلسة Telegram")

    data_check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash):
        raise HTTPException(status_code=401, detail="جلسة Telegram غير موثوقة")

    try:
        user = json.loads(pairs.get("user", "{}"))
        user_id = int(user["id"])
        auth_date = int(pairs.get("auth_date", "0"))
    except Exception:
        raise HTTPException(status_code=401, detail="بيانات مستخدم Telegram غير صالحة")

    if not auth_date or (int(time.time()) - auth_date > INIT_DATA_MAX_AGE):
        raise HTTPException(status_code=401, detail="انتهت صلاحية جلسة Telegram، أعد فتح التطبيق")

    return {
        "id": user_id,
        "username": user.get("username", "") or "",
        "first_name": user.get("first_name", "") or "",
        "last_name": user.get("last_name", "") or "",
    }

def init_user_from_header(request: Request) -> dict:
    return verify_init_data(request.headers.get("X-Telegram-Init-Data", ""))

def require_admin(request: Request) -> dict:
    user = init_user_from_header(request)
    if user["id"] != ADMIN_TELEGRAM_ID:
        raise HTTPException(status_code=403, detail="غير مصرح لك بالوصول إلى لوحة التحكم")
    return user

# =========================================================
# Models
# =========================================================

class OrderIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")
    service_type: Literal["research", "autocad", "minitab", "formatting", "cv", "cv_ats", "logo", "identity"]
    department: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    page_count: str = Field(default="", max_length=40)
    language: str = Field(default="", max_length=80)
    autocad_type: str = Field(default="", max_length=100)
    deadline: str = Field(min_length=1, max_length=40)
    notes: str = Field(default="", max_length=2000)
    attachment_token: str = Field(default="", max_length=100)
    service_details: dict[str, str] = Field(default_factory=dict)

class QuotationIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")
    price: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    currency: str = Field(default="IQD", max_length=10)
    delivery_date: str = Field(min_length=1, max_length=40)
    delivery_time: str = Field(default="", max_length=40)
    notes: str = Field(default="", max_length=1000)

class StatusUpdateIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")
    status: str = Field(min_length=1, max_length=50)
    admin_note: str = Field(default="", max_length=1000)

class PaymentIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    currency: str = Field(default="IQD", max_length=10)
    payment_method: str = Field(default="CASH", max_length=50)

class AdminNoteIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")
    admin_note: str = Field(default="", max_length=2000)

# =========================================================
# Order Logic
# =========================================================

def new_order_id() -> str:
    return f"NB-{datetime.now().year}-{''.join(secrets.choice(string.digits) for _ in range(6))}"

def create_order(user: dict, data: OrderIn, idempotency_key: Optional[str] = None):
    for _ in range(10):
        order_id = new_order_id()
        try:
            with db() as conn:
                attachment = None
                if data.attachment_token:
                    attachment = conn.execute("""
                        SELECT * FROM attachments WHERE token=%s AND user_id=%s FOR UPDATE
                    """, (data.attachment_token, user["id"])).fetchone()

                order = conn.execute("""
                    INSERT INTO orders (
                        order_id, user_id, username, service_type, service_name,
                        department, title, page_count, language, autocad_type,
                        deadline, notes, service_details, idempotency_key
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                    RETURNING *
                """, (
                    order_id, user["id"], user["username"] or user["first_name"] or "",
                    data.service_type, SERVICES[data.service_type], data.department, data.title,
                    str(data.page_count or ""), str(data.language or ""), str(data.autocad_type or ""),
                    data.deadline, data.notes or "", json.dumps(data.service_details, ensure_ascii=False),
                    idempotency_key
                )).fetchone()

                if attachment:
                    conn.execute("UPDATE attachments SET order_id=%s WHERE token=%s", (order_id, attachment["token"]))

                add_audit_log_conn(conn, order_id, "ORDER_CREATED", f"User_{user['id']}", "تم إنشاء الطلب")
            return order, attachment, False
        except psycopg.errors.UniqueViolation:
            continue
    raise HTTPException(status_code=500, detail="تعذر إنشاء رقم الطلب")

def parse_channel_id():
    if not CHANNEL_ID:
        return None
    return int(CHANNEL_ID) if CHANNEL_ID.lstrip("-").isdigit() else CHANNEL_ID

def build_order_message(order: dict, user: dict) -> str:
    e = html.escape
    username = user.get("username") or "بدون_يوزر"
    text = (
        "📥 <b>طلب جديد من النبع</b>\n\n"
        f"🆔 <b>رقم الطلب:</b> <code>{e(order['order_id'])}</code>\n"
        f"👤 <b>الطالب:</b> {e(user.get('first_name') or '')} (@{e(username)})\n"
        f"🆔 <b>Telegram ID:</b> <code>{order['user_id']}</code>\n"
        f"🛠️ <b>الخدمة:</b> {e(order['service_name'])}\n"
        f"🏫 <b>التخصص:</b> {e(order['department'])}\n"
        f"📌 <b>العنوان:</b> {e(order['title'])}\n"
        f"📄 <b>الصفحات:</b> {e(order['page_count'] or 'غير محدد')}\n"
        f"🌐 <b>اللغة:</b> {e(order['language'] or 'غير محدد')}\n"
        f"⏰ <b>الموعد:</b> {e(order['deadline'])}\n"
    )
    if order["service_type"] == "autocad":
        text += f"📐 <b>نوع الرسم:</b> {e(order['autocad_type'] or 'غير محدد')}\n"

    details = order.get("service_details") or {}
    if isinstance(details, str):
        try: details = json.loads(details)
        except Exception: details = {}

    if details:
        text += "\n🎯 <b>تفاصيل الخدمة:</b>\n"
        for k, v in details.items():
            text += f"• <b>{e(SERVICE_DETAIL_LABELS.get(k, k))}:</b> {e(str(v))}\n"

    notes = order["notes"] or "لا توجد ملاحظات"
    text += f"\n📝 <b>الملاحظات:</b> {e(notes[:500])}"
    return text

async def notify_customer(user_id: int, text: str):
    try:
        await telegram_app.bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
        return True
    except Exception as exc:
        logger.error("Could not notify user %s: %s", user_id, exc)
        return False

async def deliver_order(order: dict, user: dict, attachment) -> bool:
    channel = parse_channel_id()
    if not channel:
        return False
    try:
        msg = await telegram_app.bot.send_message(chat_id=channel, text=build_order_message(order, user), parse_mode="HTML")
        att_msg = None
        if attachment:
            data = bytes(attachment["data"])
            caption = f"📎 مرفق الطلب {order['order_id']}"
            if (attachment["content_type"] or "").startswith("image/"):
                att_msg = await telegram_app.bot.send_photo(chat_id=channel, photo=data, caption=caption)
            else:
                att_msg = await telegram_app.bot.send_document(chat_id=channel, document=data, filename=attachment["filename"], caption=caption)

        with db() as conn:
            conn.execute("""
                UPDATE orders SET delivery_channel_status='DELIVERED', channel_message_id=%s, channel_attachment_message_id=%s WHERE order_id=%s
            """, (msg.message_id, att_msg.message_id if att_msg else None, order['order_id']))
        return True
    except Exception:
        logger.exception("Failed to deliver order to channel")
        return False

# =========================================================
# Telegram Handlers (الرد على رسائل الزبون وقبول العروض)
# =========================================================

async def customer_private_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.effective_message:
        return
    user = update.effective_user
    if user.id == ADMIN_TELEGRAM_ID:
        return

    raw_text = (update.effective_message.text or update.effective_message.caption or "مرفق غير نصي").strip()
    norm = raw_text.lower().replace("أ", "ا").replace("إ", "ا")

    # التعامل مع ردود عرض السعر
    if norm in {"موافق", "موافقة", "اوافق", "نعم", "yes"}:
        with db() as conn:
            order = conn.execute("SELECT * FROM orders WHERE user_id=%s AND order_status='WAITING_CUSTOMER' ORDER BY created_at DESC LIMIT 1", (user.id,)).fetchone()
            if order:
                conn.execute("UPDATE orders SET customer_decision='ACCEPTED', order_status='ACCEPTED' WHERE order_id=%s", (order['order_id'],))
                await telegram_app.bot.send_message(chat_id=parse_channel_id(), text=f"✅ الزبون وافق على عرض السعر للطلب <code>{order['order_id']}</code>", parse_mode="HTML")
                await notify_customer(user.id, "✅ تم تسجيل موافقتك على عرض السعر بنجاح.")
                return

    if norm in {"ارفض", "رفض", "أرفض", "لا", "no"}:
        with db() as conn:
            order = conn.execute("SELECT * FROM orders WHERE user_id=%s AND order_status='WAITING_CUSTOMER' ORDER BY created_at DESC LIMIT 1", (user.id,)).fetchone()
            if order:
                conn.execute("UPDATE orders SET customer_decision='REJECTED', order_status='REJECTED' WHERE order_id=%s", (order['order_id'],))
                await telegram_app.bot.send_message(chat_id=parse_channel_id(), text=f"❌ الزبون رفض عرض السعر للطلب <code>{order['order_id']}</code>", parse_mode="HTML")
                await notify_customer(user.id, "❌ تم تسجيل رفضك لعرض السعر.")
                return

    # توجيه رسالة الزبون العادية إلى القناة
    with db() as conn:
        order = conn.execute("SELECT order_id FROM orders WHERE user_id=%s ORDER BY created_at DESC LIMIT 1", (user.id,)).fetchone()

    order_id = order["order_id"] if order else "بدون طلب"
    channel_msg = (
        f"💬 <b>رسالة جديدة من الزبون</b>\n\n"
        f"🆔 <b>الطلب:</b> <code>{order_id}</code>\n"
        f"👤 <b>الزبون:</b> @{html.escape(user.username or 'بدون_يوزر')} | ID: <code>{user.id}</code>\n\n"
        f"📝 <b>الرسالة:</b>\n{html.escape(raw_text)}"
    )

    channel = parse_channel_id()
    if channel:
        sent = await telegram_app.bot.send_message(chat_id=channel, text=channel_msg, parse_mode="HTML")
        if sent:
            with db() as conn:
                conn.execute("INSERT INTO channel_customer_messages (order_id, user_id, channel_message_id) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (order if order else None, user.id, sent.message_id))
            await notify_customer(user.id, "✅ تم إرسال رسالتك إلى الكادر.")

async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    post = update.channel_post
    if not post or not post.reply_to_message:
        return
    reply_text = (post.text or post.caption or "").strip()
    if not reply_text:
        return

    replied_id = post.reply_to_message.message_id
    with db() as conn:
        record = conn.execute("SELECT user_id, order_id FROM channel_customer_messages WHERE channel_message_id=%s", (replied_id,)).fetchone()
        if not record:
            record = conn.execute("SELECT user_id, order_id FROM orders WHERE channel_message_id=%s OR channel_attachment_message_id=%s", (replied_id, replied_id)).fetchone()

    if record:
        oid = record["order_id"] or "عام"
        await notify_customer(record["user_id"], f"📩 <b>رسالة من إدارة النبع</b>\n🆔 <b>الطلب:</b> <code>{oid}</code>\n\n{html.escape(reply_text)}")

# =========================================================
# Excel Generator (إصلاح وتثبيت عطل التصدير بالكامل)
# =========================================================

def build_excel_report() -> bytes:
    wb = openpyxl.Workbook()
    header_fill = PatternFill(start_color="087FC1", end_color="087FC1", fill_type="solid")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    thin_border = Border(left=Side(style="thin", color="CCCCCC"), right=Side(style="thin", color="CCCCCC"), top=Side(style="thin", color="CCCCCC"), bottom=Side(style="thin", color="CCCCCC"))

    def style_sheet(ws):
        ws.views.sheetView[0].rightToLeft = True
        for col in range(1, ws.max_column + 1):
            cell = ws.cell(row=1, column=col)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")
            ws.column_dimensions[get_column_letter(col)].width = 20
        for row in range(2, ws.max_row + 1):
            for col in range(1, ws.max_column + 1):
                cell = ws.cell(row=row, column=col)
                cell.border = thin_border
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    with db() as conn:
        orders = conn.execute("SELECT * FROM orders ORDER BY created_at DESC").fetchall()
        payments = conn.execute("SELECT * FROM payments ORDER BY created_at DESC").fetchall()
        logs = conn.execute("SELECT * FROM audit_logs ORDER BY timestamp DESC").fetchall()

    ws1 = wb.active
    ws1.title = "Orders"
    ws1.append(["رقم الطلب", "المستخدم ID", "اسم المستخدم", "الخدمة", "التخصص", "العنوان", "الحالة", "السعر", "المدفوع", "المتبقي", "تاريخ الإنشاء"])
    for o in orders:
        ws1.append([o["order_id"], o["user_id"], o["username"], o["service_name"], o["department"], o["title"], o["order_status"], float(o["price"] or 0), float(o["deposit"] or 0), float(o["remaining_balance"] or 0), o["created_at"].strftime("%Y-%m-%d %H:%M") if o["created_at"] else ""])
    style_sheet(ws1)

    ws2 = wb.create_sheet("Payments")
    ws2.append(["رقم المعاملة", "رقم الطلب", "المبلغ", "العملة", "طريقة الدفع", "التاريخ"])
    for p in payments:
        ws2.append([p["id"], p["order_id"], float(p["amount"] or 0), p["currency"], p["payment_method"], p["created_at"].strftime("%Y-%m-%d %H:%M") if p["created_at"] else ""])
    style_sheet(ws2)

    ws3 = wb.create_sheet("Activity")
    ws3.append(["ID", "رقم الطلب", "الحدث", "بواسطة", "التفاصيل", "التاريخ"])
    for l in logs:
        ws3.append([l["id"], l["order_id"], l["action"], l["performed_by"], l["details"], l["timestamp"].strftime("%Y-%m-%d %H:%M") if l["timestamp"] else ""])
    style_sheet(ws3)

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()

# =========================================================
# App & Bot Init
# =========================================================

telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()
telegram_app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("أهلاً بك في النبع للخدمات الجامعية 🎓")))
telegram_app.add_handler(MessageHandler(filters.ChatType.CHANNEL, channel_post_handler))
telegram_app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, customer_private_message_handler))

@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db_with_retry()
    await telegram_app.initialize()
    try:
        await telegram_app.bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET, allowed_updates=["message", "channel_post"])
    except Exception:
        logger.exception("Webhook configuration failed")
    yield
    await telegram_app.shutdown()

app = FastAPI(title="النبع API", version="8.5.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.get("/")
def root():
    if INDEX_FILE.exists():
        return FileResponse(INDEX_FILE, media_type="text/html")
    return {"status": "online"}

@app.post(WEBHOOK_PATH)
async def webhook(request: Request):
    if not hmac.compare_digest(request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), WEBHOOK_SECRET):
        raise HTTPException(status_code=403, detail="Forbidden")
    data = await request.json()
    await telegram_app.process_update(Update.de_json(data, telegram_app.bot))
    return {"ok": True}

@app.post("/api/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    user = init_user_from_header(request)
    data = await file.read()
    token = secrets.token_urlsafe(24)
    with db() as conn:
        conn.execute("INSERT INTO attachments (token, user_id, filename, content_type, data) VALUES (%s,%s,%s,%s,%s)", (token, user["id"], file.filename[:100], file.content_type or "application/octet-stream", data))
    return {"token": token, "filename": file.filename}

@app.post("/api/orders")
async def submit_order_api(payload: OrderIn, request: Request):
    user = init_user_from_header(request)
    order, attachment, _ = create_order(user, payload)
    delivered = await deliver_order(order, user, attachment)
    return {"ok": True, "order_id": order["order_id"], "delivered": delivered}

@app.get("/api/admin/access")
def admin_access(request: Request):
    user = require_admin(request)
    return {"ok": True, "is_admin": True, "telegram_id": user["id"]}

@app.get("/api/admin/dashboard")
def admin_dashboard(request: Request):
    require_admin(request)
    with db() as conn:
        totals = conn.execute("SELECT COUNT(*) AS total_orders, COALESCE(SUM(price),0) AS total_price FROM orders").fetchone()
        recent = conn.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 20").fetchall()
    return {"ok": True, "summary": totals, "recent_orders": recent}

@app.get("/api/admin/orders")
def list_orders(request: Request):
    require_admin(request)
    with db() as conn:
        orders = conn.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 50").fetchall()
    return {"ok": True, "orders": orders}

@app.get("/api/admin/export/excel")
async def export_excel(request: Request):
    require_admin(request)
    excel_bytes = await asyncio.to_thread(build_excel_report)
    return Response(
        content=excel_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": 'attachment; filename="NABA_Orders.xlsx"',
            "Cache-Control": "no-store, no-cache, must-revalidate",
        },
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=env_int("PORT", 8000))
