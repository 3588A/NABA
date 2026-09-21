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
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, urlparse

import psycopg
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image, ImageOps
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field, field_validator
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.error import TelegramError
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("NABA")

# ─────────────────────────── الإعدادات ───────────────────────────


def env(name: str, default: str = "") -> str:
    """يقرأ متغير بيئة ويشيل المسافات وعلامات ' " (تحدث كثيرًا عند النسخ من ملف .env إلى لوحة الاستضافة)."""
    return os.getenv(name, default).strip().strip("'\"").strip()


BOT_TOKEN = env("BOT_TOKEN")
CHANNEL_ID = env("CHANNEL_ID")
WEB_APP_URL = env("WEB_APP_URL")
DATABASE_URL = env("DATABASE_URL")
ADMIN_USER_ID = int(env("ADMIN_USER_ID", "6931187332"))
MAX_ATTACHMENT_BYTES = int(env("MAX_ATTACHMENT_MB", "5")) * 1024 * 1024
MAX_PENDING_ATTACHMENTS = 5
# من يحق له النشر بصور الكاميرا في القناة (الأدمن دائمًا + أي IDs إضافية مفصولة بفاصلة)
PHOTO_ALLOWED_IDS = {ADMIN_USER_ID} | {
    int(x) for x in env("PHOTO_ALLOWED_IDS").split(",") if x.strip().lstrip("-").isdigit()
}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not configured")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not configured. Add your Neon PostgreSQL connection string.")
if not WEB_APP_URL or not WEB_APP_URL.startswith("https://"):
    raise RuntimeError("WEB_APP_URL must be a public HTTPS Telegram Mini App URL")


def origin_of(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


# رابط الـ Backend العام (نفس رابط FastAPI Cloud). الـ webhook يُسجَّل عليه.
# إذا الواجهة (index.html) مستضافة في مكان ثاني، حدد BACKEND_URL بشكل منفصل.
BACKEND_URL = env("BACKEND_URL").rstrip("/") or origin_of(WEB_APP_URL)
WEBHOOK_PATH = "/webhook"
CAMERA_URL = env("CAMERA_URL") or WEB_APP_URL.rstrip("/") + "/camera.html"
_derived_secret = hashlib.sha256(f"naba-webhook:{BOT_TOKEN}".encode()).hexdigest()
WEBHOOK_SECRET = env("WEBHOOK_SECRET")
# نتجاهل القيمة إذا كانت نص placeholder (مثل GENERATE_...) أو فيها رموز غير مسموحة عند Telegram
if not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", WEBHOOK_SECRET) or WEBHOOK_SECRET.upper().startswith("GENERATE"):
    WEBHOOK_SECRET = _derived_secret

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index.html"

SERVICES = {
    "research": "مشاريع • تقارير • بحوث",
    "autocad": "رسم وتصميم AutoCAD",
    "minitab": "تحليل البيانات Minitab",
    "formatting": "تنسيق PowerPoint / Word / PDF",
}

# ─────────────────────────── قاعدة البيانات ───────────────────────────


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


# ─────────────────────────── التحقق من Telegram ───────────────────────────


def verify_init_data(init_data: str) -> dict:
    pairs = dict(parse_qsl(init_data or "", keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash or not pairs:
        raise HTTPException(401, "بيانات Telegram غير صالحة، افتح التطبيق من داخل البوت")
    data_check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise HTTPException(401, "تعذر التحقق من جلسة Telegram")
    try:
        user = json.loads(pairs.get("user", "{}"))
        user_id = int(user["id"])
        auth_date = int(pairs.get("auth_date", "0"))
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise HTTPException(401, "بيانات مستخدم Telegram غير صالحة")
    if auth_date and time.time() - auth_date > 86400:
        raise HTTPException(401, "انتهت صلاحية جلسة Telegram، أعد فتح التطبيق")
    return {"id": user_id, "username": user.get("username", ""), "first_name": user.get("first_name", "")}


def init_user_from_header(request: Request) -> dict:
    return verify_init_data(request.headers.get("X-Telegram-Init-Data", ""))


# ─────────────────────────── الطلبات ───────────────────────────


class OrderIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    service_type: Literal["research", "autocad", "minitab", "formatting"]
    department: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    page_count: str = Field(default="", max_length=40)
    language: str = Field(default="", max_length=80)
    autocad_type: str = Field(default="", max_length=100)
    deadline: str = Field(min_length=1, max_length=40)
    notes: str = Field(default="", max_length=2000)
    attachment_token: str = Field(default="", max_length=100)

    @field_validator("deadline")
    @classmethod
    def deadline_must_be_date(cls, value: str) -> str:
        try:
            datetime.fromisoformat(value)
        except ValueError:
            raise ValueError("invalid deadline")
        return value


def new_order_id() -> str:
    return "NB-2026-" + "".join(secrets.choice(string.digits) for _ in range(6))


def create_order(user: dict, data: OrderIn):
    """ينشئ الطلب + سجل التدقيق + يسحب المرفق، كلها في transaction واحد (تعمل داخل thread)."""
    for _ in range(5):
        order_id = new_order_id()
        try:
            with db() as conn:
                attachment = None
                if data.attachment_token:
                    attachment = conn.execute(
                        "DELETE FROM attachments WHERE token=%s AND user_id=%s RETURNING filename, content_type, data",
                        (data.attachment_token, user["id"]),
                    ).fetchone()
                    if not attachment:
                        raise HTTPException(400, "المرفق غير موجود أو انتهت صلاحيته، أعد رفعه")
                order = conn.execute(
                    """INSERT INTO orders(order_id,user_id,username,service_type,service_name,department,title,
                                          page_count,language,autocad_type,deadline,notes)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                    (
                        order_id,
                        user["id"],
                        user["username"] or user["first_name"] or "",
                        data.service_type,
                        SERVICES[data.service_type],
                        data.department,
                        data.title,
                        data.page_count or "غير محدد",
                        data.language or "غير محدد",
                        data.autocad_type or "غير محدد",
                        data.deadline,
                        data.notes or "لا توجد ملاحظات",
                    ),
                ).fetchone()
                conn.execute(
                    "INSERT INTO audit_logs(order_id, action, performed_by) VALUES(%s,%s,%s)",
                    (order_id, "ORDER_CREATED", f"User_{user['id']}"),
                )
            return order, attachment
        except psycopg.errors.UniqueViolation:
            continue  # رقم الطلب مكرر (نادر جدًا) - جرّب رقم آخر
    raise HTTPException(500, "تعذر إنشاء رقم للطلب، حاول مرة أخرى")


def store_attachment(user_id: int, filename: str, content_type: str, data: bytes) -> str:
    token = secrets.token_urlsafe(24)
    with db() as conn:
        # تنظيف المرفقات القديمة التي لم تُربط بطلب
        conn.execute("DELETE FROM attachments WHERE created_at < CURRENT_TIMESTAMP - INTERVAL '1 day'")
        pending = conn.execute("SELECT count(*) AS n FROM attachments WHERE user_id=%s", (user_id,)).fetchone()["n"]
        if pending >= MAX_PENDING_ATTACHMENTS:
            raise HTTPException(429, "عدد كبير من المرفقات غير المرسلة، أرسل الطلب أو حاول لاحقًا")
        conn.execute(
            "INSERT INTO attachments(token,user_id,filename,content_type,data) VALUES(%s,%s,%s,%s,%s)",
            (token, user_id, filename, content_type, data),
        )
    return token


# ─────────────────────────── رسائل Telegram ───────────────────────────


def parse_channel_id():
    if not CHANNEL_ID:
        return None
    return int(CHANNEL_ID) if CHANNEL_ID.lstrip("-").isdigit() else CHANNEL_ID


def build_order_message(order: dict, user: dict) -> str:
    e = html.escape
    head = (
        "📥 <b>طلب جديد من النبع</b>\n\n"
        f"🆔 <b>رقم الطلب:</b> <code>{e(order['order_id'])}</code>\n"
        f"👤 <b>الطالب:</b> {e(user.get('first_name') or '')} (@{e(user.get('username') or 'بدون_يوزر')})\n"
        f"🛠️ <b>الخدمة:</b> {e(order['service_name'])}\n"
        f"🏫 <b>التخصص:</b> {e(order['department'])}\n"
        f"📌 <b>العنوان:</b> {e(order['title'])}\n"
        f"📄 <b>الصفحات:</b> {e(order['page_count'])}\n"
        f"🌐 <b>اللغة:</b> {e(order['language'])}\n"
        f"⏰ <b>الموعد:</b> {e(order['deadline'])}\n"
    )
    if order["service_type"] == "autocad":
        head += f"📐 <b>نوع الرسم:</b> {e(order['autocad_type'])}\n"
    label = "📝 <b>الملاحظات:</b> "
    # نقص الملاحظات بحيث لا نتجاوز 4096 حرف (كل حرف قد يتحول لـ 6 أحرف بعد escape)
    room = max(0, 4000 - len(head) - len(label))
    return head + label + e(order["notes"][: room // 6])


async def send_to_chat(chat_id, text: str, order_id: str, attachment):
    await telegram_app.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
    if not attachment:
        return
    data = bytes(attachment["data"])
    filename = attachment["filename"]
    caption = f"📎 مرفق الطلب {order_id}"
    if attachment["content_type"].startswith("image/"):
        try:
            await telegram_app.bot.send_photo(chat_id=chat_id, photo=data, caption=caption)
            return
        except TelegramError:
            logger.warning("send_photo failed, falling back to document", exc_info=True)
    await telegram_app.bot.send_document(chat_id=chat_id, document=data, filename=filename, caption=caption)


async def deliver_order(order: dict, user: dict, attachment) -> bool:
    """يرسل الطلب للقناة، وإذا فشل (أو ما في قناة) يرسله للأدمن."""
    text = build_order_message(order, user)
    targets = []
    channel = parse_channel_id()
    if channel:
        targets.append(channel)
    if ADMIN_USER_ID:
        targets.append(ADMIN_USER_ID)
    for chat_id in targets:
        try:
            await send_to_chat(chat_id, text, order["order_id"], attachment)
            return True
        except TelegramError:
            logger.exception("Failed to deliver order %s to %s", order["order_id"], chat_id)
    return False


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 فتح تطبيق النبع", web_app=WebAppInfo(url=WEB_APP_URL))]])
    await update.effective_message.reply_text(
        "أهلًا بك في <b>النبع للخدمات الجامعية</b> 🎓\n\nاختر الخدمة وأرسل طلبك من التطبيق.",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


async def channel_diagnosis(send_test: bool = False):
    """يفحص وصول البوت للقناة ويرجع (نجح؟، شرح). send_test=True يرسل رسالة اختبار فعلية."""
    channel = parse_channel_id()
    if channel is None:
        return False, "CHANNEL_ID غير مضبوط (فارغ) في متغيرات البيئة، لذلك لا يُرسل شيء للقناة."
    try:
        chat = await telegram_app.bot.get_chat(channel)
        me = await telegram_app.bot.get_me()
        member = await telegram_app.bot.get_chat_member(channel, me.id)
        if member.status not in ("administrator", "creator"):
            return False, f"البوت موجود في «{chat.title}» لكن حالته «{member.status}». لازم يكون Administrator."
        if getattr(member, "can_post_messages", None) is False:
            return False, f"البوت Admin في «{chat.title}» لكن صلاحية Post messages مطفية."
        if send_test:
            await telegram_app.bot.send_message(chat_id=channel, text="✅ رسالة اختبار من بوت النبع")
        return True, f"القناة «{chat.title}» جاهزة، والبوت (@{me.username}) أدمن وقادر على النشر."
    except TelegramError as exc:
        return False, f"{type(exc).__name__}: {exc.message}"


async def testchannel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return
    ok, info = await channel_diagnosis(send_test=True)
    await update.effective_message.reply_text(f"{'✅' if ok else '❌'} {info}\n\nCHANNEL_ID المستخدم: {CHANNEL_ID or '(فارغ)'}")


async def camera_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in PHOTO_ALLOWED_IDS:
        return
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📸 فتح الكاميرا / المعرض", web_app=WebAppInfo(url=CAMERA_URL))]])
    await update.effective_message.reply_text("اضغط الزر لالتقاط صورة أو اختيارها ونشرها في القناة:", reply_markup=keyboard)


telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()
telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(CommandHandler("testchannel", testchannel_command))
telegram_app.add_handler(CommandHandler("camera", camera_command))
telegram_app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, start_command))

# ─────────────────────────── تطبيق FastAPI (Webhook) ───────────────────────────


async def init_db_with_retry(attempts: int = 5, delay: int = 3):
    """أخطاء الشبكة/DNS المؤقتة عند الإقلاع (مثل Neon أثناء الاستيقاظ) تُعاد محاولتها بدل ما يموت التطبيق."""
    for attempt in range(1, attempts + 1):
        try:
            await asyncio.to_thread(init_db)
            return
        except psycopg.OperationalError:
            logger.warning("Database not reachable (attempt %s/%s)", attempt, attempts, exc_info=True)
            if attempt == attempts:
                raise
            await asyncio.sleep(delay * attempt)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db_with_retry()
    await telegram_app.initialize()
    try:
        await telegram_app.bot.set_webhook(
            url=f"{BACKEND_URL}{WEBHOOK_PATH}",
            secret_token=WEBHOOK_SECRET,
            allowed_updates=["message"],
        )
        logger.info("Webhook set to %s%s", BACKEND_URL, WEBHOOK_PATH)
    except TelegramError:
        logger.exception("Failed to set webhook")
    ok, info = await channel_diagnosis(send_test=False)
    (logger.info if ok else logger.error)("Channel check: %s", info)
    yield
    await telegram_app.shutdown()


app = FastAPI(title="النبع للخدمات الجامعية API", version="3.0.0", lifespan=lifespan)

allowed_origins = {origin_of(WEB_APP_URL), BACKEND_URL}
allowed_origins.update(o.strip().rstrip("/") for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip())
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(allowed_origins),
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Telegram-Init-Data"],
)


@app.get("/")
def read_root():
    if INDEX_FILE.exists():
        return FileResponse(INDEX_FILE, media_type="text/html", headers={"Cache-Control": "no-cache"})
    return {"status": "Online", "system": "النبع للخدمات الجامعية API", "database": "Neon PostgreSQL"}


@app.get("/health")
def health():
    try:
        with db() as conn:
            conn.execute("SELECT 1")
        return {"status": "ok", "database": "connected", "telegram": bool(BOT_TOKEN)}
    except Exception:
        logger.exception("Health check failed")
        return JSONResponse(status_code=503, content={"status": "error", "database": "unavailable"})


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    received = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(received, WEBHOOK_SECRET):
        raise HTTPException(403, "Forbidden")
    try:
        update = Update.de_json(await request.json(), telegram_app.bot)
    except Exception:
        raise HTTPException(400, "Bad update")
    try:
        await telegram_app.process_update(update)
    except Exception:
        # نرجع 200 دائمًا حتى لا يعيد Telegram إرسال نفس التحديث بلا نهاية
        logger.exception("Failed to process update")
    return {"ok": True}


DOCUMENT_SIGNATURES = {
    "application/pdf": b"%PDF",
    "application/msword": b"\xd0\xcf\x11\xe0",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": b"PK\x03\x04",
}


def validate_file(data: bytes, declared_type: str) -> str:
    """يتحقق من محتوى الملف الفعلي (وليس فقط ما يدّعيه المتصفح) ويرجع نوعه الحقيقي."""
    if declared_type.startswith("image/"):
        try:
            with Image.open(io.BytesIO(data)) as img:
                img.verify()
                mime = Image.MIME.get(img.format or "")
        except Exception:
            raise HTTPException(400, "الصورة تالفة أو صيغتها غير مدعومة، استخدم JPG أو PNG")
        if mime not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
            raise HTTPException(400, "صيغة الصورة غير مدعومة، استخدم JPG أو PNG")
        return mime
    signature = DOCUMENT_SIGNATURES.get(declared_type)
    if not signature:
        raise HTTPException(400, "نوع الملف غير مدعوم")
    if not data.startswith(signature):
        raise HTTPException(400, "محتوى الملف لا يطابق نوعه")
    return declared_type


@app.post("/api/upload")
async def upload_attachment(request: Request, file: UploadFile = File(...)):
    user = init_user_from_header(request)
    data = await file.read(MAX_ATTACHMENT_BYTES + 1)
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(413, f"حجم الملف يجب ألا يتجاوز {MAX_ATTACHMENT_BYTES // 1024 // 1024} MB")
    if not data:
        raise HTTPException(400, "الملف فارغ")
    content_type = validate_file(data, file.content_type or "")
    filename = os.path.basename(file.filename or "").strip()[:100] or "attachment"
    token = await asyncio.to_thread(store_attachment, user["id"], filename, content_type, data)
    return {"token": token, "filename": filename}


def prepare_photo(data: bytes) -> bytes:
    """يصحح اتجاه الصورة ويصغّرها (حدود Telegram) ويحولها JPEG."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img).convert("RGB")
            img.thumbnail((2560, 2560))
            out = io.BytesIO()
            img.save(out, "JPEG", quality=85, optimize=True)
            return out.getvalue()
    except Exception:
        raise HTTPException(400, "الصورة تالفة أو صيغتها غير مدعومة، استخدم JPG أو PNG")


@app.post("/api/photo")
async def publish_photo(request: Request, file: UploadFile = File(...)):
    user = init_user_from_header(request)
    if user["id"] not in PHOTO_ALLOWED_IDS:
        raise HTTPException(403, "غير مصرح لك بالنشر في القناة")
    channel = parse_channel_id()
    if channel is None:
        raise HTTPException(503, "CHANNEL_ID غير مضبوط في السيرفر")
    data = await file.read(MAX_ATTACHMENT_BYTES + 1)
    if not data:
        raise HTTPException(400, "الملف فارغ")
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(413, f"حجم الصورة يجب ألا يتجاوز {MAX_ATTACHMENT_BYTES // 1024 // 1024} MB")
    photo = await asyncio.to_thread(prepare_photo, data)
    try:
        await telegram_app.bot.send_photo(chat_id=channel, photo=photo)
    except TelegramError as exc:
        logger.exception("Photo publish failed")
        raise HTTPException(502, f"تعذر النشر في القناة: {exc.message}")
    return {"ok": True}


@app.post("/api/orders")
async def submit_order(payload: OrderIn, request: Request):
    user = init_user_from_header(request)
    order, attachment = await asyncio.to_thread(create_order, user, payload)
    delivered = await deliver_order(order, user, attachment)
    if not delivered:
        logger.error("Order %s saved but not delivered to any chat", order["order_id"])
    try:
        await telegram_app.bot.send_message(
            chat_id=user["id"],
            text=f"✅ <b>تم استلام طلبك</b>\n\nرقم الطلب: <code>{html.escape(order['order_id'])}</code>\nسيتم التواصل معك عبر هذه المحادثة.",
            parse_mode="HTML",
        )
    except TelegramError:
        logger.warning("Could not message user %s", user["id"], exc_info=True)
    return {"order_id": order["order_id"]}


@app.get("/api/orders/{order_id}")
def get_order_status(order_id: str, request: Request):
    user = init_user_from_header(request)
    with db() as conn:
        row = conn.execute(
            "SELECT order_id,service_name,title,order_status,payment_status,price,deposit,remaining_balance "
            "FROM orders WHERE order_id=%s AND user_id=%s",
            (order_id, user["id"]),
        ).fetchone()
    if not row:
        raise HTTPException(404, "الطلب غير موجود")
    return row


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
