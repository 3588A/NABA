import base64
import datetime
import io
import json
import logging
import random
import sqlite3
import string
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from PIL import Image

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ==========================================
# 1. إعدادات النظام والمتغيرات الأساسية
# ==========================================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AlNabaaBot")

TOKEN = "8824895521:AAE4Q5ZJsrjVnQ3riMHJkr9Feiqmaffc2LU"
CHANNEL_ID = "@1003981054797"

# استبدل هذا بالرابط الذي حصلت عليه من GitHub Pages للـ Web App
WEB_APP_URL = "https://3588a.github.io/NABA/"

# إنشاء تطبيق FastAPI
app = FastAPI(title="النبع للخدمات الجامعية API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================
# 2. تهيئة قاعدة البيانات (SQLite)
# ==========================================
DB_FILE = "alnabaa_orders.db"


def init_db():
  conn = sqlite3.connect(DB_FILE)
  cursor = conn.cursor()
  # جدول الطلبات الرئيسية
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            user_id INTEGER,
            username TEXT,
            service_type TEXT,
            service_name TEXT,
            department TEXT,
            title TEXT,
            page_count TEXT,
            language TEXT,
            autocad_type TEXT,
            deadline TEXT,
            notes TEXT,
            order_status TEXT,
            payment_status TEXT,
            price REAL DEFAULT 0,
            deposit REAL DEFAULT 0,
            remaining_balance REAL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
  # جدول سجل العمليات Audit Log
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT,
            action TEXT,
            performed_by TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
  conn.commit()
  conn.close()


init_db()


def log_action(order_id: str, action: str, performed_by: str = "System"):
  conn = sqlite3.connect(DB_FILE)
  cursor = conn.cursor()
  cursor.execute(
      """
        INSERT INTO audit_logs (order_id, action, performed_by)
        VALUES (?, ?, ?)
    """,
      (order_id, action, performed_by),
  )
  conn.commit()
  conn.close()


# ==========================================
# 3. معالجة وتخزين الطلبات من التليجرام
# ==========================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
  keyboard = InlineKeyboardMarkup([[
      InlineKeyboardButton(
          "🚀 فتح تطبيق النبع للخدمات", web_app={"url": WEB_APP_URL}
      )
  ]])

  await update.message.reply_text(
      "أهلاً بك في **النبع للخدمات الجامعية** 🎓✨\n\n"
      "نساعدك في إنجاز كافة المتطلبات والأعمال الأكاديمية والهندسية بجودة عالية والتزام تام بالمواعيد!\n\n"
      "للبدء واختيار الخدمة المطلوبة، يرجى الضغط على الزر أدناه لفتح استمارة الطلب ⬇️",
      reply_markup=keyboard,
      parse_mode="Markdown",
  )


async def web_app_data_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
  try:
    user = update.message.from_user
    data = json.loads(update.message.web_app_data.data)

    order_id = data.get(
        "order_id",
        f"NB-2026-{''.join(random.choices(string.digits, k=6))}",
    )
    service_name = data.get("service_name", "خدمة غير محددة")
    department = data.get("department", "غير محدد")
    title = data.get("title", "بدون عنوان")
    deadline = data.get("deadline", "غير محدد")
    notes = data.get("notes", "لا توجد ملاحظات")
    attachment_b64 = data.get("attachment", "")

    # حفظ الطلب في قاعدة البيانات
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        """
            INSERT INTO orders (
                order_id, user_id, username, service_type, service_name,
                department, title, page_count, language, autocad_type,
                deadline, notes, order_status, payment_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'NEW', 'PENDING')
        """,
        (
            order_id,
            user.id,
            user.username or user.first_name,
            data.get("service_type", ""),
            service_name,
            department,
            title,
            data.get("page_count", "غير محدد"),
            data.get("language", "غير محدد"),
            data.get("autocad_type", "غير محدد"),
            deadline,
            notes,
        ),
    )
    conn.commit()
    conn.close()

    log_action(order_id, "ORDER_CREATED", f"User_{user.id}")

    # إرسال إشعار للآدمن/القناة
    admin_msg = (
        f"📥 **طلب جديد تم استلامه!**\n\n"
        f"🆔 **رقم الطلب:** `{order_id}`\n"
        f"👤 **الطالب:** {user.first_name} (@{user.username or 'بدون_يوزر'})\n"
        f"🛠️ **الخدمة:** {service_name}\n"
        f"🏫 **التخصص/الكلية:** {department}\n"
        f"📌 **العنوان:** {title}\n"
        f"⏰ **الموعد النهائي:** {deadline}\n"
        f"📝 **الملاحظات:** {notes}\n"
    )

    # إذا توفرت صورة مرفقة تُرسل للقناة
    if attachment_b64 and "," in attachment_b64:
      _, encoded = attachment_b64.split(",", 1)
      img_bytes = base64.b64decode(encoded)
      photo_io = io.BytesIO(img_bytes)
      photo_io.name = f"{order_id}_attachment.jpg"

      await context.bot.send_photo(
          chat_id=CHANNEL_ID, photo=photo_io, caption=admin_msg
      )
    else:
      await context.bot.send_message(
          chat_id=CHANNEL_ID, text=admin_msg, parse_mode="Markdown"
      )

    # رد أكيد للزبون
    await update.message.reply_text(
        f"✅ **تم استلام طلبك بنجاح!**\n\n"
        f"🆔 **رقم الطلب المرجعي:** `{order_id}`\n\n"
        f"سيقوم المشرف بمراجعة الطلب وإرسال التكلفة والعربون ورابط التأكيد لك فوراً عبر هذه المحادثة.",
        parse_mode="Markdown",
    )

  except Exception as e:
    logger.error(f"Error handling order: {e}")
    await update.message.reply_text(
        "❌ حدث خطأ أثناء معالجة البيانات، يرجى المحاولة مرة أخرى."
    )


# ==========================================
# 4. مسارات FastAPI واختبار النظام
# ==========================================
@app.get("/")
def read_root():
  return {"status": "Online", "system": "النبع للخدمات الجامعية API"}


@app.get("/api/orders/{order_id}")
def get_order_status(order_id: str):
  conn = sqlite3.connect(DB_FILE)
  cursor = conn.cursor()
  cursor.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,))
  row = cursor.fetchone()
  conn.close()

  if not row:
    raise HTTPException(status_code=404, detail="Order not found")

  return {
      "order_id": row[0],
      "service_name": row[4],
      "title": row[6],
      "order_status": row[12],
      "payment_status": row[13],
      "price": row[14],
      "deposit": row[15],
      "remaining_balance": row[16],
  }


# ==========================================
# 5. تشغيل البوت مع FastAPI
# ==========================================
telegram_app = ApplicationBuilder().token(TOKEN).build()
telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(
    MessageHandler(filters.StatusUpdate.WEB_APP_DATA, web_app_data_handler)
)


@app.on_event("startup")
async def startup_event():
  await telegram_app.initialize()
  await telegram_app.start()
  await telegram_app.updater.start_polling()


@app.on_event("shutdown")
async def shutdown_event():
  await telegram_app.updater.stop()
  await telegram_app.stop()


# لتشغيل السيرفر محلياً: uvicorn main:app --reload
