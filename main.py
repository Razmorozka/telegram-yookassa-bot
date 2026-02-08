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
except:
    GROUP_ID = 0

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")

# Контакты и настройки
ADMIN_USERNAME = "kairos_007"    # Тех. поддержка
EXPERT_USERNAME = "Liya_Sharova" # Эксперт Лия
SECRET_WORD = "лапки-лапки"

# ---------------- Basic checks ----------------
if not BOT_TOKEN or not PUBLIC_BASE_URL:
    raise RuntimeError("Нужно задать BOT_TOKEN и PUBLIC_BASE_URL в ENV")
if not GROUP_ID:
    raise RuntimeError("Нужно задать GROUP_ID (ID закрытой группы) в ENV")
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
        cur = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if not row:
            return None
        return {
            "user_id": row[0],
            "name": row[1],
            "email": row[2],
            "step": row[3],
            "last_invoice_id": row[4]
        }

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
        conn.commit()

def db_create_order(invoice_id, user_id, plan_id, amount, status, payment_id):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO orders VALUES (?, ?, ?, ?, ?, ?, ?)",
            (invoice_id, user_id, plan_id, str(amount), status, payment_id, int(time.time()))
        )
        conn.commit()

def db_get_order(invoice_id: str):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.execute("SELECT * FROM orders WHERE invoice_id = ?", (invoice_id,))
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
        conn.commit()

def db_get_order_by_payment_id(payment_id: str):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.execute("SELECT invoice_id FROM orders WHERE payment_id = ?", (payment_id,))
        row = cur.fetchone()
        return row[0] if row else None

# ---------------- Настройки пакетов ----------------
PLANS = {
    "test":  {"title": "🧪 Тест за 1 ₽",      "amount": Decimal("1.00"),    "description": 'ТЕСТ: материалы "Самодисциплина без стресса"'},
    "basic": {"title": "Войти в группу",     "amount": Decimal("2400.00"), "description": 'Доступ к материалам "Самодисциплина без стресса"'},
    "pro":   {"title": "С сопровождением",   "amount": Decimal("5400.00"), "description": 'Доступ к материалам "Самодисциплина без стресса" с сопровождением'},
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

def kb_pay(url, inv_id):
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Оплатить", url=url)
    kb.button(text="✅ Проверить оплату", callback_data=f"check:{inv_id}")
    kb.button(text="📩 Тех. поддержка", url=f"https://t.me/{ADMIN_USERNAME}")
    kb.button(text="⬅️ Назад", callback_data="choose_plan")
    kb.adjust(1)
    return kb.as_markup()

# ---------------- YooKassa helpers ----------------
def yk_auth():
    return (YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)

def yk_get_payment(payment_id: str) -> dict:
    r = requests.get(
        f"https://api.yookassa.ru/v3/payments/{payment_id}",
        auth=yk_auth(),
        timeout=20
    )
    return r.json()

# ---------------- Логика выдачи доступа ----------------
async def issue_link():
    # одноразовая, на 24 часа
    expire = int(time.time()) + 24 * 3600
    res = await bot.create_chat_invite_link(
        chat_id=GROUP_ID,
        member_limit=1,
        expire_date=expire,
    )
    return res.invite_link

async def grant_access(inv_id: str):
    order = db_get_order(inv_id)
    if not order:
        print("GRANT_ACCESS: order not found", inv_id)
        return
    if order["status"] == "paid":
        print("GRANT_ACCESS: already paid", inv_id)
        return

    db_update_order_status(inv_id, "paid")

    user = db_get_user(order["user_id"])
    link = await issue_link()

    name = (user or {}).get("name") or "Друг"
    plan_id = order.get("plan_id")

    msg = (
        f"Ура, {name}! 🎉 Оплата подтверждена.\n"
        f"🆔 Номер заказа: `{inv_id}`\n\n"
        f"⬇️ **ПЕРЕШЛИТЕ ЭТО СООБЩЕНИЕ РЕБЕНКУ** ⬇️\n\n"
        f"Привет! Твой доступ к курсу готов:\n"
        f"1️⃣ Вступай в закрытую группу: {link}\n\n"
        f"⚠️ Ссылка одноразовая и действует 24 часа.\n"
        f"Если доступ покупали для ребёнка — пожалуйста, не входите сами, просто перешлите ссылку."
    )

    if plan_id in ("pro", "test"):
        msg += (
            f"\n\n2️⃣ Твой пакет включает **личное сопровождение**.\n"
            f"Напиши эксперту: @{EXPERT_USERNAME}\n"
            f"Секретное слово: `{SECRET_WORD}`\n"
            f"И номер заказа: `{inv_id}`"
        )

    await bot.send_message(order["user_id"], msg)
    print("GRANT_ACCESS: sent link", inv_id)

async def reminder_task(inv_id):
    await asyncio.sleep(3600)
    order = db_get_order(inv_id)
    if order and order["status"] == "pending":
        try:
            await bot.send_message(
                order["user_id"],
                f"Заметили, что вы не завершили оплату 😊\nНужна помощь? Пишите @{ADMIN_USERNAME}"
            )
        except:
            pass

# ---------------- Handlers ----------------

@dp.message(CommandStart())
async def start(m: Message):
    # ты сам просил: /start = заново (для тестов)
    db_upsert_user(m.from_user.id, step="name", last_invoice_id=None)
    await m.answer(
        "Привет! 🙂 Я помогу оформить доступ в закрытую группу.\n\n"
        "Как мне лучше к тебе обращаться? Напиши своё имя:"
    )

@dp.message(Command("buy"))
async def buy(m: Message):
    u = db_get_user(m.from_user.id)
    if not u or u.get("step") != "done":
        await m.answer("Сначала введи имя и email — нажми /start 🙂")
        return
    await m.answer("Выбирай пакет участия:", reply_markup=kb_main())

@dp.chat_member()
async def welcome_new_member(event: ChatMemberUpdated):
    if event.new_chat_member.status == "member":
        try:
            await bot.send_message(
                event.chat.id,
                "Добро пожаловать в группу! 👋\n\n"
                "Изучи правила в закрепленном сообщении."
            )
        except:
            pass

@dp.message(Command("test_link"))
async def test_cmd(m: Message):
    await m.answer(f"Тест генерации ссылки: {await issue_link()}")

@dp.message(Command("broadcast"))
async def broadcast_cmd(m: Message):
    if (m.from_user.username or "") != ADMIN_USERNAME:
        return
    text = m.text.replace("/broadcast", "").strip()
    if not text:
        return await m.answer("Введите текст после команды")
    users = db_get_all_users()
    count = 0
    for uid in users:
        try:
            await bot.send_message(uid, text)
            count += 1
            await asyncio.sleep(0.05)
        except:
            continue
    await m.answer(f"📢 Рассылка завершена. Получили {count} чел.")

@dp.message()
async def flow(m: Message):
    if m.chat.type in ["group", "supergroup"]:
        return

    u = db_get_user(m.from_user.id)
    if not u:
        await m.answer("Нажми /start 🙂")
        return

    if u.get("step") == "name":
        name = (m.text or "").strip()
        if len(name) < 2:
            await m.answer("Напиши имя чуть понятнее 🙂")
            return
        db_upsert_user(m.from_user.id, name=name, step="email")
        await m.answer(f"Приятно познакомиться, {name}! 😊\nТеперь укажи email для чека:")
        return

    if u.get("step") == "email":
        email = (m.text or "").strip()
        if "@" not in email or "." not in email:
            await m.answer("Введи корректный email 🙂")
            return
        db_upsert_user(m.from_user.id, email=email, step="done")
        await m.answer("Готово! Выбирай пакет участия:", reply_markup=kb_main())
        return

    # done
    await m.answer("Выбирай действие кнопками ниже 🙂", reply_markup=kb_main())

@dp.callback_query(F.data == "choose_plan")
async def plans_cb(cb: CallbackQuery):
    await cb.message.edit_text("Доступные пакеты:", reply_markup=kb_plans())
    await cb.answer()

@dp.callback_query(F.data.startswith("plan:"))
async def pay_cb(cb: CallbackQuery):
    pid = cb.data.split(":")[1]
    if pid not in PLANS:
        await cb.answer("Неизвестный пакет", show_alert=True)
        return

    u = db_get_user(cb.from_user.id)
    if not u or u.get("step") != "done" or not u.get("email"):
        await cb.answer()
        await cb.message.edit_text("Сначала нажми /start и введи имя + email 🙂")
        return

    inv_id = f"inv_{cb.from_user.id}_{int(time.time())}"

    try:
        payload = {
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
                    "payment_subject": "service",  # важно для части касс
                }]
            }
        }

        r = requests.post(
            "https://api.yookassa.ru/v3/payments",
            auth=yk_auth(),
            headers={"Idempotence-Key": str(uuid.uuid4()), "Content-Type": "application/json"},
            json=payload,
            timeout=20
        )

        if r.status_code not in (200, 201):
            print("YOOKASSA_CREATE_ERROR:", r.status_code, r.text)
            await cb.answer("Ошибка связи с платежной системой.", show_alert=True)
            return

        res = r.json()
        payment_id = res.get("id")
        confirmation_url = (res.get("confirmation") or {}).get("confirmation_url")

        if not payment_id or not confirmation_url:
            print("YOOKASSA_CREATE_BAD_RESPONSE:", res)
            await cb.answer("Не получилось создать оплату. Попробуйте позже.", show_alert=True)
            return

        db_create_order(inv_id, cb.from_user.id, pid, PLANS[pid]["amount"], "pending", payment_id)
        db_upsert_user(cb.from_user.id, last_invoice_id=inv_id)

        asyncio.create_task(reminder_task(inv_id))

        await cb.message.edit_text(
            f"К оплате: {PLANS[pid]['amount']} ₽\n\n"
            "Нажмите «💳 Оплатить».\n"
            "Если оплатили, а ссылка не пришла — нажмите «✅ Проверить оплату».",
            reply_markup=kb_pay(confirmation_url, inv_id)
        )
        await cb.answer()

    except Exception as e:
        print("PAY_CB_ERROR:", str(e))
        await cb.answer("Ошибка связи с платежной системой.", show_alert=True)

@dp.callback_query(F.data.startswith("check:"))
async def check_cb(cb: CallbackQuery):
    inv_id = cb.data.split(":", 1)[1]
    order = db_get_order(inv_id)
    if not order:
        await cb.answer("Заказ не найден.", show_alert=True)
        return

    payment_id = order.get("payment_id")
    if not payment_id:
        await cb.answer("Не вижу payment_id. Напишите в поддержку.", show_alert=True)
        return

    try:
        pay = yk_get_payment(payment_id)
        status = pay.get("status")
        if status == "succeeded":
            await grant_access(inv_id)
            await cb.answer("Оплата подтверждена ✅")
            return

        await cb.answer(f"Пока статус: {status} ⏳", show_alert=True)

    except Exception as e:
        print("CHECK_CB_ERROR:", str(e))
        await cb.answer("Не получилось проверить оплату. Попробуйте ещё раз.", show_alert=True)

@dp.callback_query(F.data == "support")
async def supp_cb(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer(f"Тех. поддержка: @{ADMIN_USERNAME}")

@dp.callback_query(F.data == "back")
async def back_cb(cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_text("Меню:", reply_markup=kb_main())

# ---------------- Webhooks ----------------
@app.get("/")
async def root():
    return {"status": "ok"}

@app.post("/telegram/webhook")
async def tg_wh(r: Request):
    await dp.feed_raw_update(bot, await r.json())
    return {"ok": True}

# Пинг чтобы ты мог открыть URL в браузере и увидеть что живо
@app.get("/webhook/yookassa")
async def yk_ping():
    return {"ok": True, "hint": "POST сюда от ЮKassa"}

@app.post("/webhook/yookassa")
async def yk_wh(r: Request):
    d = await r.json()
    event = d.get("event")
    obj = d.get("object") or {}
    payment_id = obj.get("id")

    print("YOOKASSA_WEBHOOK_IN:", event, payment_id)

    # минимально усилили: если пришел payment_id — перепроверяем статус в API
    if event == "payment.succeeded" and payment_id:
        try:
            pay = yk_get_payment(payment_id)
            status = pay.get("status")
            inv = (pay.get("metadata") or {}).get("invoice_id")

            print("YOOKASSA_WEBHOOK_CHECK:", status, inv)

            if status == "succeeded" and inv:
                await grant_access(inv)

        except Exception as e:
            print("YOOKASSA_WEBHOOK_ERROR:", str(e))

    return {"ok": True}

@app.get("/return/{inv_id}")
async def return_page(inv_id: str):
    return {"message": "Спасибо! Можно вернуться в Telegram и нажать «✅ Проверить оплату», если ссылка не пришла.", "invoice_id": inv_id}

@app.on_event("startup")
async def on_startup():
    init_db()
    await bot.set_webhook(f"{PUBLIC_BASE_URL}/telegram/webhook")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
