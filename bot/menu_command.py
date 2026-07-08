from telegram import Update
from telegram.ext import ContextTypes

from bot.inline_menus import app_main_menu
from bot.dashboard import build_dashboard
from bot.paper_commands import paper
from bot.auto_commands import auto_state


async def open_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        build_dashboard(paper, auto_state),
        reply_markup=app_main_menu()
    )