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
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    WebAppInfo,
)
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
# Environment helpers
# =========================================================

def env(name: str, default: str = "") -> str:
    value = os.getenv(name, default)
    return value.strip().strip("'\"").strip()


def env_int(name: str, default: int) -> int:
    raw = env(name, str(default))

    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(
            f"{name} must be an integer"
        )


def origin_of(url: str) -> str:
    parsed = urlparse(url)

    if not parsed.scheme or not parsed.netloc:
        raise RuntimeError(
            f"Invalid URL: {url}"
        )

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


# =========================================================
# ADMIN SECURITY
# =========================================================

ADMIN_TELEGRAM_ID = 6931187332

# compatibility
ADMIN_USER_ID = ADMIN_TELEGRAM_ID


MAX_ATTACHMENT_MB = env_int(
    "MAX_ATTACHMENT_MB",
    5,
)

MAX_ATTACHMENT_BYTES = (
    MAX_ATTACHMENT_MB * 1024 * 1024
)

MAX_PENDING_ATTACHMENTS = 5

INIT_DATA_MAX_AGE = env_int(
    "INIT_DATA_MAX_AGE",
    3600,
)

CAMERA_URL = (
    env("CAMERA_URL")
    or f"{WEB_APP_URL.rstrip('/')}/camera.html"
)


if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is not configured"
    )

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not configured"
    )

if not WEB_APP_URL:
    raise RuntimeError(
        "WEB_APP_URL is not configured"
    )

if not WEB_APP_URL.startswith("https://"):
    raise RuntimeError(
        "WEB_APP_URL must use HTTPS"
    )

if not BACKEND_URL:
    raise RuntimeError(
        "BACKEND_URL is not configured"
    )

if not BACKEND_URL.startswith("https://"):
    raise RuntimeError(
        "BACKEND_URL must use HTTPS"
    )

if not WEBHOOK_URL:
    WEBHOOK_URL = (
        f"{BACKEND_URL}{WEBHOOK_PATH}"
    )

if not WEBHOOK_URL.startswith("https://"):
    raise RuntimeError(
        "WEBHOOK_URL must use HTTPS"
    )


# =========================================================
# Webhook secret
# =========================================================

configured_webhook_secret = env(
    "WEBHOOK_SECRET"
)

if re.fullmatch(
    r"[A-Za-z0-9_-]{16,256}",
    configured_webhook_secret or "",
):
    WEBHOOK_SECRET = (
        configured_webhook_secret
    )
else:
    WEBHOOK_SECRET = hashlib.sha256(
        f"naba-webhook:{BOT_TOKEN}".encode()
    ).hexdigest()


# =========================================================
# Camera permissions
# =========================================================

PHOTO_ALLOWED_IDS = {
    ADMIN_TELEGRAM_ID
}

for raw_id in env(
    "PHOTO_ALLOWED_IDS"
).split(","):

    raw_id = raw_id.strip()

    if raw_id.lstrip("-").isdigit():
        PHOTO_ALLOWED_IDS.add(
            int(raw_id)
        )


# =========================================================
# Application paths
# =========================================================

BASE_DIR = Path(__file__).resolve().parent

INDEX_FILE = BASE_DIR / "index.html"
MIGRATIONS_DIR = BASE_DIR / "migrations"


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


# =========================================================
# Service detail labels
# =========================================================

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

    statements = [
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
            payment_status TEXT NOT NULL DEFAULT 'PENDING',
            price NUMERIC(12,2) NOT NULL DEFAULT 0,
            deposit NUMERIC(12,2) NOT NULL DEFAULT 0,
            remaining_balance NUMERIC(12,2) NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,

        """
        ALTER TABLE orders
        ADD COLUMN IF NOT EXISTS
        service_details JSONB
        NOT NULL DEFAULT '{}'::jsonb
        """,

        """
        CREATE TABLE IF NOT EXISTS audit_logs (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            order_id TEXT NOT NULL
                REFERENCES orders(order_id)
                ON DELETE CASCADE,
            action TEXT NOT NULL,
            performed_by TEXT NOT NULL DEFAULT 'System',
            timestamp TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,

        """
        CREATE TABLE IF NOT EXISTS attachments (
            token TEXT PRIMARY KEY,
            user_id BIGINT NOT NULL,
            filename TEXT NOT NULL,
            content_type TEXT NOT NULL,
            data BYTEA NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,

        """
        CREATE INDEX IF NOT EXISTS orders_user_id_idx
        ON orders(user_id)
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
    ]

    with db() as conn:

        for statement in statements:
            conn.execute(statement)

        # =================================================
        # Phase 1 migration
        # =================================================

        migration_file = (
            MIGRATIONS_DIR
            / "001_phase1_foundation.sql"
        )

        if migration_file.exists():

            migration_sql = "\n".join(
                line
                for line in migration_file
                .read_text(
                    encoding="utf-8"
                )
                .splitlines()
                if not line.lstrip().startswith("--")
            )

            for statement in migration_sql.split(";"):

                statement = statement.strip()

                if statement:
                    conn.execute(statement)


async def init_db_with_retry(
    attempts: int = 5,
    delay: int = 3,
):

    for attempt in range(
        1,
        attempts + 1,
    ):

        try:

            await asyncio.to_thread(
                init_db
            )

            logger.info(
                "Database initialized successfully"
            )

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

            await asyncio.sleep(
                delay * attempt
            )


# =========================================================
# Safe audit helpers
# =========================================================

def add_audit_log(
    order_id: str,
    action: str,
    performed_by: str = "System",
):
    with db() as conn:

        conn.execute(
            """
            INSERT INTO audit_logs(
                order_id,
                action,
                performed_by
            )
            VALUES(%s,%s,%s)
            """,
            (
                order_id,
                action,
                performed_by,
            ),
        )


def log_audit_safely(
    conn,
    order_id: str,
    action: str,
    performed_by: str,
):
    try:

        conn.execute(
            "SAVEPOINT naba_audit"
        )

        conn.execute(
            """
            INSERT INTO audit_logs(
                order_id,
                action,
                performed_by
            )
            VALUES(%s,%s,%s)
            """,
            (
                order_id,
                action,
                performed_by,
            ),
        )

        conn.execute(
            "RELEASE SAVEPOINT naba_audit"
        )

    except Exception:

        try:
            conn.execute(
                "ROLLBACK TO SAVEPOINT naba_audit"
            )
            conn.execute(
                "RELEASE SAVEPOINT naba_audit"
            )

        except Exception:

            logger.exception(
                "Could not recover audit savepoint for %s",
                order_id,
            )

        logger.exception(
            "Audit logging failed for %s (%s)",
            order_id,
            action,
        )


def log_audit_for_order_safely(
    order_id: str,
    action: str,
    performed_by: str,
):

    try:

        with db() as conn:

            log_audit_safely(
                conn,
                order_id,
                action,
                performed_by,
            )

    except Exception:

        logger.exception(
            "Could not open database for audit event %s",
            action,
        )


# =========================================================
# Order status / state machine
# =========================================================

ORDER_STATUSES = {
    "NEW",
    "UNDER_REVIEW",
    "WAITING_CUSTOMER",
    "READY_TO_START",
    "IN_PROGRESS",
    "QUALITY_REVIEW",
    "READY_FOR_DELIVERY",
    "COMPLETED",
    "CANCELLED",
}


ORDER_TRANSITIONS = {
    "NEW": {
        "UNDER_REVIEW",
        "CANCELLED",
    },

    "UNDER_REVIEW": {
        "WAITING_CUSTOMER",
        "CANCELLED",
    },

    "WAITING_CUSTOMER": {
        "READY_TO_START",
        "CANCELLED",
    },

    "READY_TO_START": {
        "IN_PROGRESS",
        "CANCELLED",
    },

    "IN_PROGRESS": {
        "QUALITY_REVIEW",
        "CANCELLED",
    },

    "QUALITY_REVIEW": {
        "READY_FOR_DELIVERY",
        "IN_PROGRESS",
        "CANCELLED",
    },

    "READY_FOR_DELIVERY": {
        "COMPLETED",
        "IN_PROGRESS",
        "CANCELLED",
    },

    "COMPLETED": set(),
    "CANCELLED": set(),
}


def validate_order_transition(
    current_status: str,
    next_status: str,
):

    if current_status not in ORDER_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="حالة الطلب الحالية غير صالحة",
        )

    if next_status not in ORDER_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="حالة الطلب الجديدة غير صالحة",
        )

    if next_status not in ORDER_TRANSITIONS[
        current_status
    ]:

        raise HTTPException(
            status_code=409,
            detail=(
                f"لا يمكن نقل الطلب من "
                f"{current_status} إلى "
                f"{next_status}"
            ),
        )


def touch_order(
    conn,
    order_id: str,
):

    conn.execute(
        """
        UPDATE orders
        SET updated_at=CURRENT_TIMESTAMP
        WHERE order_id=%s
        """,
        (order_id,),
    )


# =========================================================
# Payment helpers
# =========================================================

def payment_totals(
    conn,
    order_id: str,
) -> dict:

    row = conn.execute(
        """
        SELECT
            COALESCE(
                SUM(
                    CASE
                        WHEN amount > 0
                        THEN amount
                        ELSE 0
                    END
                ),
                0
            ) AS paid,

            COALESCE(
                SUM(
                    CASE
                        WHEN amount < 0
                        THEN -amount
                        ELSE 0
                    END
                ),
                0
            ) AS refunded

        FROM payments

        WHERE order_id=%s
        """,
        (order_id,),
    ).fetchone()

    return {
        "paid": row["paid"],
        "refunded": row["refunded"],
    }


def derive_payment_status(
    price,
    paid,
    refunded,
) -> str:

    net_paid = paid - refunded

    if refunded > 0 and net_paid <= 0:
        return "REFUNDED"

    if net_paid <= 0:
        return "UNPAID"

    if price > 0 and net_paid >= price:
        return "PAID"

    return "PARTIALLY_PAID"


def refresh_payment_summary(
    conn,
    order_id: str,
) -> dict:

    order = conn.execute(
        """
        SELECT price
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

    totals = payment_totals(
        conn,
        order_id,
    )

    paid = (
        totals["paid"]
        - totals["refunded"]
    )

    remaining = max(
        order["price"] - paid,
        0,
    )

    status = derive_payment_status(
        order["price"],
        totals["paid"],
        totals["refunded"],
    )

    conn.execute(
        """
        UPDATE orders
        SET
            payment_status=%s,
            deposit=%s,
            remaining_balance=%s,
            updated_at=CURRENT_TIMESTAMP
        WHERE order_id=%s
        """,
        (
            status,
            paid,
            remaining,
            order_id,
        ),
    )

    return {
        "payment_status": status,
        "paid": paid,
        "remaining": remaining,
    }


def set_channel_delivery_status_safely(
    order_id: str,
    status: str,
):

    try:

        with db() as conn:

            conn.execute(
                """
                UPDATE orders
                SET
                    channel_delivery_status=%s,
                    updated_at=CURRENT_TIMESTAMP
                WHERE order_id=%s
                """,
                (
                    status,
                    order_id,
                ),
            )

    except Exception:

        logger.exception(
            "Could not update delivery status for %s",
            order_id,
        )


# =========================================================
# Telegram Mini App authentication
# =========================================================

def verify_init_data(
    init_data: str,
) -> dict:

    if not init_data:

        raise HTTPException(
            status_code=401,
            detail=(
                "افتح تطبيق النبع من داخل Telegram"
            ),
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
            detail=(
                "بيانات Telegram غير صالحة"
            ),
        )

    received_hash = pairs.pop(
        "hash",
        None,
    )

    if not received_hash:

        raise HTTPException(
            status_code=401,
            detail=(
                "تعذر التحقق من جلسة Telegram"
            ),
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
            detail=(
                "جلسة Telegram غير موثوقة"
            ),
        )

    try:

        user = json.loads(
            pairs.get(
                "user",
                "{}",
            )
        )

        user_id = int(
            user["id"]
        )

        auth_date = int(
            pairs.get(
                "auth_date",
                "0",
            )
        )

    except (
        ValueError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
    ):

        raise HTTPException(
            status_code=401,
            detail=(
                "بيانات مستخدم Telegram غير صالحة"
            ),
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
            detail=(
                "وقت جلسة Telegram غير صالح"
            ),
        )

    if now - auth_date > INIT_DATA_MAX_AGE:

        raise HTTPException(
            status_code=401,
            detail=(
                "انتهت صلاحية جلسة Telegram، "
                "أعد فتح التطبيق"
            ),
        )

    return {
        "id": user_id,
        "username": (
            user.get("username", "")
            or ""
        ),
        "first_name": (
            user.get("first_name", "")
            or ""
        ),
        "last_name": (
            user.get("last_name", "")
            or ""
        ),
    }


def init_user_from_header(
    request: Request,
) -> dict:

    return verify_init_data(
        request.headers.get(
            "X-Telegram-Init-Data",
            "",
        )
    )


# =========================================================
# ADMIN AUTHORIZATION
# =========================================================

def require_admin(
    request: Request,
) -> dict:

    user = init_user_from_header(
        request
    )

    if user["id"] != ADMIN_TELEGRAM_ID:

        raise HTTPException(
            status_code=403,
            detail=(
                "غير مصرح لك بالوصول إلى لوحة التحكم"
            ),
        )

    return user


# =========================================================
# Order model
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

    @field_validator(
        "service_details"
    )
    @classmethod
    def validate_service_details(
        cls,
        value: dict[str, str],
    ) -> dict[str, str]:

        if len(value) > 20:

            raise ValueError(
                "تفاصيل الخدمة كثيرة جدًا"
            )

        for key, item in value.items():

            if len(str(key)) > 80:

                raise ValueError(
                    "اسم حقل الخدمة طويل جدًا"
                )

            if len(str(item)) > 1000:

                raise ValueError(
                    "قيمة تفاصيل الخدمة طويلة جدًا"
                )

        encoded = json.dumps(
            value,
            ensure_ascii=False,
        )

        if len(encoded) > 6000:

            raise ValueError(
                "تفاصيل الخدمة تتجاوز الحد المسموح"
            )

        return value

    @field_validator(
        "deadline"
    )
    @classmethod
    def validate_deadline(
        cls,
        value: str,
    ) -> str:

        try:

            datetime.fromisoformat(
                value
            )

        except ValueError:

            raise ValueError(
                "Invalid deadline"
            )

        return value


# =========================================================
# Idempotency
# =========================================================

def order_payload_fingerprint(
    data: OrderIn,
) -> str:

    payload = data.model_dump(
        mode="json"
    )

    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    return hashlib.sha256(
        serialized.encode("utf-8")
    ).hexdigest()


def validate_idempotency_key(
    key: str,
) -> str:

    key = (key or "").strip()

    if not key:

        raise HTTPException(
            status_code=400,
            detail="Idempotency-Key مفقود",
        )

    if len(key) > 200:

        raise HTTPException(
            status_code=400,
            detail="Idempotency-Key طويل جدًا",
        )

    if not re.fullmatch(
        r"[A-Za-z0-9._:-]+",
        key,
    ):

        raise HTTPException(
            status_code=400,
            detail="Idempotency-Key غير صالح",
        )

    return key


# =========================================================
# Order helpers
# =========================================================

def new_order_id() -> str:

    year = datetime.now().year

    random_part = "".join(
        secrets.choice(string.digits)
        for _ in range(6)
    )

    return f"NB-{year}-{random_part}"


def get_attachment(
    user_id: int,
    token: str,
):

    with db() as conn:

        row = conn.execute(
            """
            SELECT
                token,
                user_id,
                filename,
                content_type,
                data,
                created_at,
                order_id,
                attached_at
            FROM attachments
            WHERE token=%s
              AND user_id=%s
            """,
            (
                token,
                user_id,
            ),
        ).fetchone()

    return row


def create_order(
    user: dict,
    data: OrderIn,
    idempotency_key: str,
):

    fingerprint = order_payload_fingerprint(
        data
    )

    # =====================================================
    # Existing idempotent request
    # =====================================================

    with db() as conn:

        existing = conn.execute(
            """
            SELECT *
            FROM orders
            WHERE user_id=%s
              AND idempotency_key=%s
            """,
            (
                user["id"],
                idempotency_key,
            ),
        ).fetchone()

        if existing:

            stored_fingerprint = (
                existing.get(
                    "idempotency_fingerprint"
                )
                if hasattr(
                    existing,
                    "get",
                )
                else None
            )

            # Preferred comparison
            if stored_fingerprint:

                if (
                    stored_fingerprint
                    != fingerprint
                ):

                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "تم استخدام "
                            "Idempotency-Key "
                            "مع طلب مختلف"
                        ),
                    )

            else:

                # Compatibility fallback for an order
                # created before fingerprint storage.
                existing_attachment = (
                    conn.execute(
                        """
                        SELECT token
                        FROM attachments
                        WHERE order_id=%s
                        ORDER BY
                            attached_at NULLS LAST,
                            created_at
                        LIMIT 1
                        """,
                        (
                            existing["order_id"],
                        ),
                    ).fetchone()
                )

                existing_payload = OrderIn(
                    service_type=existing[
                        "service_type"
                    ],

                    department=existing[
                        "department"
                    ],

                    title=existing[
                        "title"
                    ],

                    page_count=(
                        ""
                        if existing[
                            "page_count"
                        ] == "غير محدد"
                        else existing[
                            "page_count"
                        ]
                    ),

                    language=(
                        ""
                        if existing[
                            "language"
                        ] == "غير محدد"
                        else existing[
                            "language"
                        ]
                    ),

                    autocad_type=(
                        ""
                        if existing[
                            "autocad_type"
                        ] == "غير محدد"
                        else existing[
                            "autocad_type"
                        ]
                    ),

                    deadline=existing[
                        "deadline"
                    ],

                    notes=(
                        ""
                        if existing[
                            "notes"
                        ] == "لا توجد ملاحظات"
                        else existing[
                            "notes"
                        ]
                    ),

                    attachment_token=(
                        existing_attachment[
                            "token"
                        ]
                        if existing_attachment
                        else ""
                    ),

                    service_details=(
                        existing.get(
                            "service_details"
                        )
                        or {}
                    ),
                )

                if (
                    order_payload_fingerprint(
                        existing_payload
                    )
                    != fingerprint
                ):

                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "تم استخدام "
                            "Idempotency-Key "
                            "مع طلب مختلف"
                        ),
                    )

                # Backfill fingerprint for this order.
                try:

                    conn.execute(
                        """
                        UPDATE orders
                        SET
                            idempotency_fingerprint=%s
                        WHERE order_id=%s
                        """,
                        (
                            fingerprint,
                            existing["order_id"],
                        ),
                    )

                except Exception:

                    logger.warning(
                        "Could not backfill "
                        "idempotency fingerprint "
                        "for %s",
                        existing["order_id"],
                        exc_info=True,
                    )

            attachment = None

            if data.attachment_token:

                attachment = conn.execute(
                    """
                    SELECT *
                    FROM attachments
                    WHERE token=%s
                      AND user_id=%s
                      AND order_id=%s
                    """,
                    (
                        data.attachment_token,
                        user["id"],
                        existing["order_id"],
                    ),
                ).fetchone()

            return (
                existing,
                attachment,
                False,
            )

    # =====================================================
    # New order
    # =====================================================

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
                            order_id,
                            attached_at
                        FROM attachments
                        WHERE token=%s
                          AND user_id=%s
                          AND order_id IS NULL
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
                                "المرفق غير موجود "
                                "أو انتهت صلاحيته، "
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
                        idempotency_fingerprint,
                        updated_at
                    )
                    VALUES (
                        %s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,%s::jsonb,
                        %s,%s,CURRENT_TIMESTAMP
                    )
                    RETURNING *
                    """,
                    (
                        order_id,
                        user["id"],
                        (
                            user["username"]
                            or user["first_name"]
                            or ""
                        ),
                        data.service_type,
                        SERVICES[
                            data.service_type
                        ],
                        data.department,
                        data.title,
                        (
                            data.page_count
                            or "غير محدد"
                        ),
                        (
                            data.language
                            or "غير محدد"
                        ),
                        (
                            data.autocad_type
                            or "غير محدد"
                        ),
                        data.deadline,
                        (
                            data.notes
                            or "لا توجد ملاحظات"
                        ),
                        json.dumps(
                            data.service_details,
                            ensure_ascii=False,
                        ),
                        idempotency_key,
                        fingerprint,
                    ),
                ).fetchone()

                # Keep payment summary consistent.
                refresh_payment_summary(
                    conn,
                    order_id,
                )

                # Attach file inside the same transaction.
                if attachment:

                    conn.execute(
                        """
                        UPDATE attachments
                        SET
                            order_id=%s,
                            attached_at=CURRENT_TIMESTAMP
                        WHERE token=%s
                          AND user_id=%s
                          AND order_id IS NULL
                        """,
                        (
                            order_id,
                            attachment["token"],
                            user["id"],
                        ),
                    )

                    attachment["order_id"] = (
                        order_id
                    )

                    attachment["attached_at"] = (
                        datetime.now(timezone.utc)
                    )

                log_audit_safely(
                    conn,
                    order_id,
                    "ORDER_CREATED",
                    f"User_{user['id']}",
                )

            return (
                order,
                attachment,
                True,
            )

        except psycopg.errors.UniqueViolation as exc:

            if (
                exc.diag.constraint_name
                == "orders_user_id_idempotency_key_uniq"
            ):

                return create_order(
                    user,
                    data,
                    idempotency_key,
                )

            # order_id collision
            if (
                exc.diag.constraint_name
                == "orders_pkey"
            ):
                continue

            raise

    raise HTTPException(
        status_code=500,
        detail=(
            "تعذر إنشاء رقم الطلب، "
            "حاول مرة أخرى"
        ),
    )


# =========================================================
# Attachment storage
# =========================================================

def store_attachment(
    user_id: int,
    filename: str,
    content_type: str,
    data: bytes,
) -> str:

    token = secrets.token_urlsafe(24)

    with db() as conn:

        # Only delete old temporary/unlinked files.
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

        if pending >= MAX_PENDING_ATTACHMENTS:

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
                data
            )
            VALUES(%s,%s,%s,%s,%s)
            """,
            (
                token,
                user_id,
                filename,
                content_type,
                data,
            ),
        )

    return token


# =========================================================
# Telegram messages
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

        f"🛠️ <b>الخدمة:</b> "
        f"{e(order['service_name'])}\n"

        f"🏫 <b>التخصص:</b> "
        f"{e(order['department'])}\n"

        f"📌 <b>العنوان:</b> "
        f"{e(order['title'])}\n"

        f"📄 <b>الصفحات:</b> "
        f"{e(order['page_count'])}\n"

        f"🌐 <b>اللغة:</b> "
        f"{e(order['language'])}\n"

        f"⏰ <b>الموعد:</b> "
        f"{e(order['deadline'])}\n"
    )

    if order["service_type"] == "autocad":

        text += (
            f"📐 <b>نوع الرسم:</b> "
            f"{e(order['autocad_type'])}\n"
        )

    service_details = (
        order.get("service_details")
        or {}
    )

    if isinstance(
        service_details,
        str,
    ):

        try:

            service_details = json.loads(
                service_details
            )

        except Exception:

            service_details = {}

    if service_details:

        details_text = (
            "\n🎯 <b>تفاصيل الخدمة:</b>\n"
        )

        for key, value in service_details.items():

            label = SERVICE_DETAIL_LABELS.get(
                key,
                str(key)
                .replace("_", " ")
                .strip(),
            )

            details_text += (
                f"• <b>{e(label)}:</b> "
                f"{e(str(value))}\n"
            )

        text += details_text

    notes = (
        order["notes"]
        or "لا توجد ملاحظات"
    )

    available = max(
        100,
        3800 - len(text),
    )

    text += (
        "📝 <b>الملاحظات:</b> "
        + e(notes[:available])
    )

    return text


# =========================================================
# Telegram delivery
# =========================================================

async def send_attachment_to_chat(
    chat_id,
    order_id: str,
    attachment,
):

    if not attachment:
        return

    data = bytes(
        attachment["data"]
    )

    filename = (
        attachment["filename"]
    )

    caption = (
        f"📎 مرفق الطلب {order_id}"
    )

    content_type = (
        attachment["content_type"]
        or ""
    )

    if content_type.startswith(
        "image/"
    ):

        try:

            await telegram_app.bot.send_photo(
                chat_id=chat_id,
                photo=data,
                caption=caption,
            )

            return

        except TelegramError:

            logger.warning(
                "send_photo failed; "
                "trying document",
                exc_info=True,
            )

    await telegram_app.bot.send_document(
        chat_id=chat_id,
        document=data,
        filename=filename,
        caption=caption,
    )


async def send_to_chat(
    chat_id,
    text: str,
    order_id: str,
    attachment,
    text_already_sent: bool = False,
):

    # =====================================================
    # 1. Send text only when it has not already been sent.
    # =====================================================

    if not text_already_sent:

        await telegram_app.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
        )

        set_channel_delivery_status_safely(
            order_id,
            "TEXT_SENT",
        )

    # =====================================================
    # 2. No attachment
    # =====================================================

    if not attachment:

        set_channel_delivery_status_safely(
            order_id,
            "DELIVERED",
        )

        return

    # =====================================================
    # 3. Send attachment
    # =====================================================

    await send_attachment_to_chat(
        chat_id,
        order_id,
        attachment,
    )

    set_channel_delivery_status_safely(
        order_id,
        "DELIVERED",
    )


async def deliver_order(
    order: dict,
    user: dict,
    attachment,
) -> bool:

    message = build_order_message(
        order,
        user,
    )

    channel = parse_channel_id()

    if channel is None:

        logger.error(
            "No Telegram channel configured"
        )

        set_channel_delivery_status_safely(
            order["order_id"],
            "FAILED",
        )

        log_audit_for_order_safely(
            order["order_id"],
            "ORDER_DELIVERY_TO_CHANNEL_FAILED",
            "Telegram_Bot",
        )

        return False

    current_status = (
        order.get(
            "channel_delivery_status"
        )
        or "PENDING"
    )

    text_already_sent = (
        current_status == "TEXT_SENT"
    )

    # If already delivered, nothing to do.
    if current_status == "DELIVERED":
        return True

    # Mark delivery attempt as pending.
    if not text_already_sent:

        set_channel_delivery_status_safely(
            order["order_id"],
            "PENDING",
        )

    try:

        await send_to_chat(
            channel,
            message,
            order["order_id"],
            attachment,
            text_already_sent=text_already_sent,
        )

    except TelegramError:

        logger.exception(
            "Failed to deliver order %s to channel",
            order["order_id"],
        )

        set_channel_delivery_status_safely(
            order["order_id"],
            "FAILED",
        )

        log_audit_for_order_safely(
            order["order_id"],
            "ORDER_DELIVERY_TO_CHANNEL_FAILED",
            "Telegram_Bot",
        )

        return False

    except Exception:

        logger.exception(
            "Unexpected delivery error for order %s",
            order["order_id"],
        )

        set_channel_delivery_status_safely(
            order["order_id"],
            "FAILED",
        )

        log_audit_for_order_safely(
            order["order_id"],
            "ORDER_DELIVERY_TO_CHANNEL_FAILED",
            "Telegram_Bot",
        )

        return False

    set_channel_delivery_status_safely(
        order["order_id"],
        "DELIVERED",
    )

    log_audit_for_order_safely(
        order["order_id"],
        "ORDER_DELIVERED_TO_CHANNEL",
        "Telegram_Bot",
    )

    logger.info(
        "Order %s delivered to channel",
        order["order_id"],
    )

    return True


# =========================================================
# Bot commands
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

        return (
            False,
            "CHANNEL_ID غير مضبوط.",
        )

    try:

        chat = await telegram_app.bot.get_chat(
            channel
        )

        me = await telegram_app.bot.get_me()

        member = (
            await telegram_app.bot.get_chat_member(
                channel,
                me.id,
            )
        )

        if member.status not in (
            "administrator",
            "creator",
        ):

            return (
                False,
                (
                    f"البوت موجود في "
                    f"«{chat.title}» "
                    f"لكن حالته "
                    f"«{member.status}». "
                    "يجب أن يكون Administrator."
                ),
            )

        if getattr(
            member,
            "can_post_messages",
            None,
        ) is False:

            return (
                False,
                (
                    f"البوت Admin في "
                    f"«{chat.title}» "
                    "لكن صلاحية النشر غير مفعلة."
                ),
            )

        if send_test:

            await telegram_app.bot.send_message(
                chat_id=channel,
                text="✅ رسالة اختبار من بوت النبع",
            )

        return (
            True,
            (
                f"القناة «{chat.title}» جاهزة. "
                f"البوت @{me.username} قادر على النشر."
            ),
        )

    except TelegramError as exc:

        return (
            False,
            f"{type(exc).__name__}: {exc}",
        )


async def testchannel_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.effective_user:
        return

    if (
        update.effective_user.id
        != ADMIN_TELEGRAM_ID
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


async def camera_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.effective_user:
        return

    if (
        update.effective_user.id
        not in PHOTO_ALLOWED_IDS
    ):
        return

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📸 فتح الكاميرا / المعرض",
                    web_app=WebAppInfo(
                        url=CAMERA_URL
                    ),
                )
            ]
        ]
    )

    await update.effective_message.reply_text(
        (
            "اضغط الزر لالتقاط صورة "
            "أو اختيارها ونشرها في القناة:"
        ),
        reply_markup=keyboard,
    )


# =========================================================
# Telegram application
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
        "camera",
        camera_command,
    )
)

telegram_app.add_handler(
    MessageHandler(
        filters.ChatType.PRIVATE
        & filters.TEXT
        & ~filters.COMMAND,
        start_command,
    )
)


# =========================================================
# FastAPI lifespan
# =========================================================

@asynccontextmanager
async def lifespan(
    _: FastAPI,
):

    await init_db_with_retry()

    await telegram_app.initialize()

    try:

        await telegram_app.bot.set_webhook(
            url=WEBHOOK_URL,
            secret_token=WEBHOOK_SECRET,
            allowed_updates=[
                "message"
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

    try:

        ok, info = await channel_diagnosis(
            send_test=False
        )

        if ok:

            logger.info(
                "Channel diagnosis: %s",
                info,
            )

        else:

            logger.error(
                "Channel diagnosis: %s",
                info,
            )

    except Exception:

        logger.exception(
            "Channel diagnosis failed"
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
    version="5.1.0",
    lifespan=lifespan,
)


# =========================================================
# CORS
# =========================================================

allowed_origins = {
    origin_of(WEB_APP_URL),
    origin_of(BACKEND_URL),
}

extra_origins = os.getenv(
    "ALLOWED_ORIGINS",
    "",
)

for origin in extra_origins.split(","):

    origin = (
        origin.strip()
        .rstrip("/")
    )

    if origin:
        allowed_origins.add(
            origin
        )


app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(
        allowed_origins
    ),
    allow_credentials=False,
    allow_methods=[
        "GET",
        "POST",
        "OPTIONS",
    ],
    allow_headers=[
        "Content-Type",
        "X-Telegram-Init-Data",
        "Idempotency-Key",
    ],
)


# =========================================================
# Root
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


# =========================================================
# Health
# =========================================================

@app.get("/health")
def health():

    try:

        with db() as conn:

            conn.execute(
                "SELECT 1"
            )

        return {
            "status": "ok",
            "database": "connected",
            "telegram": bool(BOT_TOKEN),
            "webhook": WEBHOOK_URL,
        }

    except Exception:

        logger.exception(
            "Health check failed"
        )

        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "database": "unavailable",
            },
        )


# =========================================================
# Telegram webhook
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

    except Exception:

        raise HTTPException(
            status_code=400,
            detail="Invalid JSON",
        )

    try:

        update = Update.de_json(
            body,
            telegram_app.bot,
        )

    except Exception:

        logger.exception(
            "Invalid Telegram update"
        )

        raise HTTPException(
            status_code=400,
            detail="Bad Telegram update",
        )

    try:

        await telegram_app.process_update(
            update
        )

    except Exception:

        logger.exception(
            "Failed to process Telegram update"
        )

    return {
        "ok": True
    }


# =========================================================
# File validation
# =========================================================

DOCUMENT_SIGNATURES = {
    "application/pdf": b"%PDF",

    "application/msword":
        b"\xd0\xcf\x11\xe0",

    "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        b"PK\x03\x04",
}


def validate_file(
    data: bytes,
    declared_type: str,
) -> str:

    if declared_type.startswith(
        "image/"
    ):

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
                detail=(
                    "صيغة الصورة غير مدعومة"
                ),
            )

        return mime

    signature = DOCUMENT_SIGNATURES.get(
        declared_type
    )

    if not signature:

        raise HTTPException(
            status_code=400,
            detail=(
                "نوع الملف غير مدعوم. "
                "المسموح PDF / DOC / DOCX / صور."
            ),
        )

    if not data.startswith(
        signature
    ):

        raise HTTPException(
            status_code=400,
            detail=(
                "محتوى الملف لا يطابق نوعه"
            ),
        )

    return declared_type


# =========================================================
# Upload attachment
# =========================================================

@app.post("/api/upload")
async def upload_attachment(
    request: Request,
    file: UploadFile = File(...),
):

    user = init_user_from_header(
        request
    )

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

    declared_type = (
        file.content_type
        or ""
    )

    content_type = validate_file(
        data,
        declared_type,
    )

    filename = os.path.basename(
        file.filename or ""
    ).strip()

    filename = filename[:100]

    if not filename:
        filename = "attachment"

    token = await asyncio.to_thread(
        store_attachment,
        user["id"],
        filename,
        content_type,
        data,
    )

    logger.info(
        "Attachment uploaded by user %s: %s",
        user["id"],
        filename,
    )

    return {
        "token": token,
        "filename": filename,
    }


# =========================================================
# Photo processing
# =========================================================

def prepare_photo(
    data: bytes,
) -> bytes:

    try:

        with Image.open(
            io.BytesIO(data)
        ) as img:

            img = ImageOps.exif_transpose(
                img
            )

            img = img.convert(
                "RGB"
            )

            img.thumbnail(
                (2560, 2560)
            )

            output = io.BytesIO()

            img.save(
                output,
                "JPEG",
                quality=85,
                optimize=True,
            )

            return output.getvalue()

    except Exception:

        raise HTTPException(
            status_code=400,
            detail=(
                "الصورة تالفة أو صيغتها غير مدعومة"
            ),
        )


# =========================================================
# Camera/photo endpoint
# =========================================================

@app.post("/api/photo")
async def publish_photo(
    request: Request,
    file: UploadFile = File(...),
):

    user = init_user_from_header(
        request
    )

    if user["id"] not in PHOTO_ALLOWED_IDS:

        raise HTTPException(
            status_code=403,
            detail="غير مصرح لك بالنشر",
        )

    channel = parse_channel_id()

    if channel is None:

        raise HTTPException(
            status_code=503,
            detail="CHANNEL_ID غير مضبوط",
        )

    data = await file.read(
        MAX_ATTACHMENT_BYTES + 1
    )

    if not data:

        raise HTTPException(
            status_code=400,
            detail="الصورة فارغة",
        )

    if len(data) > MAX_ATTACHMENT_BYTES:

        raise HTTPException(
            status_code=413,
            detail=(
                f"حجم الصورة يجب ألا يتجاوز "
                f"{MAX_ATTACHMENT_MB} MB"
            ),
        )

    photo = await asyncio.to_thread(
        prepare_photo,
        data,
    )

    try:

        await telegram_app.bot.send_photo(
            chat_id=channel,
            photo=photo,
        )

    except TelegramError as exc:

        logger.exception(
            "Photo publishing failed"
        )

        raise HTTPException(
            status_code=502,
            detail=(
                f"تعذر النشر في القناة: {exc}"
            ),
        )

    return {
        "ok": True
    }


# =========================================================
# Submit order
# =========================================================

@app.post("/api/orders")
async def submit_order(
    payload: OrderIn,
    request: Request,
):

    user = init_user_from_header(
        request
    )

    idempotency_key = validate_idempotency_key(
        request.headers.get(
            "Idempotency-Key",
            "",
        )
    )

    order, attachment, created = (
        await asyncio.to_thread(
            create_order,
            user,
            payload,
            idempotency_key,
        )
    )

    # =====================================================
    # Existing order retry / new order
    # =====================================================

    current_delivery_status = (
        order.get(
            "channel_delivery_status"
        )
        or "PENDING"
    )

    if current_delivery_status == "DELIVERED":

        delivered = True

    else:

        delivered = await deliver_order(
            order,
            user,
            attachment,
        )

    if not delivered:

        logger.error(
            "Order %s saved but channel delivery failed",
            order["order_id"],
        )

        raise HTTPException(
            status_code=502,
            detail=(
                "تم حفظ الطلب، لكن تعذر إرساله "
                "إلى القناة. "
                "حاول مرة أخرى أو تواصل مع الإدارة."
            ),
        )

    # =====================================================
    # Do NOT delete linked attachment.
    # It belongs to the order now.
    # =====================================================

    # =====================================================
    # Notify customer
    # =====================================================

    if created:

        try:

            await telegram_app.bot.send_message(
                chat_id=user["id"],
                text=(
                    "✅ <b>تم استلام طلبك</b>\n\n"
                    f"رقم الطلب: "
                    f"<code>"
                    f"{html.escape(order['order_id'])}"
                    f"</code>\n"
                    "سيتم التواصل معك من قبل الكادر."
                ),
                parse_mode="HTML",
            )

        except TelegramError:

            logger.warning(
                "Could not notify user %s",
                user["id"],
                exc_info=True,
            )

    return {
        "ok": True,
        "order_id": order["order_id"],
        "delivered": True,
    }


# =========================================================
# ADMIN DASHBOARD
# =========================================================

@app.get("/api/admin/access")
def admin_access(
    request: Request,
):

    user = require_admin(
        request
    )

    return {
        "ok": True,
        "is_admin": True,
        "telegram_id": user["id"],
    }


@app.get("/api/admin/dashboard")
def admin_dashboard(
    request: Request,
):

    require_admin(
        request
    )

    with db() as conn:

        totals = conn.execute(
            """
            SELECT
                COUNT(*) AS total_orders,

                COUNT(*) FILTER (
                    WHERE created_at >= CURRENT_DATE
                ) AS today_orders,

                COUNT(*) FILTER (
                    WHERE created_at >=
                    date_trunc(
                        'month',
                        CURRENT_TIMESTAMP
                    )
                ) AS month_orders,

                COALESCE(
                    SUM(price),
                    0
                ) AS total_price,

                COALESCE(
                    SUM(deposit),
                    0
                ) AS total_deposit,

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
            ORDER BY count DESC, status ASC
            """
        ).fetchall()

        service_rows = conn.execute(
            """
            SELECT
                service_type,
                service_name,
                COUNT(*) AS count
            FROM orders
            GROUP BY
                service_type,
                service_name
            ORDER BY
                count DESC,
                service_type ASC
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
            ORDER BY count DESC, status ASC
            """
        ).fetchall()

        recent_rows = conn.execute(
            """
            SELECT
                order_id,
                user_id,
                username,
                service_type,
                service_name,
                department,
                title,
                order_status,
                payment_status,
                price,
                deposit,
                remaining_balance,
                created_at
            FROM orders
            ORDER BY created_at DESC
            LIMIT 20
            """
        ).fetchall()

        activity_rows = conn.execute(
            """
            SELECT
                id,
                order_id,
                action,
                performed_by,
                timestamp
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
        str(row["status"]):
            int(row["count"])
        for row in status_rows
    }

    new_count = status_counts.get(
        "NEW",
        0,
    )

    completed_count = status_counts.get(
        "COMPLETED",
        0,
    )

    cancelled_count = status_counts.get(
        "CANCELLED",
        0,
    )

    processing_count = sum(
        count
        for status, count
        in status_counts.items()
        if status not in {
            "NEW",
            "COMPLETED",
            "CANCELLED",
        }
    )

    recent_orders = []

    for row in recent_rows:

        recent_orders.append(
            {
                "order_id":
                    row["order_id"],

                "user_id":
                    row["user_id"],

                "username":
                    row["username"],

                "service_type":
                    row["service_type"],

                "service_name":
                    row["service_name"],

                "department":
                    row["department"],

                "title":
                    row["title"],

                "order_status":
                    row["order_status"],

                "payment_status":
                    row["payment_status"],

                "price":
                    money(row["price"]),

                "deposit":
                    money(row["deposit"]),

                "remaining_balance":
                    money(
                        row["remaining_balance"]
                    ),

                "created_at":
                    (
                        row["created_at"].isoformat()
                        if row["created_at"]
                        else None
                    ),
            }
        )

    activities = []

    for row in activity_rows:

        activities.append(
            {
                "id":
                    int(row["id"]),

                "order_id":
                    row["order_id"],

                "action":
                    row["action"],

                "performed_by":
                    row["performed_by"],

                "timestamp":
                    (
                        row["timestamp"].isoformat()
                        if row["timestamp"]
                        else None
                    ),
            }
        )

    return {
        "ok": True,

        "generated_at":
            datetime.now(
                timezone.utc
            ).isoformat(),

        "summary": {

            "total_orders":
                int(
                    totals["total_orders"]
                    or 0
                ),

            "today_orders":
                int(
                    totals["today_orders"]
                    or 0
                ),

            "month_orders":
                int(
                    totals["month_orders"]
                    or 0
                ),

            "new_orders":
                new_count,

            "processing_orders":
                processing_count,

            "completed_orders":
                completed_count,

            "cancelled_orders":
                cancelled_count,

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
            recent_orders,

        "activities":
            activities,
    }


# =========================================================
# Order status
# =========================================================

@app.get(
    "/api/orders/{order_id}"
)
def get_order_status(
    order_id: str,
    request: Request,
):

    user = init_user_from_header(
        request
    )

    with db() as conn:

        row = conn.execute(
            """
            SELECT
                order_id,
                service_name,
                title,
                order_status,
                payment_status,
                price,
                deposit,
                remaining_balance,
                created_at
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
# Run locally
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
