import os
import time
import uuid
import sqlite3
import asyncio
from decimal import Decimal

import requests
from fastapi import FastAPI, Request

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, ChatMemberUpdated
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ---------------- ENV (Railway) ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")

try:
    GROUP_ID = int(os.getenv("GROUP_ID", "0"))
except Exception:
    GROUP_ID = 0

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")

# Контакты и настройки
ADMIN_USERNAME = "kairos_007"     # Тех. поддержка (без @)
EXPERT_USERNAME = "Liya_Sharova"  # Эксперт (без @)
SECRET_WORD = "лапки-лапки"

if not BOT_TOKEN or not PUBLIC_BASE_URL:
    raise RuntimeError("Нужно задать BOT_TOKEN и PUBLIC_BASE_URL в ENV")
if not GROUP_ID:
    raise RuntimeError("Нужно задать GROUP_ID в ENV")
if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
    raise RuntimeError("Нужно задать YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY в ENV")

# ---------------- Database ----------------
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

def db_get_all_users():
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.execute("SELECT user_id FROM users")
        return [row[0] for row in cur.fetchall()]

def db_get_user(user_id: int):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.execute("SELECT user_id, name, email, step, last_invoice_id FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if not row:
            return None
        return {"user_id": row[0], "name": row[1], "email": row[2], "step": row[3], "last_invoice_id": row[4]}

def db_upsert_user(user_id: int, **kwargs):
    current = db_get_user(user_id) or {
        "user_id": user_id,
        "name": None,
        "email": None,
        "step": None,
        "last_invoice_id": None
    }
    for key, value in kwargs.items():
        current[key] = value

    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO users (user_id, name, email, step, last_invoice_id)
            VALUES (:user_id, :name, :email, :step, :last_invoice_id)
        """, current)
        conn.commit()  # ✅ ВАЖНО

def db_create_order(invoice_id, user_id, plan_id, amount, status, payment_id):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO orders (invoice_id, user_id, plan_id, amount, status, payment_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (invoice_id, user_id, plan_id, str(amount), status, payment_id, int(time.time()))
        )
        conn.commit()  # ✅ ВАЖНО

def db_get_order(invoice_id: str):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.execute("SELECT invoice_id, user_id, plan_id, amount, status, payment_id, created_at FROM orders WHERE invoice_id = ?", (invoice_id,))
        row = cur.fetchone()
        if not row:
            return None
        return {
            "invoice_id": row[0],
            "user_id": row[1],
            "plan_id": row[2],
            "amount": row[3],
            "status": row[4],
            "payment_id": row[5],
            "created_at": row[6],
        }

def db_update_order_status(invoice_id: str, status: str):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("UPDATE orders SET status = ? WHERE invoice_id = ?", (status, invoice_id))
        conn.commit()  # ✅ ВАЖНО

def db_set_user_last_invoice(user_id: int, invoice_id: str):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("UPDATE users SET last_invoice_id = ? WHERE user_id = ?", (invoice_id, user_id))
        conn.commit()  # ✅ ВАЖНО

# ---------------- Пакеты ----------------
PLANS = {
    "test": {
        "title": "🧪 Тест за 1 ₽",
        "amount": Decimal("1.00"),
        "description": 'ТЕСТ: материалы "Самодисциплина без стресса"',
    },
    "basic": {
        "title": "Войти в группу",
        "amount": Decimal("2400.00"),
        "description": 'Доступ к материалам "Самодисциплина без стресса"',
    },
    "pro": {
        "title": "С сопровождением",
        "amount": Decimal("5400.00"),
        "description": 'Доступ к материалам "Самодисциплина без стресса" с сопровождением',
    },
}

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
app = FastAPI()

# ---------------- Клавиатуры ----------------
def kb_main():
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Выбрать пакет", callback_data="choose_plan")
    kb.button(text="❓ Поддержка", callback_data="support")
    kb.adjust(1)
    return kb.as_markup()

def kb_plans():
    kb = InlineKeyboardBuilder()
    for pid, p in PLANS.items():
        kb.button(text=f"{p['title']} — {p['amount']} ₽", callback_data=f"plan:{pid}")
    kb.button(text="⬅️ Назад", callback_data="back")
    kb.adjust(1)
    return kb.as_markup()

def kb_pay(url: str, inv_id: str, plan_id: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Оплатить", url=url)
    kb.button(text="✅ Я оплатил — проверить", callback_data=f"check:{inv_id}")

    if plan_id == "pro":
        kb.button(text="📩 Написать админу", url=f"https://t.me/{ADMIN_USERNAME}")

    kb.button(text="🔁 Получить ссылку ещё раз", callback_data="resend_link")
    kb.button(text="⬅️ Назад", callback_data="choose_plan")
    kb.adjust(1)
    return kb.as_markup()

# ---------------- YooKassa helpers ----------------
def yk_auth():
    return (YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)

def yk_create_payment(amount: Decimal, description: str, email: str, invoice_id: str) -> dict:
    url = "https://api.yookassa.ru/v3/payments"
    headers = {
        "Idempotence-Key": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }

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
            "items": [
                {
                    "description": description,
                    "quantity": "1.00",
                    "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
                    "vat_code": 1,                  # без НДС
                    "payment_mode": "full_payment", # ✅ ВАЖНО
                    "payment_subject": "service",   # ✅ ВАЖНО
                }
            ],
        },
    }

    r = requests.post(url, auth=yk_auth(), headers=headers, json=payload, timeout=20)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"YooKassa create payment error: {r.status_code} {r.text}")
    return r.json()

def yk_get_payment(payment_id: str) -> dict:
    url = f"https://api.yookassa.ru/v3/payments/{payment_id}"
    r = requests.get(url, auth=yk_auth(), timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"YooKassa get payment error: {r.status_code} {r.text}")
    return r.json()

# ---------------- Логика выдачи доступа ----------------
async def issue_link() -> str:
    expire_date = int(time.time()) + 24 * 3600
    res = await bot.create_chat_invite_link(chat_id=GROUP_ID, member_limit=1, expire_date=expire_date)
    return res.invite_link

async def grant_access(inv_id: str):
    order = db_get_order(inv_id)
    if not order or order["status"] == "paid":
        return

    link = await issue_link()
    db_update_order_status(inv_id, "paid")

    user = db_get_user(order["user_id"]) or {}
    name = user.get("name") or "Друг"
    plan_id = order.get("plan_id")

    msg = (
        f"Оплата подтверждена ✅\n\n"
        f"Ура, {name}! 🎉\n"
        f"🆔 Номер заказа: `{inv_id}`\n\n"
        f"⬇️ **ПЕРЕШЛИТЕ ЭТО СООБЩЕНИЕ РЕБЕНКУ** ⬇️\n\n"
        f"Вот персональная ссылка для входа в закрытую группу.\n"
        f"Ссылка одноразовая и действует 24 часа.\n\n"
        f"{link}\n"
    )

    if plan_id in ("pro", "test"):
        msg += (
            f"\nПакет включает сопровождение.\n"
            f"Напиши эксперту: @{EXPERT_USERNAME}\n"
            f"Секретное слово: `{SECRET_WORD}`\n"
            f"И номер заказа: `{inv_id}`\n"
        )

    await bot.send_message(order["user_id"], msg)

async def reminder_task(inv_id: str):
    await asyncio.sleep(3600)
    order = db_get_order(inv_id)
    if order and order["status"] == "pending":
        try:
            await bot.send_message(order["user_id"], f"Похоже, вы не завершили оплату 🙂\nНужна помощь? Напишите @{ADMIN_USERNAME}")
        except Exception:
            pass

# ---------------- Handlers ----------------
@dp.message(CommandStart())
async def start(m: Message):
    u = db_get_user(m.from_user.id)

    # ✅ Если пользователь уже зарегистрирован — НЕ спрашиваем заново
    if u and u.get("step") == "done" and u.get("email"):
        name = u.get("name") or m.from_user.first_name or "друг"
        await m.answer(f"Привет, {name}! 🙂\nВыбирай пакет:", reply_markup=kb_main())
        return

    # иначе стартуем onboarding
    db_upsert_user(m.from_user.id, name=None, email=None, step="name", last_invoice_id=None)
    await m.answer("Привет! 🙂 Я помогу оформить доступ в закрытую группу.\n\nКак тебя зовут?")

@dp.chat_member()
async def welcome_new_member(event: ChatMemberUpdated):
    if event.new_chat_member.status == "member":
        try:
            await bot.send_message(
                event.chat.id,
                "Добро пожаловать в группу! 👋\n\nИзучи правила в закрепленном сообщении."
            )
        except Exception:
            pass

@dp.message(Command("test_link"))
async def test_cmd(m: Message):
    await m.answer(f"Тест генерации ссылки: {await issue_link()}")

@dp.message()
async def flow(m: Message):
    if m.chat.type in ["group", "supergroup"]:
        return

    u = db_get_user(m.from_user.id)
    if not u:
        await m.answer("Давай начнём сначала — нажми /start 🙂")
        return

    if u["step"] == "name":
        name = (m.text or "").strip()
        if len(name) < 2:
            await m.answer("Напиши имя чуть понятнее 🙂")
            return
        db_upsert_user(m.from_user.id, name=name, step="email")
        await m.answer(f"Приятно познакомиться, {name}! 😊 Теперь укажи email для чека:")
        return

    if u["step"] == "email":
        email = (m.text or "").strip()
        if "@" not in email or "." not in email:
            await m.answer("Похоже, email с ошибкой. Попробуй ещё раз 🙂")
            return
        db_upsert_user(m.from_user.id, email=email, step="done")
        name = db_get_user(m.from_user.id).get("name") or "друг"
        await m.answer(f"{name}, готово ✅\nВыбирай пакет:", reply_markup=kb_main())
        return

    await m.answer("Выбирай действие кнопками ниже 🙂", reply_markup=kb_main())

@dp.callback_query(F.data == "choose_plan")
async def plans_cb(cb: CallbackQuery):
    await cb.message.edit_text("Доступные пакеты:", reply_markup=kb_plans())
    await cb.answer()

@dp.callback_query(F.data.startswith("plan:"))
async def pay_cb(cb: CallbackQuery):
    pid = cb.data.split(":", 1)[1]
    if pid not in PLANS:
        await cb.answer("Неизвестный пакет", show_alert=True)
        return

    u = db_get_user(cb.from_user.id)
    if not u or u.get("step") != "done" or not u.get("email"):
        await cb.answer()
        await cb.message.edit_text("Нажми /start и введи имя + email 🙂")
        return

    inv_id = f"inv_{cb.from_user.id}_{int(time.time())}"
    plan = PLANS[pid]

    try:
        res = yk_create_payment(
            amount=plan["amount"],
            description=plan["description"],
            email=u["email"],
            invoice_id=inv_id,
        )
        payment_id = res.get("id")
        confirm_url = (res.get("confirmation") or {}).get("confirmation_url")

        if not payment_id or not confirm_url:
            print("YOOKASSA_BAD_RESPONSE:", res)
            await cb.answer("Проблема с оплатой. Напишите в поддержку.", show_alert=True)
            return

        db_create_order(inv_id, cb.from_user.id, pid, plan["amount"], "pending", payment_id)
        db_set_user_last_invoice(cb.from_user.id, inv_id)

        asyncio.create_task(reminder_task(inv_id))

        await cb.message.edit_text(
            f"Пакет: {plan['title']}\n"
            f"Сумма: {plan['amount']} ₽\n\n"
            "Нажмите кнопку ниже, оплатите, и я пришлю ссылку ✅\n\n"
            "Если оплатили, а ссылка не пришла — нажмите «✅ Я оплатил — проверить».",
            reply_markup=kb_pay(confirm_url, inv_id, pid)
        )
        await cb.answer()

    except Exception as e:
        print("YOOKASSA_CREATE_ERROR:", str(e))
        await cb.answer("Ошибка связи с платежной системой.", show_alert=True)

@dp.callback_query(F.data.startswith("check:"))
async def check_cb(cb: CallbackQuery):
    inv_id = cb.data.split(":", 1)[1]
    order = db_get_order(inv_id)
    if not order:
        await cb.answer("Заказ не найден.", show_alert=True)
        return

    try:
        p = yk_get_payment(order["payment_id"])
        status = p.get("status")
        if status == "succeeded":
            await grant_access(inv_id)
            await cb.answer("Оплата подтверждена ✅")
        else:
            await cb.answer(f"Пока статус: {status}. Если вы только что оплатили — подождите минуту 🙂", show_alert=True)
    except Exception as e:
        print("YOOKASSA_GET_ERROR:", str(e))
        await cb.answer("Не получилось проверить оплату. Попробуйте ещё раз.", show_alert=True)

@dp.callback_query(F.data == "resend_link")
async def resend_link(cb: CallbackQuery):
    u = db_get_user(cb.from_user.id)
    if not u or not u.get("last_invoice_id"):
        await cb.answer("Не вижу у вас заказа. Нажмите «Выбрать пакет».", show_alert=True)
        return

    order = db_get_order(u["last_invoice_id"])
    if not order:
        await cb.answer("Не вижу у вас заказа. Нажмите «Выбрать пакет».", show_alert=True)
        return

    if order.get("status") != "paid":
        await cb.answer("Ссылка появится после успешной оплаты 🙂", show_alert=True)
        return

    link = await issue_link()
    await cb.message.answer(
        "Вот ваша персональная ссылка для входа в закрытую группу.\n"
        "Ссылка одноразовая и действует 24 часа.\n\n"
        "Если вы покупали доступ для ребёнка, пожалуйста, не входите сами — просто перешлите ссылку ребёнку:\n"
        f"{link}"
    )
    await cb.answer("Отправил ✅")

@dp.callback_query(F.data == "support")
async def supp_cb(cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_text(f"Поддержка: @{ADMIN_USERNAME}", reply_markup=kb_main())

@dp.callback_query(F.data == "back")
async def back_cb(cb: CallbackQuery):
    await cb.message.edit_text("Меню:", reply_markup=kb_main())
    await cb.answer()

# ---------------- Webhooks ----------------
@app.get("/")
async def root():
    return {"status": "ok"}

@app.post("/telegram/webhook")
async def tg_wh(r: Request):
    await dp.feed_raw_update(bot, await r.json())
    return {"ok": True}

@app.get("/webhook/yookassa")
async def yk_wh_ping():
    return {"ok": True, "hint": "use POST for real notifications"}

@app.post("/webhook/yookassa")
async def yk_wh(r: Request):
    payload = await r.json()
    event = payload.get("event")
    obj = payload.get("object") or {}
    payment_id = obj.get("id")

    print("YOOKASSA_WEBHOOK_IN:", event, payment_id)

    if not payment_id:
        return {"ok": True}

    try:
        payment = yk_get_payment(payment_id)
    except Exception as e:
        print("YOOKASSA_GET_ERROR(webhook):", str(e))
        return {"ok": True}

    status = payment.get("status")
    meta = payment.get("metadata") or {}
    inv = meta.get("invoice_id")

    if event == "payment.succeeded" and status == "succeeded" and inv:
        await grant_access(inv)

    return {"ok": True}

@app.get("/return/{invoice_id}")
async def return_page(invoice_id: str):
    return {
        "message": "Спасибо! Если оплата прошла, бот пришлёт ссылку в течение минуты.",
        "invoice_id": invoice_id
    }

@app.on_event("startup")
async def on_startup():
    init_db()
    await bot.set_webhook(f"{PUBLIC_BASE_URL}/telegram/webhook")
