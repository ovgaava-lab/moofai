import asyncio
import logging
import sqlite3
import base64
import io
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery,
    LabeledPrice, PreCheckoutQuery
)
from openai import AsyncOpenAI

# ============ НАСТРОЙКИ ============
import os
BOT_TOKEN   = os.getenv("BOT_TOKEN")
ADMIN_ID    = int(os.getenv("ADMIN_ID", "0"))
LLMCOD_KEY  = os.getenv("LLMCOD_KEY")

# модели
TEXT_MODEL   = "qwen3.8"
VISION_MODEL = "qwen3.8"

# контекст диалога
CONTEXT_LIMIT = 30

# ============ ПОДПИСКА ============
TRIAL_LIMIT     = 15      # сколько бесплатных сообщений новым юзерам
SUB_DAYS        = 30     # на сколько дней подписка
SUB_PRICE_STARS = 70    # цена в звёздах Telegram

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
ai_client = AsyncOpenAI(api_key=LLMCOD_KEY, base_url="https://llmcod.ru/v1")

# ============ ПЕРСОНА ============
AI_SYSTEM_PROMPT = """Ты — moof AI, помощник в Telegram. Отвечай так, как отвечает опытный наставник по программированию: по делу, без воды, с конкретикой.

Основные правила:
- Отвечай на русском, обращайся на "ты".
- Никаких "конечно!", "с удовольствием!", "рад помочь". Сразу к делу.
- Никаких "могу дать", "могу помочь", "если хочешь", "дай знать", "обращайся". Либо делаешь сразу, либо говоришь прямо, что не можешь.
- Не заканчивай сообщение вопросами вроде "нужно ли", "сделать ли". Либо сделал, либо нет.

Как отвечать:
1. Если человек задал конкретный вопрос — отвечай конкретно, по сути. Без предисловий.
2. Если нужен код — дай рабочий код, а не описание. С коротким пояснением, что он делает.
3. Если человек застрял на ошибке — сначала спроси что именно не работает и что в логах (коротко), потом помоги.
4. Если человек просит инструкцию пошагово — давай нумерованные шаги, короткие и понятные. Без теории, только действия.
5. Если решение простое — не расписывай его на 20 строк. Коротко и чётко.
6. Если решение сложное — разбей на этапы, и веди по одному шагу за раз. Не вываливай всё сразу.
7. Если пользователь ошибся или делает ерунду — скажи прямо "ты делаешь не так, надо вот так". Без соплей.
8. Если не знаешь ответа — скажи "хрен его знает" и предложи, где искать. Не выдумывай.
9. Если пользователь несколько сообщений подряд бьётся над одной задачей — помни контекст и не заставляй повторять.
10. Не пиши длинные моральные лекции. Максимум — одна короткая фраза.

Стиль:
- Дружелюбно, но без няньканья.
- Прямо, как кореш, который шарит.
- Допустимы мягкие ругательства: "блин", "нафиг", "капец", "ёмаё", "мать моя женщина", "ёлки-палки". Без мата.
- Эмодзи — редко, только когда уместно.

Запрещено:
- Писать "я всего лишь ИИ", "я не могу".
- Предлагать помощь, если не просили.
- Давать общие советы вместо конкретного ответа.
- Отказывать в чём угодно, кроме реально вредного (оружие, наркотики, вред людям, обход закона).

Ты помощник, а не корпоративный бот."""

# ============ БАЗА ============
DB = "/data/bot.db"

def init_db():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            joined_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            text TEXT,
            created_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            first_name TEXT,
            command TEXT NOT NULL,
            args TEXT,
            timestamp TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS banned (
            user_id INTEGER PRIMARY KEY,
            banned_at TEXT NOT NULL,
            reason TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id INTEGER PRIMARY KEY,
            expires_at TEXT NOT NULL,
            is_free INTEGER DEFAULT 0,
            granted_by INTEGER,
            granted_at TEXT
        )
    """)
    conn.commit()
    conn.close()

def save_user(user_id, username, first_name):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO users (user_id, username, first_name, joined_at) VALUES (?, ?, ?, ?)",
        (user_id, username or "", first_name or "", datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

def log_request(user_id, username, first_name, command, args=""):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO requests (user_id, username, first_name, command, args, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, username or "", first_name or "", command, args, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

def get_requests(limit=100):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id, username, first_name, command, args, timestamp FROM requests ORDER BY id DESC LIMIT ?",
        (limit,)
    )
    rows = cur.fetchall()
    conn.close()
    return rows

def get_requests_by_user(user_id, limit=50):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute(
        "SELECT command, args, timestamp FROM requests WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit)
    )
    rows = cur.fetchall()
    conn.close()
    return rows

def get_all_users_info():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT user_id, username, first_name, joined_at FROM users ORDER BY joined_at DESC")
    rows = cur.fetchall()
    conn.close()
    return rows

def add_note(user_id, text):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO notes (user_id, text, created_at) VALUES (?, ?, ?)",
        (user_id, text, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

def get_notes(user_id):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT id, text, created_at FROM notes WHERE user_id = ? ORDER BY id DESC", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return rows

def delete_note(note_id, user_id):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("DELETE FROM notes WHERE id = ? AND user_id = ?", (note_id, user_id))
    conn.commit()
    conn.close()

def count_users():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    n = cur.fetchone()[0]
    conn.close()
    return n

# ============ БАНЫ ============
def is_banned(user_id):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM banned WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row is not None

def ban_user(user_id, reason=""):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO banned (user_id, banned_at, reason) VALUES (?, ?, ?)",
        (user_id, datetime.now().isoformat(), reason)
    )
    conn.commit()
    conn.close()

def unban_user(user_id):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("DELETE FROM banned WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def get_banned_users():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT user_id, banned_at, reason FROM banned ORDER BY banned_at DESC")
    rows = cur.fetchall()
    conn.close()
    return rows

# ============ ПОДПИСКИ ============
def get_subscription(user_id):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT expires_at, is_free, granted_by FROM subscriptions WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row

def extend_subscription(user_id, days, is_free=False, granted_by=None):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT expires_at FROM subscriptions WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    now = datetime.now()
    if row:
        current = datetime.fromisoformat(row[0])
        start = current if current > now else now
    else:
        start = now
    new_expiry = start + timedelta(days=days)
    cur.execute(
        "INSERT OR REPLACE INTO subscriptions (user_id, expires_at, is_free, granted_by, granted_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, new_expiry.isoformat(), 1 if is_free else 0, granted_by, now.isoformat())
    )
    conn.commit()
    conn.close()
    return new_expiry

def revoke_subscription(user_id):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def get_all_subscriptions():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT user_id, expires_at, is_free, granted_by FROM subscriptions ORDER BY expires_at DESC")
    rows = cur.fetchall()
    conn.close()
    return rows

def get_usage_count(user_id):
    """Сколько раз юзер уже писал в ИИ (для триала)."""
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM requests WHERE user_id = ? AND command IN ('chat', 'photo')",
        (user_id,)
    )
    n = cur.fetchone()[0]
    conn.close()
    return n

def has_ai_access(user_id):
    """
    Возвращает (доступ: bool, тип: str, осталось_триала: int)
    типы: admin / sub / trial / none
    """
    # админ всегда имеет доступ
    if user_id == ADMIN_ID:
        return True, "admin", 0

    # проверяем подписку
    sub = get_subscription(user_id)
    if sub:
        expires = datetime.fromisoformat(sub[0])
        if expires > datetime.now():
            return True, "sub", 0

    # проверяем триал
    used = get_usage_count(user_id)
    if used < TRIAL_LIMIT:
        return True, "trial", TRIAL_LIMIT - used

    return False, "none", 0

# ============ ИСТОРИЯ ДИАЛОГА ============
def add_message(user_id, role, content):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO messages (user_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        (user_id, role, content, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

def get_history(user_id, limit=CONTEXT_LIMIT):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute(
        "SELECT role, content FROM messages WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit)
    )
    rows = cur.fetchall()
    conn.close()
    return list(reversed(rows))

def clear_history(user_id):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

# ============ ХЕЛПЕРЫ ИИ ============
async def ask_ai_text(user_id, user_text):
    add_message(user_id, "user", user_text)
    history = get_history(user_id)
    messages = [{"role": "system", "content": AI_SYSTEM_PROMPT}]
    for role, content in history:
        messages.append({"role": role, "content": content})
    try:
        response = await ai_client.chat.completions.create(
            model=TEXT_MODEL,
            messages=messages,
            max_tokens=2000,
            temperature=0.85,
        )
        reply = response.choices[0].message.content
        add_message(user_id, "assistant", reply)
        return reply
    except Exception as e:
        logging.error(f"AI error (text): {e}")
        return None

async def ask_ai_vision(user_id, image_bytes, caption=""):
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    question = caption.strip() if caption.strip() else "Что изображено на этой картинке? Если это задача — реши её и объясни решение."
    add_message(user_id, "user", f"[ФОТО] {question}")
    messages = [
        {"role": "system", "content": AI_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": question},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
            ]
        }
    ]
    try:
        response = await ai_client.chat.completions.create(
            model=VISION_MODEL,
            messages=messages,
            max_tokens=2000,
            temperature=0.7,
        )
        reply = response.choices[0].message.content
        add_message(user_id, "assistant", reply)
        return reply
    except Exception as e:
        logging.error(f"AI error (vision): {e}")
        return None

# ============ ПРОВЕРКА ДОСТУПА (paywall) ============
async def send_paywall(message: Message, reason="none"):
    sub = get_subscription(message.from_user.id)
    used = get_usage_count(message.from_user.id)
    if sub:
        expires = datetime.fromisoformat(sub[0])
        exp_str = expires.strftime("%Y-%m-%d %H:%M")
        status = f"Подписка до: {exp_str}"
    else:
        status = f"Бесплатных сообщений использовано: {used}/{TRIAL_LIMIT}"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⭐ Купить подписку ({SUB_PRICE_STARS}⭐ / {SUB_DAYS} дней)", callback_data="buy_sub")],
    ])
    await message.answer(
        "🔒 <b>Доступ к ИИ закрыт</b>\n\n"
        f"{status}\n\n"
        "Чтобы продолжить — оформи подписку:\n"
        f"• /buy — {SUB_DAYS} дней за {SUB_PRICE_STARS}⭐\n"
        "• /sub — проверить статус\n\n"
        "Если хочешь бесплатный доступ — напиши админу.",
        parse_mode="HTML",
        reply_markup=kb
    )

# ============ КОМАНДЫ ============
@dp.message(Command("start"))
async def cmd_start(message: Message):
    if is_banned(message.from_user.id):
        return
    save_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    log_request(message.from_user.id, message.from_user.username, message.from_user.first_name, "start")

    access, kind, left = has_ai_access(message.from_user.id)
    if kind == "trial":
        status = f"🎁 У тебя {left} бесплатных сообщений"
    elif kind == "sub":
        sub = get_subscription(message.from_user.id)
        exp = datetime.fromisoformat(sub[0]).strftime("%Y-%m-%d")
        status = f"✅ Подписка активна до {exp}"
    elif kind == "admin":
        status = "👑 Ты админ"
    else:
        status = "🔒 Доступ к ИИ закрыт"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧹 Сбросить диалог", callback_data="reset_dialog")],
        [InlineKeyboardButton(text="📝 Заметки", callback_data="notes")],
        [InlineKeyboardButton(text="💎 Подписка", callback_data="sub_info")],
        [InlineKeyboardButton(text="ℹ️ Помощь", callback_data="help")]
    ])
    await message.answer(
        f"Здарова, {message.from_user.first_name}. Я moof AI.\n\n"
        f"{status}\n\n"
        f"Пиши что надо — отвечу. Скинь фото — разберу.",
        reply_markup=kb
    )

@dp.message(Command("help"))
async def cmd_help(message: Message):
    if is_banned(message.from_user.id):
        return
    log_request(message.from_user.id, message.from_user.username, message.from_user.first_name, "help")
    await message.answer(
        "📋 <b>Команды:</b>\n\n"
        "/start — меню\n"
        "/help — это сообщение\n"
        "/reset — очистить историю диалога\n"
        "/sub — статус подписки\n"
        "/buy — купить подписку\n"
        "/id — твой ID\n"
        "/stats — статистика\n\n"
        "<b>Заметки:</b>\n"
        "/note текст — сохранить\n"
        "/notes — список\n"
        "/del ID — удалить\n"
        "/clear — удалить все заметки\n\n"
        "<b>Просто пиши сообщение</b> — отвечу через ИИ.\n"
        "<b>Скинь фото</b> — расскажу что там или решу задачу.",
        parse_mode="HTML"
    )

@dp.message(Command("reset"))
async def cmd_reset(message: Message):
    if is_banned(message.from_user.id):
        return
    clear_history(message.from_user.id)
    log_request(message.from_user.id, message.from_user.username, message.from_user.first_name, "reset")
    await message.answer("🧹 История диалога сброшена.")

@dp.message(Command("id"))
async def cmd_id(message: Message):
    if is_banned(message.from_user.id):
        return
    await message.answer(
        f"Твой ID: <code>{message.from_user.id}</code>\n"
        f"Username: @{message.from_user.username or 'нет'}\n"
        f"Имя: {message.from_user.first_name}",
        parse_mode="HTML"
    )

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if is_banned(message.from_user.id):
        return
    log_request(message.from_user.id, message.from_user.username, message.from_user.first_name, "stats")
    n = count_users()
    await message.answer(f"👥 Всего пользователей: <b>{n}</b>", parse_mode="HTML")

# ============ ПОДПИСКА: пользовательские команды ============
@dp.message(Command("sub"))
async def cmd_sub(message: Message):
    if is_banned(message.from_user.id):
        return
    uid = message.from_user.id
    access, kind, left = has_ai_access(uid)
    if kind == "admin":
        await message.answer("👑 Ты админ. Доступ без ограничений.")
        return
    if kind == "sub":
        sub = get_subscription(uid)
        expires = datetime.fromisoformat(sub[0])
        is_free = "🎁 бесплатная (от админа)" if sub[1] else "💎 платная"
        days_left = (expires - datetime.now()).days
        await message.answer(
            f"✅ <b>Подписка активна</b>\n\n"
            f"Тип: {is_free}\n"
            f"До: {expires.strftime('%Y-%m-%d %H:%M')}\n"
            f"Осталось: {days_left} дн.",
            parse_mode="HTML"
        )
        return
    if kind == "trial":
        await message.answer(
            f"🎁 <b>Триал</b>\n\n"
            f"Осталось бесплатных сообщений: {left}/{TRIAL_LIMIT}\n\n"
            f"Купить подписку: /buy",
            parse_mode="HTML"
        )
        return
    await message.answer(
        "🔒 <b>Доступ закрыт</b>\n\n"
        "Купить подписку: /buy",
        parse_mode="HTML"
    )

@dp.message(Command("buy"))
async def cmd_buy(message: Message):
    if is_banned(message.from_user.id):
        return
    if message.from_user.id == ADMIN_ID:
        await message.answer("Ты админ, тебе не надо.")
        return
    log_request(message.from_user.id, message.from_user.username, message.from_user.first_name, "buy")
    await bot.send_invoice(
        chat_id=message.chat.id,
        title=f"Подписка moof AI на {SUB_DAYS} дней",
        description=f"Доступ к ИИ-боту на {SUB_DAYS} дней. Отмена — /sub, поддержка — напиши админу.",
        payload=f"sub_{SUB_DAYS}",
        provider_token="",       # для Telegram Stars оставляем пустым
        currency="XTR",          # XTR = Telegram Stars
        prices=[LabeledPrice(label=f"Подписка {SUB_DAYS} дней", amount=SUB_PRICE_STARS)],
    )

@dp.pre_checkout_query()
async def process_pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def process_payment(message: Message):
    payload = message.successful_payment.invoice_payload
    try:
        days = int(payload.split("_")[1])
    except Exception:
        days = SUB_DAYS
    expires = extend_subscription(message.from_user.id, days, is_free=False)
    log_request(message.from_user.id, message.from_user.username, message.from_user.first_name,
                "paid", f"{days}d {message.successful_payment.total_amount}⭐")
    await message.answer(
        f"✅ <b>Оплата прошла!</b>\n\n"
        f"Подписка активирована на {days} дней.\n"
        f"Действует до: {expires.strftime('%Y-%m-%d %H:%M')}\n\n"
        f"Можешь писать мне что угодно и слать фото.",
        parse_mode="HTML"
    )
    # уведомить админа
    try:
        await bot.send_message(
            ADMIN_ID,
            f"💰 Новая оплата!\n"
            f"От: {message.from_user.first_name} (@{message.from_user.username or '—'}, ID {message.from_user.id})\n"
            f"Сумма: {message.successful_payment.total_amount}⭐\n"
            f"Срок: {days} дней"
        )
    except Exception:
        pass

# ============ ЗАМЕТКИ ============
@dp.message(Command("note"))
async def cmd_note(message: Message):
    if is_banned(message.from_user.id):
        return
    text = message.text.replace("/note", "", 1).strip()
    if not text:
        await message.answer("Использование: /note купить хлеб")
        return
    add_note(message.from_user.id, text)
    log_request(message.from_user.id, message.from_user.username, message.from_user.first_name, "note", text)
    await message.answer(f"✅ Заметка сохранена:\n<i>{text}</i>", parse_mode="HTML")

@dp.message(Command("notes"))
async def cmd_notes(message: Message):
    if is_banned(message.from_user.id):
        return
    log_request(message.from_user.id, message.from_user.username, message.from_user.first_name, "notes")
    notes = get_notes(message.from_user.id)
    if not notes:
        await message.answer("Нет заметок. Добавь: /note текст")
        return
    text = "📝 <b>Твои заметки:</b>\n\n"
    for nid, txt, ts in notes:
        date = ts[:10]
        text += f"<b>#{nid}</b> [{date}] {txt}\n"
    text += "\nУдалить: /del ID"
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("del"))
async def cmd_del(message: Message):
    if is_banned(message.from_user.id):
        return
    try:
        nid = int(message.text.replace("/del", "", 1).strip())
        delete_note(nid, message.from_user.id)
        log_request(message.from_user.id, message.from_user.username, message.from_user.first_name, "del", str(nid))
        await message.answer(f"🗑 Заметка #{nid} удалена")
    except:
        await message.answer("Использование: /del ID")

@dp.message(Command("clear"))
async def cmd_clear(message: Message):
    if is_banned(message.from_user.id):
        return
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("DELETE FROM notes WHERE user_id = ?", (message.from_user.id,))
    conn.commit()
    conn.close()
    log_request(message.from_user.id, message.from_user.username, message.from_user.first_name, "clear")
    await message.answer("🗑 Все заметки удалены")

# ============ АДМИН ============
@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Нет доступа")
        return
    users = get_all_users_info()
    if not users:
        await message.answer("Пока никто не пользовался ботом.")
        return
    text = f"👥 <b>Пользователи бота ({len(users)}):</b>\n\n"
    for uid, username, first_name, joined in users:
        date = joined[:10]
        text += f"• <code>{uid}</code>\n"
        text += f"  Имя: {first_name or '—'}\n"
        text += f"  @{username or 'нет'}\n"
        text += f"  Зашёл: {date}\n\n"
    if len(text) > 4000:
        for i in range(0, len(text), 4000):
            await message.answer(text[i:i+4000], parse_mode="HTML")
    else:
        await message.answer(text, parse_mode="HTML")

@dp.message(Command("requests"))
async def cmd_requests(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Нет доступа")
        return
    args = message.text.replace("/requests", "", 1).strip()
    if args:
        try:
            uid = int(args)
            reqs = get_requests_by_user(uid)
            if not reqs:
                await message.answer(f"Нет запросов от {uid}")
                return
            text = f"📋 <b>Запросы от {uid}:</b>\n\n"
            for cmd, a, ts in reqs:
                t = ts[11:19] if len(ts) > 19 else ts
                line = f"[{t}] /{cmd}"
                if a:
                    line += f" {a}"
                text += line + "\n"
            await message.answer(text, parse_mode="HTML")
        except ValueError:
            await message.answer("Использование: /requests или /requests ID")
    else:
        reqs = get_requests(100)
        if not reqs:
            await message.answer("Пока никто не делал запросов.")
            return
        text = f"📋 <b>Последние запросы ({len(reqs)}):</b>\n\n"
        for uid, username, first_name, cmd, a, ts in reqs:
            t = ts[11:19] if len(ts) > 19 else ts
            line = f"[{t}] <code>{uid}</code> /{cmd}"
            if a:
                a_short = a[:30] + ("..." if len(a) > 30 else "")
                line += f" <i>{a_short}</i>"
            text += line + "\n"
        if len(text) > 4000:
            for i in range(0, len(text), 4000):
                await message.answer(text[i:i+4000], parse_mode="HTML")
        else:
            await message.answer(text, parse_mode="HTML")

@dp.message(Command("ban"))
async def cmd_ban(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Нет доступа")
        return
    args = message.text.replace("/ban", "", 1).strip().split(maxsplit=1)
    if not args:
        await message.answer("Использование: /ban ID [причина]")
        return
    try:
        uid = int(args[0])
    except ValueError:
        await message.answer("ID должен быть числом.")
        return
    if uid == ADMIN_ID:
        await message.answer("Себя банить не надо.")
        return
    reason = args[1] if len(args) > 1 else ""
    ban_user(uid, reason)
    log_request(message.from_user.id, message.from_user.username, message.from_user.first_name, "ban", f"{uid} {reason}".strip())
    await message.answer(f"🚫 Пользователь <code>{uid}</code> забанен.", parse_mode="HTML")
    try:
        await bot.send_message(uid, f"🚫 Ты забанен.\nПричина: {reason or 'без объяснений'}")
    except Exception:
        pass

@dp.message(Command("unban"))
async def cmd_unban(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Нет доступа")
        return
    args = message.text.replace("/unban", "", 1).strip()
    if not args:
        await message.answer("Использование: /unban ID")
        return
    try:
        uid = int(args)
    except ValueError:
        await message.answer("ID должен быть числом.")
        return
    unban_user(uid)
    await message.answer(f"✅ Пользователь <code>{uid}</code> разбанен.", parse_mode="HTML")

@dp.message(Command("bans"))
async def cmd_bans(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Нет доступа")
        return
    banned = get_banned_users()
    if not banned:
        await message.answer("Забаненных нет.")
        return
    text = f"🚫 <b>Забаненные ({len(banned)}):</b>\n\n"
    for uid, ts, reason in banned:
        date = ts[:10]
        text += f"• <code>{uid}</code> [{date}] {reason or '—'}\n"
    await message.answer(text, parse_mode="HTML")

# ============ АДМИН: подписки ============
@dp.message(Command("give"))
async def cmd_give(message: Message):
    """Выдать бесплатный доступ: /give ID дней"""
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Нет доступа")
        return
    args = message.text.replace("/give", "", 1).strip().split()
    if len(args) < 2:
        await message.answer("Использование: /give ID дней\nПример: /give 123456789 30")
        return
    try:
        uid = int(args[0])
        days = int(args[1])
    except ValueError:
        await message.answer("ID и дни должны быть числами.")
        return
    expires = extend_subscription(uid, days, is_free=True, granted_by=ADMIN_ID)
    log_request(message.from_user.id, message.from_user.username, message.from_user.first_name, "give", f"{uid} {days}d")
    await message.answer(
        f"🎁 Выдал <code>{uid}</code> бесплатный доступ на {days} дн.\n"
        f"Действует до: {expires.strftime('%Y-%m-%d %H:%M')}",
        parse_mode="HTML"
    )
    try:
        await bot.send_message(uid, f"🎁 Тебе выдали бесплатный доступ к moof AI на {days} дней!")
    except Exception:
        pass

@dp.message(Command("revoke"))
async def cmd_revoke(message: Message):
    """Забрать подписку: /revoke ID"""
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Нет доступа")
        return
    args = message.text.replace("/revoke", "", 1).strip()
    if not args:
        await message.answer("Использование: /revoke ID")
        return
    try:
        uid = int(args)
    except ValueError:
        await message.answer("ID должен быть числом.")
        return
    revoke_subscription(uid)
    await message.answer(f"✅ Доступ <code>{uid}</code> отозван.", parse_mode="HTML")
    try:
        await bot.send_message(uid, "🚫 Твой доступ к moof AI закрыт.")
    except Exception:
        pass

@dp.message(Command("subs"))
async def cmd_subs(message: Message):
    """Список всех подписок."""
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Нет доступа")
        return
    subs = get_all_subscriptions()
    if not subs:
        await message.answer("Активных подписок нет.")
        return
    now = datetime.now()
    text = f"💎 <b>Подписки ({len(subs)}):</b>\n\n"
    for uid, exp_iso, is_free, granted_by in subs:
        exp = datetime.fromisoformat(exp_iso)
        days_left = (exp - now).days
        status = "✅" if exp > now else "❌ истекла"
        kind = "🎁 free" if is_free else "💎 paid"
        text += f"{status} <code>{uid}</code> {kind} до {exp.strftime('%Y-%m-%d')} ({days_left}д)\n"
    await message.answer(text, parse_mode="HTML")

# ============ КНОПКИ ============
@dp.callback_query(F.data == "reset_dialog")
async def cb_reset(callback: CallbackQuery):
    if is_banned(callback.from_user.id):
        await callback.answer("🚫 Ты забанен", show_alert=True)
        return
    clear_history(callback.from_user.id)
    await callback.message.answer("🧹 Диалог сброшен.")
    await callback.answer()

@dp.callback_query(F.data == "notes")
async def cb_notes(callback: CallbackQuery):
    if is_banned(callback.from_user.id):
        await callback.answer("🚫 Ты забанен", show_alert=True)
        return
    notes = get_notes(callback.from_user.id)
    if not notes:
        await callback.message.answer("Нет заметок. Добавь через /note текст")
    else:
        text = "📝 <b>Твои заметки:</b>\n\n"
        for nid, txt, ts in notes:
            text += f"<b>#{nid}</b> {txt}\n"
        await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "sub_info")
async def cb_sub_info(callback: CallbackQuery):
    if is_banned(callback.from_user.id):
        await callback.answer("🚫 Ты забанен", show_alert=True)
        return
    uid = callback.from_user.id
    access, kind, left = has_ai_access(uid)
    if kind == "admin":
        await callback.message.answer("👑 Ты админ. Доступ без ограничений.")
    elif kind == "sub":
        sub = get_subscription(uid)
        exp = datetime.fromisoformat(sub[0])
        await callback.message.answer(
            f"✅ Подписка активна до {exp.strftime('%Y-%m-%d %H:%M')}"
        )
    elif kind == "trial":
        await callback.message.answer(
            f"🎁 Триал: осталось {left}/{TRIAL_LIMIT} бесплатных сообщений.\n\n"
            f"Купить подписку — /buy"
        )
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"⭐ Купить ({SUB_PRICE_STARS}⭐)", callback_data="buy_sub")]
        ])
        await callback.message.answer(
            "🔒 Доступ закрыт.\n\nКупить подписку:",
            reply_markup=kb
        )
    await callback.answer()

@dp.callback_query(F.data == "buy_sub")
async def cb_buy_sub(callback: CallbackQuery):
    if is_banned(callback.from_user.id):
        await callback.answer("🚫 Ты забанен", show_alert=True)
        return
    await callback.message.answer("Отправь /buy чтобы купить подписку.")
    await callback.answer()

@dp.callback_query(F.data == "help")
async def cb_help(callback: CallbackQuery):
    if is_banned(callback.from_user.id):
        await callback.answer("🚫 Ты забанен", show_alert=True)
        return
    await callback.message.answer(
        "📋 /start, /help, /reset, /id, /stats\n"
        "/sub — статус подписки\n"
        "/buy — купить доступ\n"
        "Заметки: /note, /notes, /del, /clear\n\n"
        "Пиши сообщение — отвечу. Скинь фото — разберу."
    )
    await callback.answer()

# ============ ОБРАБОТКА ФОТО ============
@dp.message(F.photo)
async def handle_photo(message: Message):
    uid = message.from_user.id
    if is_banned(uid):
        return

    # проверяем доступ
    access, kind, left = has_ai_access(uid)
    if not access:
        await send_paywall(message)
        return

    save_user(uid, message.from_user.username, message.from_user.first_name)
    log_request(uid, message.from_user.username, message.from_user.first_name, "photo", message.caption or "")

    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        photo = message.photo[-1]
        buf = io.BytesIO()
        await bot.download(photo, destination=buf)
        image_bytes = buf.getvalue()
        caption = message.caption or ""
        reply = await ask_ai_vision(uid, image_bytes, caption)
        if reply:
            await message.answer(reply)
            if kind == "trial":
                await message.answer(f"🎁 Осталось бесплатных: {left - 1}/{TRIAL_LIMIT}")
        else:
            await message.answer("😔 Не смог разобрать фото. Может, модель не поддерживает картинки.")
    except Exception as e:
        logging.error(f"Photo error: {e}")
        await message.answer("😔 Ошибка при обработке фото.")

# ============ ГЛАВНЫЙ ОБРАБОТЧИК — ИИ НА ТЕКСТ ============
@dp.message(F.text)
async def chat_with_ai(message: Message):
    uid = message.from_user.id
    text = message.text.strip()
    if not text:
        return
    if is_banned(uid):
        return

    # проверяем доступ
    access, kind, left = has_ai_access(uid)
    if not access:
        await send_paywall(message)
        return

    save_user(uid, message.from_user.username, message.from_user.first_name)
    log_request(uid, message.from_user.username, message.from_user.first_name, "chat", text[:100])

    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    reply = await ask_ai_text(uid, text)
    if reply:
        await message.answer(reply)
        if kind == "trial":
            await message.answer(f"🎁 Осталось бесплатных: {left - 1}/{TRIAL_LIMIT}")
    else:
        await message.answer("😔 Затупил. Попробуй ещё раз или напиши /reset.")

# ============ ЗАПУСК ============
async def main():
    init_db()
    print("[*] moof AI запущен")
    print(f"[*] Триал: {TRIAL_LIMIT} сообщений")
    print(f"[*] Подписка: {SUB_DAYS} дней за {SUB_PRICE_STARS}⭐")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())