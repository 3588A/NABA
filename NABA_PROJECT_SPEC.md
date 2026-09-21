# NABA — Project Specification & Development Rules

## 1. Project Identity

Project name:

**النبع للخدمات الجامعية — NABA**

Repository:

`3588A/NABA`

Primary branch:

`main`

The project is an existing working Telegram Mini App / service-order management system.

The system currently consists of:

* Telegram Bot
* Telegram Mini App frontend
* FastAPI backend
* PostgreSQL database hosted on Neon
* GitHub repository
* Admin Dashboard
* Telegram channel for receiving full order details
* Customer private Telegram confirmation
* File attachments
* Audit logging

---

# 2. CRITICAL DEVELOPMENT RULE

This is an EXISTING WORKING SYSTEM.

Do NOT rebuild the application from scratch.

Do NOT remove existing working functionality.

Do NOT change working authentication, database connection, Telegram configuration, webhook behavior, attachment handling, or order creation unless explicitly required by this specification.

The priority is:

1. Preserve all existing functionality.
2. Add the new workflow.
3. Keep frontend and backend contracts consistent.
4. Avoid unnecessary database migrations.
5. Avoid duplicate tables or duplicate systems.
6. Avoid breaking existing API endpoints.
7. Make changes incrementally and safely.

Before modifying anything:

* Read this entire specification.
* Read the existing `index.html`.
* Read the existing `main.py`.
* Inspect the existing database initialization/schema.
* Understand existing API endpoints.
* Identify existing functionality before changing it.

Do not assume that an old implementation should be replaced merely because another implementation is technically possible.

---

# 3. CURRENT TECHNOLOGY

Frontend:

* HTML
* CSS
* JavaScript
* Arabic RTL
* Telegram Web App

Backend:

* Python
* FastAPI

Database:

* PostgreSQL
* Neon

Telegram:

* Telegram Web App
* Telegram Bot API

File handling:

* FastAPI upload endpoint
* Existing attachment system

Excel:

* Python `openpyxl` may be used for Excel generation/export.

Existing Python dependencies include:

```text
fastapi[standard]
uvicorn[standard]
python-multipart
pillow
python-telegram-bot
jinja2
python-dotenv
psycopg[binary]
```

Do not add dependencies unnecessarily.

If an additional dependency is required, explain why before introducing it.

---

# 4. EXISTING FRONTEND

The current frontend is an Arabic RTL Telegram Mini App.

Existing API base:

```text
https://naba-88f2c996.fastapicloud.dev
```

Existing Telegram channel:

```text
https://t.me/ALNABASURVICE
```

Existing admin Telegram ID:

```text
6931187332
```

The frontend already supports:

* Telegram Web App initialization
* Telegram init data
* Service selection
* Dynamic service fields
* File attachments
* `/api/upload`
* `/api/orders`
* `X-Telegram-Init-Data`
* Order success screen
* Admin access
* Admin dashboard
* Arabic RTL interface

Do not remove these functions.

---

# 5. EXISTING SERVICES

Current services include:

```python
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
```

Do not remove or rename existing service types unless explicitly required.

---

# 6. CURRENT ORDER CREATION

The customer creates an order through the existing Mini App.

The existing order data may include:

* Telegram user ID
* customer name
* service type
* department
* title
* page count
* language
* AutoCAD type
* deadline
* notes
* attachment token
* service-specific details
* creation timestamp
* order ID

Existing order creation must continue working.

The customer must continue receiving a short private Telegram confirmation after successful order creation.

Example:

```text
✅ تم استلام طلبك

رقم الطلب: NABA-20260921-0042
سيتم التواصل معك من قبل الكادر.
```

Do not remove this confirmation.

---

# 7. ORDER DELIVERY RULE

The complete order details and attachment must be sent to the Telegram CHANNEL only.

The admin private chat must NOT receive a duplicate full order automatically.

The customer receives only the short confirmation.

Therefore:

```text
Customer creates order
        |
        +----> Telegram Channel
        |       Full order details
        |       Attachment
        |
        +----> Customer private chat
                Short confirmation
```

Do not send the complete order to the admin private chat automatically.

---

# 8. NEW ORDER WORKFLOW

The new system must implement the following lifecycle.

## Stage 1 — New Orders

### 📨 الطلبات

These are newly submitted customer requests.

Admin reviews:

* Customer data
* Requested service
* Description
* Attachment
* Deadline
* Notes
* Service details

The order should not immediately become an active work item.

---

# 9. ADMIN REVIEW

After reviewing the order, the admin can define:

### Price

Example:

```text
50,000 IQD
```

### Delivery time/date

Example:

```text
25/09/2026
```

or an appropriate delivery date/time format.

### Admin note

Optional message to the customer.

The admin then has:

### 📤 إرسال العرض للزبون

This sends the customer a formal quotation/confirmation message.

---

# 10. CUSTOMER QUOTATION MESSAGE

The customer should receive a clear Telegram message containing:

```text
📋 تفاصيل طلبك

🆔 رقم الطلب:
NABA-20260921-0042

🛠 الخدمة:
تصميم AutoCAD

💰 السعر:
50,000 IQD

⏰ موعد التسليم:
25/09/2026

📝 ملاحظات:
...
```

Then buttons:

```text
✅ تأكيد الطلب
❌ رفض الطلب
```

The buttons must be linked to the specific order.

The customer must not be able to confirm or reject another customer's order.

Authentication and order ownership must be verified.

---

# 11. PENDING ORDERS

After the quotation is sent to the customer, the order moves automatically to:

### ⏳ الطلبات المعلقة

Meaning:

The quotation has been sent, but the customer has not yet accepted or rejected it.

The admin dashboard should show:

* Order ID
* Customer
* Service
* Price
* Delivery time
* Sent time
* Current waiting status

Example:

```text
NABA-20260921-0042
AutoCAD
50,000 IQD
Delivery: 25/09/2026
Status: Waiting for customer
```

---

# 12. CUSTOMER ACCEPTANCE

If the customer presses:

### ✅ تأكيد الطلب

The system must:

1. Verify the customer owns the order.
2. Verify that the order is currently waiting for customer decision.
3. Record the acceptance.
4. Record acceptance timestamp.
5. Add an audit log entry.
6. Change order workflow status.
7. Move the order to:

### ⚙️ الأعمال

The customer should receive a confirmation that the order has been accepted and work can begin.

Do not allow repeated acceptance to create duplicate actions.

---

# 13. CUSTOMER REJECTION

If the customer presses:

### ❌ رفض الطلب

The system must:

1. Verify the customer owns the order.
2. Verify current order state.
3. Record rejection.
4. Record rejection timestamp.
5. Optionally allow a rejection reason.
6. Add an audit log entry.
7. Move the order to:

### ❌ الملغاة

The order must not remain in pending orders after rejection.

---

# 14. WORK ORDERS

Accepted orders move to:

### ⚙️ الأعمال

Suggested execution statuses:

```text
READY_TO_START
IN_PROGRESS
UNDER_REVIEW
READY_FOR_DELIVERY
COMPLETED
```

UI labels:

```text
⚙️ جاهز للمباشرة
🔨 قيد التنفيذ
🔍 قيد المراجعة
📤 جاهز للتسليم
✅ مكتمل
```

The admin can update the execution status.

Each status change should create an audit log entry.

---

# 15. CANCELLED ORDERS

### ❌ الملغاة

Contains orders rejected by the customer or cancelled by the admin according to the system rules.

Store:

* cancellation reason
* who cancelled/rejected
* timestamp

Do not delete cancelled orders from the database.

Historical data must remain available.

---

# 16. IMPORTANT: SEPARATE ORDER STATUS FROM CUSTOMER DECISION

Do NOT use one field for everything.

The system should conceptually separate:

### Workflow status

Example:

```text
NEW
UNDER_REVIEW
WAITING_CUSTOMER
READY_TO_START
IN_PROGRESS
UNDER_REVIEW
READY_FOR_DELIVERY
COMPLETED
CANCELLED
```

### Customer decision

```text
WAITING
ACCEPTED
REJECTED
```

### Payment status

```text
UNPAID
PARTIALLY_PAID
PAID
REFUNDED
```

This separation is important.

For example:

```text
order_status = IN_PROGRESS
customer_decision = ACCEPTED
payment_status = PARTIALLY_PAID
```

is valid.

Do not combine these into a single status field.

---

# 17. PRICE MANAGEMENT

Admin must be able to set or update the order price.

Example:

```text
💰 السعر

50,000 IQD
```

The price should be stored in PostgreSQL.

Do not rely on Excel as the primary source.

---

# 18. PAYMENT MANAGEMENT

Payments are manual.

There is NO automatic payment gateway at this stage.

Admin should be able to record payments against a specific order.

A payment should contain conceptually:

```text
payment_id
order_id
amount
currency
payment_method
note
performed_by
created_at
```

Example:

```text
Total price: 50,000 IQD
Paid: 20,000 IQD
Remaining: 30,000 IQD
```

If another payment of 10,000 is received:

```text
Total price: 50,000 IQD
Paid: 30,000 IQD
Remaining: 20,000 IQD
```

Do not overwrite previous payments.

Every payment should be a separate transaction record.

---

# 19. REMAINING BALANCE

Remaining balance should preferably be calculated:

```text
remaining = total_price - SUM(payments)
```

Do not create unnecessary duplicated fields if they can cause inconsistent financial data.

The database is the authoritative source.

---

# 20. DATABASE

The existing database already contains important tables including:

```text
orders
attachments
audit_logs
```

The existing `audit_logs` table must be reused.

Do NOT create another activity/history table.

If a payments table does not already exist, add:

```text
payments
```

Only if necessary.

Before creating it, inspect the existing schema.

Do not create duplicate tables.

---

# 21. EXISTING AUDIT LOG

Existing audit table:

```sql
CREATE TABLE IF NOT EXISTS audit_logs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id TEXT NOT NULL
        REFERENCES orders(order_id)
        ON DELETE CASCADE,
    action TEXT NOT NULL,
    performed_by TEXT NOT NULL DEFAULT 'System',
    timestamp TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
)
```

Reuse this system.

Examples of real audit actions:

```text
ORDER_CREATED
ORDER_DELIVERED_TO_CHANNEL
PRICE_SET
PRICE_UPDATED
QUOTE_SENT_TO_CUSTOMER
CUSTOMER_ACCEPTED_ORDER
CUSTOMER_REJECTED_ORDER
PAYMENT_RECORDED
ORDER_STATUS_CHANGED
ORDER_CANCELLED
ORDER_COMPLETED
```

Do not fabricate activity records.

Only log actions that actually happened.

---

# 22. ADMIN SEARCH

Admin must have:

### 🔎 البحث عن طلب

Input:

```text
NABA-20260921-0042
```

The system should return the complete order details.

Search result should include:

* Order ID
* Customer
* Service
* Department
* Title
* Deadline
* Price
* Total paid
* Remaining
* Payment status
* Customer decision
* Workflow status
* Notes
* Attachments
* Creation date
* Last update
* Recent activity

---

# 23. ADMIN ORDER DETAILS

Order details should have clear action buttons:

```text
💰 تعديل السعر
⏰ تعديل وقت التسليم
📤 إرسال العرض للزبون
💵 تسجيل دفعة
📌 تغيير الحالة
📝 إضافة ملاحظة
📎 المرفقات
📜 سجل الطلب
```

Only show actions that are valid for the current order state.

For example, an already completed order should not have a "Send quotation" action.

---

# 24. ADMIN DASHBOARD

Existing Admin Dashboard must be preserved and extended.

Main sections:

```text
📊 لوحة التحكم

📨 الطلبات الجديدة
⏳ الطلبات المعلقة
⚙️ الأعمال
🔍 قيد المراجعة
📤 جاهز للتسليم
✅ المكتملة
❌ الملغاة
```

Dashboard statistics should be based on real PostgreSQL data.

Do not use hardcoded numbers.

---

# 25. DASHBOARD FINANCIAL STATISTICS

Where appropriate, display:

```text
إجمالي الطلبات
طلبات اليوم
طلبات الشهر
طلبات جديدة
طلبات معلقة
طلبات قيد التنفيذ
طلبات مكتملة
طلبات ملغاة

إجمالي قيمة الطلبات
إجمالي المبالغ المستلمة
إجمالي المبالغ المتبقية
```

All values must come from PostgreSQL.

---

# 26. DASHBOARD ACTIVITY

Use the existing `audit_logs` table.

Display recent real activity.

Example:

```text
ORDER_CREATED
QUOTE_SENT_TO_CUSTOMER
CUSTOMER_ACCEPTED_ORDER
PAYMENT_RECORDED
ORDER_STATUS_CHANGED
```

Each activity should include:

* ID
* Order ID
* Action
* Performed by
* Timestamp

Do not create fake activity just to make the dashboard look populated.

---

# 27. EXCEL SYSTEM

Excel is NOT the primary database.

PostgreSQL is the source of truth.

Excel is a reporting/export mechanism.

Use ONE logical Excel workbook:

```text
NABA_Orders.xlsx
```

or a dated export such as:

```text
NABA_Orders_2026-09-21.xlsx
```

depending on implementation.

The workbook should be generated from PostgreSQL.

Do not depend on Excel for the operation of the application.

---

# 28. EXCEL WORKBOOK STRUCTURE

Recommended sheets:

## Sheet: Orders

Columns:

```text
Order ID
Customer
Telegram ID
Service
Department
Title
Order Status
Customer Decision
Payment Status
Price
Paid
Remaining
Deadline
Created At
Updated At
Notes
```

## Sheet: Payments

Columns:

```text
Payment ID
Order ID
Amount
Currency
Payment Method
Note
Performed By
Created At
```

## Sheet: Activity

Columns:

```text
Activity ID
Order ID
Action
Performed By
Timestamp
```

## Sheet: Summary

Include real statistics such as:

```text
Total Orders
Today's Orders
Pending Orders
Active Work
Completed Orders
Cancelled Orders
Total Sales
Total Paid
Total Remaining
```

Use appropriate formatting.

Do not hardcode values.

---

# 29. EXCEL EXPORT BUTTON

Admin Dashboard should contain:

### 📥 تصدير Excel

When clicked:

1. Verify admin authentication.
2. Query PostgreSQL.
3. Generate a fresh Excel workbook.
4. Populate all relevant sheets.
5. Save temporarily on the server.
6. Send the generated Excel file to the admin's private Telegram chat.
7. Use admin Telegram ID:

```text
6931187332
```

8. Clean up temporary files after successful/attempted delivery where appropriate.

Do not store sensitive Excel files permanently unless necessary.

---

# 30. TELEGRAM EXCEL DELIVERY

The generated Excel file should be sent to the admin's private Telegram account.

Possible caption:

```text
📊 تقرير طلبات NABA

تم إنشاء التقرير من قاعدة البيانات الحالية.

تاريخ التقرير: ...
```

The Excel file must be generated from current PostgreSQL data.

---

# 31. CUSTOMER EXPERIENCE

The customer should NOT see internal admin controls.

The customer should only see relevant order information and actions.

Customer quotation:

```text
📋 تفاصيل طلبك

🆔 رقم الطلب
🛠 الخدمة
💰 السعر
⏰ موعد التسليم
📝 الملاحظات

[✅ تأكيد الطلب]
[❌ رفض الطلب]
```

After confirmation:

```text
✅ تم تأكيد طلبك.
سيتم البدء بالعمل.
```

After rejection:

```text
❌ تم إلغاء الطلب بناءً على رفض العرض.
```

Keep customer messages concise and professional.

---

# 32. SECURITY

Never expose:

* BOT_TOKEN
* DATABASE_URL
* database password
* Telegram secrets
* environment variables

Do not hardcode secrets into frontend JavaScript.

Do not create or restore `.env` files in Git.

Existing `.gitignore` includes:

```text
.env
__pycache__/
*.db
*.sqlite3
```

Preserve it.

---

# 33. TELEGRAM AUTHENTICATION

Existing Telegram authentication must remain intact.

The frontend currently sends:

```text
X-Telegram-Init-Data
```

to the backend.

Do not bypass Telegram authentication.

For customer actions such as:

* accept quotation
* reject quotation

the backend must verify the authenticated Telegram user owns the order.

Do not trust a Telegram user ID supplied only in the frontend payload.

---

# 34. ADMIN AUTHENTICATION

Existing admin authentication must remain intact.

Current admin Telegram ID:

```text
6931187332
```

Existing endpoint:

```text
/api/admin/access
```

must remain compatible.

Existing endpoint:

```text
/api/admin/dashboard
```

must remain compatible unless extension is required.

Do not remove existing admin authorization.

---

# 35. API DESIGN

When adding endpoints, use clear REST-like routes.

Possible structure:

```text
GET  /api/admin/orders/{order_id}
POST /api/admin/orders/{order_id}/price
POST /api/admin/orders/{order_id}/deadline
POST /api/admin/orders/{order_id}/quote
POST /api/admin/orders/{order_id}/payment
POST /api/admin/orders/{order_id}/status
GET  /api/admin/orders/{order_id}/activity
GET  /api/admin/export/excel
```

Customer actions may use routes such as:

```text
POST /api/orders/{order_id}/accept
POST /api/orders/{order_id}/reject
```

Exact implementation may differ if existing project architecture requires another approach.

Do not create duplicate endpoints if an equivalent existing endpoint already exists.

---

# 36. API RESPONSE DESIGN

Responses should be predictable.

Success example:

```json
{
  "ok": true,
  "message": "..."
}
```

Error example:

```json
{
  "ok": false,
  "error": "..."
}
```

Do not expose internal exceptions, database credentials, stack traces, or secrets to users.

---

# 37. ORDER STATE TRANSITIONS

Valid state flow:

```text
NEW
  ↓
UNDER_REVIEW
  ↓
WAITING_CUSTOMER
  ├── ACCEPTED → READY_TO_START
  │                  ↓
  │              IN_PROGRESS
  │                  ↓
  │             UNDER_REVIEW
  │                  ↓
  │           READY_FOR_DELIVERY
  │                  ↓
  │              COMPLETED
  │
  └── REJECTED → CANCELLED
```

Admin cancellation may transition an order to:

```text
CANCELLED
```

Do not allow arbitrary invalid transitions without validation.

---

# 38. IDEMPOTENCY / DUPLICATE ACTIONS

Important:

Customer acceptance must not be processed twice.

Customer rejection must not be processed twice.

A customer must not be able to:

```text
Accept
then Reject
```

unless the business rules explicitly permit reopening.

For the initial implementation:

Once accepted or rejected, the customer decision is final.

Likewise:

* Do not record duplicate payments because of repeated requests.
* Do not send the same quotation multiple times accidentally.
* Do not create duplicate orders because an API request is retried.

Where practical, validate the current state before modifying it.

---

# 39. ORDER ID

Existing order ID generation must be preserved unless there is a clear reason to change it.

Do not change existing order ID format unnecessarily.

Order ID is the primary human reference used by:

* Customer
* Admin
* Dashboard
* Excel
* Audit logs
* Payment records

---

# 40. ATTACHMENTS

Existing attachment upload/storage must continue working.

Do not remove:

* `/api/upload`
* attachment validation
* size limits
* existing storage logic

Existing attachment handling is part of the working system.

---

# 41. DATABASE MIGRATION SAFETY

Database changes must be backward compatible where possible.

Before adding a column:

* Check whether it already exists.
* Do not blindly run destructive migrations.

Never:

```text
DROP TABLE
DROP DATABASE
DELETE all orders
```

Do not reset production data.

If a migration is required, use safe:

```text
ALTER TABLE ... ADD COLUMN IF NOT EXISTS ...
```

or equivalent safe migration logic where supported.

---

# 42. EXISTING DATA

Existing orders must remain valid after deployment.

Existing orders may not have:

* price
* payment status
* customer decision
* quote timestamp

Handle existing records gracefully.

Use sensible defaults such as:

```text
customer_decision = NULL or WAITING
payment_status = UNPAID
```

depending on the schema and business state.

Do not pretend old orders were accepted by customers if there is no historical record.

---

# 43. ERROR HANDLING

A failure in Excel generation must NOT break:

* order creation
* customer acceptance
* customer rejection
* existing Telegram order delivery

A failure in audit logging should generally not make the main business operation fail, unless the audit record is legally/business-critical.

Log server-side warnings/errors appropriately.

Do not expose internal details to the customer.

---

# 44. EXCEL GENERATION FAILURE

If Excel export fails:

Return a clear admin-facing error:

```text
❌ تعذر إنشاء ملف Excel.
يرجى المحاولة مرة أخرى.
```

Do not corrupt or partially overwrite an existing permanent report.

Generate a fresh workbook for each export.

---

# 45. UI/UX RULES

Frontend is Arabic RTL.

Maintain responsive design.

Do not introduce unnecessary libraries.

Keep the current visual identity unless improvement is required.

Use clear status badges.

Use confirmation dialogs for destructive actions such as:

* Reject order
* Cancel order
* Mark completed
* Record sensitive financial changes if appropriate

Do not make destructive operations one-click without confirmation.

---

# 46. ADMIN DASHBOARD STRUCTURE

Recommended layout:

```text
┌──────────────────────────────┐
│ 📊 لوحة التحكم               │
├──────────────────────────────┤
│ 📨 الطلبات       ⏳ المعلقة  │
│ ⚙️ الأعمال       ❌ الملغاة  │
│ 🔍 المراجعة      📤 التسليم │
│ ✅ المكتملة                  │
├──────────────────────────────┤
│ 🔎 البحث عن رقم الطلب        │
├──────────────────────────────┤
│ 📊 الإحصائيات                │
├──────────────────────────────┤
│ 📜 النشاط الأخير             │
├──────────────────────────────┤
│ 📥 تصدير Excel               │
└──────────────────────────────┘
```

---

# 47. ORDER DETAIL SCREEN

Recommended structure:

```text
📋 تفاصيل الطلب

🆔 رقم الطلب
👤 الزبون
🛠 الخدمة
🏫 القسم
📝 العنوان
📅 الموعد
📎 المرفقات

💰 السعر
💵 المدفوع
💳 المتبقي
📌 حالة الدفع

👤 قرار الزبون
📌 حالة الطلب

📝 الملاحظات

[💰 السعر]
[⏰ التسليم]
[📤 إرسال العرض]
[💵 دفعة]
[📌 الحالة]
[📜 السجل]
```

---

# 48. PAYMENT UI

When recording a payment:

```text
💵 تسجيل دفعة

رقم الطلب:
NABA-20260921-0042

السعر الكلي:
50,000 IQD

المدفوع حالياً:
20,000 IQD

المتبقي:
30,000 IQD

المبلغ الجديد:
[________]

طريقة الدفع:
[يدوي]

ملاحظة:
[________]

[✅ تسجيل الدفعة]
[❌ إلغاء]
```

After saving, recalculate totals from database.

---

# 49. QUOTATION UI

Admin quotation form:

```text
📤 إرسال العرض

رقم الطلب:
NABA-20260921-0042

السعر:
[________]

موعد التسليم:
[________]

ملاحظة للزبون:
[________]

[📤 إرسال للزبون]
```

After successful sending:

```text
✅ تم إرسال العرض للزبون.

تم نقل الطلب إلى:
⏳ الطلبات المعلقة
```

---

# 50. CUSTOMER ACCEPT/REJECT SECURITY

The backend must validate:

1. Telegram authentication.
2. Order existence.
3. Authenticated customer matches order owner.
4. Current order status is `WAITING_CUSTOMER`.
5. Customer decision is still `WAITING`.

Only then perform the action.

---

# 51. CUSTOMER ACCEPTANCE AUDIT

When accepted:

```text
action = CUSTOMER_ACCEPTED_ORDER
performed_by = User_<telegram_id>
```

When rejected:

```text
action = CUSTOMER_REJECTED_ORDER
performed_by = User_<telegram_id>
```

Use actual authenticated ID.

---

# 52. ADMIN AUDIT

For admin actions, use the authenticated admin identity where possible.

Examples:

```text
PRICE_SET
PRICE_UPDATED
QUOTE_SENT_TO_CUSTOMER
PAYMENT_RECORDED
ORDER_STATUS_CHANGED
ORDER_CANCELLED
```

Do not fabricate an admin name if the actual ID is available.

---

# 53. EXISTING ORDER DELIVERY AUDIT

If the existing channel delivery succeeds, optionally record:

```text
ORDER_DELIVERED_TO_CHANNEL
```

with:

```text
performed_by = Telegram_Bot
```

Audit logging must not cause a successful Telegram delivery to become a failed order.

---

# 54. CURRENT CHANNEL DELIVERY

Full order details and attachment:

```text
→ Telegram Channel
```

Admin private chat:

```text
→ NO automatic full-order copy
```

Customer private chat:

```text
→ Short confirmation
```

This behavior must remain.

---

# 55. EXCEL IS A REPORT, NOT A DATABASE

Do NOT implement:

```text
Bot → Excel → PostgreSQL
```

Do NOT implement:

```text
Dashboard → Excel → database
```

Correct architecture:

```text
Customer/Admin
      ↓
FastAPI
      ↓
PostgreSQL
      ↓
Dashboard
      ↓
Excel Export
```

---

# 56. FUTURE AUTOMATION

The current implementation should leave room for future automation.

Later we may automate:

* Excel generation
* payment tracking
* automatic customer reminders
* pending-order reminders
* automatic overdue detection
* financial reports
* service statistics
* customer history
* performance metrics

Do not overbuild these now.

Implement only the requested current workflow.

---

# 57. CURRENT IMPLEMENTATION PRIORITY

Priority order:

## Phase 1

Frontend UI:

* New Orders
* Pending
* Work
* Review
* Ready for Delivery
* Completed
* Cancelled
* Order search
* Order details
* Price
* Delivery time
* Send quotation
* Accept/reject handling
* Payment UI
* Activity UI
* Excel export button

## Phase 2

Backend:

* Safe database extensions
* Price management
* Delivery date management
* Quotation sending
* Customer acceptance
* Customer rejection
* Workflow transitions
* Payment records
* Audit logs
* Order search
* Excel generation
* Telegram Excel delivery

## Phase 3

Testing:

* Create order
* Upload attachment
* Channel delivery
* Customer confirmation
* Admin search
* Set price
* Set delivery time
* Send quotation
* Pending state
* Customer accepts
* Move to work
* Customer rejects
* Move to cancelled
* Record payment
* Calculate remaining
* Change work status
* Complete order
* Generate Excel
* Send Excel to admin

---

# 58. DO NOT MODIFY WITHOUT A REASON

Do not modify:

```text
BOT_TOKEN
DATABASE_URL
Telegram authentication
existing order ID logic
existing attachment storage
existing upload validation
existing customer confirmation
existing service definitions
existing channel delivery architecture
existing admin authentication
```

unless the modification is required to implement this specification.

---

# 59. FILE OWNERSHIP

The frontend file is the source of truth for the user interface.

The backend file is the source of truth for API/business logic.

The database is the source of truth for persistent business data.

Do not solve a backend problem by hardcoding data into frontend JavaScript.

Do not solve a database problem by storing important business data only in Excel.

---

# 60. IMPORTANT RULE FOR CODEX

When modifying `index.html`:

* Preserve existing working features.
* Implement the complete new UI.
* Make API calls compatible with the planned backend.
* Keep all frontend IDs/functions organized.
* Avoid fake/mock statistics.
* Avoid hardcoded orders.
* Do not create fake customer data.

When modifying `main.py`:

* First inspect the entire existing file.
* Reuse existing functions.
* Reuse existing database connection logic.
* Reuse existing authentication.
* Reuse existing audit log.
* Reuse existing order model.
* Add only what is necessary.

---

# 61. NO FAKE DATA

Never use:

```text
fake orders
fake statistics
fake payments
fake activity
fake customers
fake prices
```

All displayed production data must come from PostgreSQL.

For UI development, mock data may only be used temporarily if clearly isolated and must be removed before final implementation.

---

# 62. TESTING REQUIREMENT

Before declaring implementation complete, test at minimum:

### Customer

* Create order
* Receive order confirmation
* Receive quotation
* Accept quotation
* Reject quotation

### Admin

* Open dashboard
* Search order
* View details
* Set price
* Set delivery time
* Send quotation
* See pending order
* See accepted order in work
* See rejected order in cancelled
* Record payment
* Verify remaining balance
* Change work status
* Complete order
* View activity
* Export Excel
* Receive Excel through Telegram

### Security

* Unauthorized user cannot access admin endpoints.
* Customer cannot accept another customer's order.
* Customer cannot reject another customer's order.
* Customer cannot execute actions twice.
* Secrets are not exposed.
* Existing Telegram authentication continues working.

---

# 63. DEPLOYMENT SAFETY

Do not deploy destructive database migrations.

Before any migration:

* Verify current schema.
* Verify existing data.
* Make migration backward compatible.

Do not delete production orders.

Do not rotate secrets.

Do not modify environment variables unless explicitly requested.

---

# 64. FINAL ARCHITECTURE

The intended final system is:

```text
                         NABA
                          |
          ┌───────────────┴───────────────┐
          |                               |
       CUSTOMER                          ADMIN
          |                               |
     Telegram Mini App              Admin Dashboard
          |                               |
          └───────────────┬───────────────┘
                          |
                       FastAPI
                          |
                     PostgreSQL
                          |
        ┌─────────────────┼─────────────────┐
        |                 |                 |
      Orders           Payments         Audit Logs
        |                 |                 |
        └─────────────────┼─────────────────┘
                          |
                    Excel Export
                          |
                          ▼
                  Telegram Admin
                  6931187332
```

---

# 65. FINAL ORDER LIFECYCLE

The definitive workflow is:

```text
📨 الطلبات
     ↓
🔍 تدقيق
     ↓
💰 السعر + ⏰ التسليم
     ↓
📤 إرسال العرض للزبون
     ↓
⏳ الطلبات المعلقة
     |
     ├───────────────┐
     |               |
     ▼               ▼
✅ قبول             ❌ رفض
     |               |
     ▼               ▼
⚙️ الأعمال          ❌ الملغاة
     |
     ▼
🔨 قيد التنفيذ
     |
     ▼
🔍 قيد المراجعة
     |
     ▼
📤 جاهز للتسليم
     |
     ▼
✅ مكتمل
```

---

# 66. DEVELOPMENT PRINCIPLE

The goal is not to create the largest possible system.

The goal is to create a reliable workflow that can be expanded later.

Priority:

```text
Reliability
>
Data integrity
>
Security
>
Clear workflow
>
Ease of use
>
Automation
>
Extra features
```

Do not add unnecessary features.

Do not redesign working components without a clear reason.

When uncertain between two implementations, prefer the one that:

1. Preserves existing behavior.
2. Minimizes database risk.
3. Minimizes code duplication.
4. Keeps PostgreSQL as the source of truth.
5. Keeps Excel as an export/reporting layer.
6. Is easier to maintain later.

---

# 67. CODEX EXECUTION RULE

Before changing code:

1. Read this specification.
2. Inspect repository structure.
3. Read current `index.html`.
4. Read current `main.py`.
5. Inspect current database schema.
6. Identify existing endpoints.
7. Identify existing admin dashboard functions.
8. Identify existing Telegram handlers.
9. Identify existing audit logging.

Then provide a short implementation plan.

Do NOT immediately rewrite the project.

Implement changes incrementally.

After each major change, verify that existing functionality remains intact.

---

# 68. IMPORTANT USER WORKFLOW

The user may provide the frontend first and backend later.

If only `index.html` is provided:

* Complete the frontend according to this specification.
* Do not invent backend behavior beyond clearly defined API contracts.
* Keep API calls organized and easy to connect to FastAPI.
* Do not modify backend files unnecessarily.

When `main.py` is later provided:

* Read the finalized frontend.
* Modify the backend to match it.
* Do NOT ask the user to resend the frontend if it is already available in the repository.
* Do NOT redesign the frontend again unless an actual incompatibility exists.

---

# 69. SUCCESS CRITERIA

The implementation is successful only if:

* Existing order creation still works.
* Existing attachment upload still works.
* Existing Telegram channel delivery still works.
* Customer confirmation still works.
* Admin authentication still works.
* Admin can review and price orders.
* Admin can set delivery time.
* Admin can send quotation to customer.
* Quotation moves order to pending.
* Customer can accept.
* Accepted order moves to work.
* Customer can reject.
* Rejected order moves to cancelled.
* Admin can manage execution status.
* Admin can record manual payments.
* Remaining balance is calculated correctly.
* Activity log records real actions.
* Dashboard displays real database data.
* Excel can be generated from PostgreSQL.
* Excel can be sent to admin Telegram account.
* No secrets are exposed.
* No existing production data is deleted.
* No unnecessary duplicate database systems are introduced.

---

# END OF NABA PROJECT SPECIFICATION
