from telegram import ReplyKeyboardMarkup


def main_keyboard():
    keyboard = [
        ["📊 Анализ", "🔥 Сканер"],
        ["🤖 Auto Once", "🚀 Auto On", "⛔ Auto Off"],
        ["📄 Paper Status", "📌 Position"],
        ["📊 Paper Stats", "📜 Paper History"],
    ]

    return ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True
    )