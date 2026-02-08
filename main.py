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
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ---------------- ENV ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")
GROUP_ID = os.getenv("GROUP_ID", "0") # Считываем как строку, преобразуем где надо

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

# --- Helpers for DB ---
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
    # Сначала пробуем получить, чтобы сохранить старые поля
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
    # Ссылка на 24 часа, на 1 человека
    expire_date = int(time.time()) + 24 * 3600
    try:
        invite = await bot.create_chat_invite_link(
            chat_id=GROUP_ID,
            member_limit=1,
            expire_date=expire_date,
        )
        return invite.invite_link
    except Exception as e:
        print(f"ERROR creating invite link: {e}")
        return "Ошибка генерации ссылки. Напишите в поддержку."

async def grant_access_by_invoice(invoice_id: str):
    order = db_get_order(invoice_id)
    # Если заказа нет или он уже оплачен - выходим
    if not order or order.get("status") == "paid":
        return

    # Меняем статус в БД
    db_update_order_status(invoice_id, "paid")
    
    # Генерируем ссылку
    link = await issue_one_time_invite()
    uid = order["user_id"]
    
    try:
        await bot.send_message(
            uid,
            "Оплата подтверждена ✅\n\n"
            "Вот ваша персональная ссылка для входа в закрытую группу.\n"
            "Ссылка одноразовая и действует 24 часа.\n\n"
            "⚠️ Не заходите сами, если купили для другого человека — перешлите ссылку ему.\n"
            f"{link}"
        )
    except Exception as e:
        print(f"ERROR sending message to user {uid}: {e}")

async def auto_check_payment(invoice_id: str):
    """
    Мягкая автопроверка.
    """
    await asyncio.sleep(15)
    
    order = db_get_order(invoice_id)
    if not order or order["status"] == "paid": return
    
    # Первая проверка через 15 сек
    try:
        payment = get_yookassa_payment(order["payment_id"])
        if payment.get("status") == "succeeded":
            await grant_access_by_invoice(invoice_id)
            return
    except:
        pass

    await asyncio.sleep(45) # Ждем еще 45 сек (всего 60)
    
    order = db_get_order(invoice_id)
    if not order or order["status"] == "paid": return

    # Вторая проверка
    try:
        payment = get_yookassa_payment(order["payment_id"])
        if payment.get("status") == "succeeded":
            await grant_access_by_invoice(invoice_id)
            return
    except:
        pass

    # Если спустя минуту оплаты нет - мягкое напоминание
    # Проверяем статус в БД еще раз, вдруг вебхук уже отработал
    final_order = db_get_order(invoice_id)
    if final_order and final_order["status"] != "paid":
        try:
            await bot.send_message(
                final_order["user_id"],
                "Пока не вижу оплаты.\n"
                "Если уже оплатили — нажмите «✅ Я оплатил — проверить»."
            )
        except:
            pass

# ---------------- Telegram handlers ----------------
@dp.message(CommandStart())
async def start(message: Message):
    uid = message.from_user.id
    # Сохраняем пользователя в БД, сбрасываем шаг на 'name'
    db_upsert_user(uid, step="name")
    
    await message.answer(
        "Привет! 🙂\nЯ помогу оформить доступ в закрытую группу.\n\n"
        "Как тебя зовут?"
    )

@dp.message()
async def collect(message: Message):
    uid = message.from_user.id
    user = db_get_user(uid)

    if not user:
        await message.answer("Нажми /start 🙂")
        return

    step = user.get("step")

    if step == "name":
        name = message.text.strip()
        if len(name) < 2:
            await message.answer("Напиши имя чуть понятнее 🙂")
            return
        db_upsert_user(uid, name=name, step="email")
        await message.answer("Отлично! Теперь укажи email — туда придёт чек.")
        return

    if step == "email":
        email = message.text.strip()
        if "@" not in email or "." not in email:
            await message.answer("Похоже, email с ошибкой. Попробуй ещё раз 🙂")
            return
        db_upsert_user(uid, email=email, step="done")
        await message.answer(
            f"{user.get('name', 'друг')}, супер ✅\nВыбирай пакет:",
            reply_markup=kb_main()
        )
        return

    # Если step == done или что-то другое
    await message.answer("Выбирай действие кнопками ниже 🙂", reply_markup=kb_main())


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
    title = PLANS[plan_id]["title"]
    yk_desc = PLANS[plan_id].get("description")

    # Сначала пытаемся создать платеж
    try:
        payment = create_yookassa_payment(invoice_id, amount, yk_desc, user["email"])
    except Exception as e:
        print("YOOKASSA_CREATE_ERROR:", e)
        await cb.answer("Ошибка создания платежа", show_alert=True)
        return

    payment_id = payment.get("id")
    url = payment.get("confirmation", {}).get("confirmation_url")

    # Сохраняем заказ в БД
    db_create_order(invoice_id, uid, plan_id, amount, "pending", payment_id)
    db_upsert_user(uid, last_invoice_id=invoice_id)

    # Запускаем автопроверку
    asyncio.create_task(auto_check_payment(invoice_id))

    await cb.message.edit_text(
        f"Пакет: {title}\nСумма: {amount} ₽\n\n"
        "Оплатите по кнопке ниже и я пришлю ссылку ✅",
        reply_markup=kb_pay(url, plan_id, invoice_id)
    )
    await cb.answer()


@dp.callback_query(F.data == "resend_link")
async def resend_link(cb: CallbackQuery):
    uid = cb.from_user.id
    user = db_get_user(uid)
    last_inv = user.get("last_invoice_id")
    
    if not last_inv:
        await cb.answer("Нет активных заказов.", show_alert=True)
        return
        
    order = db_get_order(last_inv)
    if not order or order["status"] != "paid":
        await cb.answer("Этот заказ еще не оплачен.", show_alert=True)
        return

    link = await issue_one_time_invite()
    await cb.message.answer(f"Ваша ссылка:\n{link}")
    await cb.answer()


@dp.callback_query(F.data.startswith("check:"))
async def check_payment_handler(cb: CallbackQuery):
    invoice_id = cb.data.split(":", 1)[1]
    order = db_get_order(invoice_id)
    
    if not order:
        await cb.answer("Заказ не найден (возможно, устарел).", show_alert=True)
        return

    if order["status"] == "paid":
        await cb.answer("Уже оплачено! Ссылка должна быть в чате.", show_alert=True)
        return

    # Проверяем в ЮКассе
    try:
        payment = get_yookassa_payment(order["payment_id"])
        status = payment.get("status")
        
        if status == "succeeded":
            await grant_access_by_invoice(invoice_id)
            await cb.answer("Успешно! Отправляю ссылку...", show_alert=False)
        elif status == "pending":
             await cb.answer("ЮКасса пишет: ожидание оплаты ⏳", show_alert=True)
        elif status == "canceled":
             await cb.answer("Платеж отменен.", show_alert=True)
        else:
             await cb.answer(f"Статус: {status}", show_alert=True)
            
    except Exception as e:
        print("CHECK_ERROR:", e)
        await cb.answer("Ошибка связи с кассой", show_alert=True)


@dp.callback_query(F.data == "support")
async def support_handler(cb: CallbackQuery):
    await cb.message.edit_text(
        f"Поддержка: @{ADMIN_USERNAME}",
        reply_markup=kb_main()
    )
    await cb.answer()
    
@dp.callback_query(F.data == "back")
async def back_handler(cb: CallbackQuery):
    await cb.message.edit_text("Меню:", reply_markup=kb_main())
    await cb.answer()

# ---------------- Webhooks ----------------
@app.get("/")
async def root():
    return {"status": "running", "db": "ok"}

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    try:
        update = await request.json()
        await dp.feed_raw_update(bot, update)
    except Exception as e:
        print(f"Update error: {e}")
    return {"ok": True}

@app.post("/webhook/yookassa")
async def yookassa_webhook(request: Request):
    try:
        payload = await request.json()
        event = payload.get("event")
        obj = payload.get("object") or {}
        
        # Получаем invoice_id из metadata
        meta = obj.get("metadata") or {}
        invoice_id = meta.get("invoice_id")
        
        if event == "payment.succeeded" and invoice_id:
            print(f"WEBHOOK: Payment succeeded for {invoice_id}")
            # Самое главное: теперь мы берем данные из БД, а не из памяти!
            await grant_access_by_invoice(invoice_id)
            
    except Exception as e:
        print("WEBHOOK_ERROR:", e)
        
    return {"ok": True}

@app.get("/return/{invoice_id}")
async def return_page(invoice_id: str):
    return {"message": "Оплата проверяется... Вернитесь в бот.", "id": invoice_id}

@app.on_event("startup")
async def on_startup():
    init_db() # Создаем таблицы при старте
    webhook_url = f"{PUBLIC_BASE_URL}/telegram/webhook"
    print(f"Setting webhook: {webhook_url}")
    await bot.set_webhook(webhook_url)

if __name__ == "__main__":
    # Локальный запуск для тестов (на Railway запускает uvicorn через Procfile)
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
