import base64
import hashlib
import hmac
import io
import json
import logging
import os
import secrets
import string
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qsl, urlparse

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from PIL import Image
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("NABA")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "-1004493656435").strip()
WEB_APP_URL = os.getenv("WEB_APP_URL", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
WEB_APP_ORIGIN = ""
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")
if WEBHOOK_URL and not WEBHOOK_URL.endswith("/webhook"):
    WEBHOOK_URL += "/webhook"
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "6931187332"))
MAX_ATTACHMENT_BYTES = int(os.getenv("MAX_ATTACHMENT_MB", "5")) * 1024 * 1024
RUN_POLLING = os.getenv("RUN_TELEGRAM_POLLING", "0").lower() in {"1", "true", "yes"}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not configured")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not configured. Add your Neon PostgreSQL connection string.")
if not WEB_APP_URL or not WEB_APP_URL.startswith("https://"):
    raise RuntimeError("WEB_APP_URL must be a public HTTPS Telegram Mini App URL")
parsed_web_app_url = urlparse(WEB_APP_URL)
WEB_APP_ORIGIN = f"{parsed_web_app_url.scheme}://{parsed_web_app_url.netloc}"

app = FastAPI(title="النبع للخدمات الجامعية API", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[WEB_APP_ORIGIN],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Telegram-Init-Data"],
)

telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()


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
    statements = [
        """CREATE TABLE IF NOT EXISTS orders (
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
            payment_status TEXT NOT NULL DEFAULT 'PENDING',
            price NUMERIC(12,2) NOT NULL DEFAULT 0,
            deposit NUMERIC(12,2) NOT NULL DEFAULT 0,
            remaining_balance NUMERIC(12,2) NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS audit_logs (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            order_id TEXT NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
            action TEXT NOT NULL,
            performed_by TEXT NOT NULL DEFAULT 'System',
            timestamp TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS attachments (
            token TEXT PRIMARY KEY,
            user_id BIGINT NOT NULL,
            filename TEXT NOT NULL,
            content_type TEXT NOT NULL,
            data BYTEA NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS orders_user_id_idx ON orders(user_id)",
        "CREATE INDEX IF NOT EXISTS attachments_user_id_idx ON attachments(user_id)",
    ]
    with db() as conn:
        for statement in statements:
            conn.execute(statement)


def now_utc():
    return datetime.now(timezone.utc)


def verify_init_data(init_data: str) -> dict:
    pairs = dict(parse_qsl(init_data or "", keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash or not pairs:
        raise HTTPException(401, "بيانات Telegram غير صالحة")
    data_check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise HTTPException(401, "تعذر التحقق من جلسة Telegram")
    try:
        user = json.loads(pairs.get("user", "{}"))
        user_id = int(user["id"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise HTTPException(401, "بيانات مستخدم Telegram غير صالحة")
    auth_date = int(pairs.get("auth_date", "0"))
    if auth_date and time.time() - auth_date > 86400:
        raise HTTPException(401, "انتهت صلاحية جلسة Telegram، أعد فتح التطبيق")
    return {"id": user_id, "username": user.get("username", ""), "first_name": user.get("first_name", "")}


def init_user_from_header(request: Request):
    return verify_init_data(request.headers.get("X-Telegram-Init-Data", ""))


def log_action(order_id: str, action: str, performed_by: str = "System"):
    with db() as conn:
        conn.execute("INSERT INTO audit_logs(order_id, action, performed_by) VALUES(%s,%s,%s)", (order_id, action, performed_by))


def new_order_id():
    while True:
        value = "NB-2026-" + "".join(secrets.choice(string.digits) for _ in range(6))
        with db() as conn:
            exists = conn.execute("SELECT 1 FROM orders WHERE order_id=%s", (value,)).fetchone()
        if not exists:
            return value


def parse_channel_id():
    if not CHANNEL_ID:
        return None
    return int(CHANNEL_ID) if CHANNEL_ID.lstrip("-").isdigit() else CHANNEL_ID


async def send_order_message(chat_id, text, attachment=None):
    if attachment:
        stream = io.BytesIO(attachment["data"])
        stream.name = attachment["filename"]
        if attachment["content_type"].startswith("image/"):
            await telegram_app.bot.send_photo(chat_id=chat_id, photo=stream, caption=text[:1024], parse_mode="HTML")
        else:
            await telegram_app.bot.send_document(chat_id=chat_id, document=stream, caption=text[:1024], parse_mode="HTML")
    else:
        await telegram_app.bot.send_message(chat_id=chat_id, text=text[:4096], parse_mode="HTML")


def html_escape(value):
    return (str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def build_order_message(order, user):
    return (
        "📥 <b>طلب جديد من النبع</b>\n\n"
        f"🆔 <b>رقم الطلب:</b> <code>{html_escape(order['order_id'])}</code>\n"
        f"👤 <b>الطالب:</b> {html_escape(user.get('first_name'))} (@{html_escape(user.get('username') or 'بدون_يوزر')})\n"
        f"🛠️ <b>الخدمة:</b> {html_escape(order['service_name'])}\n"
        f"🏫 <b>التخصص:</b> {html_escape(order['department'])}\n"
        f"📌 <b>العنوان:</b> {html_escape(order['title'])}\n"
        f"📄 <b>الصفحات:</b> {html_escape(order['page_count'])}\n"
        f"🌐 <b>اللغة:</b> {html_escape(order['language'])}\n"
        f"⏰ <b>الموعد:</b> {html_escape(order['deadline'])}\n"
        f"📝 <b>الملاحظات:</b> {html_escape(order['notes'])}"
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 فتح تطبيق النبع", web_app=WebAppInfo(url=WEB_APP_URL))],
    ])
    await update.message.reply_text(
        "👋 أهلًا بك في <b>النبع للخدمات الجامعية</b> 🎓\n\n"
        "نساعدك على إرسال طلبات البحوث والتقارير والتصاميم الهندسية بسهولة.\n\n"
        "<b>طريقة الاستخدام:</b>\n"
        "1️⃣ اضغط زر <b>فتح تطبيق النبع</b>.\n"
        "2️⃣ اختر نوع الخدمة المطلوبة.\n"
        "3️⃣ املأ بيانات الطلب والموعد النهائي.\n"
        "4️⃣ أرفق ملف التعليمات إن وجد.\n"
        "5️⃣ اضغط إرسال، وستصلك رسالة تأكيد ورقم الطلب هنا.\n\n"
        "اضغط الزر أدناه للبدء 👇",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 فتح تطبيق النبع", web_app=WebAppInfo(url=WEB_APP_URL))],
    ])
    await update.message.reply_text(
        "📋 <b>مساعدة النبع</b>\n\n"
        "من التطبيق اختر الخدمة، اكتب التفاصيل، ثم أرسل الطلب.\n"
        "يمكنك إرفاق صورة أو PDF أو ملف Word بحجم لا يتجاوز 5 MB.\n\n"
        "للبدء اضغط الزر التالي:",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


@app.post("/webhook")
async def telegram_webhook(request: Request):
    """استقبال تحديثات Telegram عبر Webhook بدل getUpdates/Polling."""
    try:
        payload = await request.json()
        update = Update.de_json(payload, telegram_app.bot)
        await telegram_app.process_update(update)
        return {"ok": True}
    except Exception:
        logger.exception("Telegram webhook processing failed")
        raise HTTPException(400, "بيانات Telegram غير صالحة")


async def web_app_data_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    try:
        data = json.loads(message.web_app_data.data)
        order_id = new_order_id()
        service_name = str(data.get("service_name", "خدمة غير محددة"))[:200]
        order = {
            "order_id": order_id,
            "service_type": str(data.get("service_type", ""))[:60],
            "service_name": service_name,
            "department": str(data.get("department", "غير محدد"))[:200],
            "title": str(data.get("title", "بدون عنوان"))[:300],
            "page_count": str(data.get("page_count", "غير محدد"))[:40],
            "language": str(data.get("language", "غير محدد"))[:80],
            "autocad_type": str(data.get("autocad_type", "غير محدد"))[:100],
            "deadline": str(data.get("deadline", "غير محدد"))[:80],
            "notes": str(data.get("notes", "لا توجد ملاحظات"))[:2000],
        }
        attachment_token = str(data.get("attachment_token", ""))[:100]
        attachment = None
        if attachment_token:
            with db() as conn:
                attachment = conn.execute("SELECT * FROM attachments WHERE token=%s AND user_id=%s", (attachment_token, user.id)).fetchone()
            if attachment:
                with db() as conn:
                    conn.execute("DELETE FROM attachments WHERE token=%s", (attachment_token,))

        with db() as conn:
            conn.execute(
                """INSERT INTO orders(order_id,user_id,username,service_type,service_name,department,title,page_count,language,autocad_type,deadline,notes)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (order_id, user.id, user.username or user.first_name or "", order["service_type"], order["service_name"], order["department"], order["title"], order["page_count"], order["language"], order["autocad_type"], order["deadline"], order["notes"]),
            )
        log_action(order_id, "ORDER_CREATED", f"User_{user.id}")
        with db() as conn:
            saved = conn.execute("SELECT * FROM orders WHERE order_id=%s", (order_id,)).fetchone()
        notification = build_order_message(saved, {"first_name": user.first_name, "username": user.username})
        target = parse_channel_id()
        if target:
            try:
                await send_order_message(target, notification, attachment)
                channel_status = "تم إرسال نسخة إلى القناة بنجاح."
            except Exception:
                logger.exception("Channel delivery failed for order %s", order_id)
                channel_status = "تم تسجيل الطلب، وسيتم إرسال إشعار القناة بعد معالجة المشكلة."
        else:
            logger.error("CHANNEL_ID is not configured for order %s", order_id)
            channel_status = "تم تسجيل الطلب، وسيتم إرسال إشعار القناة بعد معالجة المشكلة."
        await message.reply_text(
            f"✅ <b>تم استلام طلبك</b>\n\n"
            f"رقم الطلب: <code>{order_id}</code>\n"
            f"{channel_status}\n"
            "سيتم التواصل معك عبر هذه المحادثة.",
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("Order processing failed")
        await message.reply_text("❌ تعذر معالجة الطلب. تأكد من البيانات وحاول مرة أخرى.")


@app.get("/")
def read_root():
    return {"status": "Online", "system": "النبع للخدمات الجامعية API", "database": "Neon PostgreSQL"}


@app.get("/health")
def health():
    try:
        with db() as conn:
            conn.execute("SELECT 1")
        return {"status": "ok", "database": "connected", "telegram": bool(BOT_TOKEN)}
    except Exception:
        return JSONResponse(status_code=503, content={"status": "error", "database": "unavailable"})


@app.post("/api/upload")
async def upload_attachment(request: Request, file: UploadFile = File(...)):
    user = init_user_from_header(request)
    content_type = file.content_type or "application/octet-stream"
    allowed = content_type.startswith("image/") or content_type in {"application/pdf", "application/msword", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
    if not allowed:
        raise HTTPException(400, "نوع الملف غير مدعوم")
    data = await file.read(MAX_ATTACHMENT_BYTES + 1)
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(413, f"حجم الملف يجب ألا يتجاوز {MAX_ATTACHMENT_BYTES // 1024 // 1024} MB")
    token = secrets.token_urlsafe(24)
    with db() as conn:
        conn.execute("INSERT INTO attachments(token,user_id,filename,content_type,data) VALUES(%s,%s,%s,%s,%s)", (token, user["id"], file.filename or "attachment", content_type, data))
    return {"token": token, "filename": file.filename or "attachment"}


@app.get("/api/orders/{order_id}")
def get_order_status(order_id: str, request: Request):
    user = init_user_from_header(request)
    with db() as conn:
        row = conn.execute("SELECT order_id,service_name,title,order_status,payment_status,price,deposit,remaining_balance FROM orders WHERE order_id=%s AND user_id=%s", (order_id, user["id"])).fetchone()
    if not row:
        raise HTTPException(404, "الطلب غير موجود")
    return row


@app.on_event("startup")
async def startup_event():
    init_db()
    telegram_app.add_handler(CommandHandler("start", start_command))
    telegram_app.add_handler(CommandHandler("help", help_command))
    telegram_app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, web_app_data_handler))
    await telegram_app.initialize()
    await telegram_app.start()
    if RUN_POLLING:
        await telegram_app.updater.start_polling(drop_pending_updates=False)
        logger.info("Telegram polling started")
    elif WEBHOOK_URL:
        await telegram_app.bot.set_webhook(WEBHOOK_URL, allowed_updates=Update.ALL_TYPES)
        logger.info("Telegram webhook configured: %s", WEBHOOK_URL)


@app.on_event("shutdown")
async def shutdown_event():
    if RUN_POLLING:
        await telegram_app.updater.stop()
    await telegram_app.stop()
    await telegram_app.shutdown()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
