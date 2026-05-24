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


router = Router(name="lunaria")


@router.message(CommandStart())
async def on_start(msg: Message) -> None:
    user = msg.from_user
    if user is None:
        return
    lang = resolve_lang(user.id, user.language_code)
    save_lang(user.id, lang)
    await msg.answer(
        render_greeting(lang, user.first_name),
        reply_markup=build_keyboard(lang),
    )


@router.message(Command("lang"))
async def on_lang(msg: Message) -> None:
    user = msg.from_user
    if user is None:
        return
    lang = resolve_lang(user.id, user.language_code)
    await msg.answer(
        TEXTS[lang]["lang_prompt"],
        reply_markup=build_keyboard(lang),
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
        await cb.message.edit_text(
            render_greeting(new_lang, cb.from_user.first_name),
            reply_markup=build_keyboard(new_lang),
        )
    await cb.answer(TEXTS[new_lang]["switched"])


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
