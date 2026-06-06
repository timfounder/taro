"""Lunaria Telegram bot — точка входа в Mini App.

Реагирует на /start, /lang, /help. Язык определяется автоматически из
language_code пользователя (uk → українська, иначе — русский) и
запоминается в SQLite. Под приветствием — кнопка запуска Mini App и
переключатель языка.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from contextlib import closing
from pathlib import Path

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
    },
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


def channel_url() -> str:
    """Ссылка на канал для кнопки «Подписаться»."""
    if CHANNEL_URL:
        return CHANNEL_URL
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
        member = await bot.get_chat_member(REQUIRED_CHANNEL, user_id)
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
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
