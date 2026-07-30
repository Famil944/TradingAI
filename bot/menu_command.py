import asyncio

from telegram import Update
from telegram.ext import ContextTypes

from bot.inline_menus import app_main_menu
from bot.dashboard import build_dashboard
from bot.paper_commands import paper
from bot.auto_commands import auto_state


async def open_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Imported lazily to avoid a module cycle during Telegram handler setup.
    from bot.telegram_bot import demo_controller

    text = await asyncio.to_thread(
        build_dashboard, paper, auto_state, demo_controller
    )
    await update.message.reply_text(
        text,
        reply_markup=app_main_menu()
    )
