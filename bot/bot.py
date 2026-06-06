"""Lunaria Telegram bot — точка входа в Mini App.

Реагирует на /start, /lang, /help. Язык определяется автоматически из
language_code пользователя (uk → українська, иначе — русский) и
запоминается в SQLite. Под приветствием — кнопка запуска Mini App и
переключатель языка.

Дополнительно поднимает лёгкий HTTP-API (aiohttp) для Mini App:
  • POST /api/verify   — проверка подписки по подписанному initData
  • POST /api/reminder — настройка ежедневного напоминания
и фоновую задачу, которая рассылает напоминания «вытяни карту».
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from aiohttp import web

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ["WEBAPP_URL"]
DB_PATH = Path(os.environ.get("DB_PATH", "users.db"))

# Канал обязательной подписки. Если пусто — гейт выключен и приложение
# открывается без проверки. Формат: @username (публичный канал) или
# числовой ID вида -100123456789. Для числового ID задай CHANNEL_URL.
REQUIRED_CHANNEL = os.environ.get("REQUIRED_CHANNEL", "").strip()
# Явная ссылка на канал (нужна для приватных каналов / числовых ID).
# Для @username вычисляется автоматически.
CHANNEL_URL = os.environ.get("CHANNEL_URL", "").strip()

# Порт HTTP-API. Railway передаёт его через переменную PORT.
API_PORT = int(os.environ.get("PORT", "8080"))
# initData считается просроченным после стольких секунд (защита от повтора).
INITDATA_MAX_AGE = int(os.environ.get("INITDATA_MAX_AGE", str(24 * 3600)))

# Статусы участника, которые считаем активной подпиской.
SUBSCRIBED_STATUSES = frozenset({"member", "administrator", "creator"})

SUPPORTED = ("uk", "ru")

TEXTS: dict[str, dict[str, str]] = {
    "uk": {
        "salute_named": "Вітаю, {name}. 🌙",
        "salute_anon": "Вітаю. 🌙",
        "body": (
            "<b>Lunaria — Карта Дня</b>\n\n"
            "Кожного дня тут чекає одна карта старших арканів — "
            "твій спокійний ритуал і дзеркало дня.\n\n"
            "✶ Витягни карту дня\n"
            "🌗 Увечері повернись і відзнач, як вона відгукнулась\n"
            "🌒 Збирай колекцію 22 арканів і нотатки у щоденнику\n\n"
            "Натисни кнопку нижче, щоб відкрити Lunaria."
        ),
        "open": "✶ Відкрити Lunaria",
        "switch": "🇷🇺 Русский",
        "lang_prompt": "Поточна мова — українська. Можеш перемкнути:",
        "switched": "Мову змінено на українську ✶",
        "help": (
            "<b>Команди</b>\n"
            "/start — головне меню та кнопка запуску\n"
            "/lang — змінити мову\n"
            "/help — ця підказка"
        ),
        "gate_body": (
            "<b>Lunaria — Карта Дня</b>\n\n"
            "Щоб відкрити застосунок, спершу підпишись на наш канал — "
            "там анонси, ритуали та сенси дня. 🌙\n\n"
            "1️⃣ Натисни «Підписатися»\n"
            "2️⃣ Повернись і натисни «Я підписався»"
        ),
        "subscribe": "📢 Підписатися на канал",
        "check": "✓ Я підписався",
        "not_subscribed": "Підписку не знайдено. Підпишись на канал і спробуй ще раз.",
        "subscribed_ok": "Дякую! Доступ відкрито ✶",
        "remind": (
            "🌙 <b>Час для карти дня</b>\n\n"
            "Витягни свою карту — і дізнайся, що нашіптує сьогоднішній день."
        ),
    },
    "ru": {
        "salute_named": "Здравствуй, {name}. 🌙",
        "salute_anon": "Здравствуй. 🌙",
        "body": (
            "<b>Lunaria — Карта Дня</b>\n\n"
            "Каждый день здесь ждёт одна карта старших арканов — "
            "твой спокойный ритуал и зеркало дня.\n\n"
            "✶ Вытяни карту дня\n"
            "🌗 Вечером вернись и отметь, как она откликнулась\n"
            "🌒 Собирай коллекцию из 22 арканов и заметки в дневнике\n\n"
            "Нажми кнопку ниже, чтобы открыть Lunaria."
        ),
        "open": "✶ Открыть Lunaria",
        "switch": "🇺🇦 Українська",
        "lang_prompt": "Текущий язык — русский. Можно переключить:",
        "switched": "Язык переключён на русский ✶",
        "help": (
            "<b>Команды</b>\n"
            "/start — главное меню и кнопка запуска\n"
            "/lang — сменить язык\n"
            "/help — эта подсказка"
        ),
        "gate_body": (
            "<b>Lunaria — Карта Дня</b>\n\n"
            "Чтобы открыть приложение, сначала подпишись на наш канал — "
            "там анонсы, ритуалы и смыслы дня. 🌙\n\n"
            "1️⃣ Нажми «Подписаться»\n"
            "2️⃣ Вернись и нажми «Я подписался»"
        ),
        "subscribe": "📢 Подписаться на канал",
        "check": "✓ Я подписался",
        "not_subscribed": "Подписка не найдена. Подпишись на канал и попробуй ещё раз.",
        "subscribed_ok": "Спасибо! Доступ открыт ✶",
        "remind": (
            "🌙 <b>Время для карты дня</b>\n\n"
            "Вытяни свою карту — и узнай, что нашёптывает сегодняшний день."
        ),
    },
}


# Колонки настроек напоминаний, добавляемые миграцией к старой таблице.
#   remind_enabled — 0/1, включено ли напоминание
#   remind_time    — локальное время "HH:MM"
#   tz_offset      — смещение из JS Date.getTimezoneOffset() (минуты)
#   last_remind    — дата последней отправки "YYYY-MM-DD" (защита от дублей)
REMINDER_COLUMNS = {
    "remind_enabled": "INTEGER NOT NULL DEFAULT 0",
    "remind_time": "TEXT",
    "tz_offset": "INTEGER NOT NULL DEFAULT 0",
    "last_remind": "TEXT",
}


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS users ("
            "user_id INTEGER PRIMARY KEY, "
            "lang TEXT NOT NULL, "
            "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        existing = {row[1] for row in db.execute("PRAGMA table_info(users)")}
        for col, decl in REMINDER_COLUMNS.items():
            if col not in existing:
                db.execute(f"ALTER TABLE users ADD COLUMN {col} {decl}")
        db.commit()


def get_stored_lang(user_id: int) -> str | None:
    with closing(sqlite3.connect(DB_PATH)) as db:
        row = db.execute(
            "SELECT lang FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
    return row[0] if row else None


def save_lang(user_id: int, lang: str) -> None:
    with closing(sqlite3.connect(DB_PATH)) as db:
        db.execute(
            "INSERT INTO users(user_id, lang) VALUES(?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "lang=excluded.lang, updated_at=CURRENT_TIMESTAMP",
            (user_id, lang),
        )
        db.commit()


def resolve_lang(user_id: int, tg_language_code: str | None) -> str:
    stored = get_stored_lang(user_id)
    if stored in SUPPORTED:
        return stored
    if tg_language_code and tg_language_code.lower().startswith("uk"):
        return "uk"
    return "ru"


def save_reminder(
    user_id: int, lang: str, enabled: bool, remind_time: str | None, tz_offset: int
) -> None:
    """Сохраняет настройку напоминания (создаёт пользователя при необходимости)."""
    with closing(sqlite3.connect(DB_PATH)) as db:
        db.execute(
            "INSERT INTO users(user_id, lang, remind_enabled, remind_time, tz_offset) "
            "VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "remind_enabled=excluded.remind_enabled, "
            "remind_time=excluded.remind_time, "
            "tz_offset=excluded.tz_offset, "
            "updated_at=CURRENT_TIMESTAMP",
            (user_id, lang, int(enabled), remind_time, tz_offset),
        )
        db.commit()


def get_reminder(user_id: int) -> dict | None:
    with closing(sqlite3.connect(DB_PATH)) as db:
        row = db.execute(
            "SELECT remind_enabled, remind_time, tz_offset FROM users "
            "WHERE user_id=?",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    return {"enabled": bool(row[0]), "time": row[1], "tz_offset": row[2] or 0}


def due_reminders(now_utc: datetime) -> list[tuple[int, str]]:
    """Список (user_id, lang) тех, кому пора напомнить прямо сейчас.

    Локальное время пользователя = UTC − tz_offset (минуты, как у
    JS getTimezoneOffset). Отправляем, когда совпали HH:MM и сегодня
    ещё не отправляли (last_remind != локальная дата).
    """
    due: list[tuple[int, str]] = []
    with closing(sqlite3.connect(DB_PATH)) as db:
        rows = db.execute(
            "SELECT user_id, lang, remind_time, tz_offset, last_remind "
            "FROM users WHERE remind_enabled=1 AND remind_time IS NOT NULL"
        ).fetchall()
        for user_id, lang, remind_time, tz_offset, last_remind in rows:
            local = now_utc - timedelta(minutes=tz_offset or 0)
            if local.strftime("%H:%M") != remind_time:
                continue
            local_date = local.strftime("%Y-%m-%d")
            if last_remind == local_date:
                continue
            db.execute(
                "UPDATE users SET last_remind=? WHERE user_id=?",
                (local_date, user_id),
            )
            due.append((user_id, lang if lang in SUPPORTED else "ru"))
        db.commit()
    return due


def channel_chat_id() -> str:
    """Идентификатор канала для getChatMember.

    Принимает @username, числовой ID (-100…) или ссылку t.me/<username> —
    из ссылки извлекается @username. Инвайт-ссылки (t.me/+…) для проверки
    членства не годятся: для приватного канала задавай числовой ID.
    """
    raw = REQUIRED_CHANNEL
    if raw.startswith("http://") or raw.startswith("https://"):
        handle = raw.rstrip("/").rsplit("/", 1)[-1]
        return handle if handle.startswith(("@", "+", "-")) else f"@{handle}"
    if raw and not raw.startswith(("@", "-", "+")):
        return f"@{raw}"
    return raw


def channel_url() -> str:
    """Ссылка на канал для кнопки «Подписаться»."""
    if CHANNEL_URL:
        return CHANNEL_URL
    if REQUIRED_CHANNEL.startswith(("http://", "https://")):
        return REQUIRED_CHANNEL
    handle = REQUIRED_CHANNEL.lstrip("@")
    return f"https://t.me/{handle}"


async def is_subscribed(bot: Bot, user_id: int) -> bool:
    """Проверяет подписку пользователя на REQUIRED_CHANNEL.

    Если гейт выключен (канал не задан) — всегда True. При ошибке запроса
    (например, бот не админ канала) считаем подписку отсутствующей и пишем
    предупреждение в лог — подписка обязательна, поэтому fail-closed.
    """
    if not REQUIRED_CHANNEL:
        return True
    try:
        member = await bot.get_chat_member(channel_chat_id(), user_id)
    except TelegramAPIError as err:
        logging.warning(
            "Не удалось проверить подписку на %s для %s: %s. "
            "Убедись, что бот добавлен администратором канала.",
            REQUIRED_CHANNEL,
            user_id,
            err,
        )
        return False
    return member.status in SUBSCRIBED_STATUSES


def build_gate_keyboard(lang: str) -> InlineKeyboardMarkup:
    other = "ru" if lang == "uk" else "uk"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=TEXTS[lang]["subscribe"], url=channel_url())],
            [InlineKeyboardButton(text=TEXTS[lang]["check"], callback_data="checksub")],
            [
                InlineKeyboardButton(
                    text=TEXTS[lang]["switch"],
                    callback_data=f"setlang:{other}",
                )
            ],
        ]
    )


def build_keyboard(lang: str) -> InlineKeyboardMarkup:
    other = "ru" if lang == "uk" else "uk"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=TEXTS[lang]["open"],
                    web_app=WebAppInfo(url=WEBAPP_URL),
                )
            ],
            [
                InlineKeyboardButton(
                    text=TEXTS[lang]["switch"],
                    callback_data=f"setlang:{other}",
                )
            ],
        ]
    )


def render_greeting(lang: str, name: str | None) -> str:
    t = TEXTS[lang]
    salute = t["salute_named"].format(name=name) if name else t["salute_anon"]
    return f"{salute}\n\n{t['body']}"


async def render_entry(
    bot: Bot, user_id: int, lang: str, name: str | None
) -> tuple[str, InlineKeyboardMarkup]:
    """Экран входа: приветствие с кнопкой запуска, если подписан, иначе гейт."""
    if await is_subscribed(bot, user_id):
        return render_greeting(lang, name), build_keyboard(lang)
    return TEXTS[lang]["gate_body"], build_gate_keyboard(lang)


router = Router(name="lunaria")


@router.message(CommandStart())
async def on_start(msg: Message) -> None:
    user = msg.from_user
    if user is None:
        return
    lang = resolve_lang(user.id, user.language_code)
    save_lang(user.id, lang)
    text, keyboard = await render_entry(
        msg.bot, user.id, lang, user.first_name
    )
    await msg.answer(text, reply_markup=keyboard)


@router.message(Command("lang"))
async def on_lang(msg: Message) -> None:
    user = msg.from_user
    if user is None:
        return
    lang = resolve_lang(user.id, user.language_code)
    if await is_subscribed(msg.bot, user.id):
        await msg.answer(
            TEXTS[lang]["lang_prompt"],
            reply_markup=build_keyboard(lang),
        )
    else:
        await msg.answer(
            TEXTS[lang]["gate_body"],
            reply_markup=build_gate_keyboard(lang),
        )


@router.message(Command("help"))
async def on_help(msg: Message) -> None:
    user = msg.from_user
    if user is None:
        return
    lang = resolve_lang(user.id, user.language_code)
    await msg.answer(TEXTS[lang]["help"])


@router.callback_query(F.data.startswith("setlang:"))
async def on_set_lang(cb: CallbackQuery) -> None:
    new_lang = (cb.data or "").split(":", 1)[1]
    if new_lang not in SUPPORTED:
        await cb.answer()
        return
    save_lang(cb.from_user.id, new_lang)
    if isinstance(cb.message, Message):
        text, keyboard = await render_entry(
            cb.bot, cb.from_user.id, new_lang, cb.from_user.first_name
        )
        await cb.message.edit_text(text, reply_markup=keyboard)
    await cb.answer(TEXTS[new_lang]["switched"])


@router.callback_query(F.data == "checksub")
async def on_check_sub(cb: CallbackQuery) -> None:
    lang = resolve_lang(cb.from_user.id, cb.from_user.language_code)
    if not await is_subscribed(cb.bot, cb.from_user.id):
        await cb.answer(TEXTS[lang]["not_subscribed"], show_alert=True)
        return
    if isinstance(cb.message, Message):
        await cb.message.edit_text(
            render_greeting(lang, cb.from_user.first_name),
            reply_markup=build_keyboard(lang),
        )
    await cb.answer(TEXTS[lang]["subscribed_ok"])


async def set_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Lunaria — Карта Дня"),
            BotCommand(command="lang", description="Мова / Язык"),
            BotCommand(command="help", description="Допомога / Помощь"),
        ]
    )


# ============ HTTP-API для Mini App ============

def validate_init_data(init_data: str) -> dict | None:
    """Проверяет подпись Telegram WebApp initData и возвращает данные user.

    Алгоритм из документации Telegram: secret = HMAC_SHA256("WebAppData",
    bot_token); валидный hash = HMAC_SHA256(secret, data_check_string).
    Возвращает dict пользователя (id, first_name, language_code…) или None.
    """
    if not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None
    data_check_string = "\n".join(
        f"{k}={pairs[k]}" for k in sorted(pairs)
    )
    secret_key = hmac.new(
        b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256
    ).digest()
    expected = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        return None
    # защита от повторного использования старого initData
    auth_date = pairs.get("auth_date")
    if auth_date and auth_date.isdigit():
        if time.time() - int(auth_date) > INITDATA_MAX_AGE:
            return None
    try:
        return json.loads(pairs.get("user", "null"))
    except json.JSONDecodeError:
        return None


def _cors(resp: web.Response) -> web.Response:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


async def _read_user(request: web.Request) -> tuple[dict | None, dict]:
    """Парсит тело запроса и валидирует initData. Возвращает (user, body)."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return None, {}
    user = validate_init_data(body.get("initData", ""))
    return user, body


async def handle_verify(request: web.Request) -> web.Response:
    user, _ = await _read_user(request)
    if not user or "id" not in user:
        return _cors(web.json_response({"ok": False}, status=401))
    bot: Bot = request.app["bot"]
    subscribed = await is_subscribed(bot, int(user["id"]))
    return _cors(
        web.json_response(
            {
                "ok": True,
                "gated": bool(REQUIRED_CHANNEL),
                "subscribed": subscribed,
                "channelUrl": channel_url() if REQUIRED_CHANNEL else "",
            }
        )
    )


async def handle_reminder(request: web.Request) -> web.Response:
    user, body = await _read_user(request)
    if not user or "id" not in user:
        return _cors(web.json_response({"ok": False}, status=401))
    enabled = bool(body.get("enabled"))
    remind_time = str(body.get("time", "")).strip()
    if enabled and not _valid_hhmm(remind_time):
        return _cors(web.json_response({"ok": False, "error": "time"}, status=400))
    try:
        tz_offset = int(body.get("tzOffset", 0))
    except (TypeError, ValueError):
        tz_offset = 0
    lang = resolve_lang(int(user["id"]), user.get("language_code"))
    save_reminder(
        int(user["id"]),
        lang,
        enabled,
        remind_time if enabled else None,
        tz_offset,
    )
    return _cors(web.json_response({"ok": True, "enabled": enabled}))


async def handle_options(request: web.Request) -> web.Response:
    return _cors(web.Response())


async def handle_health(request: web.Request) -> web.Response:
    return _cors(web.json_response({"ok": True}))


def _valid_hhmm(value: str) -> bool:
    if len(value) != 5 or value[2] != ":":
        return False
    hh, mm = value[:2], value[3:]
    if not (hh.isdigit() and mm.isdigit()):
        return False
    return 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59


def build_api(bot: Bot) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app.router.add_post("/api/verify", handle_verify)
    app.router.add_post("/api/reminder", handle_reminder)
    app.router.add_route("OPTIONS", "/api/{tail:.*}", handle_options)
    app.router.add_get("/api/health", handle_health)
    return app


async def run_api(bot: Bot) -> None:
    runner = web.AppRunner(build_api(bot))
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", API_PORT)
    await site.start()
    logging.info("HTTP-API слушает на :%s", API_PORT)


# ============ Фоновые напоминания ============

async def reminder_loop(bot: Bot) -> None:
    """Раз в минуту рассылает напоминания тем, у кого совпало время."""
    while True:
        try:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            for user_id, lang in due_reminders(now):
                try:
                    await bot.send_message(
                        user_id,
                        TEXTS[lang]["remind"],
                        reply_markup=build_keyboard(lang),
                    )
                except TelegramAPIError as err:
                    logging.warning("Напоминание %s не отправлено: %s", user_id, err)
        except Exception:  # noqa: BLE001 — цикл не должен падать
            logging.exception("Ошибка в reminder_loop")
        # выравниваемся на начало следующей минуты
        await asyncio.sleep(60 - datetime.now().second)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    init_db()
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)
    await set_bot_commands(bot)
    await bot.delete_webhook(drop_pending_updates=True)
    await run_api(bot)
    asyncio.create_task(reminder_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
