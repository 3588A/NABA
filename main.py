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
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from PIL import Image
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field, field_validator
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
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

ADMIN_TELEGRAM_ID = env_int(
    "ADMIN_TELEGRAM_ID",
    6931187332,
)

ADMIN_USER_ID = ADMIN_TELEGRAM_ID

MAX_ATTACHMENT_MB = env_int("MAX_ATTACHMENT_MB", 5)
MAX_ATTACHMENT_BYTES = MAX_ATTACHMENT_MB * 1024 * 1024

MAX_PENDING_ATTACHMENTS = env_int(
    "MAX_PENDING_ATTACHMENTS",
    5,
)

INIT_DATA_MAX_AGE = env_int(
    "INIT_DATA_MAX_AGE",
    3600,
)

CAMERA_URL = (
    env("CAMERA_URL")
    or f"{WEB_APP_URL.rstrip('/')}/camera.html"
)

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

if re.fullmatch(
    r"[A-Za-z0-9_-]{16,256}",
    configured_webhook_secret or "",
):
    WEBHOOK_SECRET = configured_webhook_secret
else:
    WEBHOOK_SECRET = hashlib.sha256(
        f"naba-webhook:{BOT_TOKEN}".encode()
    ).hexdigest()

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


# =========================================================
# Workflow
# =========================================================

VALID_STATUSES = [
    "NEW",
    "UNDER_REVIEW",
    "WAITING_CUSTOMER",
    "ACCEPTED",
    "READY_TO_START",
    "IN_PROGRESS",
    "READY_FOR_DELIVERY",
    "COMPLETED",
    "REJECTED",
    "CANCELLED",
]

ALLOWED_STATUS_TRANSITIONS = {
    "NEW": ["UNDER_REVIEW", "REJECTED", "CANCELLED"],
    "UNDER_REVIEW": [
        "WAITING_CUSTOMER",
        "READY_TO_START",
        "REJECTED",
        "CANCELLED",
    ],
    "WAITING_CUSTOMER": [
        "ACCEPTED",
        "REJECTED",
        "CANCELLED",
    ],
    "ACCEPTED": [
        "READY_TO_START",
        "CANCELLED",
    ],
    "READY_TO_START": [
        "IN_PROGRESS",
        "CANCELLED",
    ],
    "IN_PROGRESS": [
        "UNDER_REVIEW",
        "READY_FOR_DELIVERY",
        "CANCELLED",
    ],
    "READY_FOR_DELIVERY": [
        "CANCELLED",
    ],
    "COMPLETED": [],
    "REJECTED": [],
    "CANCELLED": [],
}


# =========================================================
# Database
# =========================================================

@contextmanager
def db():
    conn = psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row,
        connect_timeout=10,
    )

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """
    Migration-safe initialization.
    All ALTER TABLE operations happen before indexes.
    """

    with db() as conn:

        conn.execute(
            """
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
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                order_id TEXT NOT NULL
                    REFERENCES orders(order_id)
                    ON DELETE CASCADE,
                amount NUMERIC(12,2) NOT NULL,
                currency TEXT NOT NULL DEFAULT 'IQD',
                payment_method TEXT NOT NULL DEFAULT 'CASH',
                recorded_by TEXT NOT NULL DEFAULT 'Admin',
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_logs (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                order_id TEXT NOT NULL
                    REFERENCES orders(order_id)
                    ON DELETE CASCADE,
                action TEXT NOT NULL,
                performed_by TEXT NOT NULL DEFAULT 'System',
                details TEXT NOT NULL DEFAULT '',
                timestamp TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS attachments (
                token TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                filename TEXT NOT NULL,
                content_type TEXT NOT NULL,
                data BYTEA NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                order_id TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS channel_message_links (
                channel_id BIGINT NOT NULL,
                channel_message_id BIGINT NOT NULL,
                order_id TEXT NOT NULL
                    REFERENCES orders(order_id)
                    ON DELETE CASCADE,
                user_id BIGINT NOT NULL,
                message_kind TEXT NOT NULL DEFAULT 'CUSTOMER',
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (channel_id, channel_message_id)
            )
            """
        )

        # -----------------------------
        # Orders migrations
        # -----------------------------

        order_columns = [
            ("username", "TEXT NOT NULL DEFAULT ''"),
            ("service_type", "TEXT NOT NULL DEFAULT ''"),
            ("service_name", "TEXT NOT NULL DEFAULT ''"),
            ("department", "TEXT NOT NULL DEFAULT ''"),
            ("title", "TEXT NOT NULL DEFAULT ''"),
            ("page_count", "TEXT NOT NULL DEFAULT ''"),
            ("language", "TEXT NOT NULL DEFAULT ''"),
            ("autocad_type", "TEXT NOT NULL DEFAULT ''"),
            ("deadline", "TEXT NOT NULL DEFAULT ''"),
            ("notes", "TEXT NOT NULL DEFAULT ''"),
            ("order_status", "TEXT NOT NULL DEFAULT 'NEW'"),
            ("payment_status", "TEXT NOT NULL DEFAULT 'UNPAID'"),
            ("price", "NUMERIC(12,2) NOT NULL DEFAULT 0"),
            ("deposit", "NUMERIC(12,2) NOT NULL DEFAULT 0"),
            ("remaining_balance", "NUMERIC(12,2) NOT NULL DEFAULT 0"),
            (
                "created_at",
                "TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP",
            ),
            (
                "service_details",
                "JSONB NOT NULL DEFAULT '{}'::jsonb",
            ),
            ("admin_note", "TEXT NOT NULL DEFAULT ''"),
            ("delivery_date", "TEXT NOT NULL DEFAULT ''"),
            ("delivery_time", "TEXT NOT NULL DEFAULT ''"),
            (
                "quotation_price",
                "NUMERIC(12,2) NOT NULL DEFAULT 0",
            ),
            (
                "quotation_currency",
                "TEXT NOT NULL DEFAULT 'IQD'",
            ),
            (
                "quotation_notes",
                "TEXT NOT NULL DEFAULT ''",
            ),
            (
                "customer_decision",
                "TEXT NOT NULL DEFAULT 'PENDING'",
            ),
            ("customer_decision_at", "TIMESTAMPTZ"),
            ("idempotency_key", "TEXT"),
            ("idempotency_fingerprint", "TEXT"),
            (
                "delivery_channel_status",
                "TEXT NOT NULL DEFAULT 'PENDING'",
            ),
            ("channel_message_id", "BIGINT"),
            ("channel_attachment_message_id", "BIGINT"),
        ]

        for column_name, column_definition in order_columns:
            conn.execute(
                f"""
                ALTER TABLE orders
                ADD COLUMN IF NOT EXISTS
                {column_name}
                {column_definition}
                """
            )

        # -----------------------------
        # Payments migrations
        # -----------------------------

        payment_columns = [
            ("currency", "TEXT NOT NULL DEFAULT 'IQD'"),
            ("payment_method", "TEXT NOT NULL DEFAULT 'CASH'"),
            ("recorded_by", "TEXT NOT NULL DEFAULT 'Admin'"),
            (
                "created_at",
                "TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP",
            ),
        ]

        for column_name, column_definition in payment_columns:
            conn.execute(
                f"""
                ALTER TABLE payments
                ADD COLUMN IF NOT EXISTS
                {column_name}
                {column_definition}
                """
            )

        # -----------------------------
        # Audit migrations
        # -----------------------------

        audit_columns = [
            ("performed_by", "TEXT NOT NULL DEFAULT 'System'"),
            ("details", "TEXT NOT NULL DEFAULT ''"),
            (
                "timestamp",
                "TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP",
            ),
        ]

        for column_name, column_definition in audit_columns:
            conn.execute(
                f"""
                ALTER TABLE audit_logs
                ADD COLUMN IF NOT EXISTS
                {column_name}
                {column_definition}
                """
            )

        # -----------------------------
        # Attachment migrations
        # -----------------------------

        conn.execute(
            """
            ALTER TABLE attachments
            ADD COLUMN IF NOT EXISTS
            created_at
            TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            """
        )

        conn.execute(
            """
            ALTER TABLE attachments
            ADD COLUMN IF NOT EXISTS
            order_id TEXT
            """
        )

        # -----------------------------
        # Normalize legacy NULLs
        # -----------------------------

        conn.execute(
            """
            UPDATE orders
            SET
                username = COALESCE(username, ''),
                service_type = COALESCE(service_type, ''),
                service_name = COALESCE(service_name, ''),
                department = COALESCE(department, ''),
                title = COALESCE(title, ''),
                page_count = COALESCE(page_count, ''),
                language = COALESCE(language, ''),
                autocad_type = COALESCE(autocad_type, ''),
                deadline = COALESCE(deadline, ''),
                notes = COALESCE(notes, ''),
                order_status = COALESCE(order_status, 'NEW'),
                payment_status = COALESCE(payment_status, 'UNPAID'),
                price = COALESCE(price, 0),
                deposit = COALESCE(deposit, 0),
                remaining_balance = COALESCE(remaining_balance, 0),
                created_at = COALESCE(created_at, CURRENT_TIMESTAMP),
                service_details = COALESCE(
                    service_details,
                    '{}'::jsonb
                ),
                admin_note = COALESCE(admin_note, ''),
                delivery_date = COALESCE(delivery_date, ''),
                delivery_time = COALESCE(delivery_time, ''),
                quotation_price = COALESCE(quotation_price, 0),
                quotation_currency = COALESCE(
                    quotation_currency,
                    'IQD'
                ),
                quotation_notes = COALESCE(
                    quotation_notes,
                    ''
                ),
                customer_decision = COALESCE(
                    customer_decision,
                    'PENDING'
                ),
                delivery_channel_status = COALESCE(
                    delivery_channel_status,
                    'PENDING'
                )
            """
        )

        conn.execute(
            """
            UPDATE payments
            SET
                currency = COALESCE(currency, 'IQD'),
                payment_method = COALESCE(
                    payment_method,
                    'CASH'
                ),
                recorded_by = COALESCE(
                    recorded_by,
                    'Admin'
                ),
                created_at = COALESCE(
                    created_at,
                    CURRENT_TIMESTAMP
                )
            """
        )

        conn.execute(
            """
            UPDATE audit_logs
            SET
                performed_by = COALESCE(
                    performed_by,
                    'System'
                ),
                details = COALESCE(details, ''),
                timestamp = COALESCE(
                    timestamp,
                    CURRENT_TIMESTAMP
                )
            """
        )

        conn.execute(
            """
            UPDATE attachments
            SET created_at = COALESCE(
                created_at,
                CURRENT_TIMESTAMP
            )
            """
        )

        # -----------------------------
        # Indexes AFTER migrations
        # -----------------------------

        indexes = [
            """
            CREATE INDEX IF NOT EXISTS orders_user_id_idx
            ON orders(user_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS orders_idempotency_idx
            ON orders(idempotency_key)
            """,
            """
            CREATE INDEX IF NOT EXISTS attachments_user_id_idx
            ON attachments(user_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS orders_created_at_idx
            ON orders(created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS audit_logs_timestamp_idx
            ON audit_logs(timestamp)
            """,
            """
            CREATE INDEX IF NOT EXISTS payments_order_id_idx
            ON payments(order_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS orders_channel_message_idx
            ON orders(channel_message_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS attachments_order_id_idx
            ON attachments(order_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS channel_message_links_order_idx
            ON channel_message_links(order_id)
            """,
        ]

        for statement in indexes:
            conn.execute(statement)

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
            orders_user_id_idempotency_unique
            ON orders(user_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL
            """
        )


async def init_db_with_retry(
    attempts: int = 5,
    delay: int = 3,
):
    for attempt in range(1, attempts + 1):
        try:
            await asyncio.to_thread(init_db)
            logger.info("Database initialized successfully")
            return
        except psycopg.OperationalError:
            logger.warning(
                "Database unavailable - attempt %s/%s",
                attempt,
                attempts,
                exc_info=True,
            )

            if attempt == attempts:
                raise

            await asyncio.sleep(delay * attempt)


def add_audit_log_conn(
    conn,
    order_id: str,
    action: str,
    performed_by: str = "System",
    details: str = "",
):
    conn.execute(
        """
        INSERT INTO audit_logs(
            order_id,
            action,
            performed_by,
            details
        )
        VALUES(%s, %s, %s, %s)
        """,
        (
            order_id,
            action,
            performed_by,
            details,
        ),
    )


def add_audit_log(
    order_id: str,
    action: str,
    performed_by: str = "System",
    details: str = "",
):
    with db() as conn:
        add_audit_log_conn(
            conn,
            order_id,
            action,
            performed_by,
            details,
        )


# =========================================================
# Telegram Mini App Authentication
# =========================================================

def verify_init_data(init_data: str) -> dict:
    if not init_data:
        raise HTTPException(
            status_code=401,
            detail="افتح تطبيق النبع من داخل Telegram",
        )

    try:
        pairs_list = parse_qsl(
            init_data,
            keep_blank_values=True,
        )
        pairs = dict(pairs_list)
    except Exception:
        raise HTTPException(
            status_code=401,
            detail="بيانات Telegram غير صالحة",
        )

    received_hash = pairs.pop("hash", None)

    if not received_hash:
        raise HTTPException(
            status_code=401,
            detail="تعذر التحقق من جلسة Telegram",
        )

    data_check_string = "\n".join(
        f"{key}={pairs[key]}"
        for key in sorted(pairs)
    )

    secret_key = hmac.new(
        b"WebAppData",
        BOT_TOKEN.encode(),
        hashlib.sha256,
    ).digest()

    expected_hash = hmac.new(
        secret_key,
        data_check_string.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(
        expected_hash,
        received_hash,
    ):
        raise HTTPException(
            status_code=401,
            detail="جلسة Telegram غير موثوقة",
        )

    try:
        user = json.loads(
            pairs.get("user", "{}")
        )

        user_id = int(user["id"])

        auth_date = int(
            pairs.get("auth_date", "0")
        )

    except (
        ValueError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
    ):
        raise HTTPException(
            status_code=401,
            detail="بيانات مستخدم Telegram غير صالحة",
        )

    if not auth_date:
        raise HTTPException(
            status_code=401,
            detail="auth_date مفقود",
        )

    now = int(time.time())

    if auth_date > now + 60:
        raise HTTPException(
            status_code=401,
            detail="وقت جلسة Telegram غير صالح",
        )

    if now - auth_date > INIT_DATA_MAX_AGE:
        raise HTTPException(
            status_code=401,
            detail="انتهت صلاحية جلسة Telegram، أعد فتح التطبيق",
        )

    return {
        "id": user_id,
        "username": user.get("username", "") or "",
        "first_name": user.get("first_name", "") or "",
        "last_name": user.get("last_name", "") or "",
    }


def init_user_from_header(request: Request) -> dict:
    return verify_init_data(
        request.headers.get(
            "X-Telegram-Init-Data",
            "",
        )
    )


def require_admin(request: Request) -> dict:
    user = init_user_from_header(request)

    if user["id"] != ADMIN_TELEGRAM_ID:
        raise HTTPException(
            status_code=403,
            detail="غير مصرح لك بالوصول إلى لوحة التحكم",
        )

    return user


# =========================================================
# Models
# =========================================================

class OrderIn(BaseModel):
    model_config = ConfigDict(
        str_strip_whitespace=True,
        extra="ignore",
    )

    service_type: Literal[
        "research",
        "autocad",
        "minitab",
        "formatting",
        "cv",
        "cv_ats",
        "logo",
        "identity",
    ]

    department: str = Field(
        min_length=1,
        max_length=200,
    )

    title: str = Field(
        min_length=1,
        max_length=300,
    )

    page_count: str = Field(
        default="",
        max_length=40,
    )

    language: str = Field(
        default="",
        max_length=80,
    )

    autocad_type: str = Field(
        default="",
        max_length=100,
    )

    deadline: str = Field(
        min_length=1,
        max_length=40,
    )

    notes: str = Field(
        default="",
        max_length=2000,
    )

    attachment_token: str = Field(
        default="",
        max_length=100,
    )

    service_details: dict[str, str] = Field(
        default_factory=dict
    )

    @field_validator("service_details")
    @classmethod
    def validate_service_details(cls, value):
        if len(value) > 20:
            raise ValueError("تفاصيل الخدمة كثيرة جدًا")

        for key, item in value.items():
            if len(str(key)) > 80:
                raise ValueError("اسم حقل الخدمة طويل جدًا")

            if len(str(item)) > 1000:
                raise ValueError("قيمة تفاصيل الخدمة طويلة جدًا")

        return value

    @field_validator("deadline")
    @classmethod
    def validate_deadline(cls, value):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError("صيغة الموعد غير صحيحة")

        if parsed.tzinfo is not None:
            now = datetime.now(parsed.tzinfo)
        else:
            now = datetime.now()

        if parsed < now:
            raise ValueError(
                "موعد التسليم يجب أن يكون في المستقبل"
            )

        return value


class QuotationIn(BaseModel):
    model_config = ConfigDict(
        str_strip_whitespace=True,
        extra="ignore",
    )

    price: Decimal = Field(
        gt=0,
        max_digits=12,
        decimal_places=2,
    )

    currency: str = Field(
        default="IQD",
        max_length=10,
    )

    delivery_date: str = Field(
        min_length=1,
        max_length=40,
    )

    delivery_time: str = Field(
        default="",
        max_length=40,
    )

    notes: str = Field(
        default="",
        max_length=1000,
    )

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value):
        value = value.upper().strip()

        if not re.fullmatch(
            r"[A-Z]{3,10}",
            value,
        ):
            raise ValueError("العملة غير صالحة")

        return value


class StatusUpdateIn(BaseModel):
    model_config = ConfigDict(
        str_strip_whitespace=True,
        extra="ignore",
    )

    status: str = Field(
        min_length=1,
        max_length=50,
    )

    admin_note: str = Field(
        default="",
        max_length=1000,
    )


class PaymentIn(BaseModel):
    model_config = ConfigDict(
        str_strip_whitespace=True,
        extra="ignore",
    )

    amount: Decimal = Field(
        gt=0,
        max_digits=12,
        decimal_places=2,
    )

    currency: str = Field(
        default="IQD",
        max_length=10,
    )

    payment_method: str = Field(
        default="CASH",
        max_length=50,
    )

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value):
        value = value.upper().strip()

        if not re.fullmatch(
            r"[A-Z]{3,10}",
            value,
        ):
            raise ValueError("العملة غير صالحة")

        return value


class AdminNoteIn(BaseModel):
    model_config = ConfigDict(
        str_strip_whitespace=True,
        extra="ignore",
    )

    admin_note: str = Field(
        default="",
        max_length=2000,
    )


# =========================================================
# Order Helpers
# =========================================================

def new_order_id() -> str:
    year = datetime.now().year

    random_part = "".join(
        secrets.choice(string.digits)
        for _ in range(6)
    )

    return f"NB-{year}-{random_part}"


def compute_order_fingerprint(
    user_id: int,
    data: OrderIn,
) -> str:
    raw = "|".join(
        [
            str(user_id),
            data.service_type,
            data.title.strip().lower(),
            data.department.strip().lower(),
            data.deadline.strip(),
            json.dumps(
                data.service_details,
                ensure_ascii=False,
                sort_keys=True,
            ),
        ]
    )

    return hashlib.sha256(
        raw.encode()
    ).hexdigest()


def create_order(
    user: dict,
    data: OrderIn,
    idempotency_key: Optional[str] = None,
):
    fingerprint = compute_order_fingerprint(
        user["id"],
        data,
    )

    if idempotency_key:
        idempotency_key = idempotency_key.strip()

        if len(idempotency_key) > 200:
            raise HTTPException(
                status_code=400,
                detail="Idempotency-Key طويل جدًا",
            )

    with db() as conn:

        if idempotency_key:
            existing = conn.execute(
                """
                SELECT *
                FROM orders
                WHERE user_id=%s
                  AND idempotency_key=%s
                FOR UPDATE
                """,
                (
                    user["id"],
                    idempotency_key,
                ),
            ).fetchone()

            if existing:
                return existing, None, True

        existing_fp = conn.execute(
            """
            SELECT *
            FROM orders
            WHERE user_id=%s
              AND idempotency_fingerprint=%s
              AND created_at >
                  CURRENT_TIMESTAMP - INTERVAL '2 minutes'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (
                user["id"],
                fingerprint,
            ),
        ).fetchone()

        if existing_fp:
            return existing_fp, None, True

    for _ in range(10):

        order_id = new_order_id()

        try:

            with db() as conn:

                attachment = None

                if data.attachment_token:
                    attachment = conn.execute(
                        """
                        SELECT
                            token,
                            user_id,
                            filename,
                            content_type,
                            data,
                            created_at,
                            order_id
                        FROM attachments
                        WHERE token=%s
                          AND user_id=%s
                        FOR UPDATE
                        """,
                        (
                            data.attachment_token,
                            user["id"],
                        ),
                    ).fetchone()

                    if not attachment:
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                "المرفق غير موجود أو انتهت صلاحيته، "
                                "أعد رفعه"
                            ),
                        )

                order = conn.execute(
                    """
                    INSERT INTO orders (
                        order_id,
                        user_id,
                        username,
                        service_type,
                        service_name,
                        department,
                        title,
                        page_count,
                        language,
                        autocad_type,
                        deadline,
                        notes,
                        service_details,
                        idempotency_key,
                        idempotency_fingerprint
                    )
                    VALUES (
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s::jsonb,%s,%s
                    )
                    RETURNING *
                    """,
                    (
                        order_id,
                        user["id"],
                        user["username"]
                        or user["first_name"]
                        or "",
                        data.service_type,
                        SERVICES[data.service_type],
                        data.department,
                        data.title,
                        str(data.page_count or ""),
                        str(data.language or ""),
                        str(data.autocad_type or ""),
                        data.deadline,
                        data.notes or "",
                        json.dumps(
                            data.service_details,
                            ensure_ascii=False,
                        ),
                        idempotency_key,
                        fingerprint,
                    ),
                ).fetchone()

                if attachment:
                    conn.execute(
                        """
                        UPDATE attachments
                        SET order_id=%s
                        WHERE token=%s
                        """,
                        (
                            order_id,
                            attachment["token"],
                        ),
                    )

                add_audit_log_conn(
                    conn,
                    order_id,
                    "ORDER_CREATED",
                    f"User_{user['id']}",
                    "تم إنشاء الطلب بنجاح",
                )

            return order, attachment, False

        except psycopg.errors.UniqueViolation:

            if idempotency_key:
                with db() as conn:
                    existing = conn.execute(
                        """
                        SELECT *
                        FROM orders
                        WHERE user_id=%s
                          AND idempotency_key=%s
                        LIMIT 1
                        """,
                        (
                            user["id"],
                            idempotency_key,
                        ),
                    ).fetchone()

                    if existing:
                        return existing, None, True

            continue

    raise HTTPException(
        status_code=500,
        detail="تعذر إنشاء رقم الطلب، حاول مرة أخرى",
    )


def store_attachment(
    user_id: int,
    filename: str,
    content_type: str,
    data: bytes,
    order_id: Optional[str] = None,
) -> str:
    token = secrets.token_urlsafe(24)

    with db() as conn:

        conn.execute(
            """
            DELETE FROM attachments
            WHERE created_at <
                CURRENT_TIMESTAMP - INTERVAL '1 day'
              AND order_id IS NULL
            """
        )

        pending = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM attachments
            WHERE user_id=%s
              AND order_id IS NULL
            """,
            (user_id,),
        ).fetchone()["n"]

        if pending >= MAX_PENDING_ATTACHMENTS and not order_id:
            raise HTTPException(
                status_code=429,
                detail=(
                    "لديك مرفقات غير مستخدمة كثيرة. "
                    "أرسل الطلب الحالي أولًا."
                ),
            )

        conn.execute(
            """
            INSERT INTO attachments(
                token,
                user_id,
                filename,
                content_type,
                data,
                order_id
            )
            VALUES(%s,%s,%s,%s,%s,%s)
            """,
            (
                token,
                user_id,
                filename,
                content_type,
                data,
                order_id,
            ),
        )

    return token


def delete_attachment(
    user_id: int,
    token: str,
):
    if not token:
        return

    with db() as conn:
        conn.execute(
            """
            DELETE FROM attachments
            WHERE token=%s
              AND user_id=%s
            """,
            (
                token,
                user_id,
            ),
        )


# =========================================================
# Telegram Helpers
# =========================================================

def parse_channel_id():
    if not CHANNEL_ID:
        return None

    if CHANNEL_ID.lstrip("-").isdigit():
        return int(CHANNEL_ID)

    return CHANNEL_ID


def build_order_message(
    order: dict,
    user: dict,
) -> str:
    e = html.escape

    username = (
        user.get("username")
        or "بدون_يوزر"
    )

    text = (
        "📥 <b>طلب جديد من النبع</b>\n\n"
        f"🆔 <b>رقم الطلب:</b> "
        f"<code>{e(order['order_id'])}</code>\n"
        f"👤 <b>الطالب:</b> "
        f"{e(user.get('first_name') or '')} "
        f"(@{e(username)})\n"
        f"🆔 <b>Telegram ID:</b> "
        f"<code>{order['user_id']}</code>\n"
        f"🛠️ <b>الخدمة:</b> "
        f"{e(order['service_name'])}\n"
        f"🏫 <b>التخصص:</b> "
        f"{e(order['department'])}\n"
        f"📌 <b>العنوان:</b> "
        f"{e(order['title'])}\n"
        f"📄 <b>الصفحات:</b> "
        f"{e(order['page_count'] or 'غير محدد')}\n"
        f"🌐 <b>اللغة:</b> "
        f"{e(order['language'] or 'غير محدد')}\n"
        f"⏰ <b>الموعد:</b> "
        f"{e(order['deadline'])}\n"
    )

    if order["service_type"] == "autocad":
        text += (
            f"📐 <b>نوع الرسم:</b> "
            f"{e(order['autocad_type'] or 'غير محدد')}\n"
        )

    service_details = order.get("service_details") or {}

    if isinstance(service_details, str):
        try:
            service_details = json.loads(service_details)
        except Exception:
            service_details = {}

    if service_details:
        text += "\n🎯 <b>تفاصيل الخدمة:</b>\n"

        for key, value in service_details.items():
            label = SERVICE_DETAIL_LABELS.get(
                key,
                str(key).replace("_", " ").strip(),
            )

            text += (
                f"• <b>{e(label)}:</b> "
                f"{e(str(value))}\n"
            )

    notes = order["notes"] or "لا توجد ملاحظات"

    remaining_chars = max(
        100,
        3900 - len(text),
    )

    text += (
        "📝 <b>الملاحظات:</b> "
        + e(notes[:remaining_chars])
    )

    text += (
        "\n\n"
        "💬 <b>للرد على صاحب الطلب من الخاص:</b>\n"
        f"<code>/reply {e(order['order_id'])} نص الرد</code>"
    )

    return text


async def send_channel_text(
    text: str,
):
    channel = parse_channel_id()

    if not channel:
        logger.error("CHANNEL_ID is not configured")
        return None

    try:
        return await telegram_app.bot.send_message(
            chat_id=channel,
            text=text,
            parse_mode="HTML",
        )
    except TelegramError:
        logger.exception(
            "Could not send channel text"
        )
        return None


def save_channel_message_link(
    channel_message_id: int,
    order_id: str,
    user_id: int,
    message_kind: str = "CUSTOMER",
):
    channel = parse_channel_id()
    if channel is None:
        return

    with db() as conn:
        conn.execute(
            """
            INSERT INTO channel_message_links(
                channel_id,
                channel_message_id,
                order_id,
                user_id,
                message_kind
            )
            VALUES(%s, %s, %s, %s, %s)
            ON CONFLICT (channel_id, channel_message_id)
            DO UPDATE SET
                order_id=EXCLUDED.order_id,
                user_id=EXCLUDED.user_id,
                message_kind=EXCLUDED.message_kind
            """,
            (
                channel,
                channel_message_id,
                order_id,
                user_id,
                message_kind,
            ),
        )


async def send_to_chat(
    chat_id,
    text: str,
    order_id: str,
    attachment,
):
    message = await telegram_app.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
    )

    attachment_message = None

    if not attachment:
        return message, None

    data = bytes(attachment["data"])

    filename = (
        attachment["filename"]
        or "attachment"
    )

    caption = f"📎 مرفق الطلب {order_id}"

    content_type = (
        attachment["content_type"]
        or ""
    )

    if content_type.startswith("image/"):
        try:
            attachment_message = (
                await telegram_app.bot.send_photo(
                    chat_id=chat_id,
                    photo=data,
                    caption=caption,
                )
            )

            return message, attachment_message

        except TelegramError:
            logger.warning(
                "send_photo failed; trying document",
                exc_info=True,
            )

    attachment_message = (
        await telegram_app.bot.send_document(
            chat_id=chat_id,
            document=data,
            filename=filename,
            caption=caption,
        )
    )

    return message, attachment_message


async def deliver_order(
    order: dict,
    user: dict,
    attachment,
) -> bool:

    order_id = order["order_id"]
    channel = parse_channel_id()

    if not channel:
        logger.error(
            "CHANNEL_ID is not configured"
        )
        return False

    try:
        with db() as conn:
            current = conn.execute(
                """
                SELECT
                    delivery_channel_status,
                    channel_message_id,
                    channel_attachment_message_id
                FROM orders
                WHERE order_id=%s
                """,
                (order_id,),
            ).fetchone()

        if (
            current
            and current["delivery_channel_status"] == "DELIVERED"
            and current["channel_message_id"]
        ):
            return True

    except Exception:
        logger.exception(
            "Could not check delivery status"
        )

    try:
        with db() as conn:
            conn.execute(
                """
                UPDATE orders
                SET delivery_channel_status='SENDING'
                WHERE order_id=%s
                """,
                (order_id,),
            )
    except Exception:
        logger.exception(
            "Could not mark order as SENDING"
        )

    try:

        message, attachment_message = (
            await send_to_chat(
                channel,
                build_order_message(
                    order,
                    user,
                ),
                order_id,
                attachment,
            )
        )

        with db() as conn:
            conn.execute(
                """
                UPDATE orders
                SET
                    delivery_channel_status='DELIVERED',
                    channel_message_id=%s,
                    channel_attachment_message_id=%s
                WHERE order_id=%s
                """,
                (
                    message.message_id,
                    (
                        attachment_message.message_id
                        if attachment_message
                        else None
                    ),
                    order_id,
                ),
            )

            add_audit_log_conn(
                conn,
                order_id,
                "ORDER_DELIVERED_TO_CHANNEL",
                "System",
                (
                    f"تم الإرسال إلى القناة {channel} "
                    f"| message_id={message.message_id}"
                ),
            )

        return True

    except Exception:
        logger.exception(
            "Failed to deliver order %s",
            order_id,
        )

        try:
            with db() as conn:
                conn.execute(
                    """
                    UPDATE orders
                    SET delivery_channel_status='FAILED'
                    WHERE order_id=%s
                    """,
                    (order_id,),
                )

                add_audit_log_conn(
                    conn,
                    order_id,
                    "ORDER_CHANNEL_DELIVERY_FAILED",
                    "System",
                    "فشل إرسال الطلب إلى القناة",
                )
        except Exception:
            logger.exception(
                "Could not update failed delivery"
            )

        return False


# =========================================================
# Customer Notification
# =========================================================

async def notify_customer(
    user_id: int,
    text: str,
    reply_markup=None,
):
    try:
        await telegram_app.bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
        return True
    except TelegramError:
        logger.warning(
            "Could not notify user %s",
            user_id,
            exc_info=True,
        )
        return False


# =========================================================
# Customer Private Messages
# =========================================================

ACCEPT_WORDS = {
    "موافق",
    "موافقة",
    "اوافق",
    "أوافق",
    "اقبل",
    "أقبل",
    "نعم",
    "yes",
    "accept",
}

REJECT_WORDS = {
    "ارفض",
    "أرفض",
    "رفض",
    "ارفض العرض",
    "لا",
    "no",
    "reject",
}


def normalize_private_text(text: str) -> str:
    text = (text or "").strip().lower()

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    text = text.replace(
        "إ",
        "ا",
    ).replace(
        "أ",
        "ا",
    ).replace(
        "آ",
        "ا",
    )

    return text


def get_latest_waiting_order(
    user_id: int,
):
    with db() as conn:
        return conn.execute(
            """
            SELECT *
            FROM orders
            WHERE user_id=%s
              AND order_status='WAITING_CUSTOMER'
              AND customer_decision='PENDING'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()


def get_latest_active_order(
    user_id: int,
):
    with db() as conn:
        return conn.execute(
            """
            SELECT *
            FROM orders
            WHERE user_id=%s
              AND order_status NOT IN (
                  'COMPLETED',
                  'CANCELLED',
                  'REJECTED'
              )
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()


async def handle_customer_quotation_decision(
    user_id: int,
    accepted: bool,
    order_id: Optional[str] = None,
    notify: bool = True,
):
    with db() as conn:

        order = conn.execute(
            """
            SELECT *
            FROM orders
            WHERE user_id=%s
              AND (%s IS NULL OR order_id=%s)
              AND order_status='WAITING_CUSTOMER'
              AND customer_decision='PENDING'
            ORDER BY created_at DESC
            LIMIT 1
            FOR UPDATE
            """,
            (user_id, order_id, order_id),
        ).fetchone()

        if not order:
            return False

        order_id = order["order_id"]

        decision = (
            "ACCEPTED"
            if accepted
            else "REJECTED"
        )

        new_status = "ACCEPTED" if accepted else "CANCELLED"

        conn.execute(
            """
            UPDATE orders
            SET
                customer_decision=%s,
                customer_decision_at=CURRENT_TIMESTAMP,
                order_status=%s
            WHERE order_id=%s
            """,
            (
                decision,
                new_status,
                order_id,
            ),
        )

        action = (
            "QUOTATION_ACCEPTED"
            if accepted
            else "QUOTATION_REJECTED"
        )

        details = (
            "تم قبول عرض السعر من محادثة البوت"
            if accepted
            else "تم رفض عرض السعر من محادثة البوت"
        )

        add_audit_log_conn(
            conn,
            order_id,
            action,
            f"User_{user_id}",
            details,
        )

    channel_text = (
        "✅ <b>قرار الزبون: قبول عرض السعر</b>\n\n"
        f"🆔 الطلب: <code>{html.escape(order_id)}</code>\n"
        f"👤 Telegram ID: <code>{user_id}</code>"
        if accepted
        else
        "❌ <b>قرار الزبون: رفض عرض السعر</b>\n\n"
        f"🆔 الطلب: <code>{html.escape(order_id)}</code>\n"
        f"👤 Telegram ID: <code>{user_id}</code>"
    )

    channel_result = await send_channel_text(channel_text)

    confirmation_sent = True
    if notify:
        confirmation_sent = await notify_customer(
            user_id,
            (
                "✅ <b>تم تحديث حالة طلبك</b>\n\n"
                f"🆔 رقم الطلب: <code>{html.escape(order_id)}</code>\n"
                "📌 الحالة: <b>تمت الموافقة</b>\n"
                "سيبدأ الكادر بالعمل على طلبك."
                if accepted
                else
                "❌ <b>تم تحديث حالة طلبك</b>\n\n"
                f"🆔 رقم الطلب: <code>{html.escape(order_id)}</code>\n"
                "📌 الحالة: <b>تم الرفض وإلغاء الطلب</b>\n"
                "يمكنك إنشاء طلب جديد عند الحاجة."
            ),
        )

    if not channel_result:
        logger.warning(
            "Customer decision saved but channel notification failed: order=%s",
            order_id,
        )

    if not confirmation_sent:
        logger.error(
            "Customer decision saved but private confirmation failed: user=%s order=%s",
            user_id,
            order_id,
        )

    return True


async def quotation_callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    if not query or not query.from_user:
        return

    parts = (query.data or "").split(":", 2)
    if len(parts) != 3 or parts[0] != "quotation":
        return
    if parts[1] not in {"accept", "reject"}:
        return

    # أجب فورًا حتى لا يبقى زر Telegram في حالة تحميل أثناء انتظار قاعدة البيانات.
    try:
        await query.answer("جارٍ تحديث حالة الطلب...")
    except TelegramError:
        logger.info("Could not acknowledge quotation callback", exc_info=True)

    handled = await handle_customer_quotation_decision(
        query.from_user.id,
        parts[1] == "accept",
        parts[2],
        notify=False,
    )
    if not handled:
        try:
            await query.message.reply_text(
                "⚠️ هذا العرض غير متاح أو تم اتخاذ القرار بشأنه مسبقًا."
            )
        except TelegramError:
            logger.info("Could not report stale quotation", exc_info=True)
        return

    if handled:
        try:
            status_message = (
                "✅ <b>تم تحديث حالة طلبك</b>\n\n"
                f"🆔 رقم الطلب: <code>{html.escape(parts[2])}</code>\n"
                "📌 الحالة: <b>تمت الموافقة</b>\n"
                "سيبدأ الكادر بالعمل على طلبك."
                if parts[1] == "accept"
                else
                "❌ <b>تم تحديث حالة طلبك</b>\n\n"
                f"🆔 رقم الطلب: <code>{html.escape(parts[2])}</code>\n"
                "📌 الحالة: <b>تم الرفض وإلغاء الطلب</b>\n"
                "يمكنك إنشاء طلب جديد عند الحاجة."
            )
            await query.message.reply_text(
                status_message,
                parse_mode="HTML",
            )
            await query.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            logger.exception("Could not send quotation status confirmation")


async def customer_private_message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user:
        return

    if not update.effective_message:
        return

    user = update.effective_user

    if user.id == ADMIN_TELEGRAM_ID:
        return

    message = update.effective_message
    raw_text = (
        message.text
        or message.caption
        or ""
    ).strip()

    has_media = any(
        getattr(message, attribute, None)
        for attribute in (
            "photo",
            "document",
            "video",
            "audio",
            "voice",
            "animation",
            "sticker",
        )
    )

    if not raw_text and not has_media:
        return

    normalized = normalize_private_text(raw_text) if raw_text else ""

    # ---------------------------------------------
    # 1. Quotation decision
    # ---------------------------------------------

    if normalized in ACCEPT_WORDS:
        handled = await handle_customer_quotation_decision(
            user.id,
            True,
        )

        if handled:
            return

    if normalized in REJECT_WORDS:
        handled = await handle_customer_quotation_decision(
            user.id,
            False,
        )

        if handled:
            return

    # ---------------------------------------------
    # 2. Ordinary customer message
    # ---------------------------------------------

    order = await asyncio.to_thread(
        get_latest_active_order,
        user.id,
    )

    if not order:
        general_message = (
            "💬 <b>رسالة عامة من الزبون</b>\n\n"
            f"👤 <b>Telegram ID:</b> <code>{user.id}</code>\n"
            f"👤 <b>Username:</b> @{html.escape(user.username or 'بدون_يوزر')}\n\n"
            f"📝 <b>الرسالة:</b>\n{html.escape(raw_text or 'مرفق بدون نص')}"
        )
        sent_general = await send_channel_text(general_message)
        if sent_general:
            await notify_customer(
                user.id,
                "✅ تم إرسال رسالتك إلى الكادر.",
            )
        else:
            await notify_customer(
                user.id,
                "❌ تعذر تحويل رسالتك إلى الكادر حاليًا. حاول مرة أخرى.",
            )
        return

    order_id = order["order_id"]

    channel_message = (
        "💬 <b>رسالة جديدة من الزبون</b>\n\n"
        f"🆔 <b>رقم الطلب:</b> "
        f"<code>{html.escape(order_id)}</code>\n"
        f"👤 <b>Telegram ID:</b> "
        f"<code>{user.id}</code>\n"
        f"👤 <b>Username:</b> "
        f"@{html.escape(user.username or 'بدون_يوزر')}\n\n"
        f"📝 <b>الرسالة:</b>\n"
        f"{html.escape(raw_text or 'مرفق بدون نص')}\n\n"
        "💡 <b>للرد على الزبون:</b>\n"
        f"<code>/reply {html.escape(order_id)} نص الرد</code>"
    )

    channel_messages = []
    sent = await send_channel_text(channel_message)
    if sent:
        channel_messages.append(sent.message_id)

    forwarded = None
    if has_media:
        channel = parse_channel_id()
        if channel:
            try:
                forwarded = await telegram_app.bot.forward_message(
                    chat_id=channel,
                    from_chat_id=user.id,
                    message_id=message.message_id,
                )
                channel_messages.append(forwarded.message_id)
            except TelegramError:
                logger.exception(
                    "Could not forward customer media to channel: order=%s",
                    order_id,
                )

    delivery_ok = bool(sent) and (not has_media or forwarded is not None)
    if delivery_ok:
        for channel_message_id in channel_messages:
            await asyncio.to_thread(
                save_channel_message_link,
                channel_message_id,
                order_id,
                user.id,
                "CUSTOMER_MESSAGE",
            )
        await notify_customer(
            user.id,
            "✅ تم إرسال رسالتك إلى الكادر.",
        )

        try:
            with db() as conn:
                add_audit_log_conn(
                    conn,
                    order_id,
                    "CUSTOMER_MESSAGE_TO_CHANNEL",
                    f"User_{user.id}",
                    (raw_text or "مرفق")[:2000],
                )
        except Exception:
            logger.exception(
                "Could not write customer message audit"
            )
    else:
        await notify_customer(
            user.id,
            "❌ تعذر تحويل رسالتك إلى الكادر حاليًا. "
            "تأكد من إعداد القناة وحاول مرة أخرى.",
        )


# =========================================================
# Admin Reply
# =========================================================

async def admin_reply_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user:
        return

    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return

    message = update.effective_message

    if not message:
        return

    parts = (
        message.text or ""
    ).split(
        maxsplit=2
    )

    if len(parts) < 3:
        await message.reply_text(
            "الاستخدام الصحيح:\n\n"
            "/reply NB-2026-123456 نص الرد"
        )
        return

    order_id = parts[1].strip()
    reply_text = parts[2].strip()

    if not reply_text:
        await message.reply_text(
            "اكتب نص الرد بعد رقم الطلب."
        )
        return

    with db() as conn:

        order = conn.execute(
            """
            SELECT
                order_id,
                user_id,
                title
            FROM orders
            WHERE order_id=%s
            """,
            (order_id,),
        ).fetchone()

        if not order:
            await message.reply_text(
                f"❌ الطلب {order_id} غير موجود."
            )
            return

        add_audit_log_conn(
            conn,
            order_id,
            "ADMIN_MESSAGE_TO_CUSTOMER",
            "Admin",
            reply_text[:2000],
        )

    success = await notify_customer(
        order["user_id"],
        (
            "📩 <b>رسالة من إدارة النبع</b>\n\n"
            f"🆔 الطلب: "
            f"<code>{html.escape(order_id)}</code>\n\n"
            f"{html.escape(reply_text)}"
        ),
    )

    if success:
        await message.reply_text(
            f"✅ تم إرسال الرد إلى صاحب الطلب {order_id}."
        )
    else:
        await message.reply_text(
            "❌ تعذر إرسال الرسالة إلى الزبون."
        )


# =========================================================
# Channel Post
# =========================================================

async def channel_post_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """
    استقبال رد الأدمن من داخل القناة نفسها.

    المسار:
    الزبون -> البوت -> القناة
    الأدمن يعمل Reply داخل القناة
    -> channel_post
    -> البحث عن الطلب المرتبط برسالة القناة
    -> إرسال الرد/الوسائط إلى الزبون في الخاص.

    لا نعتمد على effective_user هنا لأن channel_post يمثل
    منشور القناة وليس هوية الأدمن البشري.
    """

    message = update.channel_post or update.message

    if not message:
        return

    # رسائل مجموعة النقاش تصل كـ message، ولا نعتمد فيها إلا على حساب المدير.
    if update.message:
        if (
            not update.effective_user
            or update.effective_user.id != ADMIN_TELEGRAM_ID
        ):
            return

    channel = parse_channel_id()

    if channel is None or not message.chat:
        return

    # المنشور المباشر يجب أن يكون من القناة المحددة.
    if update.channel_post and int(message.chat.id) != int(channel):
        return

    # يجب أن يكون المنشور Reply على رسالة سابقة.
    if not message.reply_to_message:
        return

    replied_to = message.reply_to_message

    logger.info(
        "CHANNEL REPLY DETECTED: channel=%s message=%s replied_to=%s",
        message.chat.id,
        message.message_id,
        replied_to.message_id,
    )

    # الرسالة الأصلية في القناة أو رسالة المرفق المرتبطة بالطلب.
    candidate_message_ids = {
        int(replied_to.message_id)
    }

    # دعم الرسائل المعاد توجيهها/الرسائل المرتبطة بالقناة.
    forward_origin = getattr(
        replied_to,
        "forward_origin",
        None,
    )

    if forward_origin is not None:
        origin_chat = getattr(
            forward_origin,
            "chat",
            None,
        )
        origin_message_id = getattr(
            forward_origin,
            "message_id",
            None,
        )

        if (
            origin_chat is not None
            and int(origin_chat.id) == int(channel)
            and origin_message_id
        ):
            candidate_message_ids.add(
                int(origin_message_id)
            )

    forward_from_chat = getattr(
        replied_to,
        "forward_from_chat",
        None,
    )
    forward_from_message_id = getattr(
        replied_to,
        "forward_from_message_id",
        None,
    )

    if (
        forward_from_chat is not None
        and int(forward_from_chat.id) == int(channel)
        and forward_from_message_id
    ):
        candidate_message_ids.add(
            int(forward_from_message_id)
        )

    try:
        with db() as conn:
            order = conn.execute(
                """
                SELECT
                    order_id,
                    user_id
                FROM orders
                WHERE channel_message_id = ANY(%s)
                   OR channel_attachment_message_id = ANY(%s)
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (
                    list(candidate_message_ids),
                    list(candidate_message_ids),
                ),
            ).fetchone()

            if not order:
                order = conn.execute(
                    """
                    SELECT order_id, user_id
                    FROM channel_message_links
                    WHERE channel_id=%s
                      AND channel_message_id = ANY(%s)
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (channel, list(candidate_message_ids)),
                ).fetchone()

        if not order:
            logger.warning(
                "No customer/order mapping for channel reply. "
                "message=%s replied_to=%s candidates=%s",
                message.message_id,
                replied_to.message_id,
                sorted(candidate_message_ids),
            )
            return

        user_id = int(order["user_id"])
        order_id = str(order["order_id"])

        prefix = (
            "👨‍💼 <b>رسالة من إدارة النبع</b>\n\n"
            f"🆔 <b>رقم الطلب:</b> "
            f"<code>{html.escape(order_id)}</code>\n\n"
        )

        # النص
        if message.text:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    prefix
                    + html.escape(message.text)
                ),
                parse_mode="HTML",
            )

        # الصورة
        elif message.photo:
            await context.bot.send_photo(
                chat_id=user_id,
                photo=message.photo[-1].file_id,
                caption=(
                    "👨‍💼 رسالة من إدارة النبع\n\n"
                    + (message.caption or "")
                )[:1024],
            )

        # المستند / الملف
        elif message.document:
            await context.bot.send_document(
                chat_id=user_id,
                document=message.document.file_id,
                caption=(
                    "👨‍💼 رسالة من إدارة النبع\n\n"
                    + (message.caption or "")
                )[:1024],
            )

        # الفيديو
        elif message.video:
            await context.bot.send_video(
                chat_id=user_id,
                video=message.video.file_id,
                caption=(
                    "👨‍💼 رسالة من إدارة النبع\n\n"
                    + (message.caption or "")
                )[:1024],
            )

        # الصوت Voice
        elif message.voice:
            await context.bot.send_voice(
                chat_id=user_id,
                voice=message.voice.file_id,
                caption=(
                    "👨‍💼 رسالة من إدارة النبع"
                ),
            )

        # الملف الصوتي Audio
        elif message.audio:
            await context.bot.send_audio(
                chat_id=user_id,
                audio=message.audio.file_id,
                caption=(
                    "👨‍💼 رسالة من إدارة النبع\n\n"
                    + (message.caption or "")
                )[:1024],
            )

        # الملصق Sticker
        elif message.sticker:
            await context.bot.send_sticker(
                chat_id=user_id,
                sticker=message.sticker.file_id,
            )

        else:
            logger.warning(
                "Unsupported admin channel reply type: %s",
                message.message_id,
            )
            return

        with db() as conn:
            add_audit_log_conn(
                conn,
                order_id,
                "ADMIN_CHANNEL_REPLY_TO_CUSTOMER",
                "Admin",
                (
                    "تم إرسال رد الأدمن من القناة إلى الزبون "
                    f"| channel_message_id={message.message_id}"
                ),
            )

        logger.info(
            "Channel reply delivered to customer: "
            "order=%s user=%s channel_message=%s",
            order_id,
            user_id,
            message.message_id,
        )

    except TelegramError:
        logger.exception(
            "Telegram failed while forwarding channel reply "
            "to customer"
        )

    except Exception:
        logger.exception(
            "Failed to forward channel reply to customer"
        )


# =========================================================
# Bot Commands
# =========================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🚀 فتح تطبيق النبع",
                    web_app=WebAppInfo(
                        url=WEB_APP_URL
                    ),
                )
            ]
        ]
    )

    if not update.effective_message:
        return

    await update.effective_message.reply_text(
        (
            "أهلًا بك في "
            "<b>النبع للخدمات الجامعية</b> 🎓\n\n"
            "اختر الخدمة وأرسل طلبك من التطبيق."
        ),
        reply_markup=keyboard,
        parse_mode="HTML",
    )


async def channel_diagnosis(
    send_test: bool = False,
):
    channel = parse_channel_id()

    if channel is None:
        return False, "CHANNEL_ID غير مضبوط."

    try:
        chat = await telegram_app.bot.get_chat(
            channel
        )

        me = await telegram_app.bot.get_me()

        member = await telegram_app.bot.get_chat_member(
            channel,
            me.id,
        )

        if member.status not in (
            "administrator",
            "creator",
        ):
            return (
                False,
                (
                    f"البوت موجود في «{chat.title}» "
                    f"لكن حالته «{member.status}». "
                    "يجب أن يكون Administrator."
                ),
            )

        if send_test:
            test_message = (
                await telegram_app.bot.send_message(
                    chat_id=channel,
                    text="✅ رسالة اختبار من بوت النبع",
                )
            )

            return (
                True,
                (
                    f"القناة «{chat.title}» جاهزة. "
                    f"البوت @{me.username} قادر على النشر. "
                    f"message_id={test_message.message_id}"
                ),
            )

        return (
            True,
            (
                f"القناة «{chat.title}» جاهزة. "
                f"البوت @{me.username} قادر على النشر."
            ),
        )

    except TelegramError as exc:
        return False, f"{type(exc).__name__}: {exc}"


async def testchannel_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if (
        not update.effective_user
        or update.effective_user.id != ADMIN_TELEGRAM_ID
    ):
        return

    ok, info = await channel_diagnosis(
        send_test=True
    )

    await update.effective_message.reply_text(
        (
            f"{'✅' if ok else '❌'} {info}\n\n"
            f"CHANNEL_ID: "
            f"{CHANNEL_ID or '(فارغ)'}"
        )
    )


async def redeliver_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if (
        not update.effective_user
        or update.effective_user.id != ADMIN_TELEGRAM_ID
    ):
        return

    message = update.effective_message

    if not message:
        return

    parts = (
        (message.text or "")
        .split(maxsplit=1)
    )

    if len(parts) != 2:
        await message.reply_text(
            "الاستخدام:\n\n"
            "/redeliver NB-2026-123456"
        )
        return

    order_id = parts[1].strip()

    with db() as conn:
        order = conn.execute(
            """
            SELECT *
            FROM orders
            WHERE order_id=%s
            """,
            (order_id,),
        ).fetchone()

    if not order:
        await message.reply_text(
            "❌ الطلب غير موجود."
        )
        return

    user = {
        "id": order["user_id"],
        "username": order["username"],
        "first_name": "",
        "last_name": "",
    }

    delivered = await deliver_order(
        order,
        user,
        None,
    )

    await message.reply_text(
        (
            f"✅ تمت محاولة إرسال الطلب {order_id} إلى القناة."
            if delivered
            else
            f"❌ فشل إرسال الطلب {order_id} إلى القناة."
        )
    )


# =========================================================
# Telegram Application
# =========================================================

telegram_app = (
    ApplicationBuilder()
    .token(BOT_TOKEN)
    .build()
)

telegram_app.add_handler(
    CommandHandler(
        "start",
        start_command,
    )
)

telegram_app.add_handler(
    CommandHandler(
        "testchannel",
        testchannel_command,
    )
)

telegram_app.add_handler(
    CommandHandler(
        "reply",
        admin_reply_command,
    )
)

telegram_app.add_handler(
    CommandHandler(
        "redeliver",
        redeliver_command,
    )
)

telegram_app.add_handler(
    CallbackQueryHandler(
        quotation_callback_handler,
        pattern=r"^quotation:(accept|reject):",
    )
)

telegram_app.add_handler(
    MessageHandler(
        filters.ChatType.CHANNEL,
        channel_post_handler,
    )
)

# ردود الأدمن على منشورات القناة قد تصل من مجموعة
# المناقشة المرتبطة بالقناة، وليس كـ channel_post.
telegram_app.add_handler(
    MessageHandler(
        filters.ChatType.GROUPS
        & ~filters.COMMAND,
        channel_post_handler,
    )
)

# IMPORTANT:
# Ordinary private customer messages MUST NOT call start_command.
telegram_app.add_handler(
    MessageHandler(
        filters.ChatType.PRIVATE & ~filters.COMMAND,
        customer_private_message_handler,
    )
)


# =========================================================
# Lifespan
# =========================================================

@asynccontextmanager
async def lifespan(_: FastAPI):

    await init_db_with_retry()

    await telegram_app.initialize()

    try:
        await telegram_app.bot.set_webhook(
            url=WEBHOOK_URL,
            secret_token=WEBHOOK_SECRET,
            allowed_updates=[
                "message",
                "channel_post",
                "edited_channel_post",
                "callback_query",
            ],
        )

        logger.info(
            "Telegram webhook configured: %s",
            WEBHOOK_URL,
        )

    except TelegramError:
        logger.exception(
            "Failed to configure Telegram webhook"
        )

    yield

    try:
        await telegram_app.shutdown()
    except Exception:
        logger.exception(
            "Telegram shutdown failed"
        )


# =========================================================
# FastAPI
# =========================================================

app = FastAPI(
    title="النبع للخدمات الجامعية API",
    version="8.0.0",
    lifespan=lifespan,
)

allowed_origins = {
    origin_of(WEB_APP_URL),
    origin_of(BACKEND_URL),
}

for origin in os.getenv(
    "ALLOWED_ORIGINS",
    "",
).split(","):

    origin = (
        origin.strip().rstrip("/")
    )

    if origin:
        allowed_origins.add(origin)

app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(
        allowed_origins
    ),
    allow_credentials=False,
    allow_methods=[
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "OPTIONS",
    ],
    allow_headers=[
        "Content-Type",
        "X-Telegram-Init-Data",
        "Idempotency-Key",
    ],
    expose_headers=[
        "Content-Disposition",
        "Content-Length",
    ],
)


# =========================================================
# File Validation
# =========================================================

DOCUMENT_SIGNATURES = {
    "application/pdf": b"%PDF",
    "application/msword": b"\xd0\xcf\x11\xe0",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        b"PK\x03\x04",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
        b"PK\x03\x04",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation":
        b"PK\x03\x04",
}


def validate_file(
    data: bytes,
    declared_type: str,
) -> str:

    declared_type = (
        declared_type or ""
    ).lower().strip()

    if declared_type.startswith("image/"):

        try:
            with Image.open(
                io.BytesIO(data)
            ) as img:
                img.verify()

                mime = Image.MIME.get(
                    img.format or ""
                )

        except Exception:
            raise HTTPException(
                status_code=400,
                detail=(
                    "الصورة تالفة أو غير مدعومة. "
                    "استخدم JPG أو PNG أو WebP."
                ),
            )

        if mime not in {
            "image/jpeg",
            "image/png",
            "image/webp",
            "image/gif",
        }:
            raise HTTPException(
                status_code=400,
                detail="صيغة الصورة غير مدعومة",
            )

        return mime

    signature = DOCUMENT_SIGNATURES.get(
        declared_type
    )

    if (
        not signature
        or not data.startswith(signature)
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "نوع الملف غير مدعوم أو تالف. "
                "المسموح PDF / DOC / DOCX / XLSX / PPTX / صور."
            ),
        )

    return declared_type


# =========================================================
# Root / Health
# =========================================================

@app.get("/")
def read_root():

    if INDEX_FILE.exists():
        return FileResponse(
            INDEX_FILE,
            media_type="text/html",
            headers={
                "Cache-Control": "no-cache"
            },
        )

    return {
        "status": "online",
        "system": "NABA",
        "database": "PostgreSQL",
    }


@app.get("/health")
def health():

    try:
        with db() as conn:
            conn.execute("SELECT 1")

        return {
            "status": "ok",
            "database": "connected",
        }

    except Exception:
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "database": "unavailable",
            },
        )


# =========================================================
# Telegram Webhook
# =========================================================

@app.post(WEBHOOK_PATH)
async def telegram_webhook(
    request: Request,
):

    received = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token",
        "",
    )

    if (
        not received
        or not hmac.compare_digest(
            received,
            WEBHOOK_SECRET,
        )
    ):
        raise HTTPException(
            status_code=403,
            detail="Forbidden",
        )

    try:

        body = await request.json()

        update = Update.de_json(
            body,
            telegram_app.bot,
        )

        await telegram_app.process_update(
            update
        )

    except Exception:
        logger.exception(
            "Failed to process Telegram update"
        )

        raise HTTPException(
            status_code=400,
            detail="Bad Telegram update",
        )

    return {"ok": True}


# =========================================================
# Customer Upload
# =========================================================

@app.post("/api/upload")
async def upload_attachment(
    request: Request,
    file: UploadFile = File(...),
):

    user = init_user_from_header(request)

    data = await file.read(
        MAX_ATTACHMENT_BYTES + 1
    )

    if not data:
        raise HTTPException(
            status_code=400,
            detail="الملف فارغ",
        )

    if len(data) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"حجم الملف يجب ألا يتجاوز "
                f"{MAX_ATTACHMENT_MB} MB"
            ),
        )

    content_type = validate_file(
        data,
        file.content_type or "",
    )

    filename = os.path.basename(
        file.filename or ""
    ).strip()[:100] or "attachment"

    token = await asyncio.to_thread(
        store_attachment,
        user["id"],
        filename,
        content_type,
        data,
        None,
    )

    return {
        "token": token,
        "filename": filename,
    }


# =========================================================
# Submit Order
# =========================================================

@app.post("/api/orders")
async def submit_order(
    payload: OrderIn,
    request: Request,
    idempotency_key: Optional[str] = Header(
        None,
        alias="Idempotency-Key",
    ),
):

    user = init_user_from_header(request)

    order, attachment, is_duplicate = (
        await asyncio.to_thread(
            create_order,
            user,
            payload,
            idempotency_key,
        )
    )

    if is_duplicate:

        delivery_status = (
            order.get(
                "delivery_channel_status"
            )
            or "PENDING"
        )

        if (
            delivery_status == "DELIVERED"
            and order.get("channel_message_id")
        ):
            return {
                "ok": True,
                "order_id": order["order_id"],
                "delivered": True,
                "duplicate": True,
            }

        # If the original request already created the order,
        # recover its linked attachment from DB.
        with db() as conn:
            recovered_attachment = conn.execute(
                """
                SELECT *
                FROM attachments
                WHERE order_id=%s
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (order["order_id"],),
            ).fetchone()

        existing_user = {
            "id": order["user_id"],
            "username": order["username"],
            "first_name": "",
            "last_name": "",
        }

        delivered = await deliver_order(
            order,
            existing_user,
            recovered_attachment,
        )

        if delivered and recovered_attachment:
            with db() as conn:
                conn.execute(
                    """
                    DELETE FROM attachments
                    WHERE token=%s
                    """,
                    (
                        recovered_attachment["token"],
                    ),
                )

        return {
            "ok": True,
            "order_id": order["order_id"],
            "delivered": delivered,
            "duplicate": True,
        }

    delivered = await deliver_order(
        order,
        user,
        attachment,
    )

    # Delete attachment ONLY after successful channel delivery.
    if (
        delivered
        and payload.attachment_token
    ):
        try:
            await asyncio.to_thread(
                delete_attachment,
                user["id"],
                payload.attachment_token,
            )
        except Exception:
            logger.exception(
                "Could not delete attachment %s",
                payload.attachment_token,
            )

    # IMPORTANT:
    confirmation = (
        "✅ <b>تم استلام طلبك</b>\n\n"
        f"🆔 <b>رقم الطلب:</b> <code>{html.escape(order['order_id'])}</code>\n"
        "سيتم التواصل معك من قبل الكادر."
    )
    if not delivered:
        confirmation += (
            "\n\n⚠️ تم حفظ الطلب، وسيعيد النظام محاولة إرساله للكادر."
        )
    await notify_customer(user["id"], confirmation)

    if not delivered:
        try:
            with db() as conn:
                add_audit_log_conn(
                    conn,
                    order["order_id"],
                    "CUSTOMER_ORDER_CREATED_CHANNEL_DELIVERY_FAILED",
                    "System",
                    "تم إنشاء الطلب لكن تعذر إرساله للقناة",
                )
        except Exception:
            logger.exception(
                "Could not write delivery failure audit"
            )

    return {
        "ok": True,
        "order_id": order["order_id"],
        "delivered": delivered,
    }


# =========================================================
# Customer Order Status
# =========================================================

@app.get("/api/orders/{order_id}")
def get_order_status(
    order_id: str,
    request: Request,
):

    user = init_user_from_header(request)

    with db() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM orders
            WHERE order_id=%s
              AND user_id=%s
            """,
            (
                order_id,
                user["id"],
            ),
        ).fetchone()

    if not row:
        raise HTTPException(
            status_code=404,
            detail="الطلب غير موجود",
        )

    return row


# =========================================================
# Accept Quotation API - kept for compatibility
# =========================================================

@app.post(
    "/api/orders/{order_id}/accept_quotation"
)
async def accept_quotation(
    order_id: str,
    request: Request,
):

    user = init_user_from_header(request)

    with db() as conn:

        order = conn.execute(
            """
            SELECT *
            FROM orders
            WHERE order_id=%s
            FOR UPDATE
            """,
            (order_id,),
        ).fetchone()

        if not order:
            raise HTTPException(
                status_code=404,
                detail="الطلب غير موجود",
            )

        if order["user_id"] != user["id"]:
            raise HTTPException(
                status_code=403,
                detail=(
                    "غير مصرح لك باتخاذ قرار "
                    "بشأن هذا الطلب"
                ),
            )

        if order["order_status"] != "WAITING_CUSTOMER":
            raise HTTPException(
                status_code=400,
                detail=(
                    "الطلب ليس في حالة انتظار "
                    "قرار الزبون"
                ),
            )

        conn.execute(
            """
            UPDATE orders
            SET
                customer_decision='ACCEPTED',
                customer_decision_at=CURRENT_TIMESTAMP,
                order_status='ACCEPTED'
            WHERE order_id=%s
            """,
            (order_id,),
        )

        add_audit_log_conn(
            conn,
            order_id,
            "QUOTATION_ACCEPTED",
            f"User_{user['id']}",
            "تم قبول العرض",
        )

    await send_channel_text(
        (
            "✅ <b>قرار الزبون: قبول عرض السعر</b>\n\n"
            f"🆔 الطلب: <code>{html.escape(order_id)}</code>\n"
            f"👤 Telegram ID: <code>{user['id']}</code>"
        )
    )

    return {
        "ok": True,
        "status": "ACCEPTED",
    }


# =========================================================
# Reject Quotation API - kept for compatibility
# =========================================================

@app.post(
    "/api/orders/{order_id}/reject_quotation"
)
async def reject_quotation(
    order_id: str,
    request: Request,
):

    user = init_user_from_header(request)

    with db() as conn:

        order = conn.execute(
            """
            SELECT *
            FROM orders
            WHERE order_id=%s
            FOR UPDATE
            """,
            (order_id,),
        ).fetchone()

        if not order:
            raise HTTPException(
                status_code=404,
                detail="الطلب غير موجود",
            )

        if order["user_id"] != user["id"]:
            raise HTTPException(
                status_code=403,
                detail="غير مصرح لك",
            )

        if order["order_status"] != "WAITING_CUSTOMER":
            raise HTTPException(
                status_code=400,
                detail=(
                    "الطلب ليس في حالة انتظار "
                    "قرار الزبون"
                ),
            )

        conn.execute(
            """
            UPDATE orders
            SET
                customer_decision='REJECTED',
                customer_decision_at=CURRENT_TIMESTAMP,
                order_status='CANCELLED'
            WHERE order_id=%s
            """,
            (order_id,),
        )

        add_audit_log_conn(
            conn,
            order_id,
            "QUOTATION_REJECTED",
            f"User_{user['id']}",
            "تم رفض العرض",
        )

    await send_channel_text(
        (
            "❌ <b>قرار الزبون: رفض عرض السعر</b>\n\n"
            f"🆔 الطلب: <code>{html.escape(order_id)}</code>\n"
            f"👤 Telegram ID: <code>{user['id']}</code>"
        )
    )

    return {
        "ok": True,
        "status": "CANCELLED",
    }


# =========================================================
# Admin Access
# =========================================================

@app.get("/api/admin/access")
def admin_access(
    request: Request,
):

    user = require_admin(request)

    return {
        "ok": True,
        "is_admin": True,
        "telegram_id": user["id"],
    }


# =========================================================
# Admin Orders
# =========================================================

@app.get("/api/admin/orders")
def search_and_list_orders(
    request: Request,
    q: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    service_type: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):

    require_admin(request)

    conditions = []
    params = []

    if q:
        conditions.append(
            """
            (
                order_id ILIKE %s
                OR username ILIKE %s
                OR title ILIKE %s
                OR CAST(user_id AS TEXT) ILIKE %s
            )
            """
        )

        wildcard = f"%{q}%"

        params.extend(
            [
                wildcard,
                wildcard,
                wildcard,
                wildcard,
            ]
        )

    if status:

        if status.upper() not in VALID_STATUSES:
            raise HTTPException(
                status_code=400,
                detail="حالة غير صالحة",
            )

        conditions.append(
            "order_status=%s"
        )

        params.append(
            status.upper()
        )

    if service_type:

        if service_type not in SERVICES:
            raise HTTPException(
                status_code=400,
                detail="نوع الخدمة غير صالح",
            )

        conditions.append(
            "service_type=%s"
        )

        params.append(
            service_type
        )

    where_clause = (
        " WHERE "
        + " AND ".join(conditions)
        if conditions
        else ""
    )

    query = f"""
        SELECT *
        FROM orders
        {where_clause}
        ORDER BY created_at DESC
        LIMIT %s OFFSET %s
    """

    query_params = list(params)
    query_params.extend(
        [
            limit,
            offset,
        ]
    )

    with db() as conn:

        rows = conn.execute(
            query,
            query_params,
        ).fetchall()

        total = conn.execute(
            f"""
            SELECT COUNT(*) AS cnt
            FROM orders
            {where_clause}
            """,
            params,
        ).fetchone()["cnt"]

    return {
        "ok": True,
        "total": total,
        "orders": rows,
    }


# =========================================================
# Admin Order Details
# =========================================================

@app.get(
    "/api/admin/orders/{order_id}"
)
def get_order_details_admin(
    order_id: str,
    request: Request,
):

    require_admin(request)

    with db() as conn:

        order = conn.execute(
            """
            SELECT *
            FROM orders
            WHERE order_id=%s
            """,
            (order_id,),
        ).fetchone()

        if not order:
            raise HTTPException(
                status_code=404,
                detail="الطلب غير موجود",
            )

        payments = conn.execute(
            """
            SELECT *
            FROM payments
            WHERE order_id=%s
            ORDER BY created_at ASC
            """,
            (order_id,),
        ).fetchall()

        audit = conn.execute(
            """
            SELECT *
            FROM audit_logs
            WHERE order_id=%s
            ORDER BY timestamp DESC
            """,
            (order_id,),
        ).fetchall()

        delivery_attachments = conn.execute(
            """
            SELECT
                token,
                filename,
                content_type,
                created_at
            FROM attachments
            WHERE order_id=%s
            ORDER BY created_at ASC
            """,
            (order_id,),
        ).fetchall()

    return {
        "ok": True,
        "order": order,
        "payments": payments,
        "audit_logs": audit,
        "delivery_attachments": delivery_attachments,
    }


# =========================================================
# Admin Quotation
# =========================================================

@app.post(
    "/api/admin/orders/{order_id}/quotation"
)
async def set_quotation(
    order_id: str,
    payload: QuotationIn,
    request: Request,
):

    require_admin(request)

    with db() as conn:

        order = conn.execute(
            """
            SELECT *
            FROM orders
            WHERE order_id=%s
            FOR UPDATE
            """,
            (order_id,),
        ).fetchone()

        if not order:
            raise HTTPException(
                status_code=404,
                detail="الطلب غير موجود",
            )

        if order["order_status"] in {
            "COMPLETED",
            "CANCELLED",
            "REJECTED",
        }:
            raise HTTPException(
                status_code=400,
                detail=(
                    "لا يمكن إرسال عرض سعر "
                    "لطلب مغلق أو مكتمل."
                ),
            )

        currency = (
            payload.currency
            .upper()
            .strip()
        )

        price = payload.price

        deposit = Decimal(
            str(order["deposit"] or 0)
        )

        if deposit > 0:

            existing_currency = conn.execute(
                """
                SELECT currency
                FROM payments
                WHERE order_id=%s
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (order_id,),
            ).fetchone()

            if (
                existing_currency
                and existing_currency["currency"]
                and existing_currency["currency"] != currency
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "عملة عرض السعر يجب أن تطابق "
                        "عملة الدفعات السابقة."
                    ),
                )

        remaining = price - deposit

        if remaining < 0:
            raise HTTPException(
                status_code=400,
                detail=(
                    "السعر الجديد أقل من إجمالي "
                    "المبلغ المدفوع مسبقًا."
                ),
            )

        if deposit == 0:
            payment_status = "UNPAID"
        elif remaining == 0:
            payment_status = "PAID"
        else:
            payment_status = "PARTIALLY_PAID"

        conn.execute(
            """
            UPDATE orders
            SET
                quotation_price=%s,
                price=%s,
                remaining_balance=%s,
                quotation_currency=%s,
                delivery_date=%s,
                delivery_time=%s,
                quotation_notes=%s,
                payment_status=%s,
                order_status='WAITING_CUSTOMER',
                customer_decision='PENDING',
                customer_decision_at=NULL
            WHERE order_id=%s
            """,
            (
                price,
                price,
                remaining,
                currency,
                payload.delivery_date,
                payload.delivery_time,
                payload.notes,
                payment_status,
                order_id,
            ),
        )

        add_audit_log_conn(
            conn,
            order_id,
            "QUOTATION_SENT",
            "Admin",
            (
                f"السعر: {price} {currency} | "
                f"الموعد: {payload.delivery_date} "
                f"{payload.delivery_time}"
            ),
        )

        customer_id = order["user_id"]

    # ONLY quotation notification.
    notification_ok = await notify_customer(
        customer_id,
        (
            f"📋 <b>عرض سعر جديد لطلبك</b>\n\n"
            f"🆔 <b>رقم الطلب:</b> "
            f"<code>{html.escape(order_id)}</code>\n\n"
            f"💰 <b>السعر الكلي:</b> "
            f"{html.escape(str(price))} "
            f"{html.escape(currency)}\n\n"
            f"📅 <b>موعد التسليم:</b> "
            f"{html.escape(payload.delivery_date)} "
            f"{html.escape(payload.delivery_time or 'غير محدد')}\n\n"
            f"📝 <b>ملاحظات العرض:</b> "
            f"{html.escape(payload.notes or 'لا يوجد')}\n\n"
            "━━━━━━━━━━━━━━\n"
            "اختر القرار من الأزرار أدناه."
        ),
        reply_markup=InlineKeyboardMarkup(
            [[
                InlineKeyboardButton(
                    "✅ تأكيد الطلب",
                    callback_data=f"quotation:accept:{order_id}",
                ),
                InlineKeyboardButton(
                    "❌ رفض الطلب",
                    callback_data=f"quotation:reject:{order_id}",
                ),
            ]]
        ),
    )

    if not notification_ok:
        with db() as conn:
            add_audit_log_conn(
                conn,
                order_id,
                "QUOTATION_NOTIFICATION_FAILED",
                "System",
                "تعذر إرسال عرض السعر للزبون",
            )

    return {
        "ok": True,
        "order_id": order_id,
        "status": "WAITING_CUSTOMER",
    }


# =========================================================
# Admin Status
# =========================================================

@app.post(
    "/api/admin/orders/{order_id}/status"
)
async def update_order_status(
    order_id: str,
    payload: StatusUpdateIn,
    request: Request,
):

    require_admin(request)

    new_status = (
        payload.status
        .upper()
        .strip()
    )

    if new_status not in VALID_STATUSES:
        raise HTTPException(
            status_code=400,
            detail="حالة غير صالحة",
        )

    # COMPLETED MUST NOT be set manually.
    if new_status == "COMPLETED":
        raise HTTPException(
            status_code=400,
            detail=(
                "لا يمكن إكمال الطلب من تغيير الحالة مباشرة. "
                "استخدم زر إرسال التسليم النهائي للزبون."
            ),
        )

    with db() as conn:

        order = conn.execute(
            """
            SELECT *
            FROM orders
            WHERE order_id=%s
            FOR UPDATE
            """,
            (order_id,),
        ).fetchone()

        if not order:
            raise HTTPException(
                status_code=404,
                detail="الطلب غير موجود",
            )

        current_status = order["order_status"]

        allowed = ALLOWED_STATUS_TRANSITIONS.get(
            current_status,
            [],
        )

        if (
            new_status not in allowed
            and new_status != current_status
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"لا يمكن الانتقال من الحالة "
                    f"'{current_status}' إلى "
                    f"'{new_status}' مباشرة."
                ),
            )

        customer_decision_sql = ""

        if new_status == "ACCEPTED":
            customer_decision_sql = (
                ", customer_decision='ACCEPTED', "
                "customer_decision_at=COALESCE("
                "customer_decision_at, CURRENT_TIMESTAMP)"
            )

        elif new_status == "REJECTED":
            customer_decision_sql = (
                ", customer_decision='REJECTED', "
                "customer_decision_at=COALESCE("
                "customer_decision_at, CURRENT_TIMESTAMP)"
            )

        conn.execute(
            f"""
            UPDATE orders
            SET
                order_status=%s,
                admin_note=CASE
                    WHEN %s <> ''
                    THEN %s
                    ELSE admin_note
                END
                {customer_decision_sql}
            WHERE order_id=%s
            """,
            (
                new_status,
                payload.admin_note,
                payload.admin_note,
                order_id,
            ),
        )

        add_audit_log_conn(
            conn,
            order_id,
            f"STATUS_CHANGED_TO_{new_status}",
            "Admin",
            payload.admin_note,
        )

    # IMPORTANT:
    # NO customer notification on status changes.

    return {
        "ok": True,
        "order_id": order_id,
        "status": new_status,
    }


# =========================================================
# Admin Payment
# =========================================================

@app.post(
    "/api/admin/orders/{order_id}/payments"
)
def record_payment(
    order_id: str,
    payload: PaymentIn,
    request: Request,
):

    require_admin(request)

    with db() as conn:

        order = conn.execute(
            """
            SELECT *
            FROM orders
            WHERE order_id=%s
            FOR UPDATE
            """,
            (order_id,),
        ).fetchone()

        if not order:
            raise HTTPException(
                status_code=404,
                detail="الطلب غير موجود",
            )

        total_price = Decimal(
            str(order["price"] or 0)
        )

        if total_price <= 0:
            raise HTTPException(
                status_code=400,
                detail=(
                    "يرجى تحديد سعر الطلب أولاً "
                    "قبل تسجيل الدفع"
                ),
            )

        quotation_currency = (
            order["quotation_currency"]
            or "IQD"
        ).upper()

        payment_currency = (
            payload.currency
            .upper()
            .strip()
        )

        if payment_currency != quotation_currency:
            raise HTTPException(
                status_code=400,
                detail=(
                    "عملة الدفعة يجب أن تطابق "
                    f"عملة الطلب: {quotation_currency}"
                ),
            )

        existing_payments = conn.execute(
            """
            SELECT COALESCE(SUM(amount),0) AS total
            FROM payments
            WHERE order_id=%s
              AND currency=%s
            """,
            (
                order_id,
                quotation_currency,
            ),
        ).fetchone()["total"]

        existing_payments = Decimal(
            str(existing_payments or 0)
        )

        new_total_paid = (
            existing_payments
            + payload.amount
        )

        remaining = (
            total_price
            - new_total_paid
        )

        if remaining < 0:
            raise HTTPException(
                status_code=400,
                detail=(
                    "مبلغ الدفعة يتجاوز "
                    "المتبقي للطلب"
                ),
            )

        if remaining == 0:
            payment_status = "PAID"
        elif new_total_paid > 0:
            payment_status = "PARTIALLY_PAID"
        else:
            payment_status = "UNPAID"

        conn.execute(
            """
            INSERT INTO payments(
                order_id,
                amount,
                currency,
                payment_method,
                recorded_by
            )
            VALUES(%s,%s,%s,%s,'Admin')
            """,
            (
                order_id,
                payload.amount,
                payment_currency,
                payload.payment_method,
            ),
        )

        conn.execute(
            """
            UPDATE orders
            SET
                deposit=%s,
                remaining_balance=%s,
                payment_status=%s
            WHERE order_id=%s
            """,
            (
                new_total_paid,
                remaining,
                payment_status,
                order_id,
            ),
        )

        add_audit_log_conn(
            conn,
            order_id,
            "PAYMENT_RECORDED",
            "Admin",
            (
                f"مبلغ: {payload.amount} "
                f"{payment_currency} | "
                f"الحالة الجديدة: {payment_status}"
            ),
        )

    return {
        "ok": True,
        "order_id": order_id,
        "total_paid": str(new_total_paid),
        "remaining_balance": str(remaining),
        "payment_status": payment_status,
    }


# =========================================================
# Admin Note
# =========================================================

@app.post(
    "/api/admin/orders/{order_id}/notes"
)
def update_admin_note(
    order_id: str,
    payload: AdminNoteIn,
    request: Request,
):

    require_admin(request)

    with db() as conn:

        order = conn.execute(
            """
            SELECT order_id
            FROM orders
            WHERE order_id=%s
            FOR UPDATE
            """,
            (order_id,),
        ).fetchone()

        if not order:
            raise HTTPException(
                status_code=404,
                detail="الطلب غير موجود",
            )

        conn.execute(
            """
            UPDATE orders
            SET admin_note=%s
            WHERE order_id=%s
            """,
            (
                payload.admin_note,
                order_id,
            ),
        )

        add_audit_log_conn(
            conn,
            order_id,
            "ADMIN_NOTE_UPDATED",
            "Admin",
            payload.admin_note,
        )

    return {
        "ok": True,
        "order_id": order_id,
    }


# =========================================================
# Admin Delivery Upload
# =========================================================

@app.post(
    "/api/admin/orders/{order_id}/delivery-upload"
)
async def upload_delivery_attachment(
    order_id: str,
    request: Request,
    file: UploadFile = File(...),
):

    admin = require_admin(request)

    data = await file.read(
        MAX_ATTACHMENT_BYTES + 1
    )

    if not data:
        raise HTTPException(
            status_code=400,
            detail="الملف فارغ",
        )

    if len(data) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"حجم الملف يجب ألا يتجاوز "
                f"{MAX_ATTACHMENT_MB} MB"
            ),
        )

    content_type = validate_file(
        data,
        file.content_type or "",
    )

    filename = os.path.basename(
        file.filename or ""
    ).strip()[:100] or "delivery"

    with db() as conn:

        order = conn.execute(
            """
            SELECT order_id, user_id, order_status
            FROM orders
            WHERE order_id=%s
            """,
            (order_id,),
        ).fetchone()

        if not order:
            raise HTTPException(
                status_code=404,
                detail="الطلب غير موجود",
            )

        if order["order_status"] != "READY_FOR_DELIVERY":
            raise HTTPException(
                status_code=400,
                detail=(
                    "يمكن رفع ملفات التسليم فقط "
                    "عندما يكون الطلب جاهزًا للتسليم."
                ),
            )

        token = secrets.token_urlsafe(24)

        conn.execute(
            """
            INSERT INTO attachments(
                token,
                user_id,
                filename,
                content_type,
                data,
                order_id
            )
            VALUES(%s,%s,%s,%s,%s,%s)
            """,
            (
                token,
                order["user_id"],
                filename,
                content_type,
                data,
                order_id,
            ),
        )

        add_audit_log_conn(
            conn,
            order_id,
            "DELIVERY_ATTACHMENT_UPLOADED",
            f"Admin_{admin['id']}",
            filename,
        )

    return {
        "ok": True,
        "order_id": order_id,
        "token": token,
        "filename": filename,
    }


# =========================================================
# Final Delivery
# =========================================================

async def send_final_delivery(
    order: dict,
    attachments: list[dict],
) -> bool:

    user_id = order["user_id"]
    order_id = order["order_id"]

    try:

        await telegram_app.bot.send_message(
            chat_id=user_id,
            text=(
                "🎓 <b>تسليم طلبك من النبع</b>\n\n"
                f"🆔 <b>رقم الطلب:</b> "
                f"<code>{html.escape(order_id)}</code>\n\n"
                "✅ تم الانتهاء من طلبك، "
                "وتجد الملفات المرفقة أدناه.\n\n"
                "شكرًا لاختياركم النبع للخدمات الجامعية."
            ),
            parse_mode="HTML",
        )

        for attachment in attachments:

            data = bytes(
                attachment["data"]
            )

            filename = (
                attachment["filename"]
                or "delivery"
            )

            content_type = (
                attachment["content_type"]
                or ""
            )

            caption = (
                f"📎 تسليم الطلب "
                f"{order_id}\n"
                f"{filename}"
            )

            if content_type.startswith("image/"):

                try:

                    await telegram_app.bot.send_photo(
                        chat_id=user_id,
                        photo=data,
                        caption=caption,
                    )

                    continue

                except TelegramError:
                    logger.warning(
                        "Could not send delivery image "
                        "as photo, trying document",
                        exc_info=True,
                    )

            await telegram_app.bot.send_document(
                chat_id=user_id,
                document=data,
                filename=filename,
                caption=caption,
            )

        return True

    except Exception:
        logger.exception(
            "Final delivery failed for order %s",
            order_id,
        )
        return False


@app.post(
    "/api/admin/orders/{order_id}/deliver"
)
async def deliver_order_to_customer(
    order_id: str,
    request: Request,
):

    admin = require_admin(request)

    with db() as conn:

        order = conn.execute(
            """
            SELECT *
            FROM orders
            WHERE order_id=%s
            FOR UPDATE
            """,
            (order_id,),
        ).fetchone()

        if not order:
            raise HTTPException(
                status_code=404,
                detail="الطلب غير موجود",
            )

        if order["order_status"] != "READY_FOR_DELIVERY":
            raise HTTPException(
                status_code=400,
                detail=(
                    "الطلب يجب أن يكون في حالة "
                    "READY_FOR_DELIVERY قبل إرسال التسليم."
                ),
            )

        attachments = conn.execute(
            """
            SELECT *
            FROM attachments
            WHERE order_id=%s
            ORDER BY created_at ASC
            """,
            (order_id,),
        ).fetchall()

        if not attachments:
            raise HTTPException(
                status_code=400,
                detail=(
                    "لا توجد ملفات تسليم. "
                    "ارفع ملفًا واحدًا على الأقل قبل الإرسال."
                ),
            )

    delivered = await send_final_delivery(
        order,
        attachments,
    )

    if not delivered:
        with db() as conn:
            add_audit_log_conn(
                conn,
                order_id,
                "FINAL_DELIVERY_FAILED",
                f"Admin_{admin['id']}",
                "فشل إرسال ملفات التسليم للزبون",
            )

        raise HTTPException(
            status_code=502,
            detail=(
                "تعذر إرسال التسليم للزبون. "
                "لم يتم تحويل الطلب إلى مكتمل."
            ),
        )

    with db() as conn:

        # Delete delivery files only after successful delivery.
        conn.execute(
            """
            DELETE FROM attachments
            WHERE order_id=%s
            """,
            (order_id,),
        )

        conn.execute(
            """
            UPDATE orders
            SET
                order_status='COMPLETED',
                delivery_channel_status='DELIVERED'
            WHERE order_id=%s
            """,
            (order_id,),
        )

        add_audit_log_conn(
            conn,
            order_id,
            "FINAL_DELIVERY_SENT",
            f"Admin_{admin['id']}",
            "تم إرسال التسليم النهائي للزبون وتحويل الطلب إلى COMPLETED",
        )

    return {
        "ok": True,
        "order_id": order_id,
        "status": "COMPLETED",
        "message": "تم إرسال التسليم النهائي للزبون.",
    }


# =========================================================
# Admin Dashboard
# =========================================================

@app.get("/api/admin/dashboard")
def admin_dashboard(
    request: Request,
):

    require_admin(request)

    with db() as conn:

        totals = conn.execute(
            """
            SELECT
                COUNT(*) AS total_orders,
                COUNT(*) FILTER (
                    WHERE created_at >= CURRENT_DATE
                ) AS today_orders,
                COUNT(*) FILTER (
                    WHERE created_at >= date_trunc(
                        'month',
                        CURRENT_TIMESTAMP
                    )
                ) AS month_orders,
                COALESCE(SUM(price),0) AS total_price,
                COALESCE(SUM(deposit),0) AS total_deposit,
                COALESCE(
                    SUM(remaining_balance),
                    0
                ) AS total_remaining
            FROM orders
            """
        ).fetchone()

        status_rows = conn.execute(
            """
            SELECT
                COALESCE(
                    order_status,
                    'UNKNOWN'
                ) AS status,
                COUNT(*) AS count
            FROM orders
            GROUP BY order_status
            """
        ).fetchall()

        service_rows = conn.execute(
            """
            SELECT
                service_type,
                service_name,
                COUNT(*) AS count
            FROM orders
            GROUP BY service_type, service_name
            """
        ).fetchall()

        payment_rows = conn.execute(
            """
            SELECT
                COALESCE(
                    payment_status,
                    'UNKNOWN'
                ) AS status,
                COUNT(*) AS count
            FROM orders
            GROUP BY payment_status
            """
        ).fetchall()

        recent_rows = conn.execute(
            """
            SELECT *
            FROM orders
            ORDER BY created_at DESC
            LIMIT 20
            """
        ).fetchall()

        activity_rows = conn.execute(
            """
            SELECT *
            FROM audit_logs
            ORDER BY timestamp DESC
            LIMIT 50
            """
        ).fetchall()

    def money(value):
        try:
            return float(value or 0)
        except (
            TypeError,
            ValueError,
        ):
            return 0.0

    status_counts = {
        str(row["status"]): int(row["count"])
        for row in status_rows
    }

    processing_excluded = {
        "NEW",
        "COMPLETED",
        "CANCELLED",
        "REJECTED",
    }

    return {
        "ok": True,
        "generated_at":
            datetime.now(
                timezone.utc
            ).isoformat(),

        "summary": {
            "total_orders":
                int(
                    totals["total_orders"] or 0
                ),

            "today_orders":
                int(
                    totals["today_orders"] or 0
                ),

            "month_orders":
                int(
                    totals["month_orders"] or 0
                ),

            "new_orders":
                status_counts.get(
                    "NEW",
                    0,
                ),

            "processing_orders":
                sum(
                    value
                    for key, value
                    in status_counts.items()
                    if key not in processing_excluded
                ),

            "completed_orders":
                status_counts.get(
                    "COMPLETED",
                    0,
                ),

            "cancelled_orders":
                (
                    status_counts.get(
                        "CANCELLED",
                        0,
                    )
                    +
                    status_counts.get(
                        "REJECTED",
                        0,
                    )
                ),

            "total_price":
                money(
                    totals["total_price"]
                ),

            "total_deposit":
                money(
                    totals["total_deposit"]
                ),

            "total_remaining":
                money(
                    totals["total_remaining"]
                ),
        },

        "status_counts":
            status_counts,

        "service_counts": [
            {
                "service_type":
                    row["service_type"],
                "service_name":
                    row["service_name"],
                "count":
                    int(row["count"]),
            }
            for row in service_rows
        ],

        "payment_counts": {
            str(row["status"]):
                int(row["count"])
            for row in payment_rows
        },

        "recent_orders":
            recent_rows,

        "activities":
            activity_rows,
    }


# =========================================================
# Excel
# =========================================================

def build_excel_report() -> bytes:

    wb = openpyxl.Workbook()

    header_fill = PatternFill(
        start_color="087FC1",
        end_color="087FC1",
        fill_type="solid",
    )

    header_font = Font(
        name="Calibri",
        size=11,
        bold=True,
        color="FFFFFF",
    )

    thin_border = Border(
        left=Side(
            style="thin",
            color="CCCCCC",
        ),
        right=Side(
            style="thin",
            color="CCCCCC",
        ),
        top=Side(
            style="thin",
            color="CCCCCC",
        ),
        bottom=Side(
            style="thin",
            color="CCCCCC",
        ),
    )

    def style_table(ws):

        ws.views.sheetView[0].rightToLeft = True

        for col in range(
            1,
            ws.max_column + 1,
        ):

            cell = ws.cell(
                row=1,
                column=col,
            )

            cell.fill = header_fill
            cell.font = header_font

            cell.alignment = Alignment(
                horizontal="center",
                vertical="center",
            )

            ws.column_dimensions[
                get_column_letter(col)
            ].width = 20

        for row in range(
            2,
            ws.max_row + 1,
        ):

            for col in range(
                1,
                ws.max_column + 1,
            ):

                cell = ws.cell(
                    row=row,
                    column=col,
                )

                cell.border = thin_border

                cell.alignment = Alignment(
                    horizontal="center",
                    vertical="center",
                    wrap_text=True,
                )

    with db() as conn:

        orders = conn.execute(
            """
            SELECT *
            FROM orders
            ORDER BY created_at DESC
            """
        ).fetchall()

        payments = conn.execute(
            """
            SELECT *
            FROM payments
            ORDER BY created_at DESC
            """
        ).fetchall()

        activities = conn.execute(
            """
            SELECT *
            FROM audit_logs
            ORDER BY timestamp DESC
            """
        ).fetchall()

    ws1 = wb.active
    ws1.title = "Orders"

    ws1.append(
        [
            "رقم الطلب",
            "المستخدم ID",
            "اسم المستخدم",
            "الخدمة",
            "التخصص",
            "العنوان",
            "الحالة",
            "السعر",
            "المدفوع",
            "المتبقي",
            "قرار الزبون",
            "حالة الإرسال للقناة",
            "Channel Message ID",
            "موعد التسليم",
            "تاريخ الإنشاء",
        ]
    )

    for order in orders:

        ws1.append(
            [
                order["order_id"],
                order["user_id"],
                order["username"],
                order["service_name"],
                order["department"],
                order["title"],
                order["order_status"],
                float(order["price"] or 0),
                float(order["deposit"] or 0),
                float(
                    order["remaining_balance"] or 0
                ),
                order["customer_decision"],
                order["delivery_channel_status"],
                order["channel_message_id"],
                (
                    f"{order['delivery_date']} "
                    f"{order['delivery_time']}"
                ),
                (
                    order["created_at"].strftime(
                        "%Y-%m-%d %H:%M"
                    )
                    if order["created_at"]
                    else ""
                ),
            ]
        )

    style_table(ws1)

    ws2 = wb.create_sheet("Payments")

    ws2.append(
        [
            "رقم المعاملة",
            "رقم الطلب",
            "المبلغ",
            "العملة",
            "طريقة الدفع",
            "المسجل",
            "التاريخ",
        ]
    )

    for payment in payments:

        ws2.append(
            [
                payment["id"],
                payment["order_id"],
                float(payment["amount"] or 0),
                payment["currency"],
                payment["payment_method"],
                payment["recorded_by"],
                (
                    payment["created_at"].strftime(
                        "%Y-%m-%d %H:%M"
                    )
                    if payment["created_at"]
                    else ""
                ),
            ]
        )

    style_table(ws2)

    ws3 = wb.create_sheet("Activity")

    ws3.append(
        [
            "رقم النشاط",
            "رقم الطلب",
            "الحدث",
            "بواسطة",
            "التفاصيل",
            "التاريخ",
        ]
    )

    for activity in activities:

        ws3.append(
            [
                activity["id"],
                activity["order_id"],
                activity["action"],
                activity["performed_by"],
                activity["details"],
                (
                    activity["timestamp"].strftime(
                        "%Y-%m-%d %H:%M"
                    )
                    if activity["timestamp"]
                    else ""
                ),
            ]
        )

    style_table(ws3)

    ws4 = wb.create_sheet("Summary")

    ws4.append(
        [
            "المؤشر",
            "القيمة",
        ]
    )

    total_price = sum(
        (
            Decimal(
                str(order["price"] or 0)
            )
            for order in orders
        ),
        Decimal("0"),
    )

    total_paid = sum(
        (
            Decimal(
                str(order["deposit"] or 0)
            )
            for order in orders
        ),
        Decimal("0"),
    )

    total_remaining = sum(
        (
            Decimal(
                str(
                    order["remaining_balance"]
                    or 0
                )
            )
            for order in orders
        ),
        Decimal("0"),
    )

    ws4.append(
        [
            "إجمالي الطلبات",
            len(orders),
        ]
    )

    ws4.append(
        [
            "إجمالي المبالغ",
            float(total_price),
        ]
    )

    ws4.append(
        [
            "إجمالي المدفوعات",
            float(total_paid),
        ]
    )

    ws4.append(
        [
            "إجمالي المتبقي",
            float(total_remaining),
        ]
    )

    style_table(ws4)

    output = io.BytesIO()

    wb.save(output)

    return output.getvalue()


@app.get(
    "/api/admin/export/excel"
)
@app.get(
    "/api/admin/export/excel/download"
)
async def export_excel_report(
    request: Request,
    send_telegram: bool = Query(False),
):

    require_admin(request)

    excel_bytes = await asyncio.to_thread(
        build_excel_report
    )

    if send_telegram:

        try:

            await telegram_app.bot.send_document(
                chat_id=ADMIN_TELEGRAM_ID,
                document=io.BytesIO(excel_bytes),
                filename="NABA_Orders.xlsx",
                caption=(
                    "📊 تقرير طلبات وسجلات "
                    "النبع للخدمات الجامعية الشامل."
                ),
            )

        except TelegramError:
            # فشل إرسال نسخة التليجرام لا يمنع تنزيل ملف Excel.
            logger.exception(
                "Could not send Excel report to admin via Telegram"
            )

    # Return the XLSX bytes directly. This avoids proxy/client
    # issues that can occur with a streaming response for a
    # generated in-memory Excel file.
    from fastapi.responses import Response

    return Response(
        content=excel_bytes,
        media_type=(
            "application/vnd.openxmlformats-"
            "officedocument.spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition":
                'attachment; filename="NABA_Orders.xlsx"',
            "X-Content-Type-Options": "nosniff",
            "Content-Length": str(len(excel_bytes)),
            "Cache-Control": "no-store",
        },
    )


# =========================================================
# Local Execution
# =========================================================

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=env_int(
            "PORT",
            8000,
        ),
    )
