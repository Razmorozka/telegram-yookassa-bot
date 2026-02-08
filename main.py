import os
import time
import uuid
import sqlite3
import json
import asyncio
from decimal import Decimal
from typing import Dict, Any, Optional

import requests
from fastapi import FastAPI, Request

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ---------------- ENV ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")
# !!! ВАЖНО: Преобразуем ID группы в целое число сразу
try:
    GROUP_ID = int(os.getenv("GROUP_ID", "0"))
except:
    GROUP_ID = 0

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")

ADMIN_USERNAME = "kairos_007"

# ---------------- Basic checks ----------------
if not BOT_TOKEN or not PUBLIC_BASE_URL:
    raise RuntimeError("Нужно задать BOT_TOKEN и PUBLIC_BASE_URL в ENV")
if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
    raise RuntimeError("Нужно задать YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY в ENV")

# ---------------- Database (SQLite) ----------------
DB_FILE = "bot_database.db"

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT,
                email TEXT,
                step TEXT,
                last_invoice_id TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                invoice_id TEXT PRIMARY KEY,
                user_id INTEGER,
                plan_id TEXT,
                amount TEXT,
                status TEXT,
                payment_id TEXT,
                created_at INTEGER
            )
        """)
        conn.commit()

def db_get_user(user_id: int):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if row:
            return {
                "user_id": row[0], "name": row[1], "email": row[2], 
                "step": row[3], "last_invoice_id": row[4]
            }
        return None

def db_upsert_user(user_id: int, **kwargs):
    current = db_get_user(user_id) or {}
    data = {**current, "user_id": user_id, **kwargs}
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO users (user_id, name, email, step, last_invoice_id)
            VALUES (:user_id, :name, :email, :step, :last_invoice_id)
        """, {
            "user_id": user_id,
            "name": data.get("name"),
            "email": data.get("email"),
            "step": data.get("step"),
            "last_invoice_id": data.get("last_invoice_id")
        })

def db_create_order(invoice_id, user_id, plan_id, amount, status, payment_id):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            INSERT INTO orders (invoice_id, user_id, plan_id, amount, status, payment_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (invoice_id, user_id, plan_id, str(amount), status, payment_id, int(time.time())))

def db_get_order(invoice_id: str):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.execute("SELECT * FROM orders WHERE invoice_id = ?", (invoice_id,))
        row = cur.fetchone()
        if row:
            return {
                "invoice_id": row[0], "user_id": row[1], "plan_id": row[2],
                "amount": row[3], "status": row[4], "payment_id": row[5], "created_at": row[6]
            }
        return None

def db_update_order_status(invoice_id: str, status: str):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("UPDATE orders SET status = ? WHERE invoice_id = ?", (status, invoice_id))

# ---------------- Bot/App ----------------
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
app = FastAPI()

PLANS = {
    "basic": {
        "title": "Войти в закрытую группу",
        "amount": Decimal("2400.00"),
        "description": 'Доступ к материалам "Самодисциплина без стресса"',
    },
    "pro": {
        "title": "С сопровождением",
        "amount": Decimal("5400.00"),
        "description": 'Доступ к материалам "Самодисциплина без стресса" с сопровождением',
    },
    "test": {
        "title": "🧪 Вход за 1 ₽ (тест)",
        "amount": Decimal("1.00"),
        "description": 'ТЕСТОВЫЙ ДОСТУП: материалы "Самодисциплина без стресса"',
    },
}

# ---------------- UI keyboards ----------------
def kb_main():
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Выбрать пакет", callback_data="choose_plan")
    kb.button(text="❓ Поддержка", callback_data="support")
    kb.adjust(1)
    return kb.as_markup()

def kb_plans():
    kb = InlineKeyboardBuilder()
    for plan_id, p in PLANS.items():
        kb.button(text=f"{p['title']} — {p['amount']} ₽", callback_data=f"plan:{plan_id}")
    kb.button(text="⬅️ Назад", callback_data="back")
    kb.adjust(1)
    return kb.as_markup()

def kb_pay(payment_url: str, plan_id: str, invoice_id: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Перейти к оплате", url=payment_url)
    kb.button(text="✅ Я оплатил — проверить", callback_data=f"check:{invoice_id}")
    if plan_id == "pro":
        kb.button(text="📩 Написать админу", url=f"https://t.me/{ADMIN_USERNAME}")
    kb.button(text="🔁 Получить ссылку ещё раз", callback_data="resend_link")
    kb.button(text="⬅️ Назад", callback_data="choose_plan")
    kb.adjust(1)
    return kb.as_markup()

# ---------------- YooKassa helpers ----------------
def yk_auth():
    return (YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)

def create_yookassa_payment(invoice_id: str, amount: Decimal, description: str, email: str) -> Dict[str, Any]:
    url = "https://api.yookassa.ru/v3/payments"
    idempotence_key = str(uuid.uuid4())
    payload = {
        "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": f"{PUBLIC_BASE_URL}/return/{invoice_id}",
        },
        "description": description,
        "metadata": {"invoice_id": invoice_id},
        "receipt": {
            "customer": {"email": email},
            "items": [{
                "description": description,
                "quantity": "1.00",
                "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
                "vat_code": 1,
                "payment_subject": "service",
                "payment_mode": "full_payment",
            }],
        },
    }
    headers = {"Idempotence-Key": idempotence_key, "Content-Type": "application/json"}
    r = requests.post(url, auth=yk_auth(), json=payload, headers=headers, timeout=20)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"YooKassa create payment error: {r.status_code} {r.text}")
    return r.json()

def get_yookassa_payment(payment_id: str) -> Dict[str, Any]:
    url = f"https://api.yookassa.ru/v3/payments/{payment_id}"
    r = requests.get(url, auth=yk_auth(), timeout=20)
    return r.json()

# ---------------- Logic Actions ----------------
async def issue_one_time_invite() -> str:
    """Генерирует ссылку и возвращает её (или текст ошибки)"""
    expire_date = int(time.time()) + 24 * 3600
    
    # ПРОВЕРКА GROUP_ID
    if not GROUP_ID or GROUP_ID == 0:
        return "ОШИБКА: Не задан ID группы (GROUP_ID) в настройках."

    try:
        invite = await bot.create_chat_invite_link(
            chat_id=GROUP_ID,
            member_limit=1,
            expire_date=expire_date,
        )
        return invite.invite_link
    except Exception as e:
        error_msg = str(e)
        print(f"TELEGRAM API ERROR: {error_msg}")
        # Возвращаем РЕАЛЬНУЮ ошибку пользователю, чтобы понять причину
        return f"Ошибка Telegram API: {error_msg}. (ID группы: {GROUP_ID})"

async def grant_access_by_invoice(invoice_id: str):
    order = db_get_order(invoice_id)
    if not order or order.get("status") == "paid":
        return

    db_update_order_status(invoice_id, "paid")
    
    # Генерируем ссылку
    link_result = await issue_one_time_invite()
    uid = order["user_id"]
    
    # Если в link_result ошибка (начинается не с https), сообщаем об этом
    is_error = not link_result.startswith("https")
    
    msg_text = "Оплата подтверждена ✅\n\n"
    if is_error:
        msg_text += f"⚠️ Не удалось создать ссылку.\nТехническая информация: {link_result}\n\nПерешлите это сообщение администратору @{ADMIN_USERNAME}"
    else:
        msg_text += (
            "Вот ваша персональная ссылка для входа в закрытую группу.\n"
            "Ссылка одноразовая и действует 24 часа.\n\n"
            "⚠️ Не заходите сами, если купили для другого человека — перешлите ссылку ему.\n"
            f"{link_result}"
        )

    try:
        await bot.send_message(uid, msg_text)
    except Exception as e:
        print(f"ERROR sending message to user {uid}: {e}")

async def auto_check_payment(invoice_id: str):
    await asyncio.sleep(15)
    order = db_get_order(invoice_id)
    if not order or order["status"] == "paid": return
    try:
        payment = get_yookassa_payment(order["payment_id"])
        if payment.get("status") == "succeeded":
            await grant_access_by_invoice(invoice_id)
            return
    except: pass

    await asyncio.sleep(45)
    order = db_get_order(invoice_id)
    if not order or order["status"] == "paid": return
    try:
        payment = get_yookassa_payment(order["payment_id"])
        if payment.get("status") == "succeeded":
            await grant_access_by_invoice(invoice_id)
            return
    except: pass

    # Напоминание
    final_order = db_get_order(invoice_id)
    if final_order and final_order["status"] != "paid":
        try:
            await bot.send_message(
                final_order["user_id"],
                "Пока не вижу оплаты.\nЕсли уже оплатили — нажмите «✅ Я оплатил — проверить»."
            )
        except: pass

# ---------------- Telegram handlers ----------------
@dp.message(CommandStart())
async def start(message: Message):
    uid = message.from_user.id
    db_upsert_user(uid, step="name")
    await message.answer("Привет! 🙂\nЯ помогу оформить доступ в закрытую группу.\n\nКак тебя зовут?")

# --- НОВАЯ КОМАНДА ДЛЯ ТЕСТА ССЫЛКИ ---
@dp.message(Command("test_link"))
async def test_link_handler(message: Message):
    """Позволяет проверить генерацию ссылки без оплаты"""
    await message.answer("⏳ Пробую создать ссылку...")
    link_result = await issue_one_time_invite()
    await message.answer(f"Результат:\n{link_result}")

@dp.message()
async def collect(message: Message):
    uid = message.from_user.id
    user = db_get_user(uid)
    if not user:
        await message.answer("Нажми /start 🙂")
        return
    step = user.get("step")
    if step == "name":
        if len(message.text) < 2:
            await message.answer("Имя слишком короткое 🙂")
            return
        db_upsert_user(uid, name=message.text, step="email")
        await message.answer("Укажи email — туда придёт чек.")
        return
    if step == "email":
        if "@" not in message.text:
            await message.answer("Некорректный email 🙂")
            return
        db_upsert_user(uid, email=message.text, step="done")
        await message.answer(f"Супер! Выбирай пакет:", reply_markup=kb_main())
        return
    await message.answer("Используйте кнопки меню.", reply_markup=kb_main())

@dp.callback_query(F.data == "choose_plan")
async def choose_plan_handler(cb: CallbackQuery):
    await cb.message.edit_text("Выберите пакет:", reply_markup=kb_plans())
    await cb.answer()

@dp.callback_query(F.data.startswith("plan:"))
async def plan_handler(cb: CallbackQuery):
    uid = cb.from_user.id
    plan_id = cb.data.split(":", 1)[1]
    user = db_get_user(uid)
    if not user or user.get("step") != "done":
        await cb.answer("Сначала введите данные (/start)")
        return

    invoice_id = f"inv_{uid}_{int(time.time())}"
    amount = PLANS[plan_id]["amount"]
    yk_desc = PLANS[plan_id].get("description")

    try:
        payment = create_yookassa_payment(invoice_id, amount, yk_desc, user["email"])
    except Exception as e:
        await cb.answer("Ошибка создания платежа", show_alert=True)
        print(e)
        return

    payment_id = payment.get("id")
    url = payment.get("confirmation", {}).get("confirmation_url")
    db_create_order(invoice_id, uid, plan_id, amount, "pending", payment_id)
    db_upsert_user(uid, last_invoice_id=invoice_id)
    asyncio.create_task(auto_check_payment(invoice_id))
    
    await cb.message.edit_text(
        f"Сумма: {amount} ₽. Оплатите по кнопке:",
        reply_markup=kb_pay(url, plan_id, invoice_id)
    )
    await cb.answer()

@dp.callback_query(F.data == "resend_link")
async def resend_link(cb: CallbackQuery):
    uid = cb.from_user.id
    user = db_get_user(uid)
    last_inv = user.get("last_invoice_id")
    if not last_inv:
        await cb.answer("Нет заказов", show_alert=True)
        return
    order = db_get_order(last_inv)
    if not order or order["status"] != "paid":
        await cb.answer("Заказ не оплачен", show_alert=True)
        return
        
    await cb.message.answer("Генерирую новую ссылку...")
    link = await issue_one_time_invite()
    await cb.message.answer(f"Ваша ссылка:\n{link}")
    await cb.answer()

@dp.callback_query(F.data.startswith("check:"))
async def check_payment_handler(cb: CallbackQuery):
    invoice_id = cb.data.split(":", 1)[1]
    order = db_get_order(invoice_id)
    if not order:
        await cb.answer("Заказ не найден", show_alert=True)
        return
    if order["status"] == "paid":
        await cb.answer("Уже оплачено!", show_alert=True)
        return

    try:
        payment = get_yookassa_payment(order["payment_id"])
        status = payment.get("status")
        if status == "succeeded":
            await grant_access_by_invoice(invoice_id)
            await cb.answer("Успешно! Ссылка отправлена.", show_alert=False)
        elif status == "pending":
             await cb.answer("Ожидание оплаты ⏳", show_alert=True)
        else:
             await cb.answer(f"Статус: {status}", show_alert=True)
    except:
        await cb.answer("Ошибка проверки", show_alert=True)

@dp.callback_query(F.data == "support")
async def support_handler(cb: CallbackQuery):
    await cb.message.edit_text(f"Поддержка: @{ADMIN_USERNAME}", reply_markup=kb_main())

@dp.callback_query(F.data == "back")
async def back_handler(cb: CallbackQuery):
    await cb.message.edit_text("Меню:", reply_markup=kb_main())

# ---------------- Webhooks ----------------
@app.get("/")
async def root():
    return {"status": "ok", "db": "ok"}

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    try:
        update = await request.json()
        await dp.feed_raw_update(bot, update)
    except: pass
    return {"ok": True}

@app.post("/webhook/yookassa")
async def yookassa_webhook(request: Request):
    try:
        payload = await request.json()
        event = payload.get("event")
        obj = payload.get("object") or {}
        meta = obj.get("metadata") or {}
        invoice_id = meta.get("invoice_id")
        
        if event == "payment.succeeded" and invoice_id:
            await grant_access_by_invoice(invoice_id)
    except Exception as e:
        print("WEBHOOK_ERROR:", e)
    return {"ok": True}

@app.get("/return/{invoice_id}")
async def return_page(invoice_id: str):
    return {"message": "Оплата проверяется... Вернитесь в бот."}

@app.on_event("startup")
async def on_startup():
    init_db()
    await bot.set_webhook(f"{PUBLIC_BASE_URL}/telegram/webhook")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
