from telegram import Update
from telegram.ext import ContextTypes

from paper.paper_controller import PaperController


paper = PaperController()


async def paper_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(paper.on())


async def paper_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(paper.off())


async def paper_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(paper.status_text())
    