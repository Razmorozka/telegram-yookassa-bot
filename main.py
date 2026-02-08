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

ADMIN_USERNAME = "kairos_007"     # техподдержка (без @)
EXPERT_USERNAME = "Liya_Sharova"  # эксперт (без @)
SECRET_WORD = "лапки-лапки"

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
    for k, v in kwargs.items():
        current[k] = v

    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO users (user_id, name, email, step, last_invoice_id)
            VALUES (:user_id, :name, :email, :step, :last_invoice_id)
        """, current)
        conn.commit()


def db_create_order(invoice_id, user_id, plan_id, amount, status, payment_id):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO orders (invoice_id, user_id, plan_id, amount, status, payment_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (invoice_id, user_id, plan_id, str(amount), status, payment_id, int(time.time())))
        conn.commit()


def db_get_order(invoice_id: str):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.execute("""
            SELECT invoice_id, user_id, plan_id, amount, status, payment_id, created_at
            FROM orders
            WHERE invoice_id = ?
        """, (invoice_id,))
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


# ---------------- Plans ----------------
PLANS = {
    "test": {
        "title": "🧪 Тест за 1 ₽",
        "amount": Decimal("1.00"),
        "description": 'ТЕСТ: доступ к материалам "Самодисциплина без стресса"',
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


# ---------------- Bot/App ----------------
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
app = FastAPI()


# ---------------- Keyboards ----------------
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


def kb_pay(url: str, inv_id: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Оплатить", url=url)
    kb.button(text="✅ Проверить оплату", callback_data=f"check:{inv_id}")
    kb.button(text="📩 Тех. поддержка", url=f"https://t.me/{ADMIN_USERNAME}")
    kb.button(text="⬅️ Назад", callback_data="choose_plan")
    kb.adjust(1)
    return kb.as_markup()


# ---------------- Access logic ----------------
async def issue_link() -> str:
    expire_date = int(time.time()) + 24 * 3600
    invite = await bot.create_chat_invite_link(
        chat_id=GROUP_ID,
        member_limit=1,
        expire_date=expire_date,
    )
    return invite.invite_link


async def grant_access(inv_id: str):
    order = db_get_order(inv_id)
    if not order or order["status"] == "paid":
        return

    # помечаем paid сразу (чтобы не дублировать выдачу)
    db_update_order_status(inv_id, "paid")

    user = db_get_user(order["user_id"]) or {}
    name = user.get("name") or "Друг"
    plan_id = order.get("plan_id")

    try:
        link = await issue_link()
    except Exception as e:
        await bot.send_message(
            order["user_id"],
            f"✅ Оплата подтверждена (заказ `{inv_id}`), но не получилось создать ссылку.\n"
            f"Напишите @{ADMIN_USERNAME}\n\nОшибка: {str(e)}"
        )
        return

    msg = (
        f"Ура, {name}! 🎉 Оплата подтверждена.\n"
        f"🆔 Номер заказа: `{inv_id}`\n\n"
        f"⬇️ **ПЕРЕШЛИТЕ ЭТО СООБЩЕНИЕ РЕБЕНКУ** ⬇️\n\n"
        f"Привет! Твой доступ готов:\n"
        f"1️⃣ Вступай в закрытую группу по ссылке: {link}\n\n"
        f"⚠️ Ссылка одноразовая и действует 24 часа.\n"
        f"Если доступ покупали для ребёнка — пожалуйста, не входите сами, просто перешлите ссылку."
    )

    if plan_id in ("pro", "test"):
        msg += (
            "\n\n2️⃣ Твой пакет включает личное сопровождение.\n"
            f"Напиши эксперту: @{EXPERT_USERNAME}\n"
            f"Секретное слово: `{SECRET_WORD}`\n"
            f"Номер заказа: `{inv_id}`"
        )

    await bot.send_message(order["user_id"], msg)


async def reminder_task(inv_id: str):
    # мягкое напоминание через час, если не оплатил
    await asyncio.sleep(3600)
    order = db_get_order(inv_id)
    if order and order["status"] == "pending":
        try:
            await bot.send_message(
                order["user_id"],
                f"Похоже, оплата не завершена.\nЕсли нужна помощь — напишите @{ADMIN_USERNAME} 🙂"
            )
        except:
            pass


def yk_create_payment(inv_id: str, amount: Decimal, description: str, email: str) -> dict:
    """
    ВАЖНО: 54-ФЗ включён => receipt обязателен.
    Делаем receipt в самом простом и совместимом виде (как работало раньше):
    description/quantity/amount/vat_code без payment_mode/payment_subject.
    vat_code=1 => "без НДС"
    """
    url = "https://api.yookassa.ru/v3/payments"
    headers = {"Idempotence-Key": str(uuid.uuid4()), "Content-Type": "application/json"}

    payload = {
        "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": f"{PUBLIC_BASE_URL}/return/{inv_id}"},
        "description": description,
        "metadata": {"invoice_id": inv_id},
        "receipt": {
            "customer": {"email": email},
            "items": [
                {
                    "description": description,
                    "quantity": "1.00",
                    "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
                    "vat_code": 1,  # без НДС
                }
            ],
        },
    }

    r = requests.post(url, auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY), headers=headers, json=payload, timeout=20)
    if r.status_code not in (200, 201):
        # это увидишь в Railway logs, если опять будет 400
        raise RuntimeError(f"YooKassa create payment error: {r.status_code} {r.text}")

    return r.json()


def yk_get_payment(payment_id: str) -> dict:
    url = f"https://api.yookassa.ru/v3/payments/{payment_id}"
    r = requests.get(url, auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY), timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"YooKassa get payment error: {r.status_code} {r.text}")
    return r.json()


# ---------------- Handlers ----------------
@dp.message(CommandStart())
async def start(m: Message):
    u = db_get_user(m.from_user.id)

    # если уже есть имя+email => не спрашиваем заново
    if u and u.get("step") == "done" and u.get("name") and u.get("email"):
        await m.answer(f"С возвращением, {u['name']} 🙂\nВыбирай пакет:", reply_markup=kb_main())
        return

    # иначе начинаем сбор
    db_upsert_user(m.from_user.id, name=None, email=None, step="name", last_invoice_id=None)
    await m.answer("Привет! 🙂 Я помогу оформить доступ.\n\nКак тебя зовут?")


@dp.message(Command("reset"))
async def reset(m: Message):
    db_upsert_user(m.from_user.id, name=None, email=None, step="name", last_invoice_id=None)
    await m.answer("Ок, сбросил данные. Как тебя зовут? 🙂")


@dp.message(Command("buy"))
async def buy(m: Message):
    u = db_get_user(m.from_user.id)
    if not u or u.get("step") != "done":
        await m.answer("Сначала нажми /start и введи имя + email 🙂")
        return
    await m.answer("Выбирай пакет:", reply_markup=kb_main())


@dp.chat_member()
async def welcome_new_member(event: ChatMemberUpdated):
    if event.new_chat_member.status == "member":
        try:
            await bot.send_message(
                event.chat.id,
                "Добро пожаловать! 👋\nИзучи правила в закрепе.\nЕсли пакет с сопровождением — напиши эксперту 🙂"
            )
        except:
            pass


@dp.message()
async def flow(m: Message):
    if m.chat.type in ["group", "supergroup"]:
        return

    u = db_get_user(m.from_user.id)
    if not u:
        await m.answer("Нажми /start 🙂")
        return

    step = u.get("step")

    if step == "name":
        name = (m.text or "").strip()
        if len(name) < 2:
            await m.answer("Напиши имя чуть понятнее 🙂")
            return
        db_upsert_user(m.from_user.id, name=name, step="email")
        await m.answer(f"Приятно познакомиться, {name}! 😊\nТеперь укажи email для чека:")
        return

    if step == "email":
        email = (m.text or "").strip()
        if "@" not in email or "." not in email:
            await m.answer("Похоже, email с ошибкой. Попробуй ещё раз 🙂")
            return
        db_upsert_user(m.from_user.id, email=email, step="done")
        await m.answer("Готово ✅ Выбирай пакет:", reply_markup=kb_main())
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
    amount = PLANS[pid]["amount"]
    desc = PLANS[pid]["description"]

    try:
        payment = yk_create_payment(inv_id, amount, desc, u["email"])
        payment_id = payment.get("id")
        confirmation_url = (payment.get("confirmation") or {}).get("confirmation_url")

        if not payment_id or not confirmation_url:
            raise RuntimeError(f"Bad YooKassa response: {payment}")

        db_create_order(inv_id, cb.from_user.id, pid, amount, "pending", payment_id)
        db_upsert_user(cb.from_user.id, last_invoice_id=inv_id)

        asyncio.create_task(reminder_task(inv_id))

        await cb.message.edit_text(
            f"Пакет: {PLANS[pid]['title']}\n"
            f"Сумма: {amount} ₽\n\n"
            "Нажмите «💳 Оплатить».\n"
            "После оплаты нажмите «✅ Проверить оплату».",
            reply_markup=kb_pay(confirmation_url, inv_id),
        )
        await cb.answer()

    except Exception as e:
        # это будет видно в Railway logs
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
    except Exception as e:
        print("YOOKASSA_GET_ERROR:", str(e))
        await cb.answer("Не получилось проверить оплату. Попробуйте ещё раз.", show_alert=True)
        return

    if p.get("status") == "succeeded":
        await grant_access(inv_id)
        await cb.answer("Оплата подтверждена ✅")
        return

    await cb.answer(f"Пока статус: {p.get('status')}", show_alert=True)


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


@app.post("/webhook/yookassa")
async def yk_wh(r: Request):
    """
    Webhook от YooKassa.
    Не доверяем payload'у на слово — проверяем платеж по API,
    и если succeeded — выдаём доступ.
    """
    data = await r.json()
    event = data.get("event")
    obj = data.get("object") or {}
    payment_id = obj.get("id")

    if event != "payment.succeeded" or not payment_id:
        return {"ok": True}

    try:
        payment = yk_get_payment(payment_id)
    except Exception as e:
        print("YOOKASSA_WEBHOOK_GET_ERROR:", str(e))
        return {"ok": True}

    if payment.get("status") != "succeeded":
        return {"ok": True}

    inv_id = (payment.get("metadata") or {}).get("invoice_id")
    if inv_id:
        await grant_access(inv_id)

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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
