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

# ---------------- ENV ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")
try:
    GROUP_ID = int(os.getenv("GROUP_ID", "0"))
except:
    GROUP_ID = 0

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")

ADMIN_USERNAME = "kairos_007"
EXPERT_USERNAME = "Liya_Sharova"
SECRET_WORD = "лапки-лапки"

# ---------------- DB ----------------
DB_FILE = "bot_database.db"

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, name TEXT, email TEXT, step TEXT, last_invoice_id TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS orders (invoice_id TEXT PRIMARY KEY, user_id INTEGER, plan_id TEXT, amount TEXT, status TEXT, payment_id TEXT, created_at INTEGER)")
        conn.commit()

def db_get_all_users():
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.execute("SELECT user_id FROM users")
        return [row[0] for row in cur.fetchall()]

def db_get_user(user_id: int):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        return {"user_id": row[0], "name": row[1], "email": row[2], "step": row[3], "last_invoice_id": row[4]} if row else None

def db_upsert_user(user_id: int, **kwargs):
    current = db_get_user(user_id) or {"user_id": user_id, "name": None, "email": None, "step": None, "last_invoice_id": None}
    for key, value in kwargs.items(): current[key] = value
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("INSERT OR REPLACE INTO users VALUES (:user_id, :name, :email, :step, :last_invoice_id)", current)

def db_create_order(invoice_id, user_id, plan_id, amount, status, payment_id):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?)", (invoice_id, user_id, plan_id, str(amount), status, payment_id, int(time.time())))

def db_get_order(invoice_id: str):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.execute("SELECT * FROM orders WHERE invoice_id = ?", (invoice_id,))
        row = cur.fetchone()
        return {"invoice_id": row[0], "user_id": row[1], "plan_id": row[2], "amount": row[3], "status": row[4], "payment_id": row[5]} if row else None

def db_update_order_status(invoice_id: str, status: str):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("UPDATE orders SET status = ? WHERE invoice_id = ?", (status, invoice_id))

# ---------------- Настройки ----------------
PLANS = {
    "test": {"title": "🧪 Тест за 1 ₽", "amount": Decimal("1.00"), "description": "Тестовый доступ"},
    "basic": {"title": "Войти в группу", "amount": Decimal("2400.00"), "description": "Доступ к материалам"},
    "pro": {"title": "С сопровождением", "amount": Decimal("5400.00"), "description": "Материалы + Лия Шарова"}
}

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
app = FastAPI()

# ---------------- Клавиатуры (Явные) ----------------
def kb_main():
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Выбрать пакет", callback_data="choose_plan")
    kb.button(text="❓ Поддержка", callback_data="support")
    return kb.adjust(1).as_markup()

def kb_plans():
    kb = InlineKeyboardBuilder()
    for pid, p in PLANS.items():
        kb.button(text=f"{p['title']} — {p['amount']} ₽", callback_data=f"plan:{pid}")
    kb.button(text="⬅️ Назад", callback_data="back")
    return kb.adjust(1).as_markup()

def kb_pay(url, inv_id):
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Оплатить", url=url)
    kb.button(text="✅ Проверить оплату", callback_data=f"check:{inv_id}")
    kb.button(text="📩 Тех. поддержка", url=f"https://t.me/{ADMIN_USERNAME}")
    return kb.adjust(1).as_markup()

# ---------------- Логика ----------------
async def issue_link():
    try:
        # Ссылка живет 24 часа (86400 секунд)
        expire_at = int(time.time()) + 86400
        res = await bot.create_chat_invite_link(chat_id=GROUP_ID, member_limit=1, expire_date=expire_at)
        return res.invite_link
    except Exception as e: return f"Ошибка API: {str(e)}"

async def grant_access(inv_id):
    order = db_get_order(inv_id)
    if not order or order["status"] == "paid": return
    
    # Двойная проверка через API ЮKassa для безопасности
    r = requests.get(f"https://api.yookassa.ru/v3/payments/{order['payment_id']}", auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)).json()
    if r.get("status") != "succeeded": return

    db_update_order_status(inv_id, "paid")
    user = db_get_user(order["user_id"])
    link = await issue_link()
    name = user.get("name", "Друг")
    
    msg = (
        f"Ура, {name}! 🎉 Оплата подтверждена.\n"
        f"🆔 Номер заказа: `{inv_id}`\n\n"
        f"⬇️ **ПЕРЕШЛИТЕ ЭТО СООБЩЕНИЕ РЕБЕНКУ** ⬇️\n\n"
        f"Привет! Твой доступ к курсу готов:\n"
        f"1️⃣ Вступай в группу: {link}\n"
    )
    if order["plan_id"] in ["pro", "test"]:
        msg += (
            f"2️⃣ У тебя пакет с сопровождением!\n"
            f"Напиши Лие Шаровой: @{EXPERT_USERNAME}\n"
            f"Отправь секретное слово: `{SECRET_WORD}`\n"
            f"И номер заказа: `{inv_id}`\n"
        )
    msg += "\n⚠️ Ссылка действует 24 часа."
    await bot.send_message(order["user_id"], msg)

# ---------------- Handlers ----------------
@dp.message(CommandStart())
async def start(m: Message):
    db_upsert_user(m.from_user.id, name=m.from_user.first_name, step="name")
    await m.answer(f"Привет! 🙂 Как тебя зовут?")

@dp.message(Command("test_link"))
async def test_cmd(m: Message):
    await m.answer(f"Тест (на 24ч): {await issue_link()}")

@dp.message()
async def flow(m: Message):
    if m.chat.type in ["group", "supergroup"]: return
    u = db_get_user(m.from_user.id)
    if not u: return
    if u["step"] == "name":
        db_upsert_user(m.from_user.id, name=m.text, step="email")
        await m.answer(f"Приятно познакомиться! 😊 Теперь укажи email:")
    elif u["step"] == "email":
        if "@" not in m.text: return await m.answer("Введи корректный email")
        db_upsert_user(m.from_user.id, email=m.text, step="done")
        await m.answer("Выбирай пакет:", reply_markup=kb_main())

@dp.callback_query(F.data == "choose_plan")
async def plans_cb(cb: CallbackQuery): await cb.message.edit_text("Пакеты:", reply_markup=kb_plans())

@dp.callback_query(F.data.startswith("plan:"))
async def pay_cb(cb: CallbackQuery):
    pid = cb.data.split(":")[1]
    u = db_get_user(cb.from_user.id)
    inv_id = f"inv_{cb.from_user.id}_{int(time.time())}"
    try:
        res = requests.post(
            "https://api.yookassa.ru/v3/payments",
            auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
            headers={"Idempotence-Key": str(uuid.uuid4()), "Content-Type": "application/json"},
            json={
                "amount": {"value": f"{PLANS[pid]['amount']:.2f}", "currency": "RUB"},
                "capture": True,
                "confirmation": {"type": "redirect", "return_url": f"{PUBLIC_BASE_URL}/return/{inv_id}"},
                "description": PLANS[pid]["description"],
                "metadata": {"invoice_id": inv_id},
                "receipt": {
                    "customer": {"email": u["email"]},
                    "items": [{
                        "description": PLANS[pid]["description"],
                        "quantity": "1.00",
                        "amount": {"value": f"{PLANS[pid]['amount']:.2f}", "currency": "RUB"},
                        "vat_code": 1,
                        "payment_subject": "service" # Тот самый важный параметр
                    }]
                }
            }
        ).json()
        db_create_order(inv_id, cb.from_user.id, pid, PLANS[pid]["amount"], "pending", res["id"])
        await cb.message.edit_text(f"К оплате: {PLANS[pid]['amount']} ₽", reply_markup=kb_pay(res["confirmation"]["confirmation_url"], inv_id))
    except: await cb.answer("Ошибка связи с банком.", show_alert=True)

@dp.callback_query(F.data.startswith("check:"))
async def check_cb(cb: CallbackQuery):
    await grant_access(cb.data.split(":")[1])
    order = db_get_order(cb.data.split(":")[1])
    if order["status"] != "paid": await cb.answer("Оплата пока не прошла ⏳", show_alert=True)

@dp.callback_query(F.data == "support")
async def supp_cb(cb: CallbackQuery): await cb.message.answer(f"Поддержка: @{ADMIN_USERNAME}")

@dp.callback_query(F.data == "back")
async def back_cb(cb: CallbackQuery): await cb.message.edit_text("Меню:", reply_markup=kb_main())

# ---------------- Webhooks ----------------
@app.post("/telegram/webhook")
async def tg_wh(r: Request):
    await dp.feed_raw_update(bot, await r.json())
    return {"ok": True}

@app.post("/webhook/yookassa")
async def yk_wh(r: Request):
    d = await r.json()
    if d.get("event") == "payment.succeeded":
        inv = d["object"].get("metadata", {}).get("invoice_id")
        if inv: await grant_access(inv) # Тут внутри теперь есть проверка через API
    return {"ok": True}

@app.on_event("startup")
async def on_startup():
    init_db()
    await bot.set_webhook(f"{PUBLIC_BASE_URL}/telegram/webhook")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
