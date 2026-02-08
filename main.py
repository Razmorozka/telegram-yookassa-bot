import os
import time
import uuid
from decimal import Decimal
from typing import Dict, Any

import requests
from fastapi import FastAPI, Request

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder


# ---------------- ENV ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")  # например https://xxxx.up.railway.app
GROUP_ID = int(os.getenv("GROUP_ID", "0"))

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")

# Админ для сопровождения
ADMIN_USERNAME = "kairos_007"  # без @


# ---------------- Basic checks ----------------
if not BOT_TOKEN or not PUBLIC_BASE_URL:
    raise RuntimeError("Нужно задать BOT_TOKEN и PUBLIC_BASE_URL в ENV")
if not GROUP_ID:
    raise RuntimeError("Нужно задать GROUP_ID (ID закрытой группы) в ENV")
if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
    raise RuntimeError("Нужно задать YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY в ENV")


# ---------------- Bot/App ----------------
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
app = FastAPI()


# ---------------- In-memory storage (для простоты) ----------------
USERS: Dict[int, Dict[str, Any]] = {}     # user_id -> {"step":..., "name":..., "email":..., "last_invoice_id":...}
ORDERS: Dict[str, Dict[str, Any]] = {}    # invoice_id -> {"user_id":..., "plan":..., "amount":..., "payment_id":..., "status":...}


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


def kb_pay(payment_url: str, plan_id: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Перейти к оплате", url=payment_url)

    # Для пакета сопровождения — кнопка написать админу
    if plan_id == "pro":
        kb.button(text="📩 Написать админу", url=f"https://t.me/{ADMIN_USERNAME}")

    # На случай: оплата прошла, а ссылка потерялась
    kb.button(text="🔁 Получить ссылку ещё раз", callback_data="resend_link")

    kb.button(text="⬅️ Назад", callback_data="choose_plan")
    kb.adjust(1)
    return kb.as_markup()


# ---------------- YooKassa helpers ----------------
def yk_auth():
    # BasicAuth: shopId:secretKey
    return (YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)


def create_yookassa_payment(invoice_id: str, amount: Decimal, description: str, email: str) -> Dict[str, Any]:
    """
    Создаём платеж в ЮKassa через POST /v3/payments.
    ЮKassa вернёт confirmation.confirmation_url, куда направляем пользователя.
    """
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
            "items": [
    {
        "description": description,
        "quantity": "1.00",
        "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
        "vat_code": 1,
        "payment_subject": "service",
        "payment_mode": "full_payment",
                }
            ],
        },
    }

    headers = {
        "Idempotence-Key": idempotence_key,
        "Content-Type": "application/json",
    }

    r = requests.post(url, auth=yk_auth(), json=payload, headers=headers, timeout=20)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"YooKassa create payment error: {r.status_code} {r.text}")
    return r.json()


def get_yookassa_payment(payment_id: str) -> Dict[str, Any]:
    url = f"https://api.yookassa.ru/v3/payments/{payment_id}"
    r = requests.get(url, auth=yk_auth(), timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"YooKassa get payment error: {r.status_code} {r.text}")
    return r.json()


# ---------------- Telegram handlers ----------------
@dp.message(CommandStart())
async def start(message: Message):
    USERS[message.from_user.id] = {"step": "name"}
    await message.answer(
        "Привет! 🙂\nЯ помогу оформить доступ в закрытую группу.\n\n"
        "Как тебя зовут?"
    )


@dp.message()
async def collect(message: Message):
    uid = message.from_user.id
    state = USERS.get(uid)

    if not state:
        await message.answer("Давай начнём сначала — нажми /start 🙂")
        return

    step = state.get("step")

    if step == "name":
        name = message.text.strip()
        if len(name) < 2:
            await message.answer("Напиши имя чуть понятнее 🙂")
            return
        state["name"] = name
        state["step"] = "email"
        await message.answer("Отлично! Теперь укажи email — туда придёт чек.")
        return

    if step == "email":
        email = message.text.strip()
        if "@" not in email or "." not in email:
            await message.answer("Похоже, email с ошибкой. Попробуй ещё раз 🙂")
            return
        state["email"] = email
        state["step"] = "done"
        await message.answer(
            f"{state['name']}, супер ✅\nВыбирай пакет:",
            reply_markup=kb_main()
        )
        return

        await message.answer("Выбирай действие кнопками ниже 🙂", reply_markup=kb_main())


@dp.callback_query(F.data == "choose_plan")
async def choose_plan(cb: CallbackQuery):
    await cb.message.edit_text("Выберите пакет:", reply_markup=kb_plans())
    await cb.answer()


@dp.callback_query(F.data.startswith("plan:"))
async def plan(cb: CallbackQuery):
    uid = cb.from_user.id
    plan_id = cb.data.split(":", 1)[1]
    if plan_id not in PLANS:
        await cb.answer("Неизвестный пакет", show_alert=True)
        return

    user = USERS.get(uid, {})
    if user.get("step") != "done":
        await cb.answer()
        await cb.message.edit_text("Нажми /start и введи имя + email 🙂")
        return

    invoice_id = f"inv_{uid}_{int(time.time())}"
    amount = PLANS[plan_id]["amount"]
    title = PLANS[plan_id]["title"]
    yk_description = PLANS[plan_id].get("description") or f"Доступ: {title}"

    ORDERS[invoice_id] = {
        "user_id": uid,
        "plan": plan_id,
        "amount": str(amount),
        "status": "created",
        "payment_id": None,
        "created_at": int(time.time()),
    }
    USERS.setdefault(uid, {})["last_invoice_id"] = invoice_id

    try:
        payment = create_yookassa_payment(
            invoice_id=invoice_id,
            amount=amount,
            description=yk_description,
            email=user["email"],
        )
    except Exception as e:
        print("YOOKASSA_CREATE_ERROR:", str(e))
        await cb.answer("Не получилось создать оплату. Попробуйте позже.", show_alert=True)
        return

    payment_id = payment.get("id")
    confirmation_url = (payment.get("confirmation") or {}).get("confirmation_url")

    if not payment_id or not confirmation_url:
        print("YOOKASSA_CREATE_ERROR: bad response:", payment)
        await cb.answer("Проблема с оплатой. Напишите в поддержку.", show_alert=True)
        return

    ORDERS[invoice_id]["payment_id"] = payment_id
    ORDERS[invoice_id]["status"] = "pending"

    # ✅ Мягкая автопроверка оплаты (15 сек и 60 сек) — без спама
    import asyncio
    asyncio.create_task(auto_check_payment(invoice_id))

    await cb.message.edit_text(
        f"Пакет: {title}\n"
        f"Сумма: {amount} ₽\n\n"
        "Нажмите кнопку ниже, оплатите, и я пришлю ссылку в закрытую группу ✅\n\n"
        "Если оплатили, а ссылка не пришла — нажмите «✅ Я оплатил — проверить».",
        reply_markup=kb_pay(confirmation_url, plan_id, invoice_id),
    )
    await cb.answer()


@dp.callback_query(F.data == "resend_link")
async def resend_link(cb: CallbackQuery):
    uid = cb.from_user.id
    last_invoice_id = USERS.get(uid, {}).get("last_invoice_id")

    if not last_invoice_id or last_invoice_id not in ORDERS:
        await cb.answer("Не вижу у вас заказа. Нажмите «Выбрать пакет».", show_alert=True)
        return

    order = ORDERS[last_invoice_id]
    if order.get("status") != "paid":
        await cb.answer("Ссылка появится после успешной оплаты 🙂", show_alert=True)
        return

    link = await issue_one_time_invite()
    await cb.message.answer(
        "Вот ваша персональная ссылка для входа в закрытую группу.\n"
        "Ссылка одноразовая и действует 24 часа.\n\n"
        "Если вы покупали доступ для ребёнка, пожалуйста, не входите сами — просто перешлите ссылку ребёнку:\n"
        f"{link}"
    )
    await cb.answer("Отправил ✅")


@dp.callback_query(F.data.startswith("check:"))
async def check_payment(cb: CallbackQuery):
    invoice_id = cb.data.split(":", 1)[1]
    order = ORDERS.get(invoice_id)
    if not order:
        await cb.answer("Заказ не найден.", show_alert=True)
        return

    payment_id = order.get("payment_id")
    if not payment_id:
        await cb.answer("Не вижу payment_id. Напишите в поддержку.", show_alert=True)
        return

    try:
        payment = get_yookassa_payment(payment_id)
    except Exception as e:
        print("YOOKASSA_GET_ERROR:", str(e))
        await cb.answer("Не получилось проверить оплату. Попробуйте ещё раз.", show_alert=True)
        return

    status = payment.get("status")
    if status == "succeeded":
        await grant_access_by_invoice(invoice_id)
        await cb.answer("Оплата подтверждена ✅", show_alert=False)
        return

    await cb.answer(
        f"Пока статус: {status}. Если вы только что оплатили — подождите минуту и нажмите ещё раз 🙂",
        show_alert=True
    )


@dp.callback_query(F.data == "support")
async def support(cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_text(
        f"Поддержка: напишите @{ADMIN_USERNAME}\n\n"
        "Если оплата прошла, а ссылки нет — пришлите email и время оплаты 🙂",
        reply_markup=kb_main()
    )


@dp.callback_query(F.data == "back")
async def back(cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_text("Выбирай действие:", reply_markup=kb_main())


async def issue_one_time_invite() -> str:
    expire_date = int(time.time()) + 24 * 3600
    invite = await bot.create_chat_invite_link(
        chat_id=GROUP_ID,
        member_limit=1,
        expire_date=expire_date,
    )
    return invite.invite_link


async def grant_access_by_invoice(invoice_id: str):
    order = ORDERS.get(invoice_id)
    if not order or order.get("status") == "paid":
        return

    link = await issue_one_time_invite()
    order["status"] = "paid"

    uid = order["user_id"]
    await bot.send_message(
        uid,
        "Оплата подтверждена ✅\n\n"
        "Вот ваша персональная ссылка для входа в закрытую группу.\n"
        "Ссылка одноразовая и действует 24 часа.\n\n"
        "Если вы покупали доступ для ребёнка, пожалуйста, не входите сами — просто перешлите ссылку ребёнку:\n"
        f"{link}"
    )


async def auto_check_payment(invoice_id: str):
    """
    Мягкая автопроверка статуса оплаты:
    2 проверки (через 15 сек и через 60 сек), максимум 1 сообщение пользователю.
    """
    import asyncio

    order = ORDERS.get(invoice_id)
    if not order:
        return

    uid = order["user_id"]
    payment_id = order.get("payment_id")
    if not payment_id:
        return

    for delay in (15, 60):
        await asyncio.sleep(delay)

        order = ORDERS.get(invoice_id)
        if not order or order.get("status") == "paid":
            return

        try:
            payment = get_yookassa_payment(payment_id)
        except Exception as e:
            print("AUTO_CHECK_GET_ERROR:", str(e))
            continue

        status = payment.get("status")
        if status == "succeeded":
            await grant_access_by_invoice(invoice_id)
            return

    # одно мягкое сообщение, если спустя минуту оплаты нет
    order = ORDERS.get(invoice_id)
    if order and order.get("status") != "paid":
        await bot.send_message(
            uid,
            "Пока не вижу успешной оплаты.\n\n"
            "Если вы уже оплатили — нажмите «✅ Я оплатил — проверить».\n"
            "Если ещё нет — просто завершите оплату по кнопке «💳 Перейти к оплате» 🙂"
        )


# ---------------- Webhooks ----------------
@app.get("/")
async def root():
    return {"status": "ok"}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    await dp.feed_raw_update(bot, update)
    return {"ok": True}


# ✅ Пинг для проверки доступности URL из интернета
@app.get("/webhook/yookassa")
async def yookassa_webhook_ping():
    return {"ok": True, "hint": "use POST for real notifications"}


# ✅ Реальный webhook от ЮKassa
@app.post("/webhook/yookassa")
async def yookassa_webhook(request: Request):
    payload = await request.json()
    print("YOOKASSA_WEBHOOK_IN:", payload.get("event"))

    event = payload.get("event")
    obj = payload.get("object") or {}
    payment_id = obj.get("id")

    if not payment_id:
        return {"ok": True}

    try:
        payment = get_yookassa_payment(payment_id)
    except Exception as e:
        print("YOOKASSA_GET_ERROR:", str(e))
        return {"ok": True}

    status = payment.get("status")
    meta = payment.get("metadata") or {}
    invoice_id = meta.get("invoice_id")

    if event == "payment.succeeded" and status == "succeeded" and invoice_id:
        await grant_access_by_invoice(invoice_id)

    return {"ok": True}


@app.get("/return/{invoice_id}")
async def return_page(invoice_id: str):
    return {
        "message": "Спасибо! Если оплата прошла, бот пришлёт ссылку в течение минуты.",
        "invoice_id": invoice_id
    }


@app.on_event("startup")
async def on_startup():
    await bot.set_webhook(f"{PUBLIC_BASE_URL}/telegram/webhook")

