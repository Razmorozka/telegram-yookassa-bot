import os
import time
import uuid
from decimal import Decimal
from typing import Dict, Any, Optional

import requests
from fastapi import FastAPI, Request, HTTPException

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
# Для продакшна лучше Postgres, но “самое простое” — так.
USERS: Dict[int, Dict[str, Any]] = {}     # user_id -> {"step":..., "name":..., "email":...}
ORDERS: Dict[str, Dict[str, Any]] = {}    # invoice_id -> {"user_id":..., "plan":..., "amount":..., "payment_id":..., "status":...}

# Пакеты — поменяйте как нужно
PLANS = {
    "basic": {"title": "Базовый доступ", "amount": Decimal("990.00")},
    "pro": {"title": "Полный доступ", "amount": Decimal("1990.00")},
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

def kb_pay(payment_url: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Перейти к оплате", url=payment_url)
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
    ЮKassa вернёт confirmation.confirmation_url, куда направляем пользователя. :contentReference[oaicite:1]{index=1}
    """
    url = "https://api.yookassa.ru/v3/payments"
    idempotence_key = str(uuid.uuid4())

    payload = {
        "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
        "capture": True,
        "confirmation": {
            "type": "redirect",
            # куда ЮKassa вернёт пользователя после оплаты
            "return_url": f"{PUBLIC_BASE_URL}/return/{invoice_id}",
        },
        "description": description,
        # Очень важно: metadata — чтобы в webhook достать invoice_id и user_id
        "metadata": {"invoice_id": invoice_id},
        # Для чека email можно передать через receipt (зависит от ваших настроек онлайн-кассы/54-ФЗ)
        # Если чек у вас формируется на стороне ЮKassa/партнёра — оставьте receipt.
        "receipt": {
            "customer": {"email": email},
            "items": [
                {
                    "description": description,
                    "quantity": "1.00",
                    "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
                    "vat_code": 1,
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

    ORDERS[invoice_id] = {
        "user_id": uid,
        "plan": plan_id,
        "amount": str(amount),
        "status": "created",
        "payment_id": None,
    }

    try:
        payment = create_yookassa_payment(
            invoice_id=invoice_id,
            amount=amount,
            description=f"Доступ к курсу: {title}",
            email=user["email"],
        )
    except Exception:
        await cb.answer("Не получилось создать оплату. Попробуйте позже.", show_alert=True)
        return

    payment_id = payment.get("id")
    confirmation_url = (payment.get("confirmation") or {}).get("confirmation_url")

    if not payment_id or not confirmation_url:
        await cb.answer("Проблема с оплатой. Напишите в поддержку.", show_alert=True)
        return

    ORDERS[invoice_id]["payment_id"] = payment_id
    ORDERS[invoice_id]["status"] = "pending"

    await cb.message.edit_text(
        f"Пакет: {title}\nСумма: {amount} ₽\n\n"
        "Нажмите кнопку ниже, оплатите, и я сразу пришлю ссылку в закрытую группу ✅",
        reply_markup=kb_pay(confirmation_url),
    )
    await cb.answer()

@dp.callback_query(F.data == "support")
async def support(cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_text(
        "Поддержка: напишите @your_support\n\n"
        "Если оплата прошла, а ссылки нет — просто пришлите email и время оплаты 🙂",
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
        "Вот персональная ссылка для входа в закрытую группу (одноразовая, действует 24 часа):\n"
        f"{link}"
    )

# ---------------- Webhooks ----------------
@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    await dp.feed_raw_update(bot, update)
    return {"ok": True}

@app.post("/webhook/yookassa")
async def yookassa_webhook(request: Request):
    """
    Надёжная проверка: не доверяем “на слово” входящему webhook,
    а берем payment_id из payload и запрашиваем payment в ЮKassa по API,
    убеждаемся, что status == succeeded и metadata.invoice_id совпадает.
    """
    payload = await request.json()

    event = payload.get("event")
    obj = payload.get("object") or {}
    payment_id = obj.get("id")

    if not payment_id:
        return {"ok": True}

    # Проверяем реальный статус в ЮKassa
    try:
        payment = get_yookassa_payment(payment_id)
    except Exception:
        # на всякий случай не падаем — ЮKassa может ретраить
        return {"ok": True}

    status = payment.get("status")
    meta = payment.get("metadata") or {}
    invoice_id = meta.get("invoice_id")

    if event == "payment.succeeded" and status == "succeeded" and invoice_id:
        await grant_access_by_invoice(invoice_id)

    return {"ok": True}

@app.get("/return/{invoice_id}")
async def return_page(invoice_id: str):
    # Страница “вы вернулись после оплаты”
    # Здесь можно сделать простую заглушку
    return {"message": "Спасибо! Если оплата прошла, бот пришлёт ссылку в течение минуты.", "invoice_id": invoice_id}

@app.on_event("startup")
async def on_startup():
    # Ставим вебхук Телеграм
    await bot.set_webhook(f"{PUBLIC_BASE_URL}/telegram/webhook")
